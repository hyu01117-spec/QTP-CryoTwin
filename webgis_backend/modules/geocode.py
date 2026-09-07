from flask import Blueprint, request, jsonify, current_app
import requests
import logging

# 设定url_prefix，统一所有接口的前缀
geocode_bp = Blueprint('geocode', __name__, url_prefix='/modules')

logger = logging.getLogger(__name__)

@geocode_bp.route('/geocode', methods=['GET'])
def geocode():
    """处理地理编码请求"""
    address = request.args.get('address')
    if not address:
        return jsonify({'success': False, 'message': '缺少address参数'}), 400

    try:
        amap_key = current_app.config.get('AMAP_KEY')
        if not amap_key:
            logger.error("未配置高德KEY")
            return jsonify({'success': False, 'message': '未配置高德地图API密钥'}), 500

        url = 'https://restapi.amap.com/v3/geocode/geo'
        params = {
            'key': amap_key,
            'address': address
        }
        
        logger.info(f"发送地理编码请求: address={address}")
        response = requests.get(url, params=params, timeout=10)  # 添加超时设置
        
        # 检查响应状态
        response.raise_for_status()
        
        data = response.json()
        logger.info(f"地理编码API返回: status={data.get('status')}, info={data.get('info')}")

        # 检查API返回的数据状态
        if data.get('status') != '1':
            # 对于API明确返回的错误，使用404而不是500，表示未找到该地点
            api_info = data.get('info', '未知错误')
            logger.warning(f"地理编码API未找到地址: address={address}, info={api_info}")
            return jsonify({'success': False, 'message': f'未找到该地点: {api_info}'}), 404
        
        if not data.get('geocodes'):
            logger.warning(f"地理编码API返回空结果: address={address}")
            return jsonify({'success': False, 'message': '未找到该地点的地理编码信息'}), 404

        geocode_info = data['geocodes'][0]
        
        # 检查必要的字段是否存在
        if 'location' not in geocode_info:
            logger.error(f"地理编码API返回的结果缺少location字段: {geocode_info}")
            return jsonify({'success': False, 'message': '获取地理编码数据格式错误'}), 500
        
        if 'adcode' not in geocode_info:
            logger.warning(f"地理编码API返回的结果缺少adcode字段: {geocode_info}")
            # 即使没有adcode，也返回位置信息，只设置citycode为None
            citycode = None
        else:
            citycode = geocode_info['adcode']
        
        try:
            lng, lat = map(float, geocode_info['location'].split(','))
        except (ValueError, AttributeError) as e:
            logger.error(f"解析地理编码位置失败: {geocode_info['location']}, error={str(e)}")
            return jsonify({'success': False, 'message': '解析地理编码位置失败'}), 500

        result = {
            'lng': lng,
            'lat': lat,
            'citycode': citycode   # 保持前端一致
        }

        return jsonify({'success': True, 'result': result})

    except requests.exceptions.RequestException as e:
        # 网络错误，超时等
        logger.error(f"地理编码请求异常: {str(e)}")
        return jsonify({'success': False, 'message': f'网络请求失败: {str(e)}'}), 500
    except Exception as e:
        # 其他未知异常
        logger.error(f"地理编码处理异常: {str(e)}")
        return jsonify({'success': False, 'message': f'服务器处理错误: {str(e)}'}), 500