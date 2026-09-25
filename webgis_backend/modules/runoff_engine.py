# -*- coding: utf-8 -*-
"""径流模拟推理引擎（在线服务用）

与旧实现 `simulate.py` 的逐日逐像元路径相比，本引擎做了四件事：

1. **修正序列语义**：LSTM 必须沿"时间"展开，像元只做 batch。
   旧实现喂的是 `(1, B_pixels, 4)`，即把一批空间上相邻的像元当成一条时间序列，
   于是每个像元的输出依赖同批次里排在它前面的像元，结果随 batch_size 与像元
   排列顺序变化，且跨日期无任何状态传递 —— 径流模拟最核心的"记忆"被丢弃。
   本引擎喂 `(B_pixels, 1, V)` 并显式传递 `(h, c)`。

2. **掩膜前置**：先用流域几何算窗口，再栅格化出有效像元索引，
   只对流域内的像元做前向。旧实现先算全域 637 万像元、最后才裁剪。

3. **剔除无效像元**：nodata 像元不参与前向。实测研究区有效像元仅 49.1%，
   旧实现对另外 51% 照算不误。

4. **按日推进 + 状态传递**：算完一天立刻写盘，不必把 (T, H, W) 的
   结果栈在内存里；跨日记忆靠 LSTM 的 (h, c) 传递。

内存上界由 `max_state_pixels` 控制：状态张量为 `P × 2 × 2 × 100 × 4 字节`
（h/c × 双向 × 层数 × hidden × float32）。超过预算时自动退化为"逐日独立推理"
（放弃跨日记忆），并在返回的 stats 里说明原因，避免在线服务被大请求打爆。
"""
import os
import sys
import time
import tempfile

import numpy as np
import rasterio
import torch
import torch.nn as nn
from rasterio.features import geometry_mask
from rasterio.windows import Window

# float32 的 nodata 哨兵（本项目 ERA5 与阈值场均使用）
_F32_SENTINEL = -3.4028234663852886e+38

# 实测甜点：batch 16384 时约 5.6 µs/(像元·步)，batch 1024 时劣化到 7.7 µs
DEFAULT_BATCH_PIXELS = 16384
MAX_BATCH_PIXELS = 262144

# 默认状态内存预算 2 GB。
# 实际占用 = P × (h+c) × 层数 × hidden_size × 4 字节，运行时按模型推算；
# 当前模型（2 层 × hidden 100）为 P × 1600 字节，故 2 GB 约对应 134 万像元。
# 2026-09-24 起在线服务不再直接用这个常量，而是 auto_state_budget_bytes()
# 按本机内存自适应推导（16GB/9GB 可用的机器可推出 ~5.4GB，全域模拟的
# 4.65GB 跨日状态就能放得下），该常量仅作探测失败时的兜底。
DEFAULT_STATE_BUDGET_BYTES = 2 * 1024 ** 3


def detect_memory_bytes():
    """探测（总内存, 可用内存）字节数；失败返回 (None, None)。

    Windows 走 GlobalMemoryStatusEx。注意必须取物理可用与**可提交额度**
    （ullAvailPageFile，物理+页面文件减去已提交）的较小值：
    2026-09-25 教训——63GB 内存的服务器"物理可用 40GB"，但页面文件很小、
    提交额度见底，连 47.7MB 都申请不到，导致连串 OOM。
    非 Windows 退回 os.sysconf。
    """
    try:
        if os.name == 'nt':
            import ctypes
            import ctypes.wintypes as wintypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ('dwLength', wintypes.DWORD),
                    ('dwMemoryLoad', wintypes.DWORD),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                # 物理可用 与 可提交额度 取小者，避免"有物理内存却申请不到"
                avail = min(int(st.ullAvailPhys), int(st.ullAvailPageFile))
                total = int(st.ullTotalPhys)
                return total, avail
            return None, None
        page = os.sysconf('SC_PAGE_SIZE')
        total = page * os.sysconf('SC_PHYS_PAGES')
        avail = page * os.sysconf('SC_AVPHYS_PAGES')
        return total, avail
    except Exception:
        return None, None


def auto_state_budget_bytes(logger=None):
    """LSTM 状态内存预算自适应。

    规则：min(总内存 × 40%, 可用内存 × 60%, 8 GB)，下限 2 GB。
    - 总内存 40%：给系统和其他进程留大头，避免状态张量把机器顶爆；
    - 可用内存 60%：预算随提交时刻的空闲度浮动，机器上刚开过大程序
      就自动收紧，宁降级不 OOM；
    - 8 GB 上限：再空也不至于把交换区卷进来拖垮速度。
    探测失败时退回固定 2 GB（DEFAULT_STATE_BUDGET_BYTES，原行为）。
    """
    total_b, avail_b = detect_memory_bytes()
    if not total_b or not avail_b:
        _log(logger, '内存探测失败，state 预算退回固定 2 GB', 'warning')
        return DEFAULT_STATE_BUDGET_BYTES
    budget = min(int(total_b * 0.4), int(avail_b * 0.6), 8 * 1024 ** 3)
    budget = max(budget, DEFAULT_STATE_BUDGET_BYTES)
    return budget


def auto_batch_pixels(cpu_threads=None):
    """batch 像元数自适应：CPU 线程数 × 4096，夹在 [16K, 64K]。

    8 线程 → 32768。batch 越大，Python 层的分批循环越少、每次 torch
    调度喂进去的数据越足；单批额外内存只有批 × 变量数 × 4 字节
    （32768 × 4 × 4B ≈ 0.5 MB 量级），放大是安全的。
    上限 65536：2026-09-25 实测 131072 在 128 线程/63GB 服务器上，
    LSTM 单次前向要一次性分配 ~2.1GB 连续内存而 OOM（可提交内存见底），
    故把上限砍半；再不够还有 simulate_runoff 里的 OOM 降档重试兜底。
    前端显式传 batch_size 的调用不受影响（仍尊重用户值）。
    """
    n = cpu_threads or os.cpu_count() or 4
    return int(min(max(n * 4096, DEFAULT_BATCH_PIXELS), 65536))


class RunoffLSTM(nn.Module):
    """两层 LSTM + FC + ReLU。

    与训练脚本 `Training/input/train.py` 的参数结构完全一致，可直接加载
    `lstm_runoff_model.pt`。差异仅在 forward 额外返回 (h, c)，用于跨日状态传递。
    """

    def __init__(self, input_size=4, hidden_size=100, num_layers=2, dropout=0.0):
        super().__init__()
        self.lstm = nn.LSTM(input_size=input_size, hidden_size=hidden_size,
                            num_layers=num_layers, batch_first=True,
                            dropout=dropout if num_layers > 1 else 0.0)
        self.fc = nn.Linear(hidden_size, 1)
        self.relu = nn.ReLU()

    def forward(self, x, state=None):
        out, state = self.lstm(x, state)
        return self.relu(self.fc(out).squeeze(-1)), state


def _log(logger, msg, level='info'):
    if logger is None:
        return
    getattr(logger, level, logger.info)(msg)


def _windows_intersect(transform, height, width, geom, margin=1):
    """由几何外包络算栅格窗口 (r0, c0, r1, c1)，留 margin 像元安全边。"""
    from rasterio.transform import rowcol

    if geom.get('type') in ('MultiPolygon', 'GeometryCollection'):
        coords = []
        for poly in geom.get('coordinates', []):
            for ring in poly:
                coords.extend(ring)
    elif geom.get('type') == 'Polygon':
        coords = [pt for ring in geom.get('coordinates', []) for pt in ring]
    else:
        coords = list(geom.get('coordinates', []))

    if not coords:
        raise ValueError('流域几何为空')

    xs = np.array([c[0] for c in coords], dtype='float64')
    ys = np.array([c[1] for c in coords], dtype='float64')
    rows, cols = rowcol(transform, xs, ys)

    r0 = max(0, int(np.floor(rows.min())) - margin)
    r1 = min(height, int(np.ceil(rows.max())) + 1 + margin)
    c0 = max(0, int(np.floor(cols.min())) - margin)
    c1 = min(width, int(np.ceil(cols.max())) + 1 + margin)
    if r0 >= r1 or c0 >= c1:
        raise ValueError('流域几何与研究区栅格无交集')
    return r0, c0, r1, c1


def _read_day_vars(day_files, date, variables, window, rl, cl, exp_shape,
                   logger=None):
    """读取某天所有变量在有效像元上的取值。

    返回 (X, bad)：X 为 (V, P) float32，bad 为 (V, P) bool（True = 该值无效）。
    """
    V, P = len(variables), rl.shape[0]
    X = np.empty((V, P), dtype=np.float32)
    bad = np.zeros((V, P), dtype=bool)

    for vi, var in enumerate(variables):
        path = day_files.get(date, {}).get(var)
        if not path or not os.path.isfile(path):
            X[vi] = np.nan
            bad[vi] = True
            continue
        with rasterio.open(path) as src:
            # 逐文件校验网格：历史上 temperature_2m 曾有一批文件被裁成 3507 列，
            # 其余变量是 4082 列，此时 [rl, cl] 索引会静默错位而非报错。
            if src.height != exp_shape[0] or src.width != exp_shape[1]:
                raise ValueError(
                    f'栅格网格不一致：{os.path.basename(path)} 为 '
                    f'{src.height}×{src.width}，而参考文件为 '
                    f'{exp_shape[0]}×{exp_shape[1]}。'
                    f'请先执行 Training/input/fix_grid_consistency.py --fix')
            band = src.read(1, window=window)
            nod = src.nodata
        v = band[rl, cl].astype(np.float32, copy=False)
        b = ~np.isfinite(v)
        if nod is not None:
            b |= (v == nod)
        b |= (v <= _F32_SENTINEL * 0.99)
        X[vi], bad[vi] = v, b
    return X, bad


def _valid_mask_of(path, window, nodata_hint=None):
    """读一个单波段并给出"该窗口内哪些像元有真实值"的布尔掩膜。

    本项目同一目录里混着两代驱动文件，无效值编码不同：
      - 旧一代（2024-01-01 ~ 2024-04-13，104 个文件）：nodata 声明为 -3.4e38 哨兵
      - 新一代（2024-04-14 起，261 个文件）：nodata 未声明，无效值直接是 NaN
    两种都要认，否则掩膜会漏掉一整代数据。
    """
    with rasterio.open(path) as src:
        band = src.read(1, window=window)
        nod = src.nodata
    bad = ~np.isfinite(band)
    if nod is not None:
        bad |= (band == nod)
    bad |= (band <= _F32_SENTINEL * 0.99)
    return ~bad, (int(band.shape[0]), int(band.shape[1]))


def build_pixel_index(ref_path, basin_geometry=None, region_geometry=None,
                      logger=None):
    """确定计算区域（窗口）与几何掩膜。

    只做几何层面的裁剪，nodata 掩膜交给 build_valid_mask（需跨日期采样）。

    region_geometry 是研究区总边界（本项目为 TP_China.geojson，即青藏高原中国境内）。
    它是**所有模拟的默认上界**：
      - 未选流域时，计算范围 = 区域边界；
      - 选定流域时，计算范围 = 流域 ∩ 区域边界（防止流域越出研究区）。
    驱动栅格本身覆盖 68.02~104.69°E（整个青藏高原，含境外），
    而研究区边界是 73.45~104.69°E，二者相差 591,992 个像元。
    """
    with rasterio.open(ref_path) as ref:
        height, width = ref.height, ref.width
        transform, crs = ref.transform, ref.crs
        profile = ref.meta.copy()

    # 窗口取流域外包络；无流域时退回区域边界外包络，再退回全图
    scope_geom = basin_geometry if basin_geometry is not None else region_geometry
    if scope_geom is not None:
        r0, c0, r1, c1 = _windows_intersect(transform, height, width, scope_geom)
        win = Window(c0, r0, c1 - c0, r1 - r0)
    else:
        win = Window(0, 0, width, height)

    if basin_geometry is not None:
        scope = 'basin'
    elif region_geometry is not None:
        scope = 'region'
    else:
        scope = 'full'

    local_transform = rasterio.windows.transform(win, transform)
    win_h, win_w = int(win.height), int(win.width)

    keep = np.ones((win_h, win_w), dtype=bool)
    if region_geometry is not None:
        keep &= geometry_mask([region_geometry], out_shape=(win_h, win_w),
                              transform=local_transform, invert=True)
    if basin_geometry is not None:
        keep &= geometry_mask([basin_geometry], out_shape=(win_h, win_w),
                              transform=local_transform, invert=True)

    if logger is not None and not keep.any():
        _log(logger, '几何掩膜为空：所选流域与研究区边界无交集', 'warning')

    return {
        'height': height, 'width': width, 'transform': transform, 'crs': crs,
        'profile': profile, 'window': win, 'row_off': int(win.row_off),
        'col_off': int(win.col_off), 'win_shape': (win_h, win_w),
        'keep': keep, 'scope': scope,
    }


def load_region_geometry(path, logger=None):
    """读取研究区总边界（GeoJSON 首要素；多要素时取全部）。

    返回 GeoJSON geometry 字典；文件缺失或解析失败时返回 None（退化为不裁剪）。
    """
    if not path or not os.path.isfile(path):
        _log(logger, f'研究区边界文件不存在，将不按区域裁剪: {path}', 'warning')
        return None
    try:
        import json
        with open(path, 'r', encoding='utf-8') as f:
            gj = json.load(f)
        feats = gj.get('features', [])
        if not feats:
            _log(logger, f'研究区边界文件无要素: {path}', 'warning')
            return None
        if len(feats) == 1:
            return feats[0].get('geometry')
        # 多要素：包成 GeometryCollection，geometry_mask 能直接吃
        return {'type': 'GeometryCollection',
                'geometries': [f.get('geometry') for f in feats
                               if f.get('geometry')]}
    except Exception as e:
        _log(logger, f'研究区边界读取失败，将不按区域裁剪: {e}', 'warning')
        return None


def build_valid_mask(grid, day_files, all_dates, variables, n_samples=4,
                     logger=None):
    """跨日期采样求有效像元的**并集**掩膜。

    为什么必须采样而不是只看第一天：新旧两代数据的有效范围并不相同
    （旧 3,125,430 像元 ⊂ 新 3,717,422 像元，相差 591,992）。
    若只按第一天收口，跨 2024-04-14 切换点的模拟会静默丢掉 59 万个像元；
    取并集后，这些像元在旧日期上由前向填充补齐，不会凭空消失。
    """
    win = grid['window']
    win_shape = grid['win_shape']

    sample_dates = all_dates
    if len(all_dates) > n_samples:
        step = len(all_dates) / float(n_samples)
        sample_dates = [all_dates[min(len(all_dates) - 1,
                                      int(i * step))] for i in range(n_samples)]
        if all_dates[-1] not in sample_dates:
            sample_dates[-1] = all_dates[-1]

    union = np.zeros(win_shape, dtype=bool)
    n_used = 0
    for date in sample_dates:
        path = day_files.get(date, {}).get(variables[0])
        if not path or not os.path.isfile(path):
            continue
        with rasterio.open(path) as src:
            if src.height != grid['height'] or src.width != grid['width']:
                raise ValueError(
                    f'栅格网格不一致：{os.path.basename(path)} 为 '
                    f'{src.height}×{src.width}，而参考文件为 '
                    f'{grid["height"]}×{grid["width"]}。'
                    f'请先执行 Training/input/fix_grid_consistency.py --fix')
        m, _ = _valid_mask_of(path, win)
        union |= m
        n_used += 1

    if n_used == 0:
        raise ValueError('无法从驱动数据确定有效像元掩膜')

    keep = grid['keep'] & union
    _log(logger, f'有效掩膜采样 {n_used} 天（共 {len(all_dates)} 天），'
                 f'并集 {int(union.sum()):,} 像元，'
                 f'与流域掩膜相交后 {int(keep.sum()):,} 像元')
    return keep


def write_geotiff_atomic(out_path, array, profile, retries=5, logger=None):
    """原子写 GeoTIFF：先写同目录临时文件，再 os.replace 替换。

    为什么必须绕开 rasterio.open(path, 'w')：
    它内部会先调用 _delete_dataset_if_exists 删掉同名旧文件。Windows 上
    只要该文件被另一个句柄打开（例如两个并发模拟同时推进到同一天），
    删除就会抛 CPLE_AppDefinedError: Permission denied，整个任务直接 500。
    2026-08-30 线上就是这么挂的。

    os.replace 在 Windows 上是原子操作：要么完整替换，要么不动，
    不会留下写了一半的文件。由于两个写者各自写唯一临时文件、谁都不持有
    目标文件的写句柄，并发写同一路径可以两者都成功（已实测）。

    已知限制（Windows）：若目标文件正被**读取**句柄占用（如前端正在下载
    该 tif），os.replace 同样会 PermissionError（WinError 5）——
    它需要目标的删除权限，而被打开的文件拿不到。这不是本函数能规避的，
    只能退避重试；仍失败则抛出明确异常，由上层决定是否重试。
    失败时临时文件会被清理，不会留下垃圾。
    """
    directory = os.path.dirname(os.path.abspath(out_path)) or '.'
    os.makedirs(directory, exist_ok=True)

    base = os.path.basename(out_path)
    fd, tmp_path = tempfile.mkstemp(suffix='.tif', prefix=f'.tmp_{base}.',
                                    dir=directory)
    os.close(fd)
    try:
        with rasterio.open(tmp_path, 'w', **profile) as dst:
            dst.write(array, 1)

        last_err = None
        for attempt in range(retries):
            try:
                os.replace(tmp_path, out_path)
                return out_path
            except PermissionError as e:
                last_err = e
                # 目标被瞬时占用（前端下载中 / 杀毒软件扫描），退避后重试
                time.sleep(0.15 * (attempt + 1))
            except OSError as e:
                last_err = e
                time.sleep(0.15 * (attempt + 1))

        _log(logger, f'替换输出文件失败（已重试 {retries} 次）: {out_path} → {last_err}',
             'warning')
        # 最后一次不吞异常，让调用方知道这次输出没落盘
        os.replace(tmp_path, out_path)
        return out_path
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def _is_oom_error(e):
    """判断异常是否为内存不足类（torch CPU allocator / numpy 分配失败）。"""
    msg = str(e).lower()
    return ('not enough memory' in msg
            or 'out of memory' in msg
            or 'cannot allocate memory' in msg
            or 'unable to allocate' in msg
            or 'alloc_cpu' in msg
            or 'defaultcpuallocator' in msg)


def simulate_runoff(model, device, variables, day_files, all_dates, out_dates,
                    ref_path, output_dir, basin_geometry=None,
                    region_geometry=None,
                    batch_pixels=DEFAULT_BATCH_PIXELS,
                    state_budget_bytes=DEFAULT_STATE_BUDGET_BYTES,
                    logger=None, progress=None):
    """在线径流模拟主流程。

    参数
    ----
    model : RunoffLSTM，forward(x, state) -> (y, state)
    day_files : {date: {var: path}}，键覆盖 all_dates
    all_dates : 有序日期列表，warmup 段在前，输出段在后
    out_dates : 需要写盘的日期（all_dates 的子集）
    ref_path : 参考栅格，决定输出网格与 profile
    basin_geometry : GeoJSON 几何（与参考栅格同 CRS）；None 表示不按流域裁剪
    region_geometry : 研究区总边界（TP_China），所有模拟的默认上界；None 表示不裁剪
    progress : 可选回调 progress(done, total, message)

    返回
    ----
    dict：输出文件路径、有效像元数、耗时、是否启用跨日记忆及其原因
    """
    t_start = time.perf_counter()
    out_dates = set(out_dates)

    grid = build_pixel_index(ref_path, basin_geometry, region_geometry, logger)
    height, width = grid['height'], grid['width']
    win = grid['window']
    row_off, col_off = grid['row_off'], grid['col_off']
    win_h, win_w = grid['win_shape']

    # ---- 有效像元掩膜：流域几何 ∩ 跨日期采样的 nodata 并集 ----
    keep = build_valid_mask(grid, day_files, all_dates, variables, logger=logger)
    keep_flat = keep.ravel()
    idx = np.flatnonzero(keep_flat)
    P = int(idx.size)
    if P == 0:
        raise ValueError('计算区域内没有有效像元，请检查流域范围与驱动数据')

    rl = (idx // win_w).astype(np.intp)
    cl = (idx % win_w).astype(np.intp)

    # ---- 是否启用跨日记忆（受状态内存预算约束）----
    # 状态内存 = P × (h+c) × 层数 × hidden_size × 4 字节
    n_layers = model.lstm.num_layers
    hidden = model.lstm.hidden_size
    state_per_pixel = 2 * n_layers * hidden * 4

    need_state = len(all_dates) > 1
    state_bytes = P * state_per_pixel
    use_state = need_state and state_bytes <= state_budget_bytes
    fallback_reason = None
    if need_state and not use_state:
        fallback_reason = (
            f'有效像元 {P:,} 的 LSTM 状态需 {state_bytes/1024**3:.2f} GB，'
            f'超出预算 {state_budget_bytes/1024**3:.2f} GB，'
            f'本次退化为逐日独立推理（无跨日记忆）')
        _log(logger, fallback_reason, 'warning')

    _log(logger, f'计算范围 {grid["scope"]}：窗口 {win_h}×{win_w}，'
                 f'有效像元 {P:,}，跨日记忆 {"开" if use_state else "关"}，'
                 f'batch {batch_pixels}，总天数 {len(all_dates)}')

    # ---- 输出文件本身的 bbox/transform ----
    # 必须用计算窗口的 local_transform，**不能**继续写 1560×4082 全栅格。
    # 否则文件本身的地理范围仍是 68.02~104.69°E（整个青藏高原），
    # 而 TP_China 之外的部分只被填成 NaN，文件元数据层面没动过 —— 用户
    # 打开 gdalinfo / 在 GIS 软件里查看仍然看到全高原边界。
    local_transform = rasterio.windows.transform(win, grid['transform'])
    # 压缩格式必须限制在前端 webgis_frontend/js/geotiff.js 支持的范围内：
    #   1 无压缩 / 5 LZW / 8 Deflate / 32773 PackBits / 34887 LERC / 50001 ZSTD(带预测器)
    # GDAL 的 zstd 一律写 tag 50000，该库不认，故禁用。
    profile = grid['profile'].copy()
    profile.update(driver='GTiff', dtype='float32', count=1,
                   compress='lzw', nodata=np.nan,
                   height=win_h, width=win_w, transform=local_transform)

    os.makedirs(output_dir, exist_ok=True)

    def _run_days(batch_cur, state_cur):
        """跑完整日循环。OOM 时由外层降档重试，这里不做任何兜底。
        返回 (written, n_nodata_filled, state_used)。"""
        state_h = state_c = None
        last_good = None
        out_buffer = np.full((win_h, win_w), np.nan, dtype=np.float32)
        written = []
        n_nodata_filled = 0

        with torch.no_grad():
            for di, date in enumerate(all_dates):
                X, bad = _read_day_vars(day_files, date, variables, win, rl, cl,
                                        (height, width), logger)
                n_bad = int(bad.sum())

                # 缺失值前向填充：保持序列连续，否则 LSTM 会读到 NaN 并污染整条序列
                if last_good is None:
                    for vi in range(len(variables)):
                        b = bad[vi]
                        if not b.any():
                            continue
                        good = X[vi][~b]
                        X[vi][b] = np.float32(np.median(good)) if good.size else np.float32(0.0)
                else:
                    X = np.where(bad, last_good, X)
                last_good = X.copy()
                if n_bad:
                    n_nodata_filled += n_bad

                # (V, P) -> (P, 1, V)：像元是 batch，时间是长度 1 的序列
                Xt = np.ascontiguousarray(X.T)[:, None, :]

                if state_cur and state_h is None:
                    # 维度取自模型而非硬编码，换 hidden_size / 层数时不用改这里
                    n_layers = model.lstm.num_layers
                    hidden = model.lstm.hidden_size
                    state_h = torch.zeros(n_layers, P, hidden, dtype=torch.float32)
                    state_c = torch.zeros(n_layers, P, hidden, dtype=torch.float32)

                y_all = np.empty(P, dtype=np.float32)
                for s in range(0, P, batch_cur):
                    e = min(s + batch_cur, P)
                    xb = torch.from_numpy(Xt[s:e]).to(device)
                    if state_cur:
                        st = (state_h[:, s:e], state_c[:, s:e])
                        y, (h, c) = model(xb, st)
                        state_h[:, s:e] = h
                        state_c[:, s:e] = c
                    else:
                        y, _ = model(xb, None)
                    y_all[s:e] = y[:, -1].cpu().numpy()

                if date in out_dates:
                    # out_buffer 形状就是窗口（win_h, win_w），
                    # idx 是窗口内的有效像元扁平索引，直接写即可。
                    out_buffer[:] = np.nan
                    out_buffer.ravel()[idx] = y_all
                    out_path = os.path.join(output_dir, f'{date}_runoff.tif')
                    write_geotiff_atomic(out_path, out_buffer, profile,
                                         logger=logger)
                    written.append(out_path)

                if progress:
                    progress(di + 1, len(all_dates), date)

        return written, n_nodata_filled, state_h is not None

    # ---- OOM 自适应降档重试：batch 减半 → 关跨日记忆 → 明确报错 ----
    # 2026-09-25 实测：128 线程/63GB 服务器上 batch 131072 的 LSTM 单次前向
    # 要一次性分配 ~2.1GB 连续内存而 OOM。失败一般发生在开跑初期，重试成本低。
    attempt_batch, attempt_state = batch_pixels, use_state
    retry_note = None
    while True:
        try:
            written, n_nodata_filled, state_used = _run_days(
                attempt_batch, attempt_state)
            break
        except (RuntimeError, MemoryError) as e:
            if not _is_oom_error(e):
                raise
            # 强制回收上一轮尝试残留的引用（异常 traceback 会握住帧），
            # 提交额度紧张时不回收会让下一轮连 47MB 都申请不到
            import gc
            gc.collect()
            if attempt_batch > 4096:
                attempt_batch = max(attempt_batch // 2, 4096)
                retry_note = (f'推理中内存不足（{e}），batch 自动降为 '
                              f'{attempt_batch:,} 后重试')
                _log(logger, retry_note, 'warning')
                continue
            if attempt_state:
                attempt_state = False
                retry_note = ('推理中内存不足，已自动关闭跨日记忆'
                              '（逐日独立推理）并降小 batch 后重试')
                _log(logger, retry_note, 'warning')
                continue
            raise ValueError(
                '本机内存不足以完成该模拟：请缩小时间范围 / 只选单个流域，'
                '或显式传更小的 batch_size') from e

    elapsed = time.perf_counter() - t_start

    stats = {
        'files': written,
        'n_pixels': P,
        'n_days_total': len(all_dates),
        'n_days_output': len(written),
        'scope': grid['scope'],
        'window': [int(win.row_off), int(win.col_off), win_h, win_w],
        'cross_day_memory': bool(state_used),
        'fallback_reason': None if state_used else (fallback_reason or retry_note),
        'state_bytes': int(state_bytes) if state_used else 0,
        'batch_pixels': int(attempt_batch),
        'nodata_values_filled': n_nodata_filled,
        'elapsed_seconds': round(elapsed, 2),
    }

    # ---- 收尾汇总：多行日志块，长任务结束后一眼看清全部关键结果 ----
    per_day = elapsed / max(len(all_dates), 1)
    if written:
        d0 = os.path.basename(written[0])[:10]
        d1 = os.path.basename(written[-1])[:10]
        out_desc = f'{len(written)} 天（{d0} ~ {d1}）'
    else:
        out_desc = '0 天（未产出任何文件）'
    if state_used:
        mem_desc = f'开（LSTM 状态 {state_bytes/1024**3:.2f} GB）'
    else:
        reason = retry_note or fallback_reason or '单日任务无需跨日状态'
        mem_desc = f'关 —— {reason}'

    _log(logger, '=' * 20 + ' 径流模拟完成 ' + '=' * 20)
    _log(logger, f'  输出结果    : {out_desc}')
    _log(logger, f'  有效像元    : {P:,}')
    _log(logger, f'  跨日记忆    : {mem_desc}')
    _log(logger, f'  耗时        : {elapsed:.1f}s（平均 {per_day:.1f}s/天，'
                 f'batch {attempt_batch:,} 像元）')
    _log(logger, f'  缺失值填充  : {n_nodata_filled:,} 个像元值')
    _log(logger, f'  输出目录    : {output_dir}')
    return stats
