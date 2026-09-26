import os
import rasterio

# --------------------------
# 配置参数
# --------------------------
# 输入文件夹路径
input_folder_path = r"F:\TibetanPlateau\temperature_2m_monthly"
# 输出文件夹路径
output_folder_path = r"F:\TibetanPlateau\temperature_2m"
# 文件名前缀变量（可自定义，如"temp_2m_"）
file_prefix = "temperature_2m_"  # 这里是要添加的变量名/前缀

# 创建输出文件夹（如果不存在）
os.makedirs(output_folder_path, exist_ok=True)

# --------------------------
# 遍历输入文件
# --------------------------
for file_name in os.listdir(input_folder_path):
    if file_name.lower().endswith(".tif"):
        input_file_full_path = os.path.join(
            input_folder_path, file_name)

        # 读取多波段TIFF
        with rasterio.open(
                input_file_full_path) as src_dataset:
            all_bands_data = src_dataset.read()
            original_metadata = src_dataset.meta
            all_band_descriptions = src_dataset.descriptions

            # 遍历每个波段
            for band_number, single_band_data in enumerate(
                    all_bands_data, start=1):
                current_band_description = \
                all_band_descriptions[band_number - 1]

                # 提取带分隔符的日期
                try:
                    date_with_separator = \
                    current_band_description.split("_")[1]
                except (IndexError, TypeError) as e:
                    print(
                        f"警告：文件 {file_name} 波段 {band_number} 描述异常（{current_band_description}），使用默认命名")
                    date_with_separator = f"unknown_date_band{band_number}"

                # --------------------------
                # 生成带变量名前缀的文件名
                # --------------------------
                # 格式：[前缀变量名] + [日期] + .tif
                output_file_name = f"{file_prefix}{date_with_separator}.tif"
                output_file_full_path = os.path.join(
                    output_folder_path, output_file_name)

                # 保存单波段文件
                print(
                    f"处理文件：{file_name}，波段 {band_number}，输出文件名：{output_file_name}")
                single_band_metadata = original_metadata.copy()
                single_band_metadata.update(count=1)

                with rasterio.open(output_file_full_path,
                                   'w',
                                   **single_band_metadata) as dst_dataset:
                    dst_dataset.write(single_band_data, 1)

                print(f"已保存：{output_file_full_path}\n")

print("所有文件处理完成！")