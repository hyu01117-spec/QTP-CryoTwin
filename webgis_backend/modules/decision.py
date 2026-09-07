# -*- coding: utf-8 -*-
"""
决策支持内容模块 —— API 蓝图
=============================
决策支持模块下的「防灾实例 / 工程措施 / 非工程措施」内容服务。

数据源：data/webgis/content/decision_content.json（路径由 config.CONTENT_DIR 给出）。
- 内容基于公开权威资料（政府通报、权威媒体报道、学术文献、单位官网）整理，
  每条记录携带 source（资料来源），机制描述真实可靠。
- 防灾实例：青藏高原及同类冰冻圈灾害真实案例（成因机制/过程影响/处置经验/启示）。
- 工程措施：防洪、泥石流防治、滑坡与堰塞湖、冰湖防治、冻土工程、雪害防治六类。
- 非工程措施：监测预警、应急预案、群测群防、宣传教育、风险管理、保险与救助六类。
"""
import json
import logging
import os

from flask import Blueprint, jsonify, current_app

decision_bp = Blueprint('decision', __name__, url_prefix='/api/decision')
logger = logging.getLogger(__name__)


def _content_path():
    """决策支持内容库路径（data/webgis/content/decision_content.json）。"""
    return os.path.join(current_app.config.get(
        'CONTENT_DIR', os.path.join(current_app.config['BASE_DIR'],
                                    'data', 'webgis', 'content')),
        'decision_content.json')


def _load_content():
    """加载决策支持内容库。"""
    path = _content_path()
    if not os.path.exists(path):
        return {'cases': [], 'engineering': [], 'non_engineering': []}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {
            'cases': data.get('cases', []),
            'engineering': data.get('engineering', []),
            'non_engineering': data.get('non_engineering', []),
        }
    except Exception as e:
        logger.error('加载决策支持内容失败: %s', e)
        return {'cases': [], 'engineering': [], 'non_engineering': []}


@decision_bp.route('/content', methods=['GET'])
def content():
    """返回决策支持内容库（防灾实例/工程措施/非工程措施）。"""
    data = _load_content()
    return jsonify({
        'success': True,
        'cases': data['cases'],
        'engineering': data['engineering'],
        'non_engineering': data['non_engineering'],
        'counts': {
            'cases': len(data['cases']),
            'engineering': len(data['engineering']),
            'non_engineering': len(data['non_engineering']),
        },
    })
