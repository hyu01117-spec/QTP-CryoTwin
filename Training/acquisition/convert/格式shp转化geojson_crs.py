import geopandas as gpd

# 读取矢量文件"E:\data\shp\Yarkant.shp"
geodf = gpd.read_file(r"E:\data\青藏高原社会经济数据\水库水坝数据\GDW_clip\GDW_reservoirs_v1_0.shp")

# 设置坐标系（假设为 EPSG:4326）
geodf = geodf.set_crs("EPSG:4326")

# 保存带坐标系的矢量文件
geodf.to_file(r"E:\data\青藏高原社会经济数据\水库水坝数据\GDW_reservoirs_v1_0.geojson")
print("矢量文件的坐标系设置为 EPSG:4326")
