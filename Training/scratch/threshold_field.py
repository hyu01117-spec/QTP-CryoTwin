import os
import numpy as np
import rasterio
from tqdm import tqdm

# 配置
input_folder = r"D:\YHH\data\runoff_tifs2.0"  # 存放每日测t预if的文件夹
output_file = r'D:\YHH\data\grid_threshold_%.tif'  # 输出阈值栅格路径
percentile = 90  # 目标百分位，90%

# 收集所有tif文件路径
tif_files = sorted([os.path.join(input_folder, f) for f in os.listdir(input_folder) if f.endswith('.tif')])

# 检查
if len(tif_files) == 0:
    raise FileNotFoundError(f"❌ 没有找到任何.tif 文件，请检查目录：{input_folder}")

# 读取栅格大小和元数据
with rasterio.open(tif_files[0]) as src:
    profile = src.profile
    rows, cols = src.shape

# 初始化数组： [n_days, rows, cols]
n_days = len(tif_files)
data_stack = np.zeros((n_days, rows, cols), dtype=np.float32)

# 逐日加载
print(f"📥 开始加载 {n_days} 个日尺度径流栅格...")
for idx, tif_file in tqdm(enumerate(tif_files), total=n_days):
    with rasterio.open(tif_file) as src:
        data_stack[idx] = src.read(1)

# 计算每个像元的百分位数阈值
print(f"📊 开始计算每个格点的 {percentile} 分位阈值...")
threshold_grid = np.percentile(data_stack, percentile, axis=0)

# 保存为GeoTIFF
profile.update(dtype=rasterio.float32, count=1)
with rasterio.open(output_file, 'w', **profile) as dst:
    dst.write(threshold_grid.astype(np.float32), 1)

print(f"✅ 每格点阈值栅格已保存：{output_file}")
