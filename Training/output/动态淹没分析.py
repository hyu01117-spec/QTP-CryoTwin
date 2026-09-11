import os
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from datetime import datetime

# 设置中文显示
plt.rcParams["font.family"] = ["SimHei"]
plt.rcParams['axes.unicode_minus'] = False

# === 输入和输出路径 ===
png_folder = r"F:\data\flood_pngs"
output_gif = r"F:\data\animations\flood_animation.gif"
output_mp4 = r"F:\data\animations\flood_animation.mp4"

# === 筛选1-12月PNG文件 ===
all_png_files = sorted([
    f for f in os.listdir(png_folder)
    if f.endswith(".png")
])

selected_files = []
for f in all_png_files:
    try:
        date_str = f[:10]  # 文件名前10位是日期
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        if 1 <= date_obj.month <= 12:
            selected_files.append(os.path.join(png_folder, f))
    except Exception:
        continue

print(f"✅ 共找到 {len(selected_files)} 张 1-12月洪水图像，用于动画生成")

# === 创建动画 ===
fig, ax = plt.subplots(figsize=(8, 6))
img_plot = plt.imshow(plt.imread(selected_files[0]))
plt.axis('off')
title = plt.title('')

def update(frame):
    img = plt.imread(selected_files[frame])
    img_plot.set_data(img)
    date_text = os.path.basename(selected_files[frame])[:10]
    title.set_text(f"叶尔羌河流域洪水演变 {date_text}")
    return [img_plot, title]

ani = animation.FuncAnimation(
    fig,
    update,
    frames=len(selected_files),
    interval=300,  # 每帧显示时间 毫秒
    blit=True
)

# === 保存为GIF ===
ani.save(output_gif, writer='pillow', fps=3)
print(f"✅ GIF 已保存: {output_gif}")

# === 保存为MP4 ===
ani.save(output_mp4, writer='ffmpeg', fps=3)
print(f"✅ MP4 已保存: {output_mp4}")

plt.close()
