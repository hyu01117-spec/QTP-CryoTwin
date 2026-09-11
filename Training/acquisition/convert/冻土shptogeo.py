# 读取季节性冻土Shapefile (类型0)
gdf0 = gpd.read_file(r"E:\data\TP_China\Raster2\RasterT_tif0.shp")
gdf0.to_file(r"E:\data\TP_China\TP_China_permafrost0.geojson", driver="GeoJSON")

# 读取永久冻土Shapefile (类型1)
gdf1 = gpd.read_file(r"E:\data\TP_China\Raster2\RasterT_tif1.shp")
gdf1.to_file(r"E:\data\TP_China\TP_China_permafrost1.geojson", driver="GeoJSON")

# 读取未冻结土地Shapefile (类型2)
gdf2 = gpd.read_file(r"E:\data\TP_China\Raster2\RasterT_tif2.shp")
gdf2.to_file(r"E:\data\TP_China\TP_China_permafrost2.geojson", driver="GeoJSON")