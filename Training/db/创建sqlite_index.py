import os
import sqlite3
import rasterio
from tqdm import tqdm

# === 配置路径 ===
root_dir = r"E:\YHH\Cryo-floods\data\raw\TibetanPlateau"  # ERA5 数据根目录
db_path = r"E:\YHH\Cryo-floods\data\processed\db\TibetanPlateau_index.db"  # SQLite 数据库文件

# === 初始化数据库 ===
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS TibetanPlateau_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variable TEXT NOT NULL,
    date TEXT NOT NULL,
    file_path TEXT NOT NULL,
    bbox_minx REAL,
    bbox_miny REAL,
    bbox_maxx REAL,
    bbox_maxy REAL,
    bands INTEGER,
    crs TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
''')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_var_date ON TibetanPlateau_index(variable, date);')
conn.commit()

# === 扫描目录并写入数据库 ===
def scan_and_index():
    # 使用tqdm显示进度
    total_files = 0
    processed_files = 0
    
    # 先计算总文件数
    for root, dirs, files in os.walk(root_dir):
        total_files += len([f for f in files if f.endswith(".tif")])
    
    print(f"开始索引 {total_files} 个文件...")
    
    # 开始处理文件
    with tqdm(total=total_files) as pbar:
        # 遍历所有层级的目录
        for root, dirs, files in os.walk(root_dir):
            for f in files:
                if f.endswith(".tif"):
                    file_path = os.path.join(root, f)
                    try:
                        # 提取日期，格式为temperature_2m_2024-01-01.tif
                        name_parts = f.replace('.tif', '')
                        parts = name_parts.split('_')
                        if len(parts) >= 3:
                            # 日期在倒数第一个位置
                            date_part = parts[-1]  # 2024-01-01
                            
                            # 提取变量名（去掉日期部分）
                            variable_name = '_'.join(parts[:-1])  # temperature_2m
                            
                            # 也可以从目录路径中提取变量名（如果需要）
                            # 例如：从路径 "F:\put\output\TibetanPlateau\temperature_2m\temperature_2m_2024-01-01.tif" 中提取
                            path_parts = os.path.normpath(root).split(os.sep)
                            if len(path_parts) > 0:
                                dir_variable_name = path_parts[-1]  # 获取最后一级目录名
                                # 可以选择使用文件名中的变量名或目录名
                                # 这里使用文件名中的变量名，因为它更完整
                                # 如果需要使用目录名，可以取消下面这行的注释
                                # variable_name = dir_variable_name
                                
                                print(f"调试：文件变量名={variable_name}, 目录变量名={dir_variable_name}")
                            
                            with rasterio.open(file_path) as src:
                                bbox = src.bounds
                                bands = src.count
                                crs = src.crs.to_string() if src.crs else "Unknown"
                            # 插入数据库
                            cursor.execute('''
                                INSERT INTO TibetanPlateau_index 
                                (variable, date, file_path, bbox_minx, bbox_miny, bbox_maxx, bbox_maxy, bands, crs)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (variable_name, date_part, file_path,
                                  bbox.left, bbox.bottom, bbox.right, bbox.top, bands, crs))
                            processed_files += 1
                            print(f"✅ 已索引: {file_path} (变量: {variable_name}, 日期: {date_part})")
                        else:
                            print(f"⚠️ 文件名格式不正确，跳过: {file_path}")
                    except Exception as e:
                        print(f"⚠️ 处理 {file_path} 时出错: {str(e)}")
                    finally:
                        pbar.update(1)
    
    conn.commit()
    print(f"索引完成！成功处理 {processed_files} 个文件。")

if __name__ == "__main__":
    try:
        scan_and_index()
    except Exception as e:
        print(f"❌ 程序运行出错: {str(e)}")
    finally:
        conn.close()
        print("数据库连接已关闭")
