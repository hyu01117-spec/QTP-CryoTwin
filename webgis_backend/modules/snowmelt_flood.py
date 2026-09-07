#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
融雪洪水识别模块
处理融雪洪水识别的核心逻辑，包括数据预处理、模型调用和结果输出
识别逻辑：利用年最大值作为基准，保留大于等于年最大值的部分作为洪水区域
支持日期范围识别，返回所有有洪水的日期列表
"""

import os
import threading
import datetime
import numpy as np
import rasterio
from rasterio.mask import mask
from flask import Blueprint, request, jsonify, current_app, send_file
from modules.utils import resolve_within, load_json_cached, get_project_root

# 创建蓝图
snowmelt_flood_bp = Blueprint('snowmelt_flood', __name__, url_prefix='/api/snowmelt')

# 年最大图的进程内缓存：
# 原实现在「逐日」循环里每天都 rasterio.open 一次年最大图，
# 跑一年就是 365 次重复读取同一文件。这里缓存已解码的数组。
# 单个 GLDAS 网格（0.25°）约几十 MB，一年份也远小于 8 GB 可用内存，可安全缓存。
_YEARLY_MAX_CACHE = {}
_YEARLY_MAX_LOCK = threading.Lock()

def generate_yearly_max_file(year):
    """
    生成指定年份的年最大值图
    文件命名：GLDAS_snowmelt_max_YYYY.tif
    """
    input_dir = (current_app.config.get('GLDAS_SNOWMELT_DIR')
                    or os.path.join(current_app.config['BASE_DIR'], 'data', 'raw', 'GLDAS025_Snowmelt'))
    output_dir = (current_app.config.get('GLDAS_SNOWMELT_MAX_DIR')
                   or os.path.join(current_app.config['BASE_DIR'], 'data', 'processed', 'GLDAS_snowmelt_max'))
    
    os.makedirs(output_dir, exist_ok=True)
    
    output_file = os.path.join(output_dir, f"GLDAS_snowmelt_max_{year}.tif")
    
    # 如果文件已存在，直接返回
    if os.path.exists(output_file):
        current_app.logger.info(f"年份 {year} 的最大值文件已存在")
        return output_file
    
    # 获取该年份的所有文件
    year_files = []
    for filename in sorted(os.listdir(input_dir)):
        if filename.endswith('.tif'):
            date_str = filename.replace('.tif', '')
            if len(date_str) == 8 and date_str.isdigit():
                file_year = int(date_str[:4])
                if file_year == year:
                    year_files.append(os.path.join(input_dir, filename))
    
    if not year_files:
        current_app.logger.error(f"错误：没有找到年份 {year} 的数据文件")
        return None
    
    current_app.logger.info(f"正在生成年份 {year} 的最大值文件，共 {len(year_files)} 个文件")
    
    # 读取第一个文件获取元数据
    with rasterio.open(year_files[0], 'r') as src:
        meta = src.meta.copy()
        meta['driver'] = 'GTiff'
        meta['count'] = 1
        meta['dtype'] = 'float32'
        meta['nodata'] = np.nan
    
    # 计算年最大值：流式归约，不把全年 stack 进内存。
    # 原实现 np.stack(year_data) 会同时持有 365 层全栅格：
    # 若 GLDAS 是全球 0.25° 网格（1440×600），即 365×1440×600×4 B ≈ 1.26 GB，
    # 再叠加 np.nanmax 的中间量，内存峰值可达 2.5 GB 以上且随网格增大线性增长。
    # np.fmax 会逐元素取大并自动忽略 NaN，与 np.nanmax(axis=0) 结果一致。
    max_data = None
    n_used = 0
    for file_path in year_files:
        with rasterio.open(file_path, 'r') as src:
            data = src.read(1).astype(np.float32)
            # 处理填充值
            if src.nodata is not None:
                data[data == src.nodata] = np.nan
            elif np.min(data) == -9999:
                data[data == -9999] = np.nan
        if max_data is None:
            max_data = data
        else:
            np.fmax(max_data, data, out=max_data)   # 原地累积，避免再分配一层
        n_used += 1

    if max_data is not None:
        # 保存结果
        with rasterio.open(output_file, 'w', **meta) as dst:
            dst.write(max_data, 1)

        current_app.logger.info(
            f"年份 {year} 的最大值文件已保存: {output_file}（流式归约 {n_used} 层，"
            f"内存占用约 {max_data.nbytes / 1024 ** 2:.1f} MB）")
        return output_file

    return None


def get_yearly_max_array(year):
    """获取某年的年最大融雪量数组（进程内缓存，避免逐日重复读取）。

    原实现：identify_snowmelt_flood 的逐日循环里每天都调用
    generate_yearly_max_file（命中文件缓存）后再 rasterio.open 一次，
    处理 365 天就是 365 次同一文件的打开与解码。这里改为按 year 只解一次。
    """
    key = int(year)
    with _YEARLY_MAX_LOCK:
        cached = _YEARLY_MAX_CACHE.get(key)
    if cached is not None:
        return cached

    path = generate_yearly_max_file(key)
    if path is None:
        return None
    with rasterio.open(path, 'r') as src:
        arr = src.read(1).astype(np.float32)

    # 缓存上限保护：GLDAS 网格不大，但避免异常情况下无限增长
    with _YEARLY_MAX_LOCK:
        if len(_YEARLY_MAX_CACHE) > 8:
            _YEARLY_MAX_CACHE.clear()
        _YEARLY_MAX_CACHE[key] = arr
    return arr

def get_daily_snowmelt_data(date_str):
    """
    获取指定日期的融雪径流量数据
    """
    input_dir = (current_app.config.get('GLDAS_SNOWMELT_DIR')
                    or os.path.join(current_app.config['BASE_DIR'], 'data', 'raw', 'GLDAS025_Snowmelt'))
    
    # 构建文件名：YYYYMMDD.tif
    date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    filename = date_obj.strftime('%Y%m%d') + '.tif'
    file_path = os.path.join(input_dir, filename)
    
    if not os.path.exists(file_path):
        current_app.logger.debug(f"日期 {date_str} 的数据文件不存在: {file_path}")
        return None
    
    with rasterio.open(file_path, 'r') as src:
        data = src.read(1).astype(np.float32)
        meta = src.meta.copy()
        
        # 处理填充值
        if src.nodata is not None:
            data[data == src.nodata] = np.nan
        elif np.min(data) == -9999:
            data[data == -9999] = np.nan
    
    return {'data': data, 'meta': meta}

def identify_single_day_flood(target_date, yearly_max_data):
    """
    识别单日洪水
    返回：洪水识别结果（包含是否有洪水、结果文件路径、统计信息）

    注意：yearly_max_data 是已解码的年最大数组（由 get_yearly_max_array 提供），
    不要再在逐日循环里重复 rasterio.open。
    """
    output_dir = (current_app.config.get('GLDAS_SNOWMELT_MAX_DIR')
                   or os.path.join(current_app.config['BASE_DIR'], 'data', 'processed', 'GLDAS_snowmelt_max'))

    # 获取当日融雪径流量数据
    daily_data = get_daily_snowmelt_data(target_date)
    if daily_data is None:
        return None

    daily_values = daily_data['data']
    meta = daily_data['meta']
    
    # 保留大于等于年最大值的部分，其他为0
    flood_result = np.where(daily_values >= yearly_max_data, daily_values, 0).astype(np.float32)
    
    # 判断是否有洪水（存在非零值）
    has_flood = np.sum(flood_result > 0) > 0
    
    if has_flood:
        # 保存洪水识别结果
        output_file = os.path.join(output_dir, f"GLDAS_snowmelt_flood_{target_date}.tif")
        meta['driver'] = 'GTiff'
        meta['count'] = 1
        meta['dtype'] = 'float32'
        meta['nodata'] = 0
        
        with rasterio.open(output_file, 'w', **meta) as dst:
            dst.write(flood_result, 1)
        
        # 计算统计信息
        flood_mask = daily_values >= yearly_max_data
        flood_pixels = int(np.sum(flood_mask))
        total_pixels = int(np.sum(~np.isnan(daily_values)))
        flood_ratio = float(flood_pixels / total_pixels) if total_pixels > 0 else 0.0
        
        mean_value = float(np.nanmean(daily_values)) if not np.isnan(np.nanmean(daily_values)) else None
        max_value = float(np.nanmax(daily_values)) if not np.isnan(np.nanmax(daily_values)) else None
        
        # 构建URL
        filename = os.path.basename(output_file)
        result_url = f"/api/snowmelt/result/{filename}"
        
        return {
            'date': target_date,
            'has_flood': True,
            'result_file': result_url,
            'result_file_path': output_file,
            'flood_pixels': flood_pixels,
            'total_pixels': total_pixels,
            'flood_ratio': flood_ratio,
            'mean_value': mean_value,
            'max_value': max_value
        }
    
    return {
        'date': target_date,
        'has_flood': False,
        'result_file': None,
        'result_file_path': None,
        'flood_pixels': 0,
        'total_pixels': int(np.sum(~np.isnan(daily_values))),
        'flood_ratio': 0.0,
        'mean_value': None,
        'max_value': None
    }

@snowmelt_flood_bp.route('/identify', methods=['POST'])
def identify_snowmelt_flood():
    """
    融雪洪水识别接口
    输入：流域ID、模型选择、日期范围（start_date, end_date）
    输出：洪水识别结果（包含所有有洪水的日期列表和统计信息）
    """
    try:
        data = request.get_json()
        
        # 参数校验
        required_params = ['basin_id', 'model_name', 'start_date', 'end_date']
        if not all(param in data for param in required_params):
            return jsonify({'error': '参数不完整，必须包含basin_id、model_name、start_date和end_date'}), 400
        
        # 获取参数
        basin_id = data['basin_id']
        model_name = data['model_name']
        start_date_str = data['start_date']
        end_date_str = data['end_date']
        
        # 验证日期格式
        try:
            start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d')
            end_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': '日期格式错误，应为YYYY-MM-DD'}), 400
        
        if start_date > end_date:
            return jsonify({'error': '开始日期不能晚于结束日期'}), 400
        
        # 设置输出目录
        output_dir = (current_app.config.get('GLDAS_SNOWMELT_MAX_DIR')
                   or os.path.join(current_app.config['BASE_DIR'], 'data', 'processed', 'GLDAS_snowmelt_max'))
        os.makedirs(output_dir, exist_ok=True)
        
        # 遍历日期范围内的每一天进行识别
        current_date = start_date
        flood_dates = []
        total_days = 0
        valid_days = 0
        
        while current_date <= end_date:
            total_days += 1
            date_str = current_date.strftime('%Y-%m-%d')
            year = current_date.year
            
            # 获取该年份的年最大值图（进程内缓存，按年只解码一次）
            yearly_max = get_yearly_max_array(year)
            if yearly_max is None:
                current_date += datetime.timedelta(days=1)
                continue

            # 识别单日洪水
            result = identify_single_day_flood(date_str, yearly_max)
            if result is None:
                current_date += datetime.timedelta(days=1)
                continue
            
            valid_days += 1
            
            if result['has_flood']:
                # 按流域裁剪结果
                if basin_id and result.get('result_file_path'):
                    clipped_path = clip_to_basin(result['result_file_path'], basin_id)
                    if clipped_path != result['result_file_path']:
                        result['result_file_path'] = clipped_path
                        result['result_file'] = f"/api/snowmelt/result/{os.path.basename(clipped_path)}"
                flood_dates.append(result)
            
            current_date += datetime.timedelta(days=1)
        
        # 按日期排序
        flood_dates.sort(key=lambda x: x['date'])
        
        # 计算统计信息
        total_flood_days = len(flood_dates)
        flood_probability = float(total_flood_days / valid_days) if valid_days > 0 else 0.0
        
        # 计算平均洪水比例和平均均值
        avg_flood_ratio = 0.0
        avg_mean_value = 0.0
        if flood_dates:
            avg_flood_ratio = float(sum(f['flood_ratio'] for f in flood_dates) / len(flood_dates))
            valid_means = [f['mean_value'] for f in flood_dates if f['mean_value'] is not None]
            avg_mean_value = float(sum(valid_means) / len(valid_means)) if valid_means else None
        
        # 构建urls和dates_processed（与洪水淹没模块格式一致）
        urls = [f['result_file'] for f in flood_dates if f['result_file']]
        dates_processed = [f['date'] for f in flood_dates]
        
        # 返回结果（与洪水淹没模块格式一致，同时保留详细信息）
        return jsonify({
            'status': 'success',
            'message': f'融雪洪水识别完成，共识别 {valid_days} 天数据，发现 {total_flood_days} 天洪水',
            'basin_id': basin_id,
            'model_used': model_name,
            'start_date': start_date_str,
            'end_date': end_date_str,
            'total_days': total_days,
            'valid_days': valid_days,
            'flood_days': total_flood_days,
            'flood_probability': flood_probability,
            'avg_flood_ratio': avg_flood_ratio,
            'avg_mean_value': avg_mean_value,
            'flood_dates': flood_dates,
            'urls': urls,
            'dates_processed': dates_processed
        })
    
    except Exception as e:
        current_app.logger.error(f"融雪洪水识别失败: {str(e)}")
        return jsonify({'error': str(e)}), 500

# 获取融雪洪水结果文件接口
@snowmelt_flood_bp.route('/result/<path:filename>', methods=['GET'])
def get_snowmelt_result(filename):
    try:
        output_dir = (current_app.config.get('GLDAS_SNOWMELT_MAX_DIR')
                       or os.path.join(current_app.config['BASE_DIR'], 'data', 'processed', 'GLDAS_snowmelt_max'))
        
        file_path = resolve_within(output_dir, filename)
        if file_path is None or not os.path.isfile(file_path):
            return jsonify({'error': '文件不存在'}), 404

        # 返回文件内容
        return send_file(file_path, mimetype='image/tiff')
        
    except Exception as e:
        current_app.logger.error(f"获取融雪洪水结果文件失败: {str(e)}")
        return jsonify({'error': f'获取文件失败: {str(e)}'}), 500

def clip_to_basin(tif_path, basin_id):
    """将栅格数据裁剪到指定流域"""
    try:
        if not basin_id:
            return tif_path
        # 读取流域几何（进程内缓存：原实现逐日 json.load 一遍大 GeoJSON，
        # 处理 365 天就是 365 次完整解析）
        geojson_path = os.path.join(get_project_root(), 'data', 'raw', 'geo',
                                    'hydrology', 'TP_Basins_China.geojson')
        if not os.path.exists(geojson_path):
            return tif_path
        geojson_data = load_json_cached(geojson_path)
        features = geojson_data.get('features', [])
        basin_geom = None
        # 按索引匹配（与前端 value=索引 一致）
        if str(basin_id).isdigit():
            idx = int(basin_id)
            if 0 <= idx < len(features):
                basin_geom = features[idx].get('geometry')
        # 按名称匹配（动态探测字段）
        if not basin_geom and features:
            first_props = features[0].get('properties', {})
            candidates = ['NAME', 'BASIN_NAME', 'BAS_NM', 'name', 'BASIN', 'PN', 'BAS_NAME', 'BasinName']
            name_field = next((c for c in candidates if first_props.get(c)), None)
            for feat in features:
                props = feat.get('properties', {})
                if name_field and props.get(name_field) == basin_id:
                    basin_geom = feat.get('geometry')
                    break
        if not basin_geom:
            current_app.logger.warning(f"未找到流域: {basin_id}，跳过裁剪")
            return tif_path
        # rasterio mask 裁剪
        with rasterio.open(tif_path) as src:
            # CRS 对齐：流域(EPSG:4326) → tif CRS
            tif_crs = str(src.crs) if src.crs else ''
            if tif_crs and tif_crs not in ('EPSG:4326', 'EPSG:4269', ''):
                from rasterio.warp import transform_geom
                basin_geom = transform_geom('EPSG:4326', tif_crs, basin_geom)
            out_image, out_transform = mask(src, [basin_geom], crop=True, filled=True, nodata=0)
            out_meta = src.meta.copy()
            out_meta.update({
                'height': out_image.shape[1],
                'width': out_image.shape[2],
                'transform': out_transform,
                'nodata': 0
            })
            clipped_path = tif_path.replace('.tif', f'_basin{basin_id}.tif')
            with rasterio.open(clipped_path, 'w', **out_meta) as dst:
                dst.write(out_image)
        return clipped_path
    except Exception as e:
        current_app.logger.error(f"流域裁剪失败: {str(e)}")
        return tif_path
