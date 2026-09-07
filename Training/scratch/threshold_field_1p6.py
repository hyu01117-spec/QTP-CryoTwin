import os
import numpy as np
import rasterio
from tqdm import tqdm

# === 配置路径 ===
input_folder = r"F:\data\runoff_tifs"
output_tif = r"F:\data\grid_threshold_day_1p6.tif"
target_annual_count = 1.6  # 目标年均洪水日次数（可改）

# === 获取文件列表 ===
tif_files = sorted([
    os.path.join(input_folder, f)
    for f in os.listdir(input_folder)
    if f.endswith("_runoff.tif")
])

total_days = len(tif_files)
data_years = total_days / 365.25
target_event_count = target_annual_count * data_years  # 总目标天数（约72天）

print(f"📅 数据年份：约 {data_years:.2f} 年，总天数：{total_days} 天")
print(f"🎯 每格目标超过天数 ≈ {target_event_count:.1f} 天")

# === 读取第一个文件，获取元信息 ===
with rasterio.open(tif_files[0]) as src:
    meta = src.meta
    height, width = src.height, src.width
    nodata = src.nodata

# === 初始化：栅格堆叠 ===
runoff_stack = np.zeros((total_days, height, width), dtype=np.float32)

print("📥 开始加载所有日尺度栅格...")
for i, file in enumerate(tqdm(tif_files, desc="加载tif")):
    with rasterio.open(file) as src:
        data = src.read(1)
        if nodata is not None:
            data = np.where(data == nodata, np.nan, data)
        runoff_stack[i, :, :] = data

print("✅ 数据加载完成，开始计算格点阈值...")

# === 逐像元计算阈值 ===
threshold_grid = np.full((height, width), np.nan, dtype=np.float32)
rank_pos = total_days - int(np.round(target_event_count))  # 从大到小排名位置

for row in tqdm(range(height), desc="计算阈值场"):
    for col in range(width):
        pixel_series = runoff_stack[:, row, col]
        valid_values = pixel_series[~np.isnan(pixel_series)]

        if len(valid_values) > 0:
            sorted_values = np.sort(valid_values)  # 从小到大排序
            if rank_pos <= 0:
                threshold_grid[row, col] = sorted_values[0]
            elif rank_pos >= len(sorted_values):
                threshold_grid[row, col] = sorted_values[-1]
            else:
                threshold_grid[row, col] = sorted_values[rank_pos]

# === 保存输出 ===
output_meta = meta.copy()
output_meta.update({
    "dtype": "float32",
    "count": 1,
    "nodata": -9999
})

with rasterio.open(output_tif, "w", **output_meta) as dst:
    out_data = np.where(np.isnan(threshold_grid), -9999, threshold_grid)
    dst.write(out_data, 1)

print(f"✅ 阈值场保存完成：{output_tif}")
