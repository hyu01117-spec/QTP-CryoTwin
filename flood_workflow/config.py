# -*- coding: utf-8 -*-
"""洪水危险性评估通用流程 —— 统一配置。

把本文件里的路径与参数改成你自己的数据位置即可；
三个步骤各自独立运行（见 README.md）：

    python step1_runoff.py            # 1) 径流生成（可选，已有径流数据可跳过）
    python step2_hazard.py            # 2) 危险性生成
    python step3_validation.py        # 3) 危险性验证

命令行参数可覆盖这里的默认值（时间 --start/--end 为必填，必须每次运行时指定）。
"""
from pathlib import Path

# =====================================================================
# 1) 径流生成（step1_runoff.py）
# =====================================================================
# 气象驱动目录：每个变量一个子目录，文件命名 {YYYYMMDD}_clipped.tif
METEOR_DIR = Path(r"F:\data\era5_land\wholeYarkant")
VARIABLES = [
    "temperature_2m",
    "temperature_2m_max",
    "total_precipitation_max",
    "total_precipitation_sum",
]
# LSTM 模型文件（训练脚本见 Training/input/train.py 对应结构）
LSTM_MODEL = Path(r"F:\data\models\lstm_runoff_model.pt")
# 径流输出目录（step1 写盘 / step2 读取）
RUNOFF_DIR = Path(r"F:\data\runoff_tifs")
# 是否翻转数值方向（本项目 predict.py 采用 max-(x-min) 修正；常规模型请设 False）
FLIP_DIRECTION = False

# =====================================================================
# 2) 危险性生成（step2_hazard.py）
# =====================================================================
# 阈值栅格目录：grid_threshold_{p}p.tif，由历史径流分位数预先算好
THRESHOLD_DIR = Path(r"F:\data\thresholds")
THRESHOLD_P = 99                     # 使用的阈值分位（10/50/90/95/99）
# 危险性输出目录
HAZARD_OUT_DIR = Path(r"F:\data\risk_results")
# 研究区边界（可选；None 则不做边界裁剪）
REGION_BOUNDARY = Path(r"F:\data\shapefiles\TP_China.geojson")
BASIN_NAME = "all"                   # 输出文件名中的流域标识

# =====================================================================
# 3) 危险性验证（step3_validation.py）
# =====================================================================
# 历史洪水点位：支持 SHP（用几何坐标）或 CSV（需要 lon/lat 两列）
POINTS_FILE = Path(r"F:\data\flood_points\2004_2025.shp")
POINTS_SOURCE_CRS = "EPSG:4326"      # 点位的原始坐标系
RADIUS_KM = 1.0                      # 位置容差（km；0=精确像元）
RESULT_DIR = Path(r"F:\data\validation_out")

# =====================================================================
# 时间范围 —— 参考示例值
# 注意：运行时时间必须由命令行 --start/--end 显式指定（必填），
# 下面的值只是文档示例，不会作为默认值使用。
# =====================================================================
START_DATE = "2024-01-01"
END_DATE = "2024-12-31"
