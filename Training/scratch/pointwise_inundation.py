import rasterio
import numpy as np
import os
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import geopandas as gpd
from matplotlib.colors import ListedColormap
from tqdm import tqdm
from datetime import datetime

# 设置中文显示
plt.rcParams["font.family"] = ["SimHei"]
plt.rcParams['axes.unicode_minus'] = False

# === 配置路径 ===
runoff_dir = r"F:\data\runoff_tifs"
flood_output_dir = r"F:\data\flood_masks"
png_output_dir = r"F:\data\flood_pngs"
shapefile_path = r"F:\data\shapefiles\wholeYarkant.shp"
dem_file = r"F:\data\shapefiles\wholeYarkant.tif"
threshold_file = r"F:\data\threshold\grid_threshold_day.tif"

os.makedirs(flood_output_dir, exist_ok=True)
os.makedirs(png_output_dir, exist_ok=True)

# === 设置日期范围 ===
start_date = datetime.strptime("1956-01-01", "%Y-%m-%d")
end_date = datetime.strptime("1956-12-31", "%Y-%m-%d")

# === 读取DEM和边界 ===
with rasterio.open(dem_file) as dem_src:
    dem = dem_src.read(1)
    dem_nodata = dem_src.nodata
    dem_crs = dem_src.crs
    dem_transform = dem_src.transform
if dem_nodata is not None:
    dem = np.where(dem == dem_nodata, np.nan, dem)

gdf = gpd.read_file(shapefile_path)
if gdf.crs != dem_crs:
    gdf = gdf.to_crs(dem_crs)

# 计算图幅范围
def get_extent(transform, width, height):
    xmin = transform[2]
    ymax = transform[5]
    xmax = xmin + transform[0] * width
    ymin = ymax + transform[4] * height
    return [xmin, xmax, ymin, ymax]

dem_extent = get_extent(dem_transform, dem.shape[1], dem.shape[0])
red_cmap = ListedColormap(['none', 'red'])

# === 读取阈值场 ===
with rasterio.open(threshold_file) as thresh_src:
    threshold_grid = thresh_src.read(1)
    threshold_nodata = thresh_src.nodata

if threshold_nodata is not None:
    threshold_grid = np.where(threshold_grid == threshold_nodata, np.nan, threshold_grid)

# === 筛选日期范围内的 runoff 文件 ===
all_files = [f for f in os.listdir(runoff_dir) if f.endswith("_runoff.tif")]
runoff_files = []
for f in all_files:
    try:
        date_str = f[:10]
        file_date = datetime.strptime(date_str, "%Y-%m-%d")
        if start_date <= file_date <= end_date:
            runoff_files.append(f)
    except Exception:
        continue

# === 批量处理 ===
for runoff_filename in tqdm(runoff_files, desc="批量生成洪水图"):
    runoff_file = os.path.join(runoff_dir, runoff_filename)

    # ✅ 每次处理文件时重新解析日期
    try:
        date_str = runoff_filename[:10]
    except:
        date_str = "未知日期"

    flood_mask_filename = runoff_filename.replace("_runoff.tif", "_flood.tif")
    flood_mask_file = os.path.join(flood_output_dir, flood_mask_filename)
    png_filename = runoff_filename.replace("_runoff.tif", "_flood.png")
    png_file = os.path.join(png_output_dir, png_filename)

    # === 读取 runoff 栅格 ===
    with rasterio.open(runoff_file) as runoff_src:
        runoff = runoff_src.read(1)
        runoff_profile = runoff_src.profile
        runoff_nodata = runoff_src.nodata

    # === 生成阈值判断掩码 ===
    flood_mask = np.zeros_like(runoff, dtype=bool)
    valid_mask = np.ones_like(runoff, dtype=bool)

    if runoff_nodata is not None:
        valid_mask &= runoff != runoff_nodata
    valid_mask &= ~np.isnan(threshold_grid)

    flood_mask[valid_mask] = runoff[valid_mask] >= threshold_grid[valid_mask]

    # === 保存 Flood Mask TIF ===
    out_profile = runoff_profile.copy()
    out_profile.update(dtype=rasterio.uint8, count=1, nodata=0)

    with rasterio.open(flood_mask_file, "w", **out_profile) as dst:
        dst.write(flood_mask.astype(np.uint8), 1)

    # === 绘图保存 PNG ===
    flood_mask_bin = np.where(flood_mask, 1, 0)

    plt.figure(figsize=(10, 6))
    plt.imshow(dem, cmap='gist_earth', interpolation='none', extent=dem_extent, vmin=1000, vmax=6000)
    plt.colorbar(label='地形高程 (m)')
    plt.imshow(flood_mask_bin, cmap=red_cmap, alpha=0.6, interpolation='none', extent=dem_extent)

    for geom in gdf.geometry:
        polygons = []
        if geom.geom_type == 'Polygon':
            polygons = [geom]
        elif geom.geom_type == 'MultiPolygon':
            polygons = geom.geoms
        for poly in polygons:
            x, y = zip(*poly.exterior.coords)
            plt.plot(x, y, color='blue', linewidth=2)

    plt.legend(handles=[
        Patch(facecolor='red', edgecolor='black', label='淹没区域'),
        Patch(facecolor='none', edgecolor='blue', label='流域边界')
    ], loc='lower right')

    plt.title(f"洪水淹没图 {date_str}")
    plt.axis('off')
    plt.tight_layout()
    plt.savefig(png_file, dpi=300, bbox_inches='tight')
    plt.close()

    print(f"✅ 完成: \n - TIF: {flood_mask_file}\n - PNG: {png_file}")
