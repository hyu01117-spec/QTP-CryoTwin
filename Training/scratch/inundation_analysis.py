import os
import numpy as np
import rasterio
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
from tqdm import tqdm
from matplotlib.colors import ListedColormap
from matplotlib.patches import Patch

# === 配置路径 ===
flood_mask_dir = r"F:\data\flood_masks"
output_dir = r"F:\data\flood_analysis"
pixel_area_km2 = 0.1 * 0.1  # 修改为你实际像元面积
os.makedirs(output_dir, exist_ok=True)

# === 获取栅格参考信息 ===
sample_file = os.path.join(flood_mask_dir, os.listdir(flood_mask_dir)[0])
with rasterio.open(sample_file) as src:
    meta = src.meta
    height, width = src.height, src.width
    nodata = src.nodata
    transform = src.transform

# === 初始化变量 ===
total_days = 0
frequency = np.zeros((height, width), dtype=np.uint16)
max_extent = np.zeros((height, width), dtype=np.uint8)
dates, flooded_areas = [], []

# === 批量读取每个日掩码 ===
print("📥 开始读取洪水掩码栅格...")

for filename in tqdm(sorted(os.listdir(flood_mask_dir)), desc="读取flood tif"):
    if not filename.endswith("_flood.tif"):
        continue

    date_str = filename[:10]
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d")
    except:
        continue

    file_path = os.path.join(flood_mask_dir, filename)
    with rasterio.open(file_path) as src:
        data = src.read(1)

    flood_pixels = (data == 1)
    frequency += flood_pixels.astype(np.uint16)
    max_extent = np.maximum(max_extent, flood_pixels.astype(np.uint8))

    flooded_area = np.sum(flood_pixels) * pixel_area_km2
    dates.append(date)
    flooded_areas.append(flooded_area)

    total_days += 1

print(f"✅ 累计洪水天数处理完成，总天数：{total_days} 天")

# === 保存频次场 ===
freq_tif = os.path.join(output_dir, "flood_frequency.tif")
meta.update(dtype='uint16', count=1, nodata=0)
with rasterio.open(freq_tif, "w", **meta) as dst:
    dst.write(frequency, 1)

# === 保存暴露率场 ===
exposure = (frequency / total_days * 100).astype(np.float32)
exp_tif = os.path.join(output_dir, "flood_exposure_percent.tif")
meta.update(dtype='float32', nodata=-9999)
with rasterio.open(exp_tif, "w", **meta) as dst:
    dst.write(np.where(total_days>0, exposure, -9999), 1)

# === 保存最大淹没区 ===
max_tif = os.path.join(output_dir, "max_flood_extent.tif")
meta.update(dtype='uint8', nodata=0)
with rasterio.open(max_tif, "w", **meta) as dst:
    dst.write(max_extent, 1)

# === 保存每日淹没面积 CSV ===
df_area = pd.DataFrame({"date": dates, "flooded_area_km2": flooded_areas})
df_area = df_area.sort_values("date")
area_csv = os.path.join(output_dir, "daily_flood_area.csv")
df_area.to_csv(area_csv, index=False)

# === 年洪水天数统计 ===
df_area["year"] = pd.to_datetime(df_area["date"]).dt.year
df_area["is_flood"] = (df_area["flooded_area_km2"] > 0).astype(int)
annual_counts = df_area.groupby("year")["is_flood"].sum().reset_index()
annual_csv = os.path.join(output_dir, "annual_flood_day_count.csv")
annual_counts.to_csv(annual_csv, index=False)

# === 连续洪水过程长度识别 ===
df_area = df_area.sort_values("date").reset_index(drop=True)
df_area["flood_event_id"] = 0
event_id = 0
for i in range(len(df_area)):
    if df_area.loc[i, "is_flood"] == 1:
        if i == 0 or df_area.loc[i-1, "is_flood"] == 0:
            event_id += 1
        df_area.loc[i, "flood_event_id"] = event_id
df_area["flood_event_id"] = df_area["flood_event_id"].astype(int)
event_lengths = df_area[df_area["flood_event_id"] > 0].groupby("flood_event_id").size()
event_lengths.to_csv(os.path.join(output_dir, "flood_event_lengths.csv"), header=["event_length_days"])

print("✅ 时序统计分析完成！")

# === 空间可视化 ===
plt.rcParams["font.family"] = ["SimHei"]
plt.rcParams['axes.unicode_minus'] = False

def plot_raster(data, title, output_path, cmap='Reds', vmin=None, vmax=None):
    plt.figure(figsize=(10, 6))
    plt.imshow(data, cmap=cmap, extent=[
        transform[2], transform[2] + transform[0]*width,
        transform[5] + transform[4]*height, transform[5]
    ], vmin=vmin, vmax=vmax)
    plt.colorbar(label='值')
    plt.title(title)
    plt.axis('off')
    plt.tight_layout()
    plt.savefig(output_path, dpi=300)
    plt.close()
    print(f"✅ 图保存：{output_path}")

# 洪水频次图
plot_raster(frequency, "洪水频次分布", os.path.join(output_dir, "flood_frequency_map.png"), cmap='Reds')

# 暴露率图
plot_raster(exposure, "洪水暴露率 (%)", os.path.join(output_dir, "flood_exposure_percent_map.png"), cmap='Oranges')

# 最大淹没区
plot_raster(max_extent, "最大洪水影响区", os.path.join(output_dir, "max_flood_extent_map.png"), cmap=ListedColormap(['white', 'red']))

# 洪水面积时间序列折线图
plt.figure(figsize=(10, 4))
plt.plot(df_area["date"], df_area["flooded_area_km2"], '-o', color='blue')
plt.xlabel("日期")
plt.ylabel("淹没面积 (km$^2$)")
plt.title("每日洪水淹没面积变化")
plt.tight_layout()
plt.savefig(os.path.join(output_dir, "daily_flood_area_timeseries.png"), dpi=300)
plt.close()

# 年洪水天数柱状图
plt.figure(figsize=(8, 4))
plt.bar(annual_counts["year"], annual_counts["is_flood"], color='green')
plt.xlabel("年份")
plt.ylabel("洪水天数")
plt.title("年度洪水天数统计")
plt.tight_layout()
plt.savefig(os.path.join(output_dir, "annual_flood_days_bar.png"), dpi=300)
plt.close()

print("✅ 全流程分析与空间可视化已完成！")
