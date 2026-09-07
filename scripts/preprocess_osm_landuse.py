# -*- coding: utf-8 -*-
"""
OSM 土地利用权重掩膜预处理
=========================
将 OSM landuse 矢量（gis_osm_landuse_a_free_1.shp）按指标所需的 fclass
栅格化为多波段 float32 权重掩膜 GeoTIFF，作为风险评估暴露度 OSM 像素调制的
**预生成静态栅格**，避免每次 /risk_assessment 都读 33MB dbf + 16MB shp 并实时栅格化。

模板网格：flood_inundation 由径流栅格（runoff_results/*.tif）经阈值判淹没得到，
故以径流栅格（缺省回退阈值栅格 grid_threshold_10p.tif）为权威主网格，保证
OSM 掩膜与危险性 H 网格完全对齐（EPSG:4326，分辨率≈0.009°）。

波段顺序（稳定，供 alert.py 按索引读取）：
  1 residential  2 industrial  3 commercial  4 retail  5 office  6 farmland  7 grass
对应 alert.py 中 _METRIC_OSM_FCLASS 的各 fclass 组合。

产物位置（与 OSM 源同目录）：
  data/raw/socioeconomy/osm_street/TPstreet_clip/
      gis_osm_landuse_masks.tif   （7 波段，float32，DEFLATE 压缩）
      gis_osm_landuse_masks.json  （波段顺序 / 网格 / 源 / 生成信息，便于溯源）
"""
import os
import json

import numpy as np
import rasterio
from rasterio.features import rasterize
import geopandas as gpd

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))  # 项目根（脚本已移至 scripts/，回退一层）

# 主网格模板：优先径流栅格（flood_inundation 的计算网格），否则阈值栅格
RUNOFF_TIF = os.path.join(BASE_DIR, "data", "webgis", "outputs", "runoff_results", "2024-01-02_runoff.tif")
THRESHOLD_TIF = os.path.join(BASE_DIR, "data", "raw", "thresholds", "grid_threshold_10p.tif")

OSM_SHP = os.path.join(
    BASE_DIR, "data", "raw", "socioeconomy", "osm_street",
    "TPstreet_clip", "gis_osm_landuse_a_free_1.shp")
OUT_DIR = os.path.dirname(OSM_SHP)
OUT_TIF = os.path.join(OUT_DIR, "gis_osm_landuse_masks.tif")
OUT_JSON = os.path.join(OUT_DIR, "gis_osm_landuse_masks.json")

# 稳定波段顺序（与 alert.py FCLASS_ORDER 保持一致）
FCLASS_ORDER = ["residential", "industrial", "commercial", "retail",
                "office", "farmland", "grass"]


def main():
    if not os.path.exists(OSM_SHP):
        raise FileNotFoundError(f"OSM landuse 源缺失: {OSM_SHP}")

    # 1) 选定主网格模板
    tmpl = RUNOFF_TIF if os.path.exists(RUNOFF_TIF) else THRESHOLD_TIF
    if not os.path.exists(tmpl):
        raise FileNotFoundError(f"主网格模板缺失（runoff/threshold 均未找到）")
    with rasterio.open(tmpl) as ds:
        dst_shape = (ds.height, ds.width)
        dst_transform = ds.transform
        dst_crs = ds.crs
    print(f"[模板网格] {os.path.relpath(tmpl, BASE_DIR)} | "
          f"{dst_shape[1]}x{dst_shape[0]} | {dst_crs} | res={dst_transform.a:.6f}")

    # 2) 读取 OSM landuse
    gdf = gpd.read_file(OSM_SHP)
    if gdf.crs is not None and dst_crs is not None and gdf.crs != dst_crs:
        gdf = gdf.to_crs(dst_crs)
    if "fclass" not in gdf.columns:
        raise KeyError("OSM landuse 缺少 fclass 字段，无法按地类栅格化")
    present = sorted([c for c in gdf["fclass"].unique() if isinstance(c, str)])
    print(f"[OSM fclass] 共 {len(gdf)} 要素，出现地类: {present}")

    # 3) 逐 fclass 栅格化
    bands = []
    for fc in FCLASS_ORDER:
        sub = gdf[gdf["fclass"] == fc]
        if len(sub) == 0:
            print(f"  - 缺失 fclass: {fc}（该波段全 0）")
            bands.append(np.zeros(dst_shape, dtype=np.float32))
            continue
        feats = [(geom.__geo_interface__, 1.0)
                 for geom in sub.geometry
                 if geom is not None and not geom.is_empty]
        grid = rasterize(feats, out_shape=dst_shape, transform=dst_transform,
                         fill=0.0, dtype=np.float32)
        print(f"  + {fc}: {len(feats)} 个要素 -> 掩膜像元 {int(grid.sum())}")
        bands.append(grid)

    # 4) 写出多波段 GeoTIFF
    out_profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": len(FCLASS_ORDER),
        "width": dst_shape[1],
        "height": dst_shape[0],
        "crs": dst_crs,
        "transform": dst_transform,
        "nodata": 0.0,
        "compress": "deflate",
        "tiled": True,
        "interleave": "band",
        "BIGTIFF": "IF_NEEDED",
    }
    with rasterio.open(OUT_TIF, "w", **out_profile) as dst:
        for i, grid in enumerate(bands, start=1):
            dst.write(grid, i)
    print(f"[写出] {OUT_TIF} | {os.path.getsize(OUT_TIF) / 1e6:.1f} MB")

    # 5) 写出 sidecar 元数据（溯源）
    meta = {
        "description": "OSM landuse 多波段权重掩膜（风险评估暴露度 OSM 像素调制预生成栅格）",
        "source_shp": OSM_SHP,
        "template_grid": os.path.relpath(tmpl, BASE_DIR),
        "crs": str(dst_crs),
        "width": dst_shape[1],
        "height": dst_shape[0],
        "transform": list(dst_transform),
        "resolution_deg": [dst_transform.a, abs(dst_transform.e)],
        "nodata": 0.0,
        "band_order": {str(i + 1): fc for i, fc in enumerate(FCLASS_ORDER)},
        "present_fclasses": present,
        "generated_by": "preprocess_osm_landuse.py",
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[写出] {OUT_JSON}")


if __name__ == "__main__":
    main()
