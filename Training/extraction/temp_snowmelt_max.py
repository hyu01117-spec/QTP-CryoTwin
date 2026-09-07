import os
import numpy as np
import rasterio
from datetime import datetime

# 处理命令行参数
import sys

if len(sys.argv) >= 5:
    input_dir = sys.argv[1]
    output_dir = sys.argv[2]
    start_date = sys.argv[3]
    end_date = sys.argv[4]
    start_year = int(start_date[:4])
    end_year = int(end_date[:4])
else:
    # 默认参数
    input_dir = r"E:\Pycharm\Cryo-floods\data\raw\GLDAS025_Snowmelt"
    output_dir = r"E:\Pycharm\Cryo-floods\data\processed\GLDAS_snowmelt_max"
    start_year = 1980
    end_year = 2024

os.makedirs(output_dir, exist_ok=True)

# 生成输出文件名
output_file = os.path.join(output_dir, f"GLDAS_snowmelt_max_{start_year}-{end_year}.tif")

# 获取所有tif文件并按名称排序
files = [os.path.join(input_dir, f) for f in sorted(os.listdir(input_dir)) if f.endswith('.tif')]

if not files:
    print(f"错误：在 {input_dir} 目录下没有找到任何 .tif 文件。")
    sys.exit(1)

# 从文件名中提取年份和日期
years = []
dates = []
for path in files:
    filename = os.path.basename(path)
    # 解析文件名格式：YYYYMMDD.tif
    date_str = filename.replace('.tif', '')
    if len(date_str) == 8 and date_str.isdigit():
        year = int(date_str[:4])
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        years.append(year)
        dates.append(datetime(year, month, day))
    else:
        print(f"警告：无法解析文件名 {filename} 的日期信息，将跳过该文件。")

# 过滤掉无效文件
valid_indices = [i for i, year in enumerate(years) if year is not None]
files = [files[i] for i in valid_indices]
years = [years[i] for i in valid_indices]
dates = [dates[i] for i in valid_indices]

if not files:
    print("错误：没有有效的日期文件名。")
    sys.exit(1)

print(f"找到 {len(files)} 个文件，年份范围: {min(years)} - {max(years)}")

# 读取第一个文件获取元数据和空间信息
with rasterio.open(files[0], 'r') as src:
    height, width = src.height, src.width
    transform = src.transform
    crs = src.crs
    meta = src.meta.copy()
    
    print(f"图像尺寸: {width} x {height}")
    print(f"坐标系: {crs}")

# 按年份分组存储数据
yearly_data = {}
print("开始加载数据...")

for i, file_path in enumerate(files):
    with rasterio.open(file_path, 'r') as src:
        data = src.read(1).astype(np.float32)
        current_height, current_width = data.shape
        
        # 处理填充值
        if src.nodata is not None:
            data[data == src.nodata] = np.nan
        elif np.min(data) == -9999:
            data[data == -9999] = np.nan
        
        # 获取当前文件的年份
        year = years[i]
        
        # 初始化年份数据存储
        if year not in yearly_data:
            yearly_data[year] = {
                'data': [],
                'height': current_height,
                'width': current_width,
                'meta': src.meta.copy()
            }
        
        # 检查尺寸是否一致
        if (yearly_data[year]['height'] != current_height or 
            yearly_data[year]['width'] != current_width):
            print(f"警告：文件 {file_path} 的尺寸 ({current_height}×{current_width}) 与同年份其他文件尺寸 ({yearly_data[year]['height']}×{yearly_data[year]['width']}) 不一致，将跳过该文件。")
            continue
        
        yearly_data[year]['data'].append(data)
        print(f"已加载 {file_path} ({current_height}×{current_width})")

# 计算每年的最大值
yearly_max = {}
for year in sorted(yearly_data.keys()):
    if len(yearly_data[year]['data']) == 0:
        print(f"警告：年份 {year} 没有有效数据，将跳过。")
        continue
    
    # 将全年数据堆叠成三维数组
    year_data_stack = np.stack(yearly_data[year]['data'], axis=0)
    
    # 计算每个栅格的年最大值
    max_data = np.nanmax(year_data_stack, axis=0)
    
    yearly_max[year] = {
        'data': max_data,
        'meta': yearly_data[year]['meta']
    }
    print(f"已计算 {year} 年最大值 ({max_data.shape[0]}×{max_data.shape[1]})")

# 准备用于时间段分析的数据
all_years = sorted(yearly_max.keys())
if not all_years:
    print("错误：没有可用的年份数据。")
    sys.exit(1)

print(f"成功处理 {len(all_years)} 年数据，年份范围: {min(all_years)} - {max(all_years)}")
print("数据加载完成。")

# 过滤指定年份范围的年份
years_selected = [year for year in all_years if start_year <= year <= end_year]

max_data_selected = None

# 计算指定时间段的最大值
print(f"开始计算 {start_year}-{end_year} 年的最大值...")
if years_selected:
    # 检查所有年份的数据尺寸是否一致
    first_year = years_selected[0]
    target_shape = yearly_max[first_year]['data'].shape
    all_same_shape = True
    
    for year in years_selected:
        if yearly_max[year]['data'].shape != target_shape:
            print(f"警告：年份 {year} 的数据尺寸 {yearly_max[year]['data'].shape} 与其他年份 {target_shape} 不一致，将跳过该年份。")
            all_same_shape = False
            break
    
    if all_same_shape:
        # 堆叠所有年份数据
        period_data = [yearly_max[year]['data'] for year in years_selected]
        period_stack = np.stack(period_data, axis=0)
        max_data_selected = np.nanmax(period_stack, axis=0)
        print(f"已计算 {start_year}-{end_year} 年最大值 ({max_data_selected.shape[0]}×{max_data_selected.shape[1]})")
    else:
        print(f"警告：{start_year}-{end_year}时间段内数据尺寸不一致，无法计算多年最大值。")
        sys.exit(1)
else:
    print(f"警告：{start_year}-{end_year}时间段没有数据。")
    sys.exit(1)

# 保存结果为GeoTIFF文件
def save_tiff(output_path, data_array, meta):
    """辅助函数，用于保存GeoTIFF文件"""
    if data_array is None:
        print(f"警告：跳过保存 {output_path}，因为没有数据。")
        return
    
    # 更新元数据
    meta.update({
        'dtype': 'float32',
        'nodata': np.nan
    })
    
    with rasterio.open(output_path, 'w', **meta) as dst:
        dst.write(data_array.astype(np.float32), 1)
    print(f"文件已成功保存到: {output_path}")

if max_data_selected is not None and years_selected:
    # 使用第一个年份的元数据
    first_year = years_selected[0]
    save_tiff(output_file, max_data_selected, yearly_max[first_year]['meta'])

print("所有操作完成！")
