import os
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.mask import mask
from datetime import datetime
from collections import defaultdict
from tqdm import tqdm

# 配置
VARIABLES = [
    "temperature_2m",
    "temperature_2m_max",
    "total_precipitation_max",
    "total_precipitation_sum",
]
INPUT_DIR = r"F:\output2"
LABEL_FILE = r"F:\data\labelslabels\monthly_runoff.xlsx"
UPSTREAM_SHP = r"F:/data/shapefiles/wholeYarkant.shp"
OUTPUT_X = "X.npy"
OUTPUT_Y = "y.npy"

# 读取矢量边界
upstream = gpd.read_file(UPSTREAM_SHP)
upstream = upstream.to_crs("EPSG:4326")  # 确保是WGS84

# 读取监督数据（转换为 datetime 类型）
df_label = pd.read_excel(LABEL_FILE)
df_label["date"] = pd.to_datetime(df_label["date"])
monthly_y = dict(zip(df_label["date"].dt.strftime("%Y-%m"), df_label["monthly_runoff"]))

# 遍历所有 tif，按月聚合成序列
monthly_data = defaultdict(lambda: [])

print("⏳ 开始处理所有气象变量：")

for var in VARIABLES:
    var_dir = os.path.join(INPUT_DIR, var)
    files = sorted([f for f in os.listdir(var_dir) if f.endswith(".tif")])

    for tif_name in tqdm(files, desc=f"处理变量 {var}"):
        try:
            date_str = tif_name[:8]  # 如 19560101
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

# 整理为数组
X_list, y_list = [], []

for ym in sorted(monthly_data.keys()):
    records = monthly_data[ym]
    if ym not in monthly_y:
        continue

    days = sorted(list(set([r[0] for r in records])))
    day_feature_map = {day: [np.nan] * len(VARIABLES) for day in days}

    for date, var, value in records:
        var_idx = VARIABLES.index(var)
        day_feature_map[date][var_idx] = value

    daily_vectors = [day_feature_map[day] for day in sorted(day_feature_map)]
    X_list.append(daily_vectors)
    y_list.append(monthly_y[ym])

# 对齐维度
max_days = max(len(x) for x in X_list)
for i in range(len(X_list)):
    pad_len = max_days - len(X_list[i])
    if pad_len > 0:
        X_list[i] += [[np.nan] * len(VARIABLES)] * pad_len

X = np.array(X_list)    # shape: [n_months, max_days, 4]
y = np.array(y_list)    # shape: [n_months]

# 填充 NaN
X = np.nan_to_num(X, nan=np.nanmean(X))

# 保存原始数据
np.save(OUTPUT_X, X)
np.save(OUTPUT_Y, y)

print(f"\n✅ 数据预处理完成！X.shape = {X.shape}, y.shape = {y.shape}")

# ====================
# 年份划分训练/测试集
# ====================
# 获取年月列表
year_months = [datetime.strptime(m, "%Y-%m") for m in sorted(monthly_data.keys()) if m in monthly_y]

# 构建索引
train_idx = [i for i, d in enumerate(year_months) if d.year <= 1990]
test_idx = [i for i, d in enumerate(year_months) if d.year > 1990]

# 划分
X_train, y_train = X[train_idx], y[train_idx]
X_test, y_test = X[test_idx], y[test_idx]

# 保存划分后的数据
np.save("X_train.npy", X_train)
np.save("y_train.npy", y_train)
np.save("X_test.npy", X_test)
np.save("y_test.npy", y_test)

print(f"📁 年份划分完成：")
print(f" 训练集（1956–1990）：X_train.shape = {X_train.shape}, y_train.shape = {y_train.shape}")
print(f" 测试集（1991–2000）：X_test.shape  = {X_test.shape}, y_test.shape  = {y_test.shape}")
