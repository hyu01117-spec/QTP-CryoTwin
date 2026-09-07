from flask import Blueprint, jsonify
import os
import logging
import pandas as pd
from modules.utils import get_project_root, load_json_cached

# 设置日志
logger = logging.getLogger(__name__)

economy_bp = Blueprint('economy', __name__, url_prefix='/api/economy')
# 数据缓存：经济数据文件只在进程内解析一次
_ECON_CACHE = {}

def _cached(key, fn):
    if key not in _ECON_CACHE:
        _ECON_CACHE[key] = fn()
    return _ECON_CACHE[key]

def convert_gdp_value(value):
    """转换GDP值，处理'亿元'等单位"""
    if pd.isna(value):
        return None
    
    # 转换为字符串处理
    value_str = str(value).strip()
    
    # 检查是否包含'亿元'
    if '亿元' in value_str:
        # 提取数字部分
        num_str = value_str.replace('亿元', '').strip()
        try:
            # 转换为万元（1亿元 = 10000万元）
            return float(num_str) * 10000
        except ValueError:
            return None
    
    # 检查是否包含'万元'
    elif '万元' in value_str:
        # 提取数字部分
        num_str = value_str.replace('万元', '').strip()
        try:
            return float(num_str)
        except ValueError:
            return None
    
    # 直接数值
    try:
        return float(value_str)
    except ValueError:
        return None

def load_gdp_data():
    """加载GDP数据"""
    try:
        project_root = get_project_root()
        
        # 首先尝试加载新的经济数据GEO文件
        geo_file_path = os.path.join(project_root, 'data', 'raw', 'geo', 'socioeconomic', 'economy_counties.geojson')
        
        if os.path.exists(geo_file_path):
            logger.info(f"找到经济数据GEO文件: {geo_file_path}")
            return load_gdp_data_from_geojson(geo_file_path)
        
        # 如果GEO文件不存在，尝试从CSV文件加载
        possible_paths = [
            os.path.join(project_root, 'data', 'raw', 'economy', 'GDP_data.csv'),
            os.path.join(project_root, 'data', 'raw', 'GDP_data.csv'),
            os.path.join(project_root, 'data', 'GDP_data.csv'),
            os.path.join(project_root, 'GDP_data.csv')
        ]
        
        gdp_file_path = None
        for path in possible_paths:
            if os.path.exists(path):
                gdp_file_path = path
                logger.info(f"找到GDP数据文件: {path}")
                break
        
        if not gdp_file_path:
            logger.warning("未找到GDP数据文件，使用示例数据")
            return generate_sample_gdp_data()
        
        # 读取CSV文件
        df = pd.read_csv(gdp_file_path)
        
        # 处理数据：假设CSV格式为县区名和多个年份的GDP列
        # 例如：county, 2000, 2001, 2002, ..., 2023
        
        # 获取所有年份列（排除第一列县区名）
        year_columns = [col for col in df.columns if col != 'county' and col.isdigit()]
        
        if not year_columns:
            logger.warning("GDP数据文件中未找到年份列，使用示例数据")
            return generate_sample_gdp_data()
        
        # 处理数据
        gdp_data = []
        for _, row in df.iterrows():
            county_name = row['county']
            
            # 获取所有年份的GDP值
            all_years = {}
            for year in year_columns:
                gdp_value = row[year]
                converted_value = convert_gdp_value(gdp_value)
                if converted_value is not None:
                    all_years[int(year)] = converted_value
            
            if all_years:
                # 计算最新年份和GDP
                latest_year = max(all_years.keys())
                latest_gdp = all_years[latest_year]
                
                # 计算年均增长率（使用最早和最新年份）
                earliest_year = min(all_years.keys())
                earliest_gdp = all_years[earliest_year]
                
                if earliest_year != latest_year and earliest_gdp > 0:
                    years_diff = latest_year - earliest_year
                    growth_rate = ((latest_gdp / earliest_gdp) ** (1/years_diff) - 1) * 100
                else:
                    growth_rate = 0
                
                gdp_data.append({
                    'county': county_name,
                    'latest_gdp': latest_gdp,
                    'latest_year': latest_year,
                    'growth_rate': round(growth_rate, 2),
                    'all_years': all_years
                })
        
        logger.info(f"成功加载 {len(gdp_data)} 个县区的GDP数据")
        return gdp_data
        
    except Exception as e:
        logger.error(f"加载GDP数据时出错: {str(e)}")
        return generate_sample_gdp_data()

def load_gdp_data_from_geojson(geo_file_path):
    """从GEO文件加载GDP数据"""
    try:
        import json
        
        geo_data = load_json_cached(geo_file_path)
        
        gdp_data = []
        for feature in geo_data.get('features', []):
            properties = feature.get('properties', {})
            # 县名优先取中文解码名，其次拼音/原名
            county_name = (properties.get('NAME_DECODED')
                           or properties.get('NAME')
                           or properties.get('PYNAME')
                           or properties.get('county', ''))
            
            if county_name and 'latest_gdp' in properties:
                gdp_data.append({
                    'county': county_name,
                    'latest_gdp': properties.get('latest_gdp', 0),
                    'latest_year': properties.get('latest_year', 2015),
                    'growth_rate': properties.get('growth_rate', 0),
                    'all_years': properties.get('all_years', {})
                })
        
        logger.info(f"从GEO文件成功加载 {len(gdp_data)} 个县区的GDP数据")
        return gdp_data
        
    except Exception as e:
        logger.error(f"从GEO文件加载GDP数据时出错: {str(e)}")
        return generate_sample_gdp_data()

def load_powerplant_data():
    """加载能源站点数据"""
    try:
        project_root = get_project_root()
        
        # 加载能源站点GEO文件
        geo_file_path = os.path.join(project_root, 'data', 'raw', 'geo', 'infrastructure', 'powerplant.geojson')
        
        if not os.path.exists(geo_file_path):
            logger.warning(f"未找到能源站点数据文件: {geo_file_path}")
            return generate_sample_powerplant_data()
        
        logger.info(f"找到能源站点数据文件: {geo_file_path}")
        
        geo_data = load_json_cached(geo_file_path)
        
        powerplant_data = []
        for feature in geo_data.get('features', []):
            properties = feature.get('properties', {})
            geometry = feature.get('geometry', {})
            
            powerplant_item = {
                'id': properties.get('id', ''),
                'name': properties.get('name', ''),
                'type': properties.get('primary_fu', ''),
                'commission': properties.get('commission', ''),
                'capacity': properties.get('capacity_m', 0),
                'geometry': geometry
            }
            
            powerplant_data.append(powerplant_item)
        
        logger.info(f"成功加载 {len(powerplant_data)} 个能源站点数据")
        return powerplant_data
        
    except Exception as e:
        logger.error(f"加载能源站点数据时出错: {str(e)}")
        return generate_sample_powerplant_data()

def generate_sample_powerplant_data():
    """生成示例能源站点数据"""
    sample_data = [
        {
            'id': 'PP001',
            'name': '金沙江水电站',
            'type': '水力发电',
            'capacity': 16000,
            'status': '运营中',
            'year_built': '2020',
            'location': '云南省丽江市',
            'province': '云南省',
            'city': '丽江市',
            'geometry': {'type': 'Point', 'coordinates': [100.2345, 27.8901]}
        },
        {
            'id': 'PP002',
            'name': '大理风力发电场',
            'type': '风力发电',
            'capacity': 2000,
            'status': '运营中',
            'year_built': '2018',
            'location': '云南省大理州',
            'province': '云南省',
            'city': '大理白族自治州',
            'geometry': {'type': 'Point', 'coordinates': [100.1234, 25.6789]}
        },
        {
            'id': 'PP003',
            'name': '昆明太阳能电站',
            'type': '太阳能发电',
            'capacity': 1000,
            'status': '运营中',
            'year_built': '2022',
            'location': '云南省昆明市',
            'province': '云南省',
            'city': '昆明市',
            'geometry': {'type': 'Point', 'coordinates': [102.7123, 25.0456]}
        },
        {
            'id': 'PP004',
            'name': '曲靖火电厂',
            'type': '火力发电',
            'capacity': 8000,
            'status': '运营中',
            'year_built': '2015',
            'location': '云南省曲靖市',
            'province': '云南省',
            'city': '曲靖市',
            'geometry': {'type': 'Point', 'coordinates': [103.7890, 25.4567]}
        }
    ]
    
    logger.info("使用示例能源站点数据")
    return sample_data

def load_dam_data():
    """加载水坝数据"""
    try:
        project_root = get_project_root()
        
        # 加载水坝GEO文件
        geo_file_path = os.path.join(project_root, 'data', 'raw', 'geo', 'infrastructure', 'GDW_barriers.geojson')
        
        if not os.path.exists(geo_file_path):
            logger.warning(f"未找到水坝数据文件: {geo_file_path}")
            return generate_sample_dam_data()
        
        logger.info(f"找到水坝数据文件: {geo_file_path}")
        
        geo_data = load_json_cached(geo_file_path)
        
        dam_data = []
        for feature in geo_data.get('features', []):
            properties = feature.get('properties', {})
            geometry = feature.get('geometry', {})
            
            dam_item = {
                'id': properties.get('GDW_ID', ''),
                'name': properties.get('DAM_NAME', ''),
                'river': properties.get('RIVER', ''),
                'country': properties.get('COUNTRY', ''),
                'admin_unit': properties.get('ADMIN_UNIT', ''),
                'year_built': properties.get('YEAR_DAM', ''),
                'year_txt': properties.get('YEAR_TXT', ''),
                'dam_height': properties.get('DAM_HGT_M', 0),
                'dam_length': properties.get('DAM_LEN_M', 0),
                'capacity': properties.get('CAP_MCM', 0),
                'power': properties.get('POWER_MW', 0),
                'main_use': properties.get('MAIN_USE', ''),
                'geometry': geometry
            }
            
            dam_data.append(dam_item)
        
        logger.info(f"成功加载 {len(dam_data)} 个水坝数据")
        return dam_data
        
    except Exception as e:
        logger.error(f"加载水坝数据时出错: {str(e)}")
        return generate_sample_dam_data()

def generate_sample_dam_data():
    """生成示例水坝数据"""
    sample_data = [
        {
            'id': 1,
            'name': '三峡大坝',
            'river': '长江',
            'country': 'China',
            'admin_unit': '湖北省',
            'year_built': 2003,
            'year_txt': 'Built 2003',
            'dam_height': 185,
            'dam_length': 2335,
            'capacity': 39300,
            'power': 22500,
            'main_use': 'Hydroelectricity',
            'geometry': {'type': 'Point', 'coordinates': [111.0000, 30.7617]}
        },
        {
            'id': 2,
            'name': '小浪底大坝',
            'river': '黄河',
            'country': 'China',
            'admin_unit': '河南省',
            'year_built': 2001,
            'year_txt': 'Built 2001',
            'dam_height': 154,
            'dam_length': 1667,
            'capacity': 12650,
            'power': 1800,
            'main_use': 'Flood Control',
            'geometry': {'type': 'Point', 'coordinates': [112.4900, 34.9150]}
        }
    ]
    
    logger.info("使用示例水坝数据")
    return sample_data

def load_reservoir_data():
    """加载水库数据"""
    try:
        project_root = get_project_root()
        
        # 加载水库GEO文件
        geo_file_path = os.path.join(project_root, 'data', 'raw', 'geo', 'infrastructure', 'GDW_reservoirs.geojson')
        
        if not os.path.exists(geo_file_path):
            logger.warning(f"未找到水库数据文件: {geo_file_path}")
            return generate_sample_reservoir_data()
        
        logger.info(f"找到水库数据文件: {geo_file_path}")
        
        geo_data = load_json_cached(geo_file_path)
        
        reservoir_data = []
        for feature in geo_data.get('features', []):
            properties = feature.get('properties', {})
            geometry = feature.get('geometry', {})
            
            reservoir_item = {
                'id': properties.get('GDW_ID', ''),
                'name': properties.get('DAM_NAME', ''),
                'river': properties.get('RIVER', ''),
                'country': properties.get('COUNTRY', ''),
                'admin_unit': properties.get('ADMIN_UNIT', ''),
                'year_built': properties.get('YEAR_DAM', ''),
                'year_txt': properties.get('YEAR_TXT', ''),
                'area': properties.get('AREA_SKM', 0),
                'capacity': properties.get('CAP_MCM', 0),
                'depth': properties.get('DEPTH_M', 0),
                'power': properties.get('POWER_MW', 0),
                'main_use': properties.get('MAIN_USE', ''),
                'geometry': geometry
            }
            
            reservoir_data.append(reservoir_item)
        
        logger.info(f"成功加载 {len(reservoir_data)} 个水库数据")
        return reservoir_data
        
    except Exception as e:
        logger.error(f"加载水库数据时出错: {str(e)}")
        return generate_sample_reservoir_data()

def generate_sample_reservoir_data():
    """生成示例水库数据"""
    sample_data = [
        {
            'id': 1,
            'name': '千岛湖水库',
            'river': '新安江',
            'country': 'China',
            'admin_unit': '浙江省',
            'year_built': 1960,
            'year_txt': 'Built 1960',
            'area': 580,
            'capacity': 17800,
            'depth': 90,
            'power': 845,
            'main_use': 'Water Supply',
            'geometry': {'type': 'Polygon', 'coordinates': [[[119.0000, 29.5000], [119.1000, 29.5000], [119.1000, 29.6000], [119.0000, 29.6000], [119.0000, 29.5000]]]}
        },
        {
            'id': 2,
            'name': '丹江口水库',
            'river': '汉江',
            'country': 'China',
            'admin_unit': '湖北省',
            'year_built': 1973,
            'year_txt': 'Built 1973',
            'area': 1022,
            'capacity': 29050,
            'depth': 80,
            'power': 900,
            'main_use': 'Water Supply',
            'geometry': {'type': 'Polygon', 'coordinates': [[[111.5000, 32.5000], [111.6000, 32.5000], [111.6000, 32.6000], [111.5000, 32.6000], [111.5000, 32.5000]]]}
        }
    ]
    
    logger.info("使用示例水库数据")
    return sample_data

def generate_sample_gdp_data():
    """生成示例GDP数据"""
    sample_data = [
        {
            'county': '丽江纳西族自治县',
            'latest_gdp': 2456789.0,
            'latest_year': 2023,
            'growth_rate': 8.5,
            'all_years': {2020: 2000000.0, 2021: 2150000.0, 2022: 2300000.0, 2023: 2456789.0}
        },
        {
            'county': '兰坪白族普米族自治县',
            'latest_gdp': 1234567.0,
            'latest_year': 2023,
            'growth_rate': 7.2,
            'all_years': {2020: 1000000.0, 2021: 1080000.0, 2022: 1150000.0, 2023: 1234567.0}
        },
        {
            'county': '香格里拉市',
            'latest_gdp': 3456789.0,
            'latest_year': 2023,
            'growth_rate': 9.1,
            'all_years': {2020: 2800000.0, 2021: 3000000.0, 2022: 3200000.0, 2023: 3456789.0}
        },
        {
            'county': '德钦县',
            'latest_gdp': 987654.0,
            'latest_year': 2023,
            'growth_rate': 6.8,
            'all_years': {2020: 800000.0, 2021: 850000.0, 2022: 920000.0, 2023: 987654.0}
        },
        {
            'county': '维西傈僳族自治县',
            'latest_gdp': 876543.0,
            'latest_year': 2023,
            'growth_rate': 6.5,
            'all_years': {2020: 750000.0, 2021: 790000.0, 2022: 830000.0, 2023: 876543.0}
        }
    ]
    
    logger.info("使用示例GDP数据")
    return sample_data

@economy_bp.route('/gdp_data', methods=['GET'])
def get_gdp_data():
    """获取GDP数据"""
    try:
        logger.info("获取GDP数据请求")
        
        # 加载GDP数据
        gdp_data = _cached('gdp', load_gdp_data)
        
        if not gdp_data:
            logger.error("无法加载GDP数据")
            return jsonify({
                'error': 'GDP数据文件不存在或格式错误',
                'data': []
            }), 404
        
        logger.info(f"成功返回 {len(gdp_data)} 个县区的GDP数据")
        
        return jsonify(gdp_data)
        
    except Exception as e:
        logger.error(f"获取GDP数据时发生错误: {str(e)}")
        return jsonify({
            'error': f'服务器错误: {str(e)}',
            'data': []
        }), 500

@economy_bp.route('/gdp_data/<county_name>', methods=['GET'])
def get_gdp_data_by_county(county_name):
    """根据县区名称获取GDP数据"""
    try:
        logger.info(f"获取县区GDP数据请求: {county_name}")
        
        # 加载GDP数据
        gdp_data = _cached('gdp', load_gdp_data)
        
        if not gdp_data:
            return jsonify({'error': 'GDP数据文件不存在或格式错误'}), 404
        
        # 搜索匹配的县区
        matched_data = []
        for item in gdp_data:
            if county_name.lower() in item['county'].lower():
                matched_data.append(item)
        
        if not matched_data:
            logger.warning(f"未找到县区 '{county_name}' 的GDP数据")
            return jsonify({
                'error': f'未找到县区 \"{county_name}\" 的GDP数据',
                'matched_count': 0,
                'data': []
            }), 404
        
        logger.info(f"成功返回 {len(matched_data)} 个匹配的GDP数据")
        
        return jsonify({
            'matched_count': len(matched_data),
            'searched_name': county_name,
            'data': matched_data
        })
        
    except Exception as e:
        logger.error(f"获取县区GDP数据时发生错误: {str(e)}")
        return jsonify({
            'error': f'服务器错误: {str(e)}',
            'data': []
        }), 500

@economy_bp.route('/powerplant_data', methods=['GET'])
def get_powerplant_data():
    """获取能源站点数据"""
    try:
        logger.info("获取能源站点数据请求")
        
        # 加载能源站点数据
        powerplant_data = _cached('powerplant', load_powerplant_data)
        
        if not powerplant_data:
            logger.error("无法加载能源站点数据")
            return jsonify({
                'error': '能源站点数据文件不存在或格式错误',
                'data': []
            }), 404
        
        logger.info(f"成功返回 {len(powerplant_data)} 个能源站点数据")
        
        return jsonify(powerplant_data)
        
    except Exception as e:
        logger.error(f"获取能源站点数据时发生错误: {str(e)}")
        return jsonify({
            'error': f'服务器错误: {str(e)}',
            'data': []
        }), 500

@economy_bp.route('/powerplant_data/<province_name>', methods=['GET'])
def get_powerplant_data_by_province(province_name):
    """根据省份名称获取能源站点数据"""
    try:
        logger.info(f"获取省份能源站点数据请求: {province_name}")
        
        # 加载能源站点数据
        powerplant_data = _cached('powerplant', load_powerplant_data)
        
        if not powerplant_data:
            return jsonify({'error': '能源站点数据文件不存在或格式错误'}), 404
        
        # 搜索匹配的省份
        matched_data = []
        for item in powerplant_data:
            if province_name.lower() in item['province'].lower():
                matched_data.append(item)
        
        if not matched_data:
            logger.warning(f"未找到省份 '{province_name}' 的能源站点数据")
            return jsonify({
                'error': f'未找到省份 \"{province_name}\" 的能源站点数据',
                'matched_count': 0,
                'data': []
            }), 404
        
        logger.info(f"成功返回 {len(matched_data)} 个匹配的能源站点数据")
        
        return jsonify({
            'matched_count': len(matched_data),
            'searched_name': province_name,
            'data': matched_data
        })
        
    except Exception as e:
        logger.error(f"获取省份能源站点数据时发生错误: {str(e)}")
        return jsonify({
            'error': f'服务器错误: {str(e)}',
            'data': []
        }), 500

@economy_bp.route('/dam_data', methods=['GET'])
def get_dam_data():
    """获取水坝数据"""
    try:
        logger.info("获取水坝数据请求")
        
        # 加载水坝数据
        dam_data = _cached('dam', load_dam_data)
        
        if not dam_data:
            logger.error("无法加载水坝数据")
            return jsonify({
                'error': '水坝数据文件不存在或格式错误',
                'data': []
            }), 404
        
        logger.info(f"成功返回 {len(dam_data)} 个水坝数据")
        
        return jsonify(dam_data)
        
    except Exception as e:
        logger.error(f"获取水坝数据时发生错误: {str(e)}")
        return jsonify({
            'error': f'服务器错误: {str(e)}',
            'data': []
        }), 500

@economy_bp.route('/dam_data/<country_name>', methods=['GET'])
def get_dam_data_by_country(country_name):
    """根据国家名称获取水坝数据"""
    try:
        logger.info(f"获取国家水坝数据请求: {country_name}")
        
        # 加载水坝数据
        dam_data = _cached('dam', load_dam_data)
        
        if not dam_data:
            return jsonify({'error': '水坝数据文件不存在或格式错误'}), 404
        
        # 搜索匹配的国家
        matched_data = []
        for item in dam_data:
            if country_name.lower() in item['country'].lower():
                matched_data.append(item)
        
        if not matched_data:
            logger.warning(f"未找到国家 '{country_name}' 的水坝数据")
            return jsonify({
                'error': f'未找到国家 \"{country_name}\" 的水坝数据',
                'matched_count': 0,
                'data': []
            }), 404
        
        logger.info(f"成功返回 {len(matched_data)} 个匹配的水坝数据")
        
        return jsonify({
            'matched_count': len(matched_data),
            'searched_name': country_name,
            'data': matched_data
        })
        
    except Exception as e:
        logger.error(f"获取国家水坝数据时发生错误: {str(e)}")
        return jsonify({
            'error': f'服务器错误: {str(e)}',
            'data': []
        }), 500

@economy_bp.route('/reservoir_data', methods=['GET'])
def get_reservoir_data():
    """获取水库数据"""
    try:
        logger.info("获取水库数据请求")
        
        # 加载水库数据
        reservoir_data = _cached('reservoir', load_reservoir_data)
        
        if not reservoir_data:
            logger.error("无法加载水库数据")
            return jsonify({
                'error': '水库数据文件不存在或格式错误',
                'data': []
            }), 404
        
        logger.info(f"成功返回 {len(reservoir_data)} 个水库数据")
        
        return jsonify(reservoir_data)
        
    except Exception as e:
        logger.error(f"获取水库数据时发生错误: {str(e)}")
        return jsonify({
            'error': f'服务器错误: {str(e)}',
            'data': []
        }), 500

@economy_bp.route('/reservoir_data/<country_name>', methods=['GET'])
def get_reservoir_data_by_country(country_name):
    """根据国家名称获取水库数据"""
    try:
        logger.info(f"获取国家水库数据请求: {country_name}")
        
        # 加载水库数据
        reservoir_data = _cached('reservoir', load_reservoir_data)
        
        if not reservoir_data:
            return jsonify({'error': '水库数据文件不存在或格式错误'}), 404
        
        # 搜索匹配的国家
        matched_data = []
        for item in reservoir_data:
            if country_name.lower() in item['country'].lower():
                matched_data.append(item)
        
        if not matched_data:
            logger.warning(f"未找到国家 '{country_name}' 的水库数据")
            return jsonify({
                'error': f'未找到国家 \"{country_name}\" 的水库数据',
                'matched_count': 0,
                'data': []
            }), 404
        
        logger.info(f"成功返回 {len(matched_data)} 个匹配的水库数据")
        
        return jsonify({
            'matched_count': len(matched_data),
            'searched_name': country_name,
            'data': matched_data
        })
        
    except Exception as e:
        logger.error(f"获取国家水库数据时发生错误: {str(e)}")
        return jsonify({
            'error': f'服务器错误: {str(e)}',
            'data': []
        }), 500