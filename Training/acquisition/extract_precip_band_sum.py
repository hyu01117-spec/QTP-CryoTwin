import os
import rasterio
import time
from tqdm import tqdm

# --------------------------
# 配置参数
# --------------------------
# 输入文件夹路径
input_folder_path = r"F:\TibetanPlateau\total_precipitation_sum_monthly"
# 输出文件夹路径
output_folder_path = r"F:\TibetanPlateau\total_precipitation_sum"
# 文件名前缀变量（可自定义，如"temp_2m_"）
file_prefix = "total_precipitation_sum_"  # 这里是要添加的变量名/前缀

# 创建输出文件夹（如果不存在）
os.makedirs(output_folder_path, exist_ok=True)

# --------------------------
# 单文件处理函数
# --------------------------
def process_single_file(file_name):
    if not file_name.lower().endswith(".tif"):
        return False, f"跳过非TIFF文件"
    
    input_file_full_path = os.path.join(input_folder_path, file_name)
    
    # 检查文件是否存在且可读
    try:
        if not os.path.exists(input_file_full_path):
            return False, f"文件不存在"
        
        # 检查文件权限
        if not os.access(input_file_full_path, os.R_OK):
            return False, f"无权限读取文件"
        
        # 检查文件大小
        file_size = os.path.getsize(input_file_full_path)
        if file_size == 0:
            return False, f"文件为空"
            
    except Exception as e:
        return False, f"检查文件信息时出错: {str(e)}"
    
    # 不使用重试机制的文件读取
    try:
        # 使用rasterio读取文件
        with rasterio.open(input_file_full_path) as src_dataset:
            # 读取文件基本信息
            width, height = src_dataset.width, src_dataset.height
            count = src_dataset.count
            
            # 判断文件大小，大文件逐波段读取，小文件一次性读取
            file_size_mb = os.path.getsize(input_file_full_path) / (1024 * 1024)
            if file_size_mb < 100:  # 小于100MB的文件一次性读取所有波段
                all_bands_data = src_dataset.read()
                
                # 批量写入文件
                written_count = 0
                for band_number in range(1, count + 1):
                    try:
                        # 获取波段描述
                        current_band_description = src_dataset.descriptions[band_number - 1] if band_number <= len(src_dataset.descriptions) else None
                        
                        # 提取带分隔符的日期
                        try:
                            date_with_separator = current_band_description.split("_")[1] if current_band_description else f"unknown_date_band{band_number}"
                        except (IndexError, TypeError) as e:
                            date_with_separator = f"unknown_date_band{band_number}"
                        
                        # 生成带变量名前缀的文件名
                        output_file_name = f"{file_prefix}{date_with_separator}.tif"
                        output_file_full_path = os.path.join(output_folder_path, output_file_name)
                        
                        # 更新元数据
                        single_band_metadata = src_dataset.meta.copy()
                        single_band_metadata.update(count=1)
                        
                        # 直接写入文件
                        with rasterio.open(output_file_full_path, 'w', **single_band_metadata) as dst_dataset:
                            dst_dataset.write(all_bands_data[band_number - 1], 1)
                        written_count += 1
                    except Exception as e:
                        continue
                
                return True, f"成功处理 {written_count} 个波段"
            else:
                # 大文件仍使用逐波段读取
                written_count = 0
                
                for band_number in range(1, count + 1):
                    try:
                        # 逐波段读取数据
                        single_band_data = src_dataset.read(band_number)
                    except Exception as e:
                        continue
                    
                    try:
                        # 获取波段描述
                        current_band_description = src_dataset.descriptions[band_number - 1] if band_number <= len(src_dataset.descriptions) else None
                        
                        # 提取带分隔符的日期
                        try:
                            date_with_separator = current_band_description.split("_")[1] if current_band_description else f"unknown_date_band{band_number}"
                        except (IndexError, TypeError) as e:
                            date_with_separator = f"unknown_date_band{band_number}"
                        
                        # 生成带变量名前缀的文件名
                        output_file_name = f"{file_prefix}{date_with_separator}.tif"
                        output_file_full_path = os.path.join(output_folder_path, output_file_name)
                        
                        # 更新元数据
                        single_band_metadata = src_dataset.meta.copy()
                        single_band_metadata.update(count=1)
                        
                        # 立即写入文件
                        with rasterio.open(output_file_full_path, 'w', **single_band_metadata) as dst_dataset:
                            dst_dataset.write(single_band_data, 1)
                        written_count += 1
                    except Exception as e:
                        continue
                
                return True, f"成功处理 {written_count} 个波段"
                
    except Exception as e:
        return False, f"打开或读取文件失败: {str(e)}"

# --------------------------
# 主程序：顺序处理所有文件
# --------------------------
if __name__ == "__main__":
    # 记录开始时间
    start_time = time.time()
    
    # 检查输入文件夹是否存在
    if not os.path.exists(input_folder_path):
        print(f"错误：输入文件夹不存在: {input_folder_path}")
        exit(1)
    
    # 获取所有需要处理的文件列表
    all_files = [f for f in os.listdir(input_folder_path) if f.lower().endswith(".tif")]
    total_files = len(all_files)
    print(f"发现 {total_files} 个TIFF文件需要处理")
    
    # 创建失败文件记录
    failed_log_path = os.path.join(output_folder_path, "failed_files.txt")
    
    # 顺序处理文件
    success_count = 0
    fail_count = 0
    failed_files = []
    
    try:
        # 逐个处理文件，显示当前正在处理的文件名
        for idx, file_name in enumerate(all_files, 1):
            print(f"正在处理文件 {idx}/{total_files}: {file_name}")
            try:
                success, message = process_single_file(file_name)
                if success:
                    success_count += 1
                    print(f"  ✓ 成功: {message}")
                else:
                    fail_count += 1
                    failed_files.append((file_name, message))
                    print(f"  ✗ 失败: {message}")
                    # 写入失败日志
                    with open(failed_log_path, "a", encoding="utf-8") as f:
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {file_name}: {message}\n")
                
            except Exception as e:
                fail_count += 1
                error_msg = f"处理时发生异常: {str(e)}"
                failed_files.append((file_name, error_msg))
                print(f"  ✗ 失败: {error_msg}")
                # 写入失败日志
                with open(failed_log_path, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {file_name}: {error_msg}\n")
    except Exception as e:
        print(f"程序执行出错: {str(e)}")
    
    # 任务结束后打印失败的文件名
    print("\n--- 处理结果摘要 ---")
    print(f"总文件数: {total_files}")
    print(f"成功处理: {success_count} 个文件")
    print(f"处理失败: {fail_count} 个文件")
    
    if failed_files:
        print("\n失败的文件列表：")
        for file_name, message in failed_files:
            print(f"- {file_name}: {message}")
    
    # 计算总耗时
    end_time = time.time()
    total_time = end_time - start_time
    print(f"\n总耗时: {total_time:.2f} 秒")