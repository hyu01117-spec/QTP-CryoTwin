import os
import glob
import rasterio
import numpy as np
import pandas as pd
from tqdm import tqdm

# === 1. 主文件夹路径 ===
base_folder = r"F:\output2"
output_folder = r"F:\mean_results"
os.makedirs(output_folder, exist_ok=True)

# === 2. 获取所有变量子目录 ===
variable_folders = [f.path for f in os.scandir(base_folder) if f.is_dir()]
print(f"🔍 共检测到 {len(variable_folders)} 个变量目录，开始批处理...")

# === 3. 遍历每个变量文件夹 ===
for var_path in variable_folders:
    var_name = os.path.basename(var_path)
    tif_files = glob.glob(os.path.join(var_path, "*.tif"))
    print(f"\n📂 正在处理变量：{var_name}，共 {len(tif_files)} 个 TIF 文件")

    results = []
    for file in tqdm(tif_files, desc=f"⏳ 处理 {var_name}"):
        with rasterio.open(file) as src:
            data = src.read(1)
            if src.nodata is not None:
                data = np.where(data == src.nodata, np.nan, data)
            mean_val = np.nanmean(data)
            results.append({
                "file_name": os.path.basename(file),
                "mean_value": mean_val
            })

    # === 4. 保存每个变量的结果为 Excel 文件 ===
    df = pd.DataFrame(results)
    out_path = os.path.join(output_folder, f"{var_name}_mean.xlsx")
    df.to_excel(out_path, index=False)
    print(f"✅ {var_name} 处理完成，结果保存为：{out_path}")
