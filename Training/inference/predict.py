import os
import glob
import time
import numpy as np
import rasterio
import geopandas as gpd
import torch
import torch.nn as nn
import psutil
from tqdm import tqdm
from datetime import datetime

# === 配置路径 ===
INPUT_FOLDER = r"F:\data\era5_land\wholeYarkant"
VARIABLES = ["temperature_2m", "temperature_2m_max", "total_precipitation_max", "total_precipitation_sum"]
SHAPEFILE = "F:/data/shapefiles/wholeYarkant.shp"
OUTPUT_FOLDER = "F:/data/runoff_tifs"
MODEL_PATH = "lstm_runoff_model.pt"

# === 设置推理年份区间 ===
START_YEAR = 2021
END_YEAR = 2024

os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# === 流域范围 ===
gdf = gpd.read_file(SHAPEFILE)
if gdf.crs is None:
    gdf.set_crs("EPSG:4326", inplace=True)

# === 所有日期列表 ===
sample_files = sorted(glob.glob(os.path.join(INPUT_FOLDER, VARIABLES[0], "*_clipped.tif")))
all_dates = [os.path.basename(f).replace("_clipped.tif", "") for f in sample_files]
all_years = sorted(set([d[:4] for d in all_dates]))

# === 按指定区间过滤年份 ===
target_years = [y for y in all_years if START_YEAR <= int(y) <= END_YEAR]

# === 模型定义（保持与训练时一致）===
class RunoffLSTM(nn.Module):
    def __init__(self, input_size):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=100,
            num_layers=2,
            batch_first=True,
            dropout=0.2
        )
        self.fc = nn.Linear(100, 1)
        self.relu = nn.ReLU()

    def forward(self, x):
        out, _ = self.lstm(x)                     # [B, T, 100]
        return self.relu(self.fc(out).squeeze(-1))  # [B, T]

# === 加载模型 ===
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"✅ 使用设备: {device}")
model = RunoffLSTM(input_size=4).to(device)
model.load_state_dict(torch.load(MODEL_PATH, map_location=device))
model.eval()

# === 参考影像尺寸 ===
with rasterio.open(sample_files[0]) as ref:
    H, W = ref.height, ref.width
    profile = ref.profile
    profile.update(count=1, dtype=rasterio.float32, compress="lzw")

# === 分年推理（指定年份区间）===
total_start = time.time()
for year in target_years:
    print(f"\n📅 开始处理年份: {year}")
    start = time.time()

    # 获取该年份的所有日期
    year_dates = [d for d in all_dates if d.startswith(year)]
    T = len(year_dates)
    print(f"📦 包含 {T} 天")

    # === 加载变量数据 ===
    var_stacks = []
    for var in VARIABLES:
        stack = []
        for d in tqdm(year_dates, desc=f"加载变量 {var}"):
            tif_path = os.path.join(INPUT_FOLDER, var, f"{d}_clipped.tif")
            with rasterio.open(tif_path) as src:
                img = src.read(1)
                img = np.where(img == src.nodata, np.nan, img)
                stack.append(img)
        var_stacks.append(np.stack(stack, axis=0))  # [T, H, W]

    # === 推理 ===
    results = np.full((T, H, W), np.nan, dtype=np.float32)
    with torch.no_grad():
        for i in tqdm(range(H), desc=f"推理年份 {year}"):
            for j in range(W):
                pixel_series = np.stack([var_stacks[k][:, i, j] for k in range(4)], axis=-1)
                if np.isnan(pixel_series).any():
                    continue
                x = torch.tensor(pixel_series, dtype=torch.float32).unsqueeze(0).to(device)
                y_pred = model(x).squeeze(0).cpu().numpy()
                results[:, i, j] = y_pred

    # === 输出每日 TIF（修正数值分布方向）===
    for t in tqdm(range(T), desc=f"写出年份 {year}"):
        date_str = year_dates[t].split("_")[1]
        out_path = os.path.join(OUTPUT_FOLDER, f"{date_str}_runoff.tif")

        arr = results[t]
        min_val = np.nanmin(arr)
        max_val = np.nanmax(arr)
        flipped_vals = max_val - (arr - min_val)

        with rasterio.open(out_path, 'w', **profile) as dst:
            dst.write(flipped_vals, 1)

    # === 年份结束统计 ===
    year_time = time.time() - start
    print(f"✅ {year} 年处理完成，耗时 {year_time / 60:.2f} 分钟，内存使用 {psutil.virtual_memory().percent}%")

total_time = time.time() - total_start
print(f"\n🎉 指定年份区间推理完成，总耗时：{total_time / 60:.2f} 分钟")
