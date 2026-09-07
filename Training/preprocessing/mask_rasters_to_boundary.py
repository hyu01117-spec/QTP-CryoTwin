#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用矢量边界把栅格掩膜到指定范围。

背景
----
栅格数据的研究区范围（如 73.19°E 起）与矢量边界（如 73.45°E 起的真正高原）
不一致时，栅格"超出"矢量的部分会显示出来，造成"配准错位"。

本工具用 `rasterio.features.geometry_mask` 把多边形外的像元设为 NaN，
使栅格严格落在矢量范围内。这一步是研究区对齐，不改变 transform 与 CRS。

用法
----
    python mask_rasters_to_boundary.py --raster-dir DIR --boundary GEOJSON --check
    python mask_rasters_to_boundary.py --raster-dir DIR --boundary GEOJSON --inplace
    python mask_rasters_to_boundary.py --raster-dir DIR --boundary GEOJSON --output-dir OUT
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import rasterio
from rasterio.features import geometry_mask

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(x, **kwargs):
        return x


def load_boundary(path):
    with open(path, 'r', encoding='utf-8') as fh:
        gj = json.load(fh)
    geoms = [f['geometry'] for f in gj.get('features', []) if f.get('geometry')]
    crs = gj.get('crs')
    if crs and isinstance(crs, dict):
        name = crs.get('properties', {}).get('name', '')
        crs = str(name) if name else None
    return geoms, crs


def reproject_geoms(geoms, src_crs, dst_crs):
    from rasterio.warp import transform_geom
    if src_crs is None:
        src_crs = 'EPSG:4326'
    if dst_crs is None or str(src_crs) == str(dst_crs):
        return geoms
    return [transform_geom(src_crs, dst_crs, g) for g in geoms]


def mask_one(path, geoms):
    with rasterio.open(path) as src:
        arr = src.read(1).astype(np.float32)
        profile = src.profile.copy()
        transform = src.transform
        crs = src.crs
    geoms_r = reproject_geoms(geoms, 'EPSG:4326' if crs is None else str(crs), str(crs))
    mask = geometry_mask(geoms_r, out_shape=arr.shape, transform=transform, invert=True)
    nan_arr = arr.copy()
    nan_arr[~mask] = np.nan
    profile.update(nodata=np.nan, compress='lzw')
    return nan_arr, profile


def main(argv=None):
    ap = argparse.ArgumentParser(description='用矢量边界把栅格掩膜到指定范围')
    ap.add_argument('--raster-dir', required=True)
    ap.add_argument('--boundary', required=True)
    ap.add_argument('--inplace', action='store_true')
    ap.add_argument('--output-dir', default=None)
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args(argv)

    if not args.inplace and not args.output_dir and not args.check:
        args.inplace = True

    geoms, src_crs = load_boundary(args.boundary)
    if not geoms:
        raise SystemExit(f'边界里没有要素：{args.boundary}')

    files = sorted(glob.glob(os.path.join(args.raster_dir, '*.tif')))
    if not files:
        raise SystemExit(f'未找到 tif：{args.raster_dir}')

    print(f'栅格目录 : {args.raster_dir}  ({len(files)} 个 tif)')
    print(f'边界     : {args.boundary}  ({len(geoms)} 个几何)')
    if args.inplace:
        print('模式     : 原地覆盖')
    elif args.check:
        print('模式     : 仅检查')
    else:
        print(f'输出目录 : {args.output_dir}')
        os.makedirs(args.output_dir, exist_ok=True)

    if args.check:
        samples = files[::max(1, len(files)//10)][:10]
        print(f'采样 {len(samples)} 个文件报告覆盖率：')
        for f in samples:
            with rasterio.open(f) as s:
                arr = s.read(1); t = s.transform; crs = s.crs
            geoms_r = reproject_geoms(geoms,
                                      'EPSG:4326' if crs is None else src_crs or 'EPSG:4326',
                                      str(crs))
            mask = geometry_mask(geoms_r, out_shape=arr.shape, transform=t, invert=True)
            inside = int((mask & np.isfinite(arr)).sum())
            total = int(np.isfinite(arr).sum())
            print(f'  {os.path.basename(f)}: 有效像元在边界内 {inside}/{total} '
                  f'({100*inside/max(1,total):.1f}%)')
        return 0

    for f in tqdm(files, desc='掩膜', unit='file'):
        try:
            out, profile = mask_one(f, geoms)
        except Exception as e:
            print(f'  跳过（失败）: {os.path.basename(f)} -> {e}')
            continue
        out_path = f if args.inplace else os.path.join(args.output_dir, os.path.basename(f))
        with rasterio.open(out_path, 'w', **profile) as dst:
            dst.write(out.astype(np.float32), 1)
    print('完成。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
