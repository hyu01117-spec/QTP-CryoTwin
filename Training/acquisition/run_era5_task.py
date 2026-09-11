from webgis_backend.modules.meteorology.processor import \
    process_era5
import os

if __name__ == "__main__":
    start_date = '2022-07-01'
    end_date = '2022-07-01'

    raw_nc_path = f'data/raw/era5/era5_raw_{start_date}.nc'
    clipped_nc_path = f'data/processed/era5/era5_clipped_{start_date}.nc'

    # 自动创建目录（如果不存在）
    os.makedirs(os.path.dirname(raw_nc_path), exist_ok=True)
    os.makedirs(os.path.dirname(clipped_nc_path),
                exist_ok=True)

    print(f"处理 ERA5 数据：{start_date} - {end_date}")
    process_era5(start_date, end_date, raw_nc_path,
                 clipped_nc_path)
    print("处理完成。")
