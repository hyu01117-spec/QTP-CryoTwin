import geopandas as gpd

# 读取本地 Shapefile
gdf = gpd.read_file(r"F:\data\agisdata\boundary\stations\China_Meteorological_Stations.shp")

# 保存为 GeoJSON
gdf.to_file(r"F:\data\青藏高原流域边界数据集（2016）\TP_Stations.geojson", driver="GeoJSON")
