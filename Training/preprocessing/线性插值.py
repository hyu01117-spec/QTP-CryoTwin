import os
import xarray as xr
import rioxarray
import pandas as pd

# === 1. 设定数据路径和目标日期 ===
tif_dir = r'F:\data\era5_land\wholeYarkant\temperature_2m'  # 包含1973-12-30 和 1974-01-01两个tif
date_files = {
    '2021-06-11': os.path.join(tif_dir, '20210611_2021-06-11_clipped.tif'),
    '2021-06-13': os.path.join(tif_dir, '20210613_2021-06-13_clipped.tif')
}
target_date = '2021-06-12'

# === 2. 读取数据并构建时间序列 ===
data_list = []
times = []

for date_str, file_path in date_files.items():
    da = rioxarray.open_rasterio(file_path).squeeze()
    da = da.assign_coords(time=pd.Timestamp(date_str))
    data_list.append(da)
    times.append(pd.Timestamp(date_str))

# 合并为一个带时间维度的DataArray
combined = xr.concat(data_list, dim='time')

# === 3. 进行时间插值 ===
interp_time = pd.Timestamp(target_date)
interp_result = combined.interp(time=interp_time)

# === 4. 添加空间参考信息以保存为 GeoTIFF ===
interp_result = interp_result.expand_dims(dim="band").assign_coords(band=[1])  # 还原 band 维度
interp_result.rio.set_crs(combined.rio.crs, inplace=True)
interp_result.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)

# === 5. 导出为 GeoTIFF ===
# 生成符合格式要求的输出文件名
date_part1 = target_date.replace("-", "")
date_part2 = target_date
output_file = os.path.join(tif_dir, f'{date_part1}_{date_part2}_clipped.tif')
interp_result.rio.to_raster(output_file)

print(f"✅ 插值完成，文件已保存到：{output_file}")