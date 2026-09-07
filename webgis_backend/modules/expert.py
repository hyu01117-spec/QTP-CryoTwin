# -*- coding: utf-8 -*-
"""
院士专家名录 —— API 蓝图
=========================
决策支持模块下的"专家咨询"功能后端，定位为**专家名录库**（参照应急管理/水利行业
专家库管理办法的通行做法）：

- 专家名录：姓名 / 头衔 / 单位 / 专业领域 / 学术简介 / 代表成果 / 代表论文
- 联系方式：展示专家所在单位的**公开联系方式**（单位、地址、公开电话、官网），
  不展示个人联系方式，并提示"请通过单位联系"
- 检索：关键词搜索（姓名 / 领域 / 单位 / 简介 / 标签），前端不再提供"领域"分类筛选 chips

数据源：data/webgis/content/expert_profiles.json（公开学术资料整理，含来源 URL；
路径由 config.CONTENT_DIR 给出）。

科研规范：
- 所有信息基于公开学术资料（sources 字段记录来源），禁止编造；
- 不展示个人私密联系方式，仅提供单位公开渠道；
- 前端显著位置声明档案为公开资料整理，供科研参考。
"""
import json
import logging
import os

from flask import Blueprint, jsonify, request, current_app

expert_bp = Blueprint('expert', __name__, url_prefix='/api/expert')
logger = logging.getLogger(__name__)


def _profiles_path():
    """专家名录数据路径（data/webgis/content/expert_profiles.json）。"""
    return os.path.join(current_app.config.get(
        'CONTENT_DIR', os.path.join(current_app.config['BASE_DIR'],
                                    'data', 'webgis', 'content')),
        'expert_profiles.json')


def load_profiles():
    """加载专家名录（真实公开资料）。"""
    path = _profiles_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        experts = data.get('experts', []) if isinstance(data, dict) else data
        return sorted(experts, key=lambda x: x.get('sort', 999))
    except Exception as e:
        logger.error('加载专家档案失败: %s', e)
        return []


def _summarize(e):
    """输出精简字段（列表用），含联系方式。"""
    c = e.get('contact') or {}
    return {
        'id': e.get('id'),
        'name': e.get('name'),
        'title': e.get('title'),
        'institution': e.get('institution'),
        'field': e.get('field'),
        'tags': e.get('tags', []),
        'bio': e.get('bio'),
        'achievements': e.get('achievements'),
        'core_views': e.get('core_views'),
        'papers': e.get('papers'),
        'contact': {
            'unit': c.get('unit') or e.get('institution'),
            'address': c.get('address'),
            'phone': c.get('phone'),
            'website': c.get('website'),
        },
        'sort': e.get('sort', 999),
        'avatar_color': e.get('avatar_color'),
        'sources': e.get('sources', []),
    }


@expert_bp.route('/profiles', methods=['GET'])
def profiles():
    """专家名录列表。支持 ?field=领域筛选、?q=关键词搜索（含标签）。"""
    experts = [ _summarize(e) for e in load_profiles() ]
    field = (request.args.get('field') or '').strip()
    q = (request.args.get('q') or '').strip()
    if field:
        experts = [e for e in experts if field in (e.get('field') or '')]
    if q:
        ql = q.lower()
        experts = [e for e in experts if
                   ql in (e.get('name') or '').lower()
                   or ql in (e.get('field') or '').lower()
                   or ql in (e.get('institution') or '').lower()
                   or ql in (e.get('bio') or '').lower()
                   or any(ql in (t or '').lower() for t in (e.get('tags') or []))]
    return jsonify({'success': True, 'experts': experts, 'count': len(experts)})


@expert_bp.route('/fields', methods=['GET'])
def fields():
    """专业领域分类（用于筛选）。"""
    seen = {}
    for e in load_profiles():
        f = (e.get('field') or '').strip()
        if f:
            # 以首个顿号/逗号分隔的领域作为主分类
            main = f.split('、')[0].split('，')[0].split(',')[0].strip()
            seen.setdefault(main, []).append(e.get('name'))
    fields_list = [{'field': k, 'experts': v} for k, v in sorted(seen.items())]
    return jsonify({'success': True, 'fields': fields_list})
