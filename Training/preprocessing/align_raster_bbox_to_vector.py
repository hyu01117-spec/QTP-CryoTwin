#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把栅格的外包矩形对齐到矢量边界的外包矩形。

为什么需要
----------
simulate.py 输出时用的 transform 是"驱动数据能覆盖的范围"（如 73.19~104.69, 25.81~39.82），
而前端叠加显示的矢量边界（如 TP_China.geojson）是"真正的青藏高原边界"（73.45~104.69, 25.00~39.82）。
两者**外包矩形不一致**。

前端用 `image.getBoundingBox()` 按栅格 transform 算外包矩形并作 `imageExtent`，
NaN 区域被画成有色块（在旧前端里 `val === 0` 还会被错判为无效），视觉上"栅格与边界错位"。

mask_rasters_to_boundary.py 把矢量外的数据设成 NaN（治了"有数据"的部分），
但外包矩形本身的错位仍存在。本脚本治本：把栅格 transform 改成与矢量外包矩形一致，
输出尺寸随之调整（宽/高变化），原数据按像元对应关系放置在新的网格上，
矢量外的位置填 NaN。

用法
----
    # 预览
    python align_raster_bbox_to_vector.py --raster-dir DIR --check
    # 原地重写
    python align_raster_bbox_to_vector.py --raster-dir DIR --inplace
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import rasterio
from rasterio.transform import from_bounds

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(x, **kwargs):
        return x


# 默认对齐到 TP_China 边界（已修复为 4326）
DEFAULT_BOUNDS = (73.4470, 25.00, 104.6852, 39.8213)


def get_vector_bounds(path):
    """从 GeoJSON 取外包矩形 (L, B, R, T)。"""
    with open(path, 'r', encoding='utf-8') as fh:
        gj = json.load(fh)
    xs, ys = [], []

    def walk(node):
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)) and len(node) >= 2:
                xs.append(float(node[0]))
                ys.append(float(node[1]))
            else:
                for c in node:
                    walk(c)

    for f in gj.get('features', []):
        g = f.get('geometry')
        if g and g.get('coordinates') is not None:
            walk(g['coordinates'])
    if not xs:
        raise SystemExit(f'没有几何：{path}')
    return min(xs), min(ys), max(xs), max(ys)


def main(argv=None):
    ap = argparse.ArgumentParser(description='把栅格外包矩形对齐到矢量外包矩形')
    ap.add_argument('--raster-dir', required=True)
    ap.add_argument('--vector', default='data/raw/geo/TP_China.geojson',
                    help='用作对齐目标的矢量 GeoJSON（取其外包矩形）')
    ap.add_argument('--inplace', action='store_true')
    ap.add_argument('--check', action='store_true', help='只报告不修改')
    ap.add_argument('--output-dir', default=None)
    args = ap.parse_args(argv)

    if not args.inplace and not args.output_dir and not args.check:
        args.inplace = True

    L, B, R, T = get_vector_bounds(args.vector)
    print(f'对齐目标 (来自 {args.vector}): ({L}, {B}) - ({R}, {T})')

    files = sorted(glob.glob(os.path.join(args.raster_dir, '*.tif')))
    if not files:
        raise SystemExit(f'未找到 tif：{args.raster_dir}')
    print(f'栅格目录 : {args.raster_dir}  ({len(files)} 个 tif)')

    with rasterio.open(files[0]) as s:
        a, e = s.res
        a = abs(a)
    W = round((R - L) / a)
    H = round((T - B) / a)
    new_transform = from_bounds(L, B, R, T, W, H)
    print(f'新外包矩形: {W} 列 x {H} 行，分辨率 {a}')

    if args.check:
        for f in files[:5]:
            with rasterio.open(f) as s:
                ob = s.bounds
            print(f'  {os.path.basename(f)}: 现在 ({ob.left:.4f},{ob.bottom:.4f})-'
                  f'({ob.right:.4f},{ob.top:.4f}) -> 改后 ({L:.4f},{B:.4f})-({R:.4f},{T:.4f})')
        return 0

    if not args.inplace:
        os.makedirs(args.output_dir, exist_ok=True)

    for f in tqdm(files, desc='对齐外包矩形', unit='file'):
        try:
            with rasterio.open(f) as src:
                arr = src.read(1)
                old_c = src.transform.c
            # 原 c 列 -> 新 c 列 = c - (old_c - L) / a
            c_off = int(round((old_c - L) / a))

            new = np.full((H, W), np.nan, dtype=np.float32)
            # orig col [oc0, oc1) -> new col [oc0+c_off, oc1+c_off) ∩ [0, W)
            oc0 = max(0, -c_off)
            oc1 = min(arr.shape[1], W - c_off)
            nc0 = oc0 + c_off
            nc1 = oc1 + c_off
            if 0 <= nc0 < W and nc1 > 0 and oc0 < oc1:
                # 行维度可能不同（old height 通常 1560 < new height 1650），
                # 把原数据放在新数组顶部，剩余行保持 NaN。
                new[:arr.shape[0], nc0:nc1] = arr[:, oc0:oc1]

            out_path = f if args.inplace else os.path.join(
                args.output_dir, os.path.basename(f))
            with rasterio.open(out_path, 'w', height=H, width=W, count=1,
                               dtype='float32', crs='EPSG:4326',
                               transform=new_transform, nodata=np.nan,
                               compress='lzw') as dst:
                dst.write(new, 1)
        except Exception as e:
            print(f'  跳过（失败）: {os.path.basename(f)} -> {e}')

    print('完成。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
