import os
import sqlite3
import rasterio
from tqdm import tqdm

# === 配置路径 ===
db_path = r"F:\Cryo-floods\data\processed\era5_index.db"
flood_mask_dir = r"F:\data\flood_masks"  
flood_png_dir = r"F:\data\flood_pngs"

# === 连接数据库 ===
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# === 建表 ===
cursor.execute('''
CREATE TABLE IF NOT EXISTS flood_mask_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
cursor.execute('CREATE INDEX IF NOT EXISTS idx_flood_mask_date ON flood_mask_index(date);')

cursor.execute('''
CREATE TABLE IF NOT EXISTS flood_png_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    file_path TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
''')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_flood_png_date ON flood_png_index(date);')
conn.commit()

# === 扫描并写入 flood_mask_index ===
print("📦 开始索引洪水掩码 TIF 文件")
for f in tqdm(os.listdir(flood_mask_dir)):
    if f.endswith(".tif"):
        file_path = os.path.join(flood_mask_dir, f)
        try:
            date_str = f[:10]  # 假设文件名开头是 YYYY-MM-DD
            with rasterio.open(file_path) as src:
                bbox = src.bounds
                bands = src.count
                crs = src.crs.to_string()
            cursor.execute('''
                INSERT INTO flood_mask_index 
                (date, file_path, bbox_minx, bbox_miny, bbox_maxx, bbox_maxy, bands, crs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (date_str, file_path, bbox.left, bbox.bottom, bbox.right, bbox.top, bands, crs))
        except Exception as e:
            print(f"⚠️ 跳过 {file_path}，错误: {e}")
conn.commit()
print("✅ 洪水掩码 TIF 索引完成")

# === 扫描并写入 flood_png_index ===
print("📦 开始索引洪水图 PNG 文件")
for f in tqdm(os.listdir(flood_png_dir)):
    if f.endswith(".png"):
        file_path = os.path.join(flood_png_dir, f)
        try:
            date_str = f[:10]  # 假设文件名开头是 YYYY-MM-DD
            cursor.execute('''
                INSERT INTO flood_png_index (date, file_path)
                VALUES (?, ?)
            ''', (date_str, file_path))
        except Exception as e:
            print(f"⚠️ 跳过 {file_path}，错误: {e}")
conn.commit()
print("✅ 洪水图 PNG 索引完成")

conn.close()
print("🎉 所有索引已写入数据库")
