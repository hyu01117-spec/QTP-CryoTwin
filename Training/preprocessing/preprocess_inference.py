import os
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.mask import mask
from datetime import datetime
from collections import defaultdict
from tqdm import tqdm

# =====================
# 配置参数
# =====================
VARIABLES = [
    "temperature_2m",
    "temperature_2m_max",
    "total_precipitation_max",
    "total_precipitation_sum",
]
INPUT_DIR = r"F:\output2"
LABEL_FILE = r"F:\data\labelslabels\monthly_runoff.xlsx"
UPSTREAM_SHP = r"F:/data/shapefiles/wholeYarkant.shp"

OUTPUT_X = "X_inference.npy"
OUTPUT_Y = "y_inference.npy"

# =====================
# 读取流域矢量边界
# =====================
upstream = gpd.read_file(UPSTREAM_SHP)
upstream = upstream.to_crs("EPSG:4326")

# =====================
# 读取月尺度径流标签
# =====================
df_label = pd.read_excel(LABEL_FILE)
df_label["date"] = pd.to_datetime(df_label["date"])
monthly_y = dict(zip(df_label["date"].dt.strftime("%Y-%m"), df_label["monthly_runoff"]))

# =====================
# 遍历所有气象变量 TIF 文件
# =====================
monthly_data = defaultdict(lambda: [])

print("⏳ 开始处理所有气象变量...")

for var in VARIABLES:
    var_dir = os.path.join(INPUT_DIR, var)
    files = sorted([f for f in os.listdir(var_dir) if f.endswith(".tif")])

    for tif_name in tqdm(files, desc=f"处理变量 {var}"):
        try:
            date_str = tif_name[:8]  # 文件名前8位是日期，例如：19560101
            date = datetime.strptime(date_str, "%Y%m%d")
            ym_key = date.strftime("%Y-%m")
        except:
            continue

        tif_path = os.path.join(var_dir, tif_name)
        with rasterio.open(tif_path) as src:
            out_image, _ = mask(src, upstream.geometry, crop=True)
            data = out_image[0]
            data = data[data != src.nodata]
            mean_val = np.nanmean(data) if data.size > 0 else np.nan

        monthly_data[ym_key].append((date, var, mean_val))

# =====================
# 整理成 X 和 y
# =====================
X_list, y_list = [], []
year_months = []

for ym in sorted(monthly_data.keys()):
    # 只处理有标签的月份
    if ym not in monthly_y:
        continue

    records = monthly_data[ym]
    days = sorted(list(set([r[0] for r in records])))
    day_feature_map = {day: [np.nan] * len(VARIABLES) for day in days}

    for date, var, value in records:
        var_idx = VARIABLES.index(var)
        day_feature_map[date][var_idx] = value

    daily_vectors = [day_feature_map[day] for day in sorted(day_feature_map)]
    X_list.append(daily_vectors)
    y_list.append(monthly_y[ym])
    year_months.append(datetime.strptime(ym, "%Y-%m"))

# 填充对齐天数
max_days = max(len(x) for x in X_list)
for i in range(len(X_list)):
    pad_len = max_days - len(X_list[i])
    if pad_len > 0:
        X_list[i] += [[np.nan] * len(VARIABLES)] * pad_len

X = np.array(X_list)  # shape: [n_months, max_days, 4]
y = np.array(y_list)  # shape: [n_months]

# 填充 NaN
X = np.nan_to_num(X, nan=np.nanmean(X))

print(f"\n✅ 数据加载完成！X.shape = {X.shape}, y.shape = {y.shape}")

# =====================
# 筛选 2001–2024 年
# =====================
inference_idx = [i for i, d in enumerate(year_months) if 2001 <= d.year <= 2024]

X_inference = X[inference_idx]
y_inference = y[inference_idx]

# =====================
# 保存
# =====================
np.save(OUTPUT_X, X_inference)
np.save(OUTPUT_Y, y_inference)

print(f"✅ 推理集保存完成：")
print(f"X_inference.shape = {X_inference.shape}")
print(f"y_inference.shape = {y_inference.shape}")
