import glob
import datetime
import os
import time
import threading
import tempfile
import numpy as np
import sqlite3
import rasterio
from rasterio.mask import mask
from shapely.geometry import shape
import torch
from flask import Blueprint, request, jsonify, current_app
# 注：原先这里 import 了 scipy.ndimage.zoom 用于阈值场重采样，
# 但 zoom 只做缩放不做平移，在阈值场与研究区存在 604 像元横向偏移时会造成
# 约 540 km 错位。现已改为 _align_grid()（rasterio.warp.reproject），故移除。
from modules.utils import get_project_root, load_json_cached, resolve_index_path
from modules.runoff_engine import (
    RunoffLSTM, simulate_runoff, load_region_geometry,
    DEFAULT_BATCH_PIXELS, MAX_BATCH_PIXELS,
    auto_batch_pixels, auto_state_budget_bytes, detect_memory_bytes)

simulate_bp = Blueprint('simulate', __name__, url_prefix='/api/simulate')

# 洪水淹没结果写出锁：串行化所有淹没结果 TIFF 的写出，避免两个并发请求
# （侧边栏/主区两个入口或重复点击）抢同一批固定文件名造成 Windows 文件锁冲突。
FLOOD_WRITE_LOCK = threading.Lock()

# 模型定义已收口到 modules/runoff_engine.py（单一定义，训练/推理共用语义）。
# 这里的 RunoffLSTM 只是再导出，避免其他模块的旧 import 失效。
# 与旧定义的差异：forward(x, state=None) 返回 (y, state)，用于跨日状态传递。


# GPU配置参数
GPU_CONFIG = {
    'batch_size': 4096,  # 默认批处理大小
    'max_batch_size': 16384,  # 最大批处理大小
    'min_batch_size': 1024,   # 最小批处理大小
    'memory_safety_factor': 0.7  # GPU内存安全系数，防止内存溢出
}
# 模型缓存：同一模型只加载一次，避免每次请求重复 torch.load
_MODEL_CACHE = {}
_MODEL_CACHE_LOCK = threading.Lock()

# 径流模拟互斥锁：同一时刻只允许一个模拟任务在跑。
#
# 背景：本接口是同步阻塞的长任务（全域单日约 18 秒，全年约 1.8 小时），
# 且输出文件名由日期决定（{date}_runoff.tif），不含 run_id。
# 两个并发请求会写同一批文件名 —— 2026-08-30 线上就因此挂掉：
# rasterio 写前会先删同名旧文件，Windows 上撞 Permission denied，任务直接 500；
# 同时两个 torch 各开 8 线程抢 8 个核，单日耗时从 18 秒劣化到 70 秒。
#
# 这里是进程内互斥（Flask threaded=True 的多请求场景）。
# 跨进程的兜底由 runoff_engine.write_geotiff_atomic 的原子替换负责。
# 2026-09-24 恢复：该锁曾于 2026-08-30 按需求移除，但其从未 commit 进 git，
# 丢失后前端按钮双绑定的老 bug 失去兜底而暴露（详见会话记录），故恢复。
_RUNOFF_RUN_LOCK = threading.Lock()


def _ensure_torch_threads():
    """把 torch 线程数对齐 CPU 核数（默认值常为 4，本机 8 核）。

    本机环境为 torch 2.8.0+cpu（无 CUDA），GPU 分支全部是死代码，
    实际走 CPU 路径。这一步是纯收益，不改变计算结果。
    封顶 64：在 128 逻辑线程的机器上，hidden=100 的小 LSTM 用满 128
    线程属于过度订阅，且每线程工作区会推高内存压力
    （2026-09-25 63GB 服务器 OOM 事故）。封顶值是保守推算，待基准微调。
    """
    try:
        n = min(os.cpu_count() or 4, 64)
        if torch.get_num_threads() != n:
            torch.set_num_threads(n)
        return n
    except Exception:
        return torch.get_num_threads()


def _safe_basename(directory, name):
    """校验文件名：必须是纯文件名（禁止路径分隔符/..），并确认文件存在。"""
    if not name or not isinstance(name, str):
        return None
    if os.path.basename(name) != name:
        return None
    if '..' in name or '/' in name or '\\' in name:
        return None
    full = os.path.join(directory, name)
    return full if os.path.isfile(full) else None


def _load_model_cached(model_path, input_size, device):
    """加载模型并缓存（key: 路径+输入维度+设备）。"""
    key = (model_path, input_size, str(device))
    model = _MODEL_CACHE.get(key)
    if model is not None:
        return model
    model = RunoffLSTM(input_size=input_size).to(device)
    try:
        state = torch.load(model_path, map_location=device, weights_only=True)
    except Exception:
        # 兼容旧版本模型文件（不支持 weights_only 参数时）
        state = torch.load(model_path, map_location=device)
    if isinstance(state, dict) and 'state_dict' in state:
        state = state['state_dict']
    model.load_state_dict(state)
    model.eval()
    _MODEL_CACHE[key] = model
    return model


# 模型列表接口（使用配置文件中的模型目录）
def list_models():
    models_dir = current_app.config['MODELS_DIR']  # 直接从配置获取，与config.py一致
    if not os.path.exists(models_dir):
        current_app.logger.warning(f"模型目录不存在: {models_dir}")
        return []
    files = glob.glob(os.path.join(models_dir, '*.pt'))
    return [os.path.basename(f) for f in files]


@simulate_bp.route('/models', methods=['GET'])
def api_models():
    return jsonify(list_models())


def list_runoff_files():
    """列出所有径流输出文件"""
    try:
        runoff_output_dir = current_app.config['RUNOFF_OUTPUT_DIR']
        if not os.path.exists(runoff_output_dir):
            current_app.logger.warning(f"径流输出目录不存在: {runoff_output_dir}")
            return []
        
        # 获取所有径流输出文件
        runoff_files = []
        try:
            for filename in os.listdir(runoff_output_dir):
                if filename.endswith('_runoff.tif'):
                    file_path = os.path.join(runoff_output_dir, filename)
                    # 检查文件是否可访问
                    if os.access(file_path, os.R_OK):
                        # 将文件路径转换为URL路径
                        file_url = f"/backend-static/runoff_results/{filename}"
                        runoff_files.append(file_url)
                    else:
                        current_app.logger.warning(f"径流输出文件不可读: {file_path}")
        except PermissionError as pe:
            current_app.logger.error(f"访问径流输出目录时权限被拒绝: {pe}")
            return []
        
        current_app.logger.info(f"找到 {len(runoff_files)} 个径流输出文件")
        return runoff_files
    except Exception as e:
        current_app.logger.error(f"获取径流文件列表出错: {str(e)}")
        return []


@simulate_bp.route('/runoff', methods=['POST'])
def api_runoff():
    """径流模拟。

    推理已下沉到 modules/runoff_engine.py：像元做 batch、沿时间展开、
    掩膜前置、只算有效像元、按日推进并传递 LSTM 状态。

    新增参数 warmup_days：向前多读若干天驱动用于预热 LSTM 记忆（不写盘）。
    寒区融雪径流对前期积雪累积敏感，建议 ≥ 180 天；但预热会线性增加耗时，
    且受状态内存预算约束（见返回的 fallback_reason）。
    并发：同一时刻只允许一个模拟任务，第二个直接返回 409。
    """
    lock_acquired = False
    try:
        data = request.get_json() or {}
        model_name = data.get('model') or data.get('model_name')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        basin_id = data.get('basin_id')
        custom_batch_size = data.get('batch_size')
        warmup_days = data.get('warmup_days', 0)

        if not model_name or not start_date or not end_date:
            return jsonify({'error': '参数不完整，必须包含model/model_name、start_date和end_date'}), 400

        try:
            warmup_days = max(0, int(warmup_days))
        except (TypeError, ValueError):
            return jsonify({'error': 'warmup_days 必须是非负整数'}), 400

        variables = current_app.config['VARIABLES']
        models_dir = current_app.config['MODELS_DIR']
        runoff_output_dir = current_app.config['RUNOFF_OUTPUT_DIR']

        model_path = _safe_basename(models_dir, model_name)
        if model_path is None:
            return jsonify({'error': f'模型文件不存在或文件名非法: {model_name}'}), 400

        try:
            start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.datetime.strptime(end_date, '%Y-%m-%d')
        except (TypeError, ValueError):
            return jsonify({'error': '日期格式不正确，请使用 YYYY-MM-DD'}), 400
        if start_dt > end_dt:
            return jsonify({'error': '开始日期不能晚于结束日期'}), 400

        # warmup：向前多取若干天驱动预热，这些日期不产出结果文件
        warmup_start = (start_dt - datetime.timedelta(days=warmup_days)).strftime('%Y-%m-%d')

        # 数据库路径统一取自配置（旧实现靠 __file__ 硬拼，绕开了 config）
        db_path = current_app.config['DATABASE_PATH']
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT variable, date, file_path FROM TibetanPlateau_index "
                "WHERE date BETWEEN ? AND ? ORDER BY date, variable",
                (warmup_start, end_date))
            rows = cursor.fetchall()
        except sqlite3.Error as e:
            current_app.logger.error(f'气象索引查询失败: {e}', exc_info=True)
            return jsonify({'error': '气象索引查询失败，请检查数据库'}), 500
        finally:
            if conn is not None:
                conn.close()

        if not rows:
            current_app.logger.error(f'指定日期范围内没有气象数据: {warmup_start} ~ {end_date}')
            return jsonify({'error': f'指定日期范围内没有气象数据: {warmup_start} ~ {end_date}'}), 400

        # 索引库存的是相对 TIF_ROOT_DIR 的路径（2026-09-07 重扫后），
        # 需补上根目录才能打开；用 resolve_index_path 兼容历史绝对路径写法。
        era5_root = current_app.config.get('TIF_ROOT_DIR', '')
        day_files = {}
        for row in rows:
            day_files.setdefault(row['date'], {})[row['variable']] = resolve_index_path(
                era5_root, row['file_path'])

        all_dates = sorted(day_files)
        out_dates = [d for d in all_dates if d >= start_date]
        if not out_dates:
            return jsonify({'error': f'指定日期范围内没有气象数据: {start_date} ~ {end_date}'}), 400

        # 参考文件：决定输出网格与 profile
        ref_path = None
        for d in all_dates:
            for v in variables:
                p = day_files[d].get(v)
                if p and os.path.isfile(p):
                    ref_path = p
                    break
            if ref_path:
                break
        if ref_path is None:
            return jsonify({'error': '没有找到有效的气象数据文件'}), 400

        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        n_threads = _ensure_torch_threads()
        try:
            model = _load_model_cached(model_path, len(variables), device)
        except Exception as e:
            current_app.logger.error(f"模型加载失败: {str(e)}", exc_info=True)
            return jsonify({'error': '模型加载失败，请检查模型文件'}), 500

        # 研究区总边界：所有模拟的默认上界，与前端地图常显的 TP_China 图层同源
        region_geometry = load_region_geometry(
            current_app.config.get('REGION_BOUNDARY_PATH'), current_app.logger)

        # 流域几何：有则掩膜前置，只对流域内像元做前向
        basin_geometry = None
        basin_key = str(basin_id).strip() if basin_id else ''
        if basin_key:
            basin_geometry = get_basin_geometry(basin_key)
            if basin_geometry is None:
                current_app.logger.warning(
                    f'未找到流域几何，按研究区边界计算: {basin_key}')

        # batch 像元数自适应：前端未显式指定时按 CPU 线程数推导
        # （8 线程 → 32768）；显式传值仍优先。
        if isinstance(custom_batch_size, int) and custom_batch_size > 0:
            batch_pixels = min(int(custom_batch_size), MAX_BATCH_PIXELS)
        else:
            batch_pixels = auto_batch_pixels(n_threads)

        # LSTM 状态内存预算自适应：按本机总/可用内存推导，
        # 内存充裕时全域模拟的跨日记忆（~4.65 GB）就能开启
        state_budget = auto_state_budget_bytes(current_app.logger)

        # 抢不到锁说明已有模拟在跑，直接拒绝，不要两个长任务互相拖垮
        if not _RUNOFF_RUN_LOCK.acquire(blocking=False):
            current_app.logger.warning('已有径流模拟任务在运行，拒绝本次请求')
            return jsonify({
                'error': '已有径流模拟任务在运行，请等待其完成后再提交',
                'code': 'simulation_busy'
            }), 409
        lock_acquired = True

        # ---- 任务开始横幅：把全部输入参数一次性摆清楚 ----
        total_b, avail_b = detect_memory_bytes()
        mem_desc = (f'总内存 {total_b/1024**3:.1f} GB / 可用 '
                    f'{avail_b/1024**3:.1f} GB') if total_b else '内存探测失败'
        current_app.logger.info('=' * 20 + ' 径流模拟任务开始 ' + '=' * 20)
        current_app.logger.info(f'  模型       : {model_name}')
        current_app.logger.info(
            f'  时间范围   : {start_date} ~ {end_date}'
            f'（含预热共 {len(all_dates)} 天，输出 {len(out_dates)} 天，'
            f'预热 {warmup_days} 天）')
        current_app.logger.info(
            f'  计算范围   : {basin_key if basin_key else "全研究区（TP_China 边界）"}')
        current_app.logger.info(
            f'  设备/线程  : {device}（torch 线程数 {n_threads}，{mem_desc}）')
        current_app.logger.info(
            f'  自适应参数 : batch {batch_pixels} 像元/批，'
            f'LSTM 状态预算 {state_budget/1024**3:.1f} GB'
            f'（按本机性能自动推导，前端传参可覆盖）')
        current_app.logger.info(
            f'  输出目录   : {runoff_output_dir}')

        # 进度回调：每 5 天（含最后一天）打一条，带已用/预计剩余时间，
        # 避免长任务期间十几分钟完全静默、无法判断是否卡死
        progress_t0 = time.perf_counter()

        def _report_progress(done, total, date):
            if done % 5 != 0 and done != total:
                return
            used = time.perf_counter() - progress_t0
            remain = used / done * (total - done) if done else 0.0
            current_app.logger.info(
                f'[径流模拟] 进度 {done}/{total} 天（{date}）'
                f' | 已用 {used:.0f}s | 预计剩余 {remain:.0f}s')

        try:
            stats = simulate_runoff(
                model=model, device=device, variables=variables,
                day_files=day_files, all_dates=all_dates, out_dates=out_dates,
                ref_path=ref_path, output_dir=runoff_output_dir,
                basin_geometry=basin_geometry, region_geometry=region_geometry,
                batch_pixels=batch_pixels,
                state_budget_bytes=state_budget,
                logger=current_app.logger,
                progress=_report_progress)
        except ValueError as e:
            # 网格不一致、无有效像元等可预期的输入问题，按 400 返回
            current_app.logger.error(f'径流模拟参数/数据问题: {e}')
            return jsonify({'error': str(e)}), 400

        if not stats['files']:
            current_app.logger.error('未能生成任何径流结果文件')
            return jsonify({'error': '无法生成径流结果'}), 500

        output_files = [f"/backend-static/runoff_results/{os.path.basename(p)}"
                        for p in stats['files']]

        # 指定流域时按前端约定再产出一份裁剪结果
        if basin_key and basin_geometry is not None:
            cropped_files = []
            for path in stats['files']:
                filename = os.path.basename(path)
                cropped_path = os.path.join(runoff_output_dir, f"cropped_{filename}")
                try:
                    if crop_raster_by_basin(path, cropped_path, basin_geometry):
                        cropped_files.append(
                            f"/backend-static/runoff_results/cropped_{filename}")
                    else:
                        cropped_files.append(
                            f"/backend-static/runoff_results/{filename}")
                except Exception as e:
                    current_app.logger.error(f'裁剪 {filename} 出错: {e}')
                    cropped_files.append(f"/backend-static/runoff_results/{filename}")
            output_files = cropped_files

        return jsonify({
            'status': 'success',
            'message': '模拟完成',
            'data': {
                'start_date': start_date,
                'end_date': end_date,
                'model': model_name,
                'files': output_files,
                'output_dir_all_files': list_runoff_files(),
                'device_used': str(device),
                'batch_pixels': batch_pixels,
                'basin_id': basin_id,
                'warmup_days': len(all_dates) - len(out_dates),
                'n_pixels': stats['n_pixels'],
                'cross_day_memory': stats['cross_day_memory'],
                'fallback_reason': stats['fallback_reason'],
                'nodata_values_filled': stats['nodata_values_filled'],
                'elapsed_seconds': stats['elapsed_seconds'],
            }
        }), 200

    except Exception as e:
        current_app.logger.error(f'模拟接口出错: {str(e)}', exc_info=True)
        return jsonify({'error': '服务器内部错误'}), 500
    finally:
        # 无论成功、参数错误还是异常，都必须放锁，否则服务会被永久卡死
        if lock_acquired:
            _RUNOFF_RUN_LOCK.release()


# 新增：单独的"获取输出目录所有径流文件"接口（如果前端需要主动刷新）
@simulate_bp.route('/runoff/files', methods=['GET'])
def api_runoff_files():
    try:
        all_runoff_files = list_runoff_files()
        return jsonify({
            "all_runoff_files": all_runoff_files
        })
    except Exception as e:
        current_app.logger.error(f'获取径流文件列表出错: {str(e)}', exc_info=True)
        return jsonify({'error': '服务器内部错误'}), 500


# 获取最新TIFF文件接口
@simulate_bp.route('/latest_tif', methods=['GET'])
def api_get_latest_tif():
    try:
        # 获取径流输出目录
        runoff_output_dir = current_app.config['RUNOFF_OUTPUT_DIR']
        
        # 获取所有TIFF文件
        tif_files = glob.glob(os.path.join(runoff_output_dir, "*.tif"))
        
        if not tif_files:
            return jsonify({
                'status': 'success',
                'message': '没有找到TIFF文件',
                'files': []
            })
        
        # 按修改时间排序，最新的在前面
        tif_files.sort(key=os.path.getmtime, reverse=True)
        
        # 构建文件信息列表
        files_info = []
        for file_path in tif_files:
            file_name = os.path.basename(file_path)
            # 从文件名中提取日期
            date_match = os.path.splitext(file_name)[0].split('_')[0]  # 假设格式是 "YYYY-MM-DD_..."
            if len(date_match) == 10 and '-' in date_match:  # 检查是否是日期格式
                date = date_match
            else:
                # 如果无法解析日期，使用文件修改时间
                date = datetime.datetime.fromtimestamp(os.path.getmtime(file_path)).strftime('%Y-%m-%d')
            
            files_info.append({
                'filePath': f'/backend-static/runoff_results/{file_name}',  # 返回相对于前端的路径
                'fileName': file_name,
                'date': date,
                'modifiedTime': datetime.datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat()
            })
        
        return jsonify({
            'status': 'success',
            'files': files_info
        })
    except Exception as e:
        current_app.logger.error(f'获取最新TIFF文件出错: {str(e)}', exc_info=True)
        return jsonify({'error': '服务器内部错误'}), 500


# ========================
# 栅格读取与对齐辅助（淹没判定共用）
# ========================

# float32 的极小值常被当作 nodata 哨兵（本项目 ERA5 与阈值场均使用）
_F32_SENTINEL = -3.4028234663852886e+38


def _read_grid_as_nan(path):
    """读取单波段栅格，把 nodata 与 -3.4e38 哨兵统一转成 NaN。

    本项目历史产物里混用了三种无效值标记（NaN / -3.4e38 / 0），
    下游若不统一处理就会出现掩膜失效，表现为"研究区外出现极值斑块"。
    """
    with rasterio.open(path) as ds:
        arr = ds.read(1).astype(np.float32)
        profile = ds.profile.copy()
        transform = ds.transform
        crs = ds.crs
        nodata = ds.nodata
    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    # 兜底：即便文件没声明 nodata，也清理 float32 哨兵
    arr = np.where(arr <= _F32_SENTINEL * 0.99, np.nan, arr)
    return arr, profile, transform, crs


def _check_scale_consistency(runoff_data, threshold_data, threshold_name, logger):
    """量纲一致性检查：径流与阈值必须同量级，否则淹没判定毫无意义。

    阈值场 grid_threshold_*p 是由历史径流的分位数算出来的，因此与径流同量纲。
    若两者量级差一个数量级以上，多半是某一侧被重新缩放、翻转或换了单位，
    此时判定结果（淹没面积、洪水概率）会全盘失真，必须显式告警。
    """
    r = runoff_data[np.isfinite(runoff_data)]
    t = threshold_data[np.isfinite(threshold_data)]
    if r.size == 0 or t.size == 0:
        return
    r_med = float(np.median(np.abs(r)))
    t_med = float(np.median(np.abs(t)))
    if r_med <= 0 or t_med <= 0:
        return
    ratio = max(r_med, t_med) / min(r_med, t_med)
    if ratio > 10:
        logger.warning(
            f'量纲可疑：径流中位数绝对值 {r_med:.4g} 与阈值场 {threshold_name} 的 '
            f'{t_med:.4g} 相差 {ratio:.1f} 倍。淹没判定结果可能不可信，'
            f'请核对径流与阈值是否同源同量纲。')


def _align_grid(arr, src_transform, src_crs, ref_shape, ref_transform, ref_crs,
                logger=None):
    """把 arr 对齐到参考网格。

    网格完全一致时原样返回（零开销）。否则用 rasterio.warp.reproject 做带平移的
    重投影 —— 不能用 scipy.ndimage.zoom：它只做缩放、不做平移，会把阈值场在
    地理上整片平移错位（曾实测 ~540 km / 数百像元的横移，导致西藏东部的阈值被
    搬到新疆西部去判定）。故只用 rasterio.warp.reproject + nearest。阈值场与径流
    网格的"谁大谁小"不固定（取决于裁剪历史），本函数两种方向都能正确对齐。
    """
    if arr.shape == ref_shape and src_transform == ref_transform and src_crs == ref_crs:
        return arr, False

    if logger:
        logger.warning(
            f'栅格网格不一致，执行重投影对齐：源 {arr.shape} left={src_transform.c:.4f} '
            f'-> 目标 {ref_shape} left={ref_transform.c:.4f}')

    from rasterio.warp import reproject as _reproject, Resampling
    out = np.full(ref_shape, np.nan, dtype=np.float32)
    _reproject(
        source=arr, destination=out,
        src_transform=src_transform, src_crs=src_crs, src_nodata=np.nan,
        dst_transform=ref_transform, dst_crs=ref_crs, dst_nodata=np.nan,
        # 阈值是分位数，插值会造出不存在的阈值，故用 nearest
        resampling=Resampling.nearest)
    return out, True


# 洪水淹没分析接口
@simulate_bp.route('/flood-inundation', methods=['POST'])
def api_flood_inundation():
    try:
        data = request.get_json()
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        threshold = data.get('threshold')
        basin_id = data.get('basin_id')

        current_app.logger.info(f'收到洪水淹没分析请求: start_date={start_date}, end_date={end_date}, threshold={threshold}')

        # 参数校验
        if not start_date or not end_date or not threshold:
            current_app.logger.error(f'参数不完整: start_date={start_date}, end_date={end_date}, threshold={threshold}')
            return jsonify({'error': '参数不完整，必须包含start_date、end_date和threshold'}), 400

        # 验证日期格式
        try:
            start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.datetime.strptime(end_date, '%Y-%m-%d')
            if start_dt > end_dt:
                current_app.logger.error(f'日期范围错误: 开始日期({start_date})晚于结束日期({end_date})')
                return jsonify({'error': '开始日期不能晚于结束日期'}), 400
        except ValueError:
            current_app.logger.error(f'日期格式错误: start_date={start_date}, end_date={end_date}')
            return jsonify({'error': '日期格式不正确，请使用YYYY-MM-DD格式'}), 400

        # 校验阈值文件名，防止路径穿越
        thresholds_dir = current_app.config['THRESHOLDS_DIR']
        threshold_path = _safe_basename(thresholds_dir, threshold)
        if threshold_path is None:
            current_app.logger.error(f'阈值文件不存在或文件名非法: {threshold}')
            return jsonify({'error': f'阈值文件不存在: {threshold}'}), 400

        # 获取径流输出目录
        runoff_output_dir = current_app.config['RUNOFF_OUTPUT_DIR']
        current_app.logger.info(f'径流输出目录: {runoff_output_dir}')
        
        # 查找指定日期范围内的径流文件
        date_list = []
        for date_str in [(start_dt + datetime.timedelta(days=i)).strftime('%Y-%m-%d') 
                         for i in range((end_dt - start_dt).days + 1)]:
            runoff_file = os.path.join(runoff_output_dir, f"{date_str}_runoff.tif")
            if os.path.exists(runoff_file):
                date_list.append(date_str)
            else:
                current_app.logger.debug(f'径流文件不存在: {runoff_file}')

        if not date_list:
            current_app.logger.error(f'指定日期范围内没有找到径流结果文件: {start_date} ~ {end_date}')
            return jsonify({'error': f'指定日期范围内没有找到径流结果文件，请先进行径流模拟。日期范围: {start_date} ~ {end_date}'}), 400

        flood_results = []

        # 阈值场在循环外只读取一次（原实现在逐日循环内反复打开）
        threshold_data, thr_profile, thr_transform, thr_crs = _read_grid_as_nan(threshold_path)
        thr_ready = False      # 是否已按径流网格完成对齐
        thr_aligned = False    # 对齐时是否发生了重投影

        for date_str in date_list:
            try:
                runoff_file = os.path.join(runoff_output_dir, f"{date_str}_runoff.tif")
                if not os.path.exists(runoff_file):
                    current_app.logger.warning(f'径流文件不存在: {runoff_file}')
                    continue

                # 径流栅格：nodata 与 -3.4e38 哨兵统一转成 NaN
                runoff_data, runoff_profile, run_transform, run_crs = _read_grid_as_nan(runoff_file)

                # 阈值场只在首次进入时对齐一次（参考网格取自径流栅格）。
                # 原实现在逐日循环内反复打开并可能反复重采样，
                # 处理 365 天就是 365 次约 22 MB 栅格的读取与缩放。
                if not thr_ready:
                    threshold_data, thr_aligned = _align_grid(
                        threshold_data, thr_transform, thr_crs,
                        runoff_data.shape, run_transform, run_crs,
                        current_app.logger)
                    thr_ready = True
                    if thr_aligned:
                        current_app.logger.warning(
                            f'阈值场 {threshold} 与径流网格不一致，本请求已临时重投影。'
                            f'建议先执行 Training/input/fix_grid_consistency.py 预先对齐，'
                            f'避免每个请求都付出重采样开销。')
                    _check_scale_consistency(runoff_data, threshold_data,
                                             threshold, current_app.logger)

                # 淹没判定：只有"径流与阈值都有效"的像元参与比较，
                # 其余保持 NaN（原实现用 0 填充，与"无淹没"的 0 无法区分）
                valid = np.isfinite(runoff_data) & np.isfinite(threshold_data)
                inundation_result = np.full(runoff_data.shape, np.nan, dtype=np.float32)
                hit = valid & (runoff_data >= threshold_data)
                inundation_result[hit] = runoff_data[hit]
                inundation_result[valid & ~hit] = 0.0

                # 按流域裁剪（流域外置 0）
                if basin_id:
                    basin_geom = get_basin_geometry(str(basin_id))
                    if basin_geom:
                        try:
                            from rasterio.features import geometry_mask
                            from rasterio.warp import transform_geom
                            runoff_crs = str(runoff_profile.get('crs') or '')
                            # 若 runoff 栅格非 EPSG:4326，需把流域 geometry 转到栅格 CRS
                            if runoff_crs and runoff_crs not in ('EPSG:4326', 'EPSG:4269', ''):
                                basin_geom = transform_geom('EPSG:4326', runoff_crs, basin_geom)
                                current_app.logger.info(f"流域几何已转换到 {runoff_crs}")
                            basin_mask = geometry_mask(
                                [basin_geom], out_shape=inundation_result.shape,
                                transform=runoff_profile.get('transform'), all_touched=True, invert=True
                            )
                            inundation_result = np.where(basin_mask, inundation_result, 0).astype(np.float32)
                            current_app.logger.info(f"流域裁剪完成: basin_id={basin_id}, runoff CRS={runoff_crs}, 裁剪后非零像元={int(np.sum(inundation_result > 0))}")
                        except Exception as e:
                            current_app.logger.warning(f"流域裁剪失败: {e}")
                    else:
                        current_app.logger.warning(f"未找到流域几何: {basin_id}")

                has_flood = np.sum(inundation_result > 0) > 0

                if has_flood:
                    flood_output_dir = current_app.config['FLOOD_INUNDATION_DIR']
                    os.makedirs(flood_output_dir, exist_ok=True)
                    
                    output_filename = f"{date_str}_flood_inundation.tif"
                    output_path = os.path.join(flood_output_dir, output_filename)

                    output_profile = runoff_profile.copy()
                    output_profile.update(
                        dtype=rasterio.float32,
                        count=1,
                        # 原实现 compress=None：单日单层就要 22 MB，
                        # 365 天 × 5 个阈值场约 40 GB。
                        # 压缩格式只能用前端 geotiff.js 认得的：
                        #   1 无压缩 / 5 LZW / 8 Deflate / 32773 PackBits / 34887 LERC
                        # GDAL 的 zstd 写的是 tag 50000，前端不认（只注册了 50001），
                        # 会报 "Unknown compression method identifier: 50000"，故禁用。
                        compress='lzw',
                        # 无效区统一用 NaN，而不是 0 —— 0 与"无淹没"无法区分，
                        # 前端按 nodata 过滤时也会把真实的无淹没像元一起滤掉
                        nodata=np.nan
                    )

                    # 并发保护：用模块级锁串行化写出，且先写唯一临时文件再原子替换，
                    # 杜绝两个并发请求抢同一批固定文件名（侧边栏/主区双入口或重复点击）
                    # 导致的 Windows 文件锁冲突与半成品 TIFF。
                    with FLOOD_WRITE_LOCK:
                        fd, tmp_path = tempfile.mkstemp(
                            prefix=output_filename + '.', suffix='.tmp',
                            dir=flood_output_dir)
                        os.close(fd)
                        try:
                            with rasterio.open(tmp_path, 'w', **output_profile) as dst:
                                dst.write(inundation_result, 1)
                            os.replace(tmp_path, output_path)
                        except Exception:
                            if os.path.exists(tmp_path):
                                try:
                                    os.remove(tmp_path)
                                except OSError:
                                    pass
                            raise
                    current_app.logger.info(f'成功写入洪水淹没结果文件: {output_path}')

                    output_url = f"/backend-static/flood_inundation/{output_filename}"

                    transform = runoff_profile.get('transform')
                    crs = runoff_profile.get('crs')
                    
                    pixel_width = abs(transform[0]) if transform else 0.008333
                    pixel_height = abs(transform[4]) if transform else 0.008333
                    
                    if crs and crs.is_geographic:
                        center_lat = (transform[5] + transform[5] - pixel_height * runoff_data.shape[0]) / 2
                        lat_rad = np.radians(center_lat)
                        meters_per_deg_lat = 111132.954 - 559.822 * np.cos(2 * lat_rad) + 1.175 * np.cos(4 * lat_rad)
                        meters_per_deg_lon = 111412.84 * np.cos(lat_rad) - 93.5 * np.cos(3 * lat_rad) + 0.118 * np.cos(5 * lat_rad)
                        pixel_area_m2 = (pixel_width * meters_per_deg_lon) * (pixel_height * meters_per_deg_lat)
                    else:
                        pixel_area_m2 = pixel_width * pixel_height
                    
                    flood_mask = inundation_result > 0
                    flood_pixels = int(np.sum(flood_mask))
                    total_pixels = int(np.sum(~np.isnan(runoff_data)))
                    flood_ratio = float(flood_pixels / total_pixels) if total_pixels > 0 else 0.0
                    
                    flood_area_km2 = float(flood_pixels * pixel_area_m2) / 1000000 if pixel_area_m2 > 0 else 0.0
                    mean_value = float(np.nanmean(runoff_data)) if not np.isnan(np.nanmean(runoff_data)) else None
                    max_value = float(np.nanmax(runoff_data)) if not np.isnan(np.nanmax(runoff_data)) else None

                    flood_results.append({
                        'date': date_str,
                        'has_flood': True,
                        'result_file': output_url,
                        'result_file_path': output_path,
                        'flood_pixels': flood_pixels,
                        'total_pixels': total_pixels,
                        'flood_ratio': flood_ratio,
                        'flood_area_km2': flood_area_km2,
                        'mean_value': mean_value,
                        'max_value': max_value
                    })
                else:
                    flood_results.append({
                        'date': date_str,
                        'has_flood': False,
                        'result_file': None,
                        'result_file_path': None,
                        'flood_pixels': 0,
                        'total_pixels': int(np.sum(~np.isnan(runoff_data))),
                        'flood_ratio': 0.0,
                        'flood_area_km2': 0.0,
                        'mean_value': None,
                        'max_value': None
                    })

                current_app.logger.info(f'洪水淹没分析完成: {date_str}')

            except Exception as e:
                current_app.logger.error(f'处理日期 {date_str} 的洪水淹没分析失败: {str(e)}')
                continue

        flood_results.sort(key=lambda x: x['date'])

        urls = [f['result_file'] for f in flood_results if f['result_file']]
        dates_processed = [f['date'] for f in flood_results if f['result_file']]
        
        total_flood_days = len(dates_processed)
        flood_probability = float(total_flood_days / len(date_list)) if date_list else 0.0
        
        avg_flood_ratio = 0.0
        avg_mean_value = 0.0
        if flood_results:
            valid_results = [f for f in flood_results if f['has_flood']]
            if valid_results:
                avg_flood_ratio = float(sum(f['flood_ratio'] for f in valid_results) / len(valid_results))
                valid_means = [f['mean_value'] for f in valid_results if f['mean_value'] is not None]
                avg_mean_value = float(sum(valid_means) / len(valid_means)) if valid_means else None

        return jsonify({
            'status': 'success',
            'message': f'洪水淹没分析完成，共处理 {len(date_list)} 天数据，发现 {total_flood_days} 天洪水',
            'threshold': threshold,
            'start_date': start_date,
            'end_date': end_date,
            'total_days': len(date_list),
            'valid_days': len(date_list),
            'flood_days': total_flood_days,
            'flood_probability': flood_probability,
            'avg_flood_ratio': avg_flood_ratio,
            'avg_mean_value': avg_mean_value,
            'flood_results': flood_results,
            'urls': urls,
            'dates_processed': dates_processed
        }), 200

    except Exception as e:
        current_app.logger.error(f'洪水淹没分析接口出错: {str(e)}', exc_info=True)
        return jsonify({'error': '服务器内部错误'}), 500


# 洪水淹没动画生成接口
# 获取洪水淹没分析阈值列表
@simulate_bp.route('/flood/thresholds', methods=['GET'])
def get_flood_thresholds():
    try:
        thresholds_dir = current_app.config['THRESHOLDS_DIR']
        if not os.path.exists(thresholds_dir):
            return jsonify([])
        
        files = glob.glob(os.path.join(thresholds_dir, '*.tif'))
        return jsonify([os.path.basename(f) for f in files])
    except Exception as e:
        current_app.logger.error(f"洪水淹没分析阈值列表获取失败: {e}")   
        return jsonify({"error": "服务器内部错误"}), 500

# 洪水淹没结果文件列表接口
@simulate_bp.route('/flood-results', methods=['GET'])
def api_flood_results():
    try:
        # 注：此处目录名 flood_results 与实际产出的 flood_inundation 不一致，
        # 为历史遗留（.gitignore 中同样留有 flood_results/）。未擅自改动数据口径，
        # 仅随目录迁移更换根路径。
        flood_output_dir = os.path.join(
            current_app.config['OUTPUTS_DIR'], 'flood_results')
        if not os.path.exists(flood_output_dir):
            return jsonify([])

        files = glob.glob(os.path.join(flood_output_dir, '*.tif'))
        file_urls = [f"/backend-static/flood_results/{os.path.basename(f)}" for f in files]

        return jsonify(file_urls)
    except Exception as e:
        current_app.logger.error(f'获取洪水淹没结果文件列表失败: {str(e)}')
        return jsonify({'error': '服务器内部错误'}), 500


# ========================
# 流域相关辅助函数（get_project_root 见 modules/utils.py）
# ========================

def get_basin_geometry(basin_id):
    """根据流域ID获取流域几何信息"""
    try:
        project_root = get_project_root()
        geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'hydrology', 'TP_Basins_China.geojson')

        if not os.path.exists(geojson_path):
            current_app.logger.warning(f"流域数据文件不存在: {geojson_path}")
            return None

        geojson_data = load_json_cached(geojson_path)

        # 查找匹配的流域
        features = geojson_data.get('features', [])

        # 1. 如果 basin_id 是数字，优先按索引匹配（与前端 value=索引 一致）
        if basin_id.isdigit():
            idx = int(basin_id)
            if 0 <= idx < len(features):
                return features[idx].get('geometry', None)

        # 2. 动态探测名称字段匹配（兼容不同数据源字段名）
        if features:
            first_props = features[0].get('properties', {})
            candidates = ['NAME', 'BASIN_NAME', 'BAS_NM', 'name', 'BASIN', 'PN', 'BAS_NAME', 'BasinName']
            name_field = next((f for f in candidates if first_props.get(f)), None)
            for feature in features:
                props = feature.get('properties', {})
                if name_field and props.get(name_field) == basin_id:
                    return feature.get('geometry', None)
                # 兼容旧字段
                if props.get('FID_Salwee') is not None and str(props.get('FID_Salwee')) == basin_id:
                    return feature.get('geometry', None)
                if props.get('BasinName') == basin_id:
                    return feature.get('geometry', None)

        current_app.logger.warning(f"未找到流域ID: {basin_id}")
        return None
    except Exception as e:
        current_app.logger.error(f"获取流域几何信息出错: {str(e)}")
        return None


def crop_raster_by_basin(input_path, output_path, basin_geometry):
    """
    根据流域几何信息裁剪栅格数据
    """
    try:
        import rasterio
        from rasterio.mask import mask
        from shapely.geometry import shape
        import numpy as np
        
        # 将GeoJSON几何转为Shapely几何对象
        geom_shape = shape(basin_geometry)
        
        with rasterio.open(input_path) as src:
            # 使用几何形状裁剪栅格
            try:
                # 对几何体进行裁剪
                out_image, out_transform = mask(dataset=src, shapes=[geom_shape], crop=True, nodata=np.nan)
                
                # 获取裁剪后的元数据
                out_meta = src.meta.copy()
                
                # 更新元数据
                out_meta.update({
                    "driver": "GTiff",
                    "height": out_image.shape[1],
                    "width": out_image.shape[2],
                    "transform": out_transform,
                    # 与径流输出保持一致：只用前端 geotiff.js 支持的压缩格式（LZW）
                    "compress": 'lzw'
                })
                
                # 写入裁剪后的数据
                with rasterio.open(output_path, "w", **out_meta) as dest:
                    dest.write(out_image)
                    
                current_app.logger.info(f"成功裁剪栅格: {input_path} -> {output_path}")
                return True
            except ValueError as ve:
                current_app.logger.warning(f"裁剪失败，可能是几何体与栅格无交集: {ve}")
                # 如果裁剪失败，复制原始文件
                import shutil
                shutil.copy2(input_path, output_path)
                return False
                
    except Exception as e:
        current_app.logger.error(f"裁剪栅格时出错: {str(e)}")
        # 如果处理失败，复制原始文件
        try:
            import shutil
            shutil.copy2(input_path, output_path)
        except Exception as copy_error:
            current_app.logger.error(f"复制原始文件也失败: {copy_error}")
        return False