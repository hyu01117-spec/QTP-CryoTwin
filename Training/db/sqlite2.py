import sqlite3
import os

# 数据库路径
db_path = r"F:\Cryo-floods\data\processed\db\era5_index.db"
# 根目录（存TIF的地方）
tif_root_dir = r"F:\data\era5_land\wholeYarkant"

# 转换路径分隔符
tif_root_dir = os.path.abspath(tif_root_dir).replace("\\", "/")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 查询所有 file_path
cursor.execute("SELECT id, file_path FROM era5_index")
rows = cursor.fetchall()

updated = 0
for id, file_path in rows:
    file_path = file_path.replace("\\", "/")
    if file_path.startswith(tif_root_dir):
        relative_path = file_path[len(tif_root_dir):].lstrip("/")
        # 更新数据库
        cursor.execute("UPDATE era5_index SET file_path=? WHERE id=?", (relative_path, id))
        updated += 1

conn.commit()
conn.close()
print(f"已更新 {updated} 条记录，全部改为相对路径")
