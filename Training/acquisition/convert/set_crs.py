import geopandas as gpd
import numpy as np

# 读取矢量文件"E:\data\省界、国界\base\rai_4m(主要铁路)\rai_4m.shp"
geodf = gpd.read_file(r"E:\data\省界、国界\base\roa_4m(主要公路)\roa_4m.shp")

# 检查原始CRS并设置正确的坐标系
print(f"原始CRS: {geodf.crs}")

# 如果原始CRS为None，假设为WGS84 (EPSG:4326)
if geodf.crs is None:
    geodf = geodf.set_crs("EPSG:4326")
    print("已设置默认CRS为EPSG:4326")

# 转换为Web墨卡托投影（EPSG:3857）
geodf = geodf.to_crs("EPSG:3857")

# 验证坐标有效性并替换异常值
geodf.geometry = geodf.geometry.apply(lambda geom: np.nan if geom is None else geom)
geodf = geodf.dropna(subset=['geometry'])

# 检查坐标范围
minx, miny, maxx, maxy = geodf.total_bounds
print(f"转换后坐标范围: {minx:.2f},{miny:.2f} 到 {maxx:.2f},{maxy:.2f}")

# 保存带坐标系的矢量文件
geodf.to_file(r"E:\data\省界、国界\roa_4m.geojson", driver='GeoJSON')
print("矢量文件的坐标系设置为 EPSG:4326")

