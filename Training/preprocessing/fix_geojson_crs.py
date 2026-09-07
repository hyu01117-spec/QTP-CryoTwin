#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""矢量 GeoJSON 坐标系校正。

发现的问题
----------
`data/raw/geo` 下的 GeoJSON 混用了多种坐标系：

    TP_China.geojson            Polygon    声明 EPSG::3857  坐标 8176078 ~ 11653503（米）
    TP_China_permafrost.geojson Polygon×12534 声明 EPSG::3857
    TibetanPlateau.geojson      Point×106  未声明（实际是气象站点，不是边界！）
    TP_Basins_China.geojson     Polygon×15 CRS84（= EPSG:4326，仅轴序不同）
    TP_basins / TP_rivers / ...            CRS84

为什么这会破坏配准
------------------
OpenLayers 的 `ol.format.GeoJSON().readFeatures(data, {featureProjection:'EPSG:3857'})`
在未显式指定 `dataProjection` 时，默认按 **EPSG:4326** 解析，并且**忽略 GeoJSON 里的
`crs` 字段**（RFC 7946 已废弃该字段，规范规定 GeoJSON 一律为 WGS84）。

于是 `TP_China.geojson` 的米制坐标被当成经纬度：8176078° 远超 [-180, 180]，
边界被画到完全错误的位置 —— 表现为"栅格像元与矢量轮廓不重合、边缘锯齿错位"。

验证：把它的坐标按 3857→4326 正确转换后

    lon 73.4470 ~ 104.6852      lat 24.9950 ~ 39.8213

与栅格范围（lon 68.02 ~ 104.69，lat 25.81 ~ 39.82）高度吻合，
证实它确实是青藏高原边界，只是存成了 Web Mercator。

修复策略
--------
把声明为（或检测为）投影坐标系的 GeoJSON 统一转成 EPSG:4326 并原地写回，
原文件移入备份目录。这样前端即使不写 dataProjection 也能正确显示。

坐标量级判据：|x| > 180 或 |y| > 90 即判定为投影坐标（经纬度不可能超出此范围）。
"""

import argparse
import collections
import datetime
import glob
import json
import os
import shutil
import sys

DEFAULT_GEO_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'raw', 'geo'))


def iter_coords(node):
    """递归产出所有坐标点（支持 Point / LineString / Polygon / Multi*）。"""
    if isinstance(node, (list, tuple)):
        if node and isinstance(node[0], (int, float)) and len(node) >= 2:
            yield node
        else:
            for child in node:
                yield from iter_coords(child)


def map_coords(node, fn):
    """递归变换所有坐标点，保持嵌套结构。"""
    if isinstance(node, (list, tuple)):
        if node and isinstance(node[0], (int, float)) and len(node) >= 2:
            return fn(node)
        return [map_coords(c, fn) for c in node]
    return node


def sample_bounds(features, n=4000):
    """抽样统计所有要素的坐标范围（大文件不必遍历全部点）。"""
    xs, ys, cnt = [], [], 0
    for f in features:
        geom = f.get('geometry')
        if not geom:
            continue
        for c in iter_coords(geom.get('coordinates', [])):
            xs.append(float(c[0]))
            ys.append(float(c[1]))
            cnt += 1
            if cnt >= n:
                break
        if cnt >= n:
            break
    if not xs:
        return None
    return min(xs), max(xs), min(ys), max(ys), cnt


def declared_crs(gj):
    crs = gj.get('crs')
    if not crs:
        return None
    name = crs.get('properties', {}).get('name', '') if isinstance(crs, dict) else ''
    if not name:
        return None
    name = str(name)
    if '3857' in name or '900913' in name or 'WEB_MERCATOR' in name.upper():
        return 'EPSG:3857'
    if '4326' in name or 'CRS84' in name:
        return 'EPSG:4326'
    return name


def detect(gj):
    """返回 (geom_types, feature_count, declared_crs, is_projected, bounds)。"""
    features = gj.get('features', [])
    types = collections.Counter(
        (f.get('geometry') or {}).get('type', 'None') for f in features)
    declared = declared_crs(gj)
    b = sample_bounds(features)
    if b is None:
        return types, len(features), declared, False, None
    xmin, xmax, ymin, ymax, _ = b
    is_projected = abs(xmin) > 180 or abs(xmax) > 180 or abs(ymin) > 90 or abs(ymax) > 90
    return types, len(features), declared, is_projected, b


def convert_geometry(features, src_crs):
    """把要素坐标从 src_crs 转到 EPSG:4326，返回转换的点数。"""
    from rasterio.warp import transform
    n = 0

    def convert_feature(f):
        nonlocal n
        geom = f.get('geometry')
        if not geom or 'coordinates' not in geom:
            return f
        pts = list(iter_coords(geom['coordinates']))
        if not pts:
            return f
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        # 注意：rasterio.warp.transform 没有 always_xy 参数（那是 pyproj 的），
        # 它本身就是按 (x/easting, y/northing) 输入输出，返回 (lon, lat)。
        lons, lats = transform(src_crs, 'EPSG:4326', xs, ys)
        lut = {}
        for i, p in enumerate(pts):
            key = (p[0], p[1])
            newpt = [lons[i], lats[i]]
            if len(p) > 2:                      # 保留 z
                newpt.append(p[2])
            lut.setdefault(key, newpt)
        geom['coordinates'] = map_coords(
            geom['coordinates'], lambda c: lut.get((c[0], c[1]), list(c)))
        n += len(pts)
        return f

    for i, f in enumerate(features):
        features[i] = convert_feature(f)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description='矢量 GeoJSON 坐标系校正')
    ap.add_argument('--geo-dir', default=DEFAULT_GEO_DIR, help='geo 目录')
    ap.add_argument('--check', action='store_true', help='只检查并报告')
    ap.add_argument('--fix', action='store_true', help='执行转换（原文件移入备份目录）')
    ap.add_argument('--backup', default=None, help='备份目录')
    args = ap.parse_args(argv)

    if not args.check and not args.fix:
        args.check = True

    geo_dir = args.geo_dir
    if not os.path.isdir(geo_dir):
        raise SystemExit(f'目录不存在：{geo_dir}')

    files = sorted(glob.glob(os.path.join(geo_dir, '*.geojson')))
    print(f'扫描 {len(files)} 个 GeoJSON\n')
    print(f'{"文件":<34}{"几何类型":<26}{"声明CRS":<14}{"判定"}')
    print('-' * 100)

    need_fix = []
    for path in files:
        name = os.path.basename(path)
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                gj = json.load(fh)
        except Exception as e:
            print(f'{name:<34}读取失败: {e}')
            continue

        types, nfeat, declared, is_projected, b = detect(gj)
        tstr = ','.join(f'{k}×{v}' for k, v in types.most_common(2))[:24]

        if is_projected:
            verdict = '投影坐标 -> 需转 EPSG:4326'
            need_fix.append((path, declared or 'EPSG:3857'))
        elif declared and '3857' in declared:
            verdict = '声明 3857 但坐标已是经纬度'
            need_fix.append((path, None))
        else:
            verdict = 'OK（经纬度）'

        print(f'{name:<34}{tstr:<26}{str(declared or "-"):<14}{verdict}')
        if b:
            print(f'{"":<34}坐标范围 x[{b[0]:.4f}, {b[1]:.4f}]  '
                  f'y[{b[2]:.4f}, {b[3]:.4f}]')

    print()
    if not need_fix:
        print('未发现需要修正的坐标系。')
        return 0

    print(f'需要修正 {len(need_fix)} 个文件')
    if args.check:
        print('\n--check 模式：未做修改。确认后加 --fix。')
        return 1

    backup = args.backup or f'{geo_dir}_crsfix_backup_{datetime.date.today():%Y%m%d}'
    print(f'\n开始修复，备份目录：{backup}')

    for path, src_crs in need_fix:
        name = os.path.basename(path)
        if src_crs is None:
            # 只是 crs 字段声明错，坐标本身已是经纬度：清掉误导性的 crs 字段
            with open(path, 'r', encoding='utf-8') as fh:
                gj = json.load(fh)
            gj.pop('crs', None)
            os.makedirs(backup, exist_ok=True)
            shutil.copy2(path, os.path.join(backup, name))
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(gj, fh, ensure_ascii=False)
            print(f'  {name}: 已移除误导性的 crs 声明（坐标本身正确）')
            continue

        with open(path, 'r', encoding='utf-8') as fh:
            gj = json.load(fh)
        n = convert_geometry(gj.get('features', []), src_crs)
        gj.pop('crs', None)          # RFC 7946：GeoJSON 默认 WGS84，不再声明
        os.makedirs(backup, exist_ok=True)
        shutil.move(path, os.path.join(backup, name))
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(gj, fh, ensure_ascii=False)
        print(f'  {name}: {src_crs} -> EPSG:4326，转换 {n} 个点')

    print(f'\n原始文件已备份至：{backup}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
