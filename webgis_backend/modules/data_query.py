import os
import sqlite3
from flask import Blueprint, request, jsonify, current_app, send_file
import logging
import rasterio
import numpy as np
from config import Config
from modules.utils import resolve_index_path

query_bp = Blueprint('query', __name__)
logger = logging.getLogger(__name__)


def get_db_path():
    """获取数据库路径"""
    # 兜底值取自 config.Config.DATABASE_PATH 的同一默认表达式，
    # 避免此处再硬编码一份 'data/processed/db/TibetanPlateau_index.db' 造成两处不同步。
    default_db_path = getattr(Config, 'DATABASE_PATH', None)
    db_path = current_app.config.get('DATABASE_PATH') or default_db_path
    if not db_path:
        logger.warning("⚠️ 未配置数据库路径")
        return None
    db_path = os.path.abspath(db_path)
    logger.info(f"📦 使用数据库路径: {db_path}")
    return db_path


def get_tif_root_dir():
    """获取TIF根目录"""
    tif_root_dir = current_app.config.get('TIF_ROOT_DIR')
    if not tif_root_dir or not os.path.exists(tif_root_dir):
        logger.error("❌ TIF根目录未配置或不存在")
        raise ValueError("TIF根目录未配置或不存在")
    return os.path.abspath(tif_root_dir)


@query_bp.route('/api/query/variables', methods=['GET'])
def get_variables():
    """获取所有变量"""
    try:
        variables = current_app.config['VARIABLES']  # 使用配置文件中的变量列表
        logger.info(f"✅ 查询变量成功，共 {len(variables)} 个")
        return jsonify({"status": "success", "variables": variables})
    except Exception as e:
        logger.exception("❌ 获取变量出错")
        return jsonify({"status": "error", "message": str(e)}), 500


@query_bp.route('/api/query/list', methods=['GET'])
def query_era5_data():
    """按变量和日期范围查询数据"""
    # 获取前端传递的参数
    variable = request.args.get('variable')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    calculate_mean = request.args.get('calculate_mean', 'false').lower() == 'true'

    # 参数验证
    if not variable or not start_date or not end_date:
        logger.warning("⚠️ 缺少参数 variable、start_date 或 end_date")
        return jsonify({"status": "error", "message": "缺少参数 variable、start_date 或 end_date"}), 400

    # 验证日期格式是否正确 (YYYY-MM-DD)
    from datetime import datetime
    try:
        datetime.strptime(start_date, '%Y-%m-%d')
        datetime.strptime(end_date, '%Y-%m-%d')
    except ValueError:
        logger.warning("⚠️ 日期格式错误: start_date=%s, end_date=%s", start_date, end_date)
        return jsonify({"status": "error", "message": "日期格式错误，正确格式应为YYYY-MM-DD"}), 400

    # 验证开始日期不能晚于结束日期
    if start_date > end_date:
        logger.warning("⚠️ 日期范围错误: start_date=%s > end_date=%s", start_date, end_date)
        return jsonify({"status": "error", "message": "开始日期不能晚于结束日期"}), 400

    try:
        # 查询数据库（try/finally 确保连接关闭）
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            sql = """
                  SELECT variable, date, file_path
                  FROM TibetanPlateau_index
                  WHERE variable=? AND date BETWEEN ? AND ?
                  ORDER BY date
                  """
            params = (variable, start_date, end_date)
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            logger.info(f"ℹ️ 未找到数据: variable={variable}, start_date={start_date}, end_date={end_date}")
            return jsonify({"status": "success", "data": [], "message": "No data found in the specified range"})

        results = []
        for row in rows:
            result = dict(row)
            # 将完整路径转换为相对路径
            if 'file_path' in result and result['file_path']:
                full_path = result['file_path']
                # 如果路径包含盘符，提取相对路径部分
                if ':' in full_path:
                    # 从TibetanPlateau开始提取相对路径
                    parts = full_path.replace('\\', '/').split('/')
                    try:
                        tibetan_idx = parts.index('TibetanPlateau')
                        relative_path = '/'.join(parts[tibetan_idx + 1:])
                        result['file_path'] = relative_path
                    except ValueError:
                        # 如果没找到TibetanPlateau，使用原路径的最后两部分
                        result['file_path'] = '/'.join(parts[-2:])
            results.append(result)

        # 如果需要计算均值，处理每个文件的均值
        if calculate_mean:
            tif_root_dir = get_tif_root_dir()
            for result in results:
                file_path = result.get('file_path')
                if not file_path:
                    continue

                # 构建完整文件路径
                full_path = resolve_index_path(tif_root_dir, file_path)
                full_path = full_path.replace('temperature_2mm', 'temperature_2m')
                full_path = os.path.abspath(full_path)

                try:
                    if not os.path.exists(full_path):
                        logger.warning(f"⚠️ 文件不存在: {full_path}")
                        result['mean_value'] = None
                        continue

                    # 读取GeoTIFF文件计算均值
                    with rasterio.open(full_path) as dataset:
                        data = dataset.read(1)
                        nodata = dataset.nodata if dataset.nodata is not None else np.nan
                        valid_data = data[(data != nodata) & (~np.isnan(data))]
                        result['mean_value'] = float(np.mean(valid_data)) if valid_data.size > 0 else None
                except Exception as e:
                    logger.error(f"❌ 计算均值失败 {full_path}: {str(e)}")
                    result['mean_value'] = None

        logger.info(f"✅ 查询成功，返回 {len(results)} 条数据")
        return jsonify({
            "status": "success",
            "data": results,
            "range": {
                "start_date": start_date,
                "end_date": end_date,
                "variable": variable
            }
        })
    except Exception as e:
        logger.exception("❌ 查询数据出错")
        return jsonify({"status": "error", "message": str(e)}), 500


@query_bp.route('/api/query/range', methods=['GET'])
def query_available_range():
    """返回某变量在索引库中的可用日期范围与文件数。

    供前端进入"温度/降水"查询子面板时调用一次，据此把日期输入框的
    min/max 设到数据真实覆盖范围，避免用户选到整段空白区间。

    复用 get_db_path() 读取的同一个 TibetanPlateau_index.db，故历史
    file_path 绝对路径/相对路径的解析逻辑（data_query.py:108-117）
    不受影响——这里只取 MIN/MAX/COUNT，不触碰 file_path。
    """
    variable = request.args.get('variable')
    if not variable:
        return jsonify({"status": "error", "message": "缺少参数 variable"}), 400
    try:
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT MIN(date) AS min_date, MAX(date) AS max_date, "
                "COUNT(*) AS cnt FROM TibetanPlateau_index WHERE variable = ?",
                (variable,)
            ).fetchone()
        finally:
            conn.close()
        if not row or not row['cnt']:
            return jsonify({"status": "success", "available": False,
                            "variable": variable})
        return jsonify({
            "status": "success",
            "available": True,
            "variable": variable,
            "min_date": row['min_date'],
            "max_date": row['max_date'],
            "count": row['cnt'],
        })
    except Exception as e:
        logger.exception("❌ /api/query/range 出错")
        return jsonify({"status": "error", "message": str(e)}), 500


@query_bp.route('/tif/<path:path>', methods=['GET'])
def serve_tif_file(path):
    """提供TIF文件访问"""
    try:
        tif_root_dir = get_tif_root_dir()

        # 处理传入的路径参数
        from urllib.parse import unquote
        decoded_path = unquote(path)
        normalized_path = decoded_path.replace('/', os.sep).replace('\\', os.sep)
        normalized_path = normalized_path.replace('temperature_2mm', 'temperature_2m')

        # 构建完整文件路径
        if any(normalized_path.startswith(disk + ':') for disk in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
            full_path = os.path.abspath(normalized_path)
        else:
            full_path = os.path.abspath(os.path.join(tif_root_dir, normalized_path))

        # 安全检查：确保文件路径在允许的目录内（realpath 归一化后校验，防 .. 穿越）
        try:
            real_root = os.path.realpath(tif_root_dir)
            real_path = os.path.realpath(full_path)
            within = os.path.commonpath([real_root, real_path]) == real_root
        except ValueError:
            within = False
        if not within:
            logger.warning(f'⚠️ 非法访问路径 (安全检查失败): {full_path}')
            return jsonify({"status": "error", "message": "禁止访问"}), 403

        # 检查文件是否存在
        if not os.path.exists(full_path):
            logger.error(f'❌ 文件不存在: {full_path}')
            return jsonify({"status": "error", "message": "文件不存在"}), 404

        # 检查文件扩展名
        if not full_path.lower().endswith('.tif'):
            logger.warning(f'⚠️ 不是TIF文件: {full_path}')
            return jsonify({"status": "error", "message": "只允许访问TIF文件"}), 403

        logger.info(f'✅ 文件访问成功: {full_path}')

        # 返回文件
        return send_file(
            full_path,
            mimetype='image/tiff',
            as_attachment=False,
            download_name=os.path.basename(full_path),
            max_age=0  # 禁用缓存
        )
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        logger.exception(f"❌ 服务TIF文件时出错: {str(e)}")
        return jsonify({"status": "error", "message": "服务器内部错误"}), 500