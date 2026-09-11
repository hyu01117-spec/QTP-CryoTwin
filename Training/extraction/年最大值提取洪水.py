import rasterio
import numpy as np
import os

# --- 1. 定义文件路径和参数 ---
input_dir = r"E:\1data\A_data_use\0.25_aligned\snowmelt\yearly_max\年最大流量\80-24"
output_dir = r"E:\1data\A_data_use\0.25_aligned\snowmelt\yearly_max\AMF多年平均"

os.makedirs(output_dir, exist_ok=True)

output_file_1980_2003 = os.path.join(output_dir, "snowmelt_max_1980-2003.tif")
output_file_2004_2024 = os.path.join(output_dir, "snowmelt_max_2004-2024.tif")

# --- 2. 准备文件列表和年份 ---
# 获取所有tif文件并按名称排序
files = [os.path.join(input_dir, f) for f in sorted(os.listdir(input_dir)) if f.endswith('.tif')]

if not files:
    print(f"错误：在 {input_dir} 目录下没有找到任何 .tif 文件。")
    exit()

# 从文件名中提取年份
years = [int(os.path.basename(path).split('_')[-1].replace('.tif', '')) for path in files]
print(f"找到 {len(files)} 个文件，年份范围: {min(years)} - {max(years)}")

# --- 3. 读取第一个文件获取元数据和空间信息 ---
with rasterio.open(files[0]) as src:
    meta = src.meta.copy()  # 复制元数据（投影、仿射变换等）
    height, width = src.height, src.width
    crs = src.crs
    transform = src.transform

# --- 4. 创建一个大数组来存储所有年份的数据 ---
# 形状: (时间, 高度, 宽度)
all_data = np.empty((len(files), height, width), dtype=np.float32)

# --- 5. 循环读取所有文件数据到数组中 ---
print("开始加载数据...")
for i, file_path in enumerate(files):
    with rasterio.open(file_path) as src:
        # 读取第一个波段的数据，并将 nodata 值替换为 NaN
        band_data = src.read(1).astype(np.float32)
        if src.nodata is not None:
            band_data[band_data == src.nodata] = np.nan
        all_data[i, :, :] = band_data
print("数据加载完成。")

# --- 6. 根据年份进行分组和计算最大值 ---
# 将年份和数据关联起来
years_np = np.array(years)

# 找到两个时间段的索引
idx_1980_2003 = (years_np >= 1980) & (years_np <= 2003)
idx_2004_2024 = (years_np >= 2004) & (years_np <= 2024)

# 使用 np.nanmax 计算最大值，它会自动忽略 NaN 值
print("开始计算 1950-2003 年的最大值...")
max_data_1980_2003 = np.nanmax(all_data[idx_1980_2003, :, :], axis=0)

print("开始计算 2004-2024 年的最大值...")
max_data_2004_2024 = np.nanmax(all_data[idx_2004_2024, :, :], axis=0)

# --- 7. 保存结果为新的TIFF文件 ---
def save_tiff(output_path, data_array, meta):
    """辅助函数，用于保存TIFF文件"""
    # 更新元数据中的数据类型和nodata值
    meta.update(dtype=rasterio.float32, count=1, nodata=np.nan)
    
    with rasterio.open(output_path, 'w', **meta) as dst:
        dst.write(data_array.astype(rasterio.float32), 1)
    print(f"文件已成功保存到: {output_path}")

save_tiff(output_file_1980_2003, max_data_1980_2003, meta)
save_tiff(output_file_2004_2024, max_data_2004_2024, meta)

print("所有操作完成！")