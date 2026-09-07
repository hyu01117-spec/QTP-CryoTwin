# -*- coding: utf-8 -*-
"""公共工具函数：项目根目录解析、JSON 缓存读取、路径安全校验等。"""
import os
import json

def get_project_root():
    """获取项目根目录（基于本文件位置：modules -> 项目根）。"""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# 静态 JSON 缓存：GeoJSON 等只读数据只在进程内解析一次
_JSON_CACHE = {}

def load_json_cached(path):
    """读取 JSON 文件并缓存（静态数据只在进程内读一次）。"""
    real = os.path.realpath(path)
    if real in _JSON_CACHE:
        return _JSON_CACHE[real]
    with open(real, 'r', encoding='utf-8') as f:
        data = json.load(f)
    _JSON_CACHE[real] = data
    return data

def resolve_index_path(root_dir, file_path):
    """把栅格索引库里的 file_path 解析成本机绝对路径。

    索引库中的 file_path 可能是两种写法：
    - 相对路径（2026-09-07 重扫后的标准写法）：相对 root_dir，如
      `temperature_2m/temperature_2m_2024-01-01.tif`
    - 绝对路径：历史数据遗留（如旧机器的 E:\\Pycharm\\...），本机多半已失效，
      直接原样返回，由调用方判断文件是否存在。

    这样换机/换盘符不会导致索引整体失效。
    """
    if not file_path:
        return file_path
    p = file_path.replace('/', os.sep).replace('\\', os.sep)
    if os.path.isabs(p) or (len(p) > 1 and p[1] == ':'):
        return p
    return os.path.join(root_dir, p)


def resolve_within(directory, name):
    """校验文件名并把路径解析到指定目录内（防路径穿越）。

    要求 name 必须是纯文件名（禁止路径分隔符 / ..），且解析后的真实路径
    必须位于 directory（realpath）之内；不满足返回 None。
    """
    if not name or not isinstance(name, str):
        return None
    if os.path.basename(name) != name:
        return None
    if '..' in name or '/' in name or '\\' in name:
        return None
    base = os.path.realpath(directory)
    full = os.path.realpath(os.path.join(base, name))
    try:
        if os.path.commonpath([base, full]) != base:
            return None
    except ValueError:
        return None
    return full
