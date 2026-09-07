from flask import Blueprint, jsonify, current_app, request, send_file
import os
import json
import logging
import time
from modules.utils import get_project_root, load_json_cached

# 设置日志
logger = logging.getLogger(__name__)

basin_bp = Blueprint('basin', __name__,
                     url_prefix='/api/basin')

def load_basin_data():
    """加载流域数据的公共函数"""
    try:
        project_root = get_project_root()
        geojson_path = os.path.join(project_root, 'data', 'raw',
                                    'geo',
                                    'hydrology', 'TP_Basins_China.geojson')
        
        logger.info(f"尝试加载流域数据文件: {geojson_path}")
        
        
        
        # 检查文件大小，确保文件不为空
        if os.path.getsize(geojson_path) == 0:
            logger.warning(f"流域数据文件为空: {geojson_path}")
            return None
        
        # 读取文件（带缓存）
        data = load_json_cached(geojson_path)
        
        # 验证数据格式
        if 'features' not in data:
            logger.warning(f"流域数据格式不正确，缺少features字段")
            return None
        
        logger.info(f"成功加载流域数据，共{len(data.get('features', []))}个要素")
        return data
    except json.JSONDecodeError as e:
        logger.error(f"解析流域数据JSON失败: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"加载流域数据时出错: {str(e)}")
        return None

@basin_bp.route('/data', methods=['GET'])
def get_basin_geojson():
    """
    获取所有流域数据
    增强版本，提供更详细的调试信息和错误处理
    """
    try:
        logger.info("获取所有流域数据请求")
        # 加载流域数据
        basin_data = load_basin_data()
        
        if basin_data is None:
            logger.error("无法加载流域数据")
            return jsonify({
                'error': '流域数据文件不存在或格式错误',
                'debug_info': {
                    'data_loaded': False,
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                }
            }), 404
        
        if 'features' not in basin_data:
            logger.error("流域数据缺少features字段")
            return jsonify({
                'error': '流域数据格式不正确',
                'debug_info': {
                    'data_loaded': True,
                    'has_features': False,
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                }
            }), 500
        
        feature_count = len(basin_data['features'])
        logger.info(f"成功返回 {feature_count} 个流域数据")
        
        # 返回所有流域数据，并添加调试信息
        return jsonify({
            **basin_data,
            'matched_count': feature_count,
            'searched_name': 'all',
            'debug_info': {
                'total_features': feature_count,
                'returned_all': True,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            }
        })
    except Exception as e:
        logger.error(f"获取所有流域数据时发生错误: {str(e)}")
        return jsonify({
            'error': f'服务器错误: {str(e)}',
            'debug_info': {
                'exception': str(e),
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            }
        }), 500

@basin_bp.route('/search', methods=['GET'])
def search_basin_by_name():
    """
    根据流域名称搜索特定流域
    增强版本，专门针对TibetanPlateau等特殊名称进行优化
    """
    try:
        # 获取搜索参数
        search_name = request.args.get('name', '').strip()
        if not search_name:
            logger.warning("缺少流域名称参数")
            return jsonify({'error': '缺少流域名称参数'}), 400
        
        logger.info(f"搜索流域: '{search_name}'")
        
        # 特殊处理TibetanPlateau搜索
        if 'tibetan' in search_name.lower() or 'plateau' in search_name.lower():
            logger.info("检测到西藏高原相关搜索，使用特殊处理")
            
            # 加载流域数据
            basin_data = load_basin_data()
            if basin_data and 'features' in basin_data:
                # 直接返回所有流域作为西藏高原数据
                logger.info(f"返回所有{len(basin_data['features'])}个流域作为西藏高原数据")
                return jsonify({
                    'type': 'FeatureCollection',
                    'features': basin_data['features'],
                    'matched_count': len(basin_data['features']),
                    'searched_name': search_name,
                    'debug_info': {
                        'total_features': len(basin_data['features']),
                        'matched_features': len(basin_data['features']),
                        'special_handling': 'TibetanPlateau',
                        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                })
        
        # 常规搜索逻辑
        # 加载流域数据
        data = load_basin_data()
        if data is None:
            return jsonify({'error': '流域数据文件不存在或格式错误'}), 404
        
        # 获取所有特征
        features = data.get('features', [])
        logger.info(f"总特征数: {len(features)}")
        
        # 添加详细的数据检查日志
        if features:
            # 检查前3个特征的属性结构
            for i, feature in enumerate(features[:3]):
                props = feature.get('properties', {})
                logger.info(f"特征 {i+1} 的属性键: {list(props.keys())}")
                # 记录所有可能的名称字段值
                for key, value in props.items():
                    if isinstance(value, str):
                        logger.info(f"特征 {i+1} 属性 '{key}': '{value}'")
        
        # 搜索匹配的流域
        filtered_features = []
        checked_properties = set()
        
        # 扩大搜索范围，但提高匹配精度
        search_terms = search_name.lower().split()
        
        for feature in features:
            # 获取属性
            properties = feature.get('properties', {})
            
            # 记录检查过的属性键，用于调试
            if properties:
                checked_properties.update(properties.keys())
            
            # 尝试在所有属性中查找匹配的字符串
            matched = False
            
            # 遍历所有属性
            for key, value in properties.items():
                value_str = str(value).lower()
                # 检查是否包含任何搜索词
                if any(term in value_str for term in search_terms):
                    logger.info(f"在特征中找到匹配: 属性'{key}' = '{value}', 匹配搜索词'{search_name}'")
                    filtered_features.append(feature)
                    matched = True
                    break
        
        # 记录检查过的所有属性键，用于调试
        if checked_properties:
            logger.info(f"检查过的属性键: {', '.join(sorted(checked_properties))}")
        
        # 如果没有找到匹配，不再默认返回所有特征，而是返回空结果
        # 这样可以让用户知道没有找到匹配项，而不是显示所有流域
        if len(filtered_features) == 0:
            logger.warning(f"没有找到匹配'{search_name}'的流域")
        else:
            logger.info(f"找到{len(filtered_features)}个匹配'{search_name}'的流域")
        
        # 返回搜索结果
        result = {
            'type': 'FeatureCollection',
            'features': filtered_features,
            'matched_count': len(filtered_features),
            'searched_name': search_name,
            'debug_info': {
                'total_features': len(features),
                'matched_features': len(filtered_features),
                'checked_property_keys': list(sorted(checked_properties)),
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            }
        }
        
        logger.info(f"搜索完成，匹配到{len(filtered_features)}个流域")
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"搜索流域时出错: {str(e)}")
        return jsonify({
            'error': f'搜索过程中出错: {str(e)}',
            'debug_info': {
                'exception': str(e),
                'search_name': request.args.get('name', ''),
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            }
        }), 500

@basin_bp.route('/river', methods=['GET'])
def get_river_geojson():
    # 回到后端上一级目录（即项目根目录）
    project_root = get_project_root()
    river_geojson_path = os.path.join(project_root, 'data',
                                      'raw', 'geo',
                                      'hydrology', 'TP_rivers.geojson')

    if not os.path.exists(river_geojson_path):
        return jsonify({'error': '河网数据文件不存在'}), 404

    data = load_json_cached(river_geojson_path)
    return jsonify(data)

@basin_bp.route('/permafrost', methods=['GET'])
def get_permafrost_geojson():
    try:
        project_root = get_project_root()
        # 定义单个冻土文件路径
        geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'cryosphere', 'TP_China_permafrost.geojson')
        
        # 检查文件是否存在
        if not os.path.exists(geojson_path):
            current_app.logger.error(f"冻土GeoJSON文件不存在: {geojson_path}")
            return jsonify({'error': '冻土数据文件不存在'}), 404
        
        # 读取文件内容并返回JSON对象
        data = load_json_cached(geojson_path)
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取冻土数据时出错: {str(e)}")
        return jsonify({'error': '获取冻土数据失败'}), 500

@basin_bp.route('/glaciers', methods=['GET'])
def get_glaciers_geojson():
    try:
        # 构建冰川GeoJSON文件路径
        project_root = get_project_root()
        geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'cryosphere', 'TP_China_glaciers.geojson')
        if not os.path.exists(geojson_path):
            current_app.logger.error(f"冰川GeoJSON文件不存在: {geojson_path}")
            return jsonify({'error': '冰川数据文件不存在'}), 404

        # 返回GeoJSON文件
        return send_file(geojson_path, mimetype='application/geo+json')
    except Exception as e:
        current_app.logger.error(f"获取冰川数据时出错: {str(e)}")
        return jsonify({'error': '获取冰川数据失败'}), 500

@basin_bp.route('/cma', methods=['GET'])
def get_cma_geojson():
    # 回到后端上一级目录（即项目根目录）
    project_root = get_project_root()
    cma_geojson_path = os.path.join(project_root, 'data',
                                      'raw', 'geo',
                                      'station', 'TP_Stations.geojson')

    if not os.path.exists(cma_geojson_path):
        return jsonify({'error': '水文站数据文件不存在'}), 404

    data = load_json_cached(cma_geojson_path)
    return jsonify(data)

@basin_bp.route('/weather_station', methods=['GET'])
def get_weather_station_geojson():
    """获取气象站点数据"""
    try:
        # 构建气象站GeoJSON文件路径
        project_root = get_project_root()
        weather_station_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'station', 'weather_station.geojson')
        
        if not os.path.exists(weather_station_geojson_path):
            current_app.logger.error(f"气象站GeoJSON文件不存在: {weather_station_geojson_path}")
            return jsonify({'error': '气象站数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(weather_station_geojson_path)
        
        current_app.logger.info(f"成功加载气象站数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取气象站数据时出错: {str(e)}")
        return jsonify({'error': '获取气象站数据失败'}), 500

@basin_bp.route('/railway', methods=['GET'])
def get_railway_geojson():
    """获取主要铁路数据"""
    try:
        # 构建铁路GeoJSON文件路径
        project_root = get_project_root()
        railway_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'infrastructure', 'rai_4m.geojson')
        
        if not os.path.exists(railway_geojson_path):
            current_app.logger.error(f"铁路GeoJSON文件不存在: {railway_geojson_path}")
            return jsonify({'error': '铁路数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(railway_geojson_path)
        
        current_app.logger.info(f"成功加载铁路数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取铁路数据时出错: {str(e)}")
        return jsonify({'error': '获取铁路数据失败'}), 500

@basin_bp.route('/road', methods=['GET'])
def get_road_geojson():
    """获取主要公路数据"""
    try:
        # 构建公路GeoJSON文件路径
        project_root = get_project_root()
        road_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'infrastructure', 'roa_4m.geojson')
        
        if not os.path.exists(road_geojson_path):
            current_app.logger.error(f"公路GeoJSON文件不存在: {road_geojson_path}")
            return jsonify({'error': '公路数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(road_geojson_path)
        
        current_app.logger.info(f"成功加载公路数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取公路数据时出错: {str(e)}")
        return jsonify({'error': '获取公路数据失败'}), 500

@basin_bp.route('/settlement', methods=['GET'])
def get_settlement_geojson():
    """获取县级居民地数据"""
    try:
        project_root = get_project_root()
        settlement_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'settlement', 'XianCh_point.geojson')
        
        if not os.path.exists(settlement_geojson_path):
            current_app.logger.error(f"县级居民地数据文件不存在: {settlement_geojson_path}")
            return jsonify({'error': '县级居民地数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(settlement_geojson_path)
        
        current_app.logger.info(f"成功加载县级居民地数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取县级居民地数据时出错: {str(e)}")
        return jsonify({'error': '获取县级居民地数据失败'}), 500

@basin_bp.route('/city_settlement', methods=['GET'])
def get_city_settlement_geojson():
    """获取地市级居民地数据"""
    try:
        project_root = get_project_root()
        city_settlement_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'settlement', 'res2_4m.geojson')
        
        if not os.path.exists(city_settlement_geojson_path):
            current_app.logger.error(f"地市级居民地数据文件不存在: {city_settlement_geojson_path}")
            return jsonify({'error': '地市级居民地数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(city_settlement_geojson_path)
        
        current_app.logger.info(f"成功加载地市级居民地数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取地市级居民地数据时出错: {str(e)}")
        return jsonify({'error': '获取地市级居民地数据失败'}), 500

@basin_bp.route('/capital_settlement', methods=['GET'])
def get_capital_settlement_geojson():
    """获取首都和省级行政中心数据"""
    try:
        project_root = get_project_root()
        capital_settlement_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'settlement', 'res1_4m.geojson')
        
        if not os.path.exists(capital_settlement_geojson_path):
            current_app.logger.error(f"首都和省级行政中心数据文件不存在: {capital_settlement_geojson_path}")
            return jsonify({'error': '首都和省级行政中心数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(capital_settlement_geojson_path)
        
        current_app.logger.info(f"成功加载首都和省级行政中心数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取首都和省级行政中心数据时出错: {str(e)}")
        return jsonify({'error': '获取首都和省级行政中心数据失败'}), 500

@basin_bp.route('/boundary_country', methods=['GET'])
def get_boundary_country_geojson():
    """获取国界数据"""
    try:
        project_root = get_project_root()
        boundary_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'boundary', 'bou1_4m.geojson')
        
        if not os.path.exists(boundary_geojson_path):
            current_app.logger.error(f"国界数据文件不存在: {boundary_geojson_path}")
            return jsonify({'error': '国界数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(boundary_geojson_path)
        
        current_app.logger.info(f"成功加载国界数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取国界数据时出错: {str(e)}")
        return jsonify({'error': '获取国界数据失败'}), 500

@basin_bp.route('/boundary_province', methods=['GET'])
def get_boundary_province_geojson():
    """获取国界与省界数据"""
    try:
        project_root = get_project_root()
        boundary_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'boundary', 'bou2_4m.geojson')
        
        if not os.path.exists(boundary_geojson_path):
            current_app.logger.error(f"国界与省界数据文件不存在: {boundary_geojson_path}")
            return jsonify({'error': '国界与省界数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(boundary_geojson_path)
        
        current_app.logger.info(f"成功加载国界与省界数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取国界与省界数据时出错: {str(e)}")
        return jsonify({'error': '获取国界与省界数据失败'}), 500

@basin_bp.route('/boundary_city', methods=['GET'])
def get_boundary_city_geojson():
    """获取地级行政界线数据"""
    try:
        project_root = get_project_root()
        boundary_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'boundary', 'bou3_4m.geojson')
        
        if not os.path.exists(boundary_geojson_path):
            current_app.logger.error(f"地级行政界线数据文件不存在: {boundary_geojson_path}")
            return jsonify({'error': '地级行政界线数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(boundary_geojson_path)
        
        current_app.logger.info(f"成功加载地级行政界线数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取地级行政界线数据时出错: {str(e)}")
        return jsonify({'error': '获取地级行政界线数据失败'}), 500

@basin_bp.route('/boundary_county', methods=['GET'])
def get_boundary_county_geojson():
    """获取县级行政界线数据"""
    try:
        project_root = get_project_root()
        boundary_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'boundary', 'bou4_4m.geojson')
        
        if not os.path.exists(boundary_geojson_path):
            current_app.logger.error(f"县级行政界线数据文件不存在: {boundary_geojson_path}")
            return jsonify({'error': '县级行政界线数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(boundary_geojson_path)
        
        current_app.logger.info(f"成功加载县级行政界线数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取县级行政界线数据时出错: {str(e)}")
        return jsonify({'error': '获取县级行政界线数据失败'}), 500

@basin_bp.route('/lake', methods=['GET'])
def get_lake_geojson():
    """获取湖泊数据"""
    try:
        project_root = get_project_root()
        lake_geojson_path = os.path.join(project_root, 'data', 'raw', 'geo', 'hydrology', 'TP_China_Lake.geojson')
        
        if not os.path.exists(lake_geojson_path):
            current_app.logger.error(f"湖泊数据文件不存在: {lake_geojson_path}")
            return jsonify({'error': '湖泊数据文件不存在'}), 404

        # 读取文件内容并返回JSON对象
        data = load_json_cached(lake_geojson_path)
        
        current_app.logger.info(f"成功加载湖泊数据，共{len(data.get('features', []))}个要素")
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"获取湖泊数据时出错: {str(e)}")
        return jsonify({'error': '获取湖泊数据失败'}), 500
