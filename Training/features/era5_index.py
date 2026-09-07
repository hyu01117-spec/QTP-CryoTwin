import os
import sqlite3
import rasterio
from tqdm import tqdm

# === 配置路径 ===
root_dir = r"F:\data\era5_land\wholeYarkant"  # ERA5 数据根目录
db_path = r"F:\Cryo-floods\data\processed\era5_index.db"                      # SQLite 数据库文件

# === 初始化数据库 ===
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS era5_index (
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
cursor.execute('CREATE INDEX IF NOT EXISTS idx_var_date ON era5_index(variable, date);')
conn.commit()

# === 扫描目录并写入数据库 ===
def scan_and_index():
    for var_name in os.listdir(root_dir):  # 遍历每个变量文件夹
        var_dir = os.path.join(root_dir, var_name)
        if not os.path.isdir(var_dir):
            continue
        for dirpath, _, files in os.walk(var_dir):
            for f in files:
                if f.endswith(".tif") and "clipped" in f:
                    file_path = os.path.join(dirpath, f)
                    try:
                        # 提取日期
                        parts = f.split('_')
                        date_part = parts[1]  # 2024-12-30
                        with rasterio.open(file_path) as src:
                            bbox = src.bounds
                            bands = src.count
                            crs = src.crs.to_string()
                        # 插入数据库
                        cursor.execute('''
                            INSERT INTO era5_index 
                            (variable, date, file_path, bbox_minx, bbox_miny, bbox_maxx, bbox_maxy, bands, crs)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (var_name, date_part, file_path,
                              bbox.left, bbox.bottom, bbox.right, bbox.top, bands, crs))
                        print(f"✅ 已索引: {file_path}")
                    except Exception as e:
                        print(f"⚠️ 跳过 {file_path}，错误: {e}")
    conn.commit()

scan_and_index()
conn.close()
