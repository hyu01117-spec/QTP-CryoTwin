import rasterio
import geopandas as gpd
from rasterio.features import shapes
import numpy as np
import os

def tif_to_geojson(input_tif, output_geojson, threshold=0):
    """
    将TIF栅格文件转换为GeoJSON矢量文件
    
    参数:
    input_tif (str): 输入TIF文件路径
    output_geojson (str): 输出GeoJSON文件路径
    threshold (float): 阈值，值大于此阈值的像素将被转换为矢量
    """
    try:
        # 打开TIF文件
        with rasterio.open(input_tif) as src:
            # 读取数据
            data = src.read(1)
            # 获取空间转换信息
            transform = src.transform
            
            # 创建掩码，只转换值大于阈值的像素
            mask = data > threshold
            
            # 将栅格转换为矢量形状
            shapes_list = []
            for geom, val in shapes(data, mask=mask, transform=transform):
                # 确保几何体有效
                if geom is not None and val > threshold:
                    shapes_list.append({"geometry": geom, "properties": {"value": float(val)}})
            
            # 创建GeoDataFrame
            if shapes_list:
                gdf = gpd.GeoDataFrame(shapes_list)
                # 设置坐标系
                if src.crs:
                    gdf = gdf.set_crs(src.crs)
                else:
                    # 如果原始文件没有CRS信息，默认设置为WGS84
                    gdf = gdf.set_crs("EPSG:4326")
                
                # 保存为GeoJSON
                gdf.to_file(output_geojson, driver="GeoJSON")
                print(f"成功将 {input_tif} 转换为 {output_geojson}")
            else:
                print(f"警告: 没有找到符合条件的数据进行转换")
                # 创建空的GeoJSON文件
                empty_gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
                empty_gdf.to_file(output_geojson, driver="GeoJSON")
                print(f"已创建空的GeoJSON文件: {output_geojson}")
                
    except Exception as e:
        print(f"转换失败: {str(e)}")

if __name__ == "__main__":
    # 示例用法
    # 可以修改为冰川和多年冻土的TIF文件路径
    input_tif = r"E:\data\TP_China\TP_Perma_Distr_map\Perma_Distr_map21.tif"
    output_geojson = r"E:\data\TP_China\Perma_Distr_map21.geojson"
    
    # 确保输出目录存在
    os.makedirs(os.path.dirname(output_geojson), exist_ok=True)
    
    # 调用转换函数
    tif_to_geojson(input_tif, output_geojson, threshold=0)
    
