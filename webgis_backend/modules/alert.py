import os
import re
import numpy as np
import rasterio
import geopandas as gpd
from flask import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta
import traceback


alert_bp = Blueprint("alert", __name__, url_prefix="/api/alert")

# ==================== 洪水灾害风险评估 ====================
import pandas as pd
from scipy.spatial import cKDTree

# 风险等级定义（5级）
# 注意：min/max 仅为展示用的"相对分级示意区间"，实际分级由 _classify_into_levels
# 按淹没区内风险值分位数（q20/q40/q60/q80）确定，min/max 不参与计算。
RISK_LEVELS = [
    {"level": 1, "name": "低", "min": 0.0, "max": 0.2, "color": "#2c7bb6"},
    {"level": 2, "name": "较低", "min": 0.2, "max": 0.4, "color": "#abd9e9"},
    {"level": 3, "name": "中", "min": 0.4, "max": 0.6, "color": "#ffffbf"},
    {"level": 4, "name": "较高", "min": 0.6, "max": 0.8, "color": "#fdae61"},
    {"level": 5, "name": "高", "min": 0.8, "max": 1.01, "color": "#d7191c"},
]

# 脆弱性常数（项目无人口/土地利用/建筑结构等真实脆弱性数据，采用简化常数）
VULNERABILITY_CONST = 0.5


def _normalize(arr, valid_mask):
    """对 valid_mask 内的值做 min-max 归一化到 [0,1]，其余置 0"""
    out = np.zeros_like(arr, dtype=np.float32)
    vals = arr[valid_mask]
    if vals.size == 0:
        return out
    vmin, vmax = float(np.min(vals)), float(np.max(vals))
    if vmax <= vmin:
        out[valid_mask] = 1.0 if vmax > 0 else 0.0
    else:
        out[valid_mask] = (arr[valid_mask] - vmin) / (vmax - vmin)
    return out


def _parse_weights(raw, n, names, key):
    """
    解析并归一化权重向量。

    入参 raw 可为：
      - None            -> 返回 None（调用方沿用规范默认模型，零回归）
      - list/tuple(长度n) -> 按位置对应 names
      - dict            -> 必须包含 names 中全部键
    校验：必须为有限非负数；不能全为 0；长度/键名必须匹配。
    返回：归一化后的 np.float64 数组（和=1）；raw 为 None 时返回 None。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        if not all(nm in raw for nm in names):
            missing = [nm for nm in names if nm not in raw]
            raise ValueError(f"权重 {key} 缺少维度: {missing}")
        seq = [raw[nm] for nm in names]
    elif isinstance(raw, (list, tuple)):
        if len(raw) != n:
            raise ValueError(f"权重 {key} 应为长度 {n} 的数组/对象，实际长度 {len(raw)}")
        seq = list(raw)
    else:
        raise ValueError(f"权重 {key} 格式不合法（应为数组或对象）")
    try:
        arr = np.array([float(x) for x in seq], dtype=np.float64)
    except (TypeError, ValueError):
        raise ValueError(f"权重 {key} 包含非数值元素")
    if not np.all(np.isfinite(arr)) or np.any(arr < 0):
        raise ValueError(f"权重 {key} 必须为非负有限数（不支持负数）")
    total = float(arr.sum())
    if total <= 0:
        raise ValueError(f"权重 {key} 不能全为 0")
    return arr / total


def _weighted_geometric_mean(factors, weights):
    """
    加权几何平均：Risk = ∏ f_i ** w_i，权重已归一化且 ≥ 0。
    这是乘法风险模型 Risk = H×E×V 的规范加权推广（等权时退化为几何平均）。
    对因子值 f≤0：若对应权重 w>0 则该项贡献 0（与"无危险性即无风险"一致）；
    若 w==0 则该因子中性（乘 1）。仅在淹没区内计算，结果 dtype=float32。
    """
    out = np.ones_like(factors[0], dtype=np.float32)
    for f, w in zip(factors, weights):
        if w <= 0:
            continue
        f32 = f.astype(np.float32)
        # f>0 -> exp(w*ln(f))；f==0 且 w>0 -> 0（用 errstate 屏蔽 ln(0) 的除零告警）
        with np.errstate(divide='ignore', invalid='ignore'):
            logf = np.log(f32)
        term = np.where(f32 > 0, np.exp(w * logf), 0.0).astype(np.float32)
        out = out * term
    return out


def _entropy_weights(grids, mask, eps=1e-9):
    """
    熵权法客观赋权（标准定义）：依据各指标(栅格)在 mask 区域内取值的离散程度确定权重。
    离散程度越高（判别信息越多）-> 信息熵越小 -> 权重越大。

    标准熵权法归一化轴：对每个指标 j（行），按像元 i（列）归一化
        p_ij = x_ij / Σ_i x_ij ,  e_j = -k · Σ_i p_ij·ln(p_ij) ,  k = 1/ln(n)
    权重 w_j = (1 - e_j) / Σ_j (1 - e_j)。
    注意：归一化必须沿"像元"轴(axis=1)，而非"指标"轴(axis=0)；沿指标轴归一会退化为恒定等权。

    入参:
      grids: 长度 m 的归一化栅格列表（取值建议∈[0,1]）；mask: 有效像元布尔数组。
    返回:
      长度 m、元素和=1 的权重向量(np.float32)。
      退化情形（有效像元<2、全零维度、全部为常数）回退等权，避免权重塌缩为 NaN。
    """
    m = len(grids)
    if m == 0:
        return np.array([], dtype=np.float32)
    stacked = np.stack([g.astype(np.float32) for g in grids], axis=0)  # (m, H, W)
    vals = stacked[:, mask]                                            # (m, n)  m=指标, n=像元
    n = vals.shape[1]
    if n < 2:
        return np.full(m, 1.0 / m, dtype=np.float32)
    # 各指标(行)在像元(列)上的总量，用于列(指标)归一化并识别无信息维度
    row_tot = vals.sum(axis=1)                                         # (m,)
    safe = np.where(row_tot[:, None] > 0, row_tot[:, None], 1.0)
    p = vals / safe                                                    # p_ij = x_ij / Σ_i x_ij（标准熵权法：按指标对像元归一）
    with np.errstate(divide='ignore', invalid='ignore'):
        plnp = np.where(p > 0, p * np.log(p), 0.0)
    k = 1.0 / np.log(n)
    e = np.clip(-k * plnp.sum(axis=1), 0.0, 1.0)                       # (m,) 信息熵∈[0,1]
    d = 1.0 - e                                                        # 信息效用
    d = np.where(row_tot > eps, d, 0.0)                                # 全零维度无信息 -> 权重 0
    s = float(d.sum())
    if s <= eps:
        return np.full(m, 1.0 / m, dtype=np.float32)
    return (d / s).astype(np.float32)


# ==================== 顶层 H/E/V 赋权：熵权 × AHP 先验混合 ====================
# 背景：盲熵权用于"危险性/暴露度/脆弱性"三支柱会把结构性因子(Hazard/Vulnerability)判为 ~0，
#       且权重随评估时段剧烈漂移（实证 H0/E57/V43 ↔ H1/E99/V0），不可用于风险建模。
# 做法：w_j = α·w^0_j + (1-α)·w^e_j（凸组合，归一化）。主观先验 w^0 锚定物理结构，
#       α 为先验强度；因 w^0_j>0 且 α>0，任何支柱都不会被数据判为 0（避免零化 Hazard）。
# 顺序固定为 [Hazard, Exposure, Vulnerability]，须与 _entropy_weights 的入参栈顺序一致。
RISK_PRIOR_WEIGHTS = np.array([0.40, 0.35, 0.25], dtype=np.float32)  # [H, E, V]（文献/专家先验，可调）
RISK_PRIOR_STRENGTH = 0.60  # α：先验占比；越大越依赖专家/AHP 先验，越小越依赖数据离散度


def _combined_ahp_entropy(prior, entropy_w, alpha=RISK_PRIOR_STRENGTH, eps=1e-9):
    """熵权(客观)与主观先验(AHP/专家)的凸组合赋权（混合权重）。

    入参:
      prior:    长度 m、和≈1 的主观先验权重向量
      entropy_w:长度 m、和≈1 的熵权法(客观)权重向量
      alpha:    先验强度 ∈[0,1]
    返回:
      归一化后的混合权重(np.float32)；任一分量 ≥ alpha·min(prior)>0（不被数据零化）。
    说明:
      采用线性凸组合而非乘性组合——乘性组合在熵权给出精确 0（如 V=0%）时会把该维度
      直接乘成 0，复现"零化"bug；凸组合从根本上保证每个支柱保有 ≥α·先验 的下限权重。
    """
    p = np.asarray(prior, dtype=np.float32)
    e = np.asarray(entropy_w, dtype=np.float32)
    if p.size != e.size or p.size == 0:
        return e.astype(np.float32)
    a = float(np.clip(alpha, 0.0, 1.0))
    w = a * p + (1.0 - a) * e
    s = float(w.sum())
    if s <= eps:
        return e.astype(np.float32)
    return (w / s).astype(np.float32)


def _load_county_gdp_points(target_crs):
    """
    加载县区 GDP 点并转换到 target_crs。
    数据来源：
      - 坐标/名称：data/raw/geo/socioeconomic/economy_counties.geojson (EPSG:3857, Point)
      - GDP 值：data/raw/economy/GDP_data.csv (county, 2000, 2001, ...)
    匹配键：geojson.PYNAME 去空格小写 ↔ csv.county 去空格小写
    返回：np.array shape=(N,2) 坐标(在target_crs下), np.array shape=(N,) gdp值
    """
    base_dir = current_app.config["BASE_DIR"]
    geo_path = os.path.join(base_dir, "data", "raw", "geo", "economy_counties.geojson")
    csv_path = os.path.join(base_dir, "data", "raw", "economy", "GDP_data.csv")

    if not os.path.exists(geo_path):
        raise FileNotFoundError(f"县区矢量不存在: {geo_path}")
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"GDP CSV 不存在: {csv_path}")

    gdf = gpd.read_file(geo_path)
    if gdf.crs is None:
        gdf.set_crs("EPSG:3857", inplace=True)
    gdf = gdf.to_crs(target_crs)

    df = pd.read_csv(csv_path)
    year_cols = [c for c in df.columns if str(c).isdigit()]
    if not year_cols:
        raise ValueError("GDP_data.csv 未找到年份列")

    # csv county -> 最新GDP
    def _latest_gdp(row):
        """取该行最新年份的 GDP（单位统一为万元）。

        单位推断：GDP_data.csv 存在单位混用（如个别县仅在部分年份标注"亿元"，
        其余年份为无后缀明文但同为该单位）。为避免误判，按*整行*推断单位：
          - 若任一年份单元格含"亿元" → 整行视为亿元（明文年份亦按亿元处理）；
          - 否则若含"万元" → 视为万元；
          - 否则默认万元（与全国县域 GDP 量级一致）。
        此推断可修正德钦县等"2000 标亿元、后续明文亦为亿元"导致的 ~1e4× 低估。
        """
        cells = [str(row[y]) for y in year_cols if not pd.isna(row[y])]
        row_has_yi = any("亿元" in c for c in cells)
        # 无"亿元"标记的行（含纯明文或"万元"）一律按万元处理
        row_unit_to_wan = 10000.0 if row_has_yi else 1.0
        vals = {}
        for y in year_cols:
            v = row[y]
            if pd.isna(v):
                continue
            s = str(v).strip()
            try:
                if "亿元" in s:
                    vals[int(y)] = float(s.replace("亿元", "").strip()) * 10000
                elif "万元" in s:
                    vals[int(y)] = float(s.replace("万元", "").strip())
                else:
                    # 无单位明文：沿用整行推断单位
                    vals[int(y)] = float(s) * row_unit_to_wan
            except ValueError:
                continue
        if not vals:
            return None
        return vals[max(vals.keys())]

    df["_gdp"] = df.apply(_latest_gdp, axis=1)
    df["_key"] = df["county"].astype(str).str.replace(r"\s+", "", regex=True).str.lower()

    # geojson PYNAME -> key
    gdf["_key"] = gdf["PYNAME"].astype(str).str.replace(r"\s+", "", regex=True).str.lower()

    merged = gdf.merge(df[["_key", "_gdp"]], on="_key", how="inner")
    merged = merged.dropna(subset=["_gdp"])
    merged = merged[merged["_gdp"] > 0]

    if len(merged) == 0:
        # 退化为中文名匹配
        df["_key2"] = df["county"].astype(str).str.strip()
        gdf["_key2"] = gdf["NAME_DECODED"].astype(str).str.strip()
        merged = gdf.merge(df[["_key2", "_gdp"]], on="_key2", how="inner")
        merged = merged.dropna(subset=["_gdp"])
        merged = merged[merged["_gdp"] > 0]

    if len(merged) == 0:
        return np.empty((0, 2)), np.empty((0,))

    coords = np.array([(g.x, g.y) for g in merged.geometry], dtype=np.float64)
    gdp = merged["_gdp"].astype(np.float64).values
    return coords, gdp


def _load_county_population_points(target_crs):
    """
    加载县域人口点并转换到 target_crs（与 GDP 点一致的空间基准）。
    数据来源：
      - 人口值：青藏高原1980-2020年县域人口数据.xlsx（{县名: 最新年份人口}）
      - 坐标/名称：data/raw/geo/socioeconomic/economy_counties.geojson (EPSG:3857, Point)
    匹配键：人口县名(规范化) ↔ geojson.NAME_DECODED/PYNAME(规范化)
    返回：np.array shape=(N,2) 坐标(在target_crs下), np.array shape=(N,) 人口值
    """
    base_dir = current_app.config["BASE_DIR"]
    geo_path = os.path.join(base_dir, "data", "raw", "geo", "economy_counties.geojson")
    if not os.path.exists(geo_path):
        raise FileNotFoundError(f"县区矢量不存在: {geo_path}")

    pop_map = _get_pop_map_cached()
    if not pop_map:
        return np.empty((0, 2)), np.empty((0,))

    gdf = gpd.read_file(geo_path)
    if gdf.crs is None:
        gdf.set_crs("EPSG:3857", inplace=True)
    gdf = gdf.to_crs(target_crs)

    # 规范化人口县名 -> 值
    pop_norm = {_norm_county_name(k): v for k, v in pop_map.items()}
    # 兜底：完全去空格后的键（用于地名写法差异，如"西宁"vs"西宁市"）
    pop_plain = {str(k).strip().replace(" ", "").replace("\u3000", ""): v for k, v in pop_map.items()}

    def _county_value(row):
        for key in ("NAME_DECODED", "NAME", "name", "PYNAME"):
            val = row.get(key)
            if val is None:
                continue
            v = pop_norm.get(_norm_county_name(val))
            if v is not None:
                return v
        # 兜底：包含匹配（geojson 名与 xlsx 名互为子串且长度足够，如带省/州前缀）
        gkey = _norm_county_name(str(row.get("NAME_DECODED") or ""))
        if len(gkey) >= 3:
            for pk, pv in pop_plain.items():
                pk_norm = _norm_county_name(pk)
                if len(pk_norm) >= 3 and (gkey in pk_norm or pk_norm in gkey):
                    return pv
        return None

    gdf["_pop"] = gdf.apply(_county_value, axis=1)
    merged = gdf.dropna(subset=["_pop"])
    merged = merged[merged["_pop"] > 0]

    if len(merged) == 0:
        current_app.logger.warning("人口 xlsx 与县区矢量未能匹配到任何县域人口点")
        return np.empty((0, 2)), np.empty((0,))

    coords = np.array([(g.x, g.y) for g in merged.geometry], dtype=np.float64)
    pops = merged["_pop"].astype(np.float64).values
    current_app.logger.info(f"县域人口点匹配成功: {len(pops)} 县")
    return coords, pops


def _idw_interpolate(points_xy, values, dst_shape, dst_transform, k=8, power=2.0):
    """
    反距离加权插值：将散点插值到目标栅格。
    points_xy: (N,2) 在目标栅格CRS下的坐标
    values: (N,) 属性值
    dst_shape: (height, width)
    dst_transform: rasterio transform
    """
    h, w = dst_shape
    if len(points_xy) == 0:
        return np.zeros((h, w), dtype=np.float32)

    # 生成栅格像元中心坐标
    xs = np.arange(w)
    ys = np.arange(h)
    gx, gy = np.meshgrid(xs, ys)
    # transform: [a, b, c, d, e, f] -> x = a*col + c, y = e*row + f
    cx = dst_transform[2] + (gx + 0.5) * dst_transform[0]
    cy = dst_transform[5] + (gy + 0.5) * dst_transform[4]
    grid_pts = np.column_stack([cx.ravel(), cy.ravel()])

    k_use = min(k, len(points_xy))
    tree = cKDTree(points_xy)
    dist, idx = tree.query(grid_pts, k=k_use)

    if k_use == 1:
        dist = dist[:, None]
        idx = idx[:, None]

    # 避免除零
    dist = np.maximum(dist, 1e-9)
    weights = 1.0 / np.power(dist, power)
    vals = values[idx]
    result = np.sum(weights * vals, axis=1) / np.sum(weights, axis=1)
    return result.reshape(h, w).astype(np.float32)


# ==================== 脆弱性：青藏高原县域社会经济脆弱性数据集（SV_QTP） ====================
# 数据路径：data/raw/socioeconomy/vulnerability_2000_2020/dataset/spatial_distribution/
# 注：该目录树原名全为中文（含全角括号），2026-09-07 统一改为英文，避免 Windows 路径问题
SV_SHP_RELPATH = os.path.join(
    "data", "raw", "socioeconomy",
    "vulnerability_2000_2020",
    "dataset",
    "spatial_distribution",
    "SV_QTP_2000-2020.shp",
)
# 县域人口数据路径（1980-2020 年）
POP_XLSX_RELPATH = os.path.join(
    "data", "raw", "socioeconomy",
    "county_stats_1980_2020",
    "青藏高原1980-2020年县域人口数据.xlsx",
)
# 预处理生成的干净人口文件（县名,人口 两列），首次解析成功后自动生成，后续直接读取
POP_CLEANED_RELPATH = os.path.join("data", "processed", "economy", "county_population_latest.csv")
# 脆弱性优先使用的年份列（数据集仅覆盖到 2020，固定用最新一期）
SV_YEAR_COLUMNS = ["SV2020", "SV2015", "SV2010", "SV2005", "SV2000"]

# 模块级缓存，避免每次请求重复读取 shp/xlsx
_sv_cache = {"gdf": None, "crs": None, "app_id": None}
_pop_cache = {"map": None, "app_id": None}


def _get_sv_gdf_cached(target_crs):
    """带缓存的 SV gdf 加载（缓存按 app 实例 + 目标CRS 区分）"""
    app = current_app._get_current_object()
    target_crs_key = str(target_crs) if target_crs is not None else "None"
    if (_sv_cache["gdf"] is not None and _sv_cache["app_id"] == id(app)
            and _sv_cache["crs"] == target_crs_key):
        return _sv_cache["gdf"]
    gdf = _load_sv_county_gdf(target_crs)
    _sv_cache["gdf"] = gdf
    _sv_cache["crs"] = target_crs_key
    _sv_cache["app_id"] = id(app)
    return gdf


def _get_pop_map_cached():
    """带缓存的县域人口加载"""
    app = current_app._get_current_object()
    if _pop_cache["map"] is not None and _pop_cache["app_id"] == id(app):
        return _pop_cache["map"]
    pop_map = _load_county_population_map()
    _pop_cache["map"] = pop_map
    _pop_cache["app_id"] = id(app)
    return pop_map


def _norm_county_name(name):
    """县名规范化：去空格 + 去行政后缀，用于跨数据集匹配"""
    s = str(name).strip()
    for suf in ("回族自治县", "藏族自治县", "蒙古族自治县", "哈萨克自治县", "裕固族自治县",
                "土家族苗族自治县", "布依族苗族自治县", "壮族苗族自治县", "自治县",
                "市辖区", "自治州", "地区", "市", "县", "自治县"):
        s = s.replace(suf, "")
    return s.replace(" ", "").replace("\u3000", "")


def _load_sv_county_gdf(target_crs):
    """
    读取青藏高原县域社会经济脆弱性空间分布图（SV_QTP_2000-2020.shp）。
    面要素，字段含 SV2000/SV2005/SV2010/SV2015/SV2020（0~1 脆弱性指数）及中文全/中文简/代码。
    返回转换到 target_crs 的 gdf；文件缺失/读取失败返回 None。
    """
    try:
        base_dir = current_app.config["BASE_DIR"]
        shp_path = os.path.join(base_dir, *SV_SHP_RELPATH.split(os.sep))
        if not os.path.exists(shp_path):
            current_app.logger.warning(f"SV_QTP shp 不存在: {shp_path}")
            return None
        gdf = None
        for enc in ("utf-8", "gbk"):
            try:
                gdf = gpd.read_file(shp_path, encoding=enc)
                # 校验中文列能正常解码（出现大量 � 乱码则换编码）
                sample = str(gdf.iloc[0].to_dict())[:200] if len(gdf) else ""
                if "\ufffd" in sample:
                    continue
                break
            except Exception:
                continue
        if gdf is None:
            raise ValueError("SV_QTP shp 无法用 utf-8/gbk 解码")
        # 规范化列名：去空格 + 收录中文全名/简称等常用别名
        rename = {}
        for col in gdf.columns:
            norm = str(col).strip().replace(" ", "")
            if norm != str(col):
                rename[col] = norm
            for alias, target in (("中文全名", "中文全"), ("中文名", "中文全"),
                                  ("中文简称", "中文简"), ("名称", "中文全")):
                if norm == alias and target not in gdf.columns:
                    rename[col] = target
        if rename:
            gdf = gdf.rename(columns=rename)
        if gdf.crs is not None and target_crs is not None and gdf.crs != target_crs:
            gdf = gdf.to_crs(target_crs)
        current_app.logger.info(f"SV_QTP 加载成功: {len(gdf)} 个县域, 列={list(gdf.columns)}, CRS={gdf.crs}")
        return gdf
    except Exception as e:
        current_app.logger.warning(f"SV_QTP 加载失败: {e}")
        return None


def _rasterize_county_values(gdf, value_col, dst_shape, dst_transform):
    """
    将县域面矢量按 value_col 字段值栅格化到目标栅格。
    返回 float32 栅格，无值区为 0。
    """
    from rasterio.features import rasterize
    features = []
    for geom, v in zip(gdf.geometry, gdf[value_col].astype(float).fillna(0)):
        features.append((geom.__geo_interface__, float(v)))
    return rasterize(features, out_shape=dst_shape, transform=dst_transform,
                     fill=0.0, dtype=np.float32)


_YEAR_RE = re.compile(r"(19\d{2}|20\d{2})")


def _year_of(c):
    """从表头字符串中提取年份（1900-2099），无则返回 None"""
    m = _YEAR_RE.search(str(c))
    return int(m.group(1)) if m else None


def _is_year_header(c):
    """判断表头是否为年份列（含 1900-2099 的四位年份，如 1980 / 1980年）"""
    return _year_of(c) is not None


def _pick_col(cols, keywords):
    """按关键字挑选列：先精确匹配，再包含匹配"""
    for k in keywords:
        for c in cols:
            if str(c).strip() == k:
                return c
    for c in cols:
        cs = str(c)
        if any(k.lower() in cs.lower() for k in keywords):
            return c
    return None


def _pick_name_col(frame):
    """从数据框中找出县名列：表头含'县/名/地区/区域/name'，或内容中'县'字占比高"""
    obj_cols = [c for c in frame.columns if frame[c].dtype == object]
    for c in obj_cols:
        cs = str(c)
        if any(k in cs for k in ("县", "名", "地区", "区域", "NAME", "name")):
            return c
    best, best_ratio = None, 0.0
    for c in obj_cols:
        ratio = frame[c].astype(str).str.contains("县", na=False).mean()
        if ratio > best_ratio:
            best, best_ratio = c, ratio
    return best if best_ratio > 0.1 else None


def _parse_pop_frame(frame):
    """从单个数据框解析 {县名: 最新人口}。长表取最新年份行；宽表取最大年份列。失败返回空字典"""
    frame = frame.rename(columns={c: str(c).strip() for c in frame.columns})
    cols = list(frame.columns)

    # --- 长表：年份列 + 人口列 + 县名列 ---
    year_col = _pick_col(cols, ("年份", "year", "YEAR", "年"))
    pop_col = _pick_col(cols, ("人口", "pop", "POP"))
    if year_col and pop_col:
        name_col = _pick_name_col(frame)
        if name_col:
            pop_map = {}
            for _, row in frame.iterrows():
                name = str(row[name_col]).strip()
                if not name or name.lower() == "nan":
                    continue
                try:
                    year = int(float(row[year_col]))
                    pop = float(row[pop_col])
                except (TypeError, ValueError):
                    continue
                if pop > 0 and (name not in pop_map or year > pop_map.get("_y", -1)):
                    pop_map[name] = pop
                    pop_map["_y"] = year
            if len(pop_map) > 1:
                pop_map.pop("_y", None)
                return pop_map

    # --- 宽表：年份列，取最新年份 ---
    year_cols = [c for c in cols if _is_year_header(c)]
    if year_cols:
        latest = max(year_cols, key=lambda c: _year_of(c))
        name_col = _pick_name_col(frame)
        if name_col:
            pop_map = {}
            for _, row in frame.iterrows():
                name = str(row[name_col]).strip()
                if not name or name.lower() == "nan":
                    continue
                try:
                    v = float(row[latest])
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    pop_map[name] = v
            if len(pop_map) > 0:
                return pop_map
    return {}


def _read_cleaned_pop_csv(path):
    """读取预处理生成的干净人口 CSV（两列：县名,人口），失败返回空字典"""
    try:
        if not os.path.exists(path):
            return {}
        df = pd.read_csv(path, dtype=str, header=None)
        pop_map = {}
        for _, row in df.iterrows():
            name = str(row[0]).strip()
            try:
                v = float(row[1])
            except (TypeError, ValueError):
                continue
            if name and name.lower() != "nan" and v > 0:
                pop_map[name] = v
        return pop_map
    except Exception as e:
        current_app.logger.warning(f"读取预处理人口 CSV 失败: {e}")
        return {}


def _write_cleaned_pop_csv(path, pop_map):
    """把解析出的人口字典写成干净 CSV（县名,人口），供后续直接读取，也可人工核对"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        df = pd.DataFrame(sorted(pop_map.items()), columns=["county", "pop"])
        df.to_csv(path, index=False, encoding="utf-8-sig")
        current_app.logger.info(f"已生成预处理人口文件: {path}（{len(pop_map)} 县）")
    except Exception as e:
        current_app.logger.warning(f"写入预处理人口 CSV 失败: {e}")


def _load_county_population_map():
    """
    读取县域人口数据，返回 {县名: 最新年份人口} 字典。
    优先读取预处理生成的干净 CSV；若不存在则解析原始 xlsx（宽表/长表自动探测，
    遍历所有工作表并尝试多行表头），成功后自动生成干净 CSV。
    """
    base_dir = current_app.config["BASE_DIR"]
    xlsx_path = os.path.join(base_dir, *POP_XLSX_RELPATH.split(os.sep))
    cleaned_path = os.path.join(base_dir, *POP_CLEANED_RELPATH.split(os.sep))

    pop_map = _read_cleaned_pop_csv(cleaned_path)
    if pop_map:
        current_app.logger.info(f"使用预处理人口文件: {cleaned_path}（{len(pop_map)} 县）")
        return pop_map

    try:
        if not os.path.exists(xlsx_path):
            current_app.logger.warning(f"县域人口 xlsx 不存在: {xlsx_path}")
            return {}

        sheets = pd.read_excel(xlsx_path, sheet_name=None, header=None)
        for sheet_name, raw in sheets.items():
            raw = raw.dropna(how="all").dropna(axis=1, how="all")
            if raw.empty:
                continue
            # 前几行可能是标题/说明，把每一行当作候选表头逐行尝试
            for header_idx in range(min(5, len(raw))):
                frame = raw.iloc[header_idx:].copy()
                frame.columns = [str(c) if pd.notna(c) else f"col{i}"
                                 for i, c in enumerate(raw.iloc[header_idx])]
                frame = frame.dropna(how="all")
                pop_map = _parse_pop_frame(frame)
                if len(pop_map) > 0:
                    current_app.logger.info(
                        f"县域人口加载成功: {len(pop_map)} 县（工作表 {sheet_name}，表头行 {header_idx + 1}）")
                    _write_cleaned_pop_csv(cleaned_path, pop_map)
                    return pop_map
        current_app.logger.warning("县域人口 xlsx 所有工作表均未能解析出人口数据")
        # 输出各表结构诊断，便于定位问题
        for sheet_name, raw in sheets.items():
            raw0 = raw.dropna(how="all").dropna(axis=1, how="all")
            if raw0.empty:
                continue
            head = [str(x) for x in raw0.iloc[0].tolist()[:10]]
            current_app.logger.warning(f"  [诊断] 工作表 '{sheet_name}' shape={raw0.shape} 首行={head}")
        return {}
    except Exception as e:
        current_app.logger.warning(f"县域人口 xlsx 读取失败: {e}")
        return {}


def _aggregate_points_by_level(points_xy, values, transform, level_grid, hazard_valid):
    """将落入淹没区且命中有效风险等级的县域点，按其质心所在像元的风险等级汇总数值。

    返回 (total, {level: sum})。仅统计：质心位于淹没区(hazard_valid)且风险等级>=1 的县域。
    用于以"真实县域汇总值"替代 IDW 插值面积分，避免暴露量统计量级失真。
    """
    h, w = level_grid.shape
    total = 0.0
    by_level = {}
    if len(points_xy) == 0:
        return total, by_level
    cols = ((points_xy[:, 0] - transform[2]) / transform[0] - 0.5).astype(int)
    rows = ((points_xy[:, 1] - transform[5]) / transform[4] - 0.5).astype(int)
    for i in range(len(points_xy)):
        c, r = int(cols[i]), int(rows[i])
        if 0 <= r < h and 0 <= c < w and hazard_valid[r, c]:
            lv = int(level_grid[r, c])
            if lv >= 1:
                v = float(values[i])
                by_level[lv] = by_level.get(lv, 0.0) + v
                total += v
    return total, by_level


# ==================== 暴露度：清洗后经济维度 + OSM dasymetric 像素代理 ====================
# 文件由 preprocess_economy.py 预处理生成（data/raw/economy/ 与 data/processed/）
ECON_METRIC_RELPATHS = {
    "gdp": os.path.join("data", "raw", "economy", "county_gdp_latest.csv"),
    "pop": os.path.join("data", "processed", "economy", "county_population_latest.csv"),
    "arable": os.path.join("data", "processed", "economy", "county_arable_latest.csv"),
    "grain": os.path.join("data", "processed", "economy", "county_grain_latest.csv"),
    "livestock": os.path.join("data", "processed", "economy", "county_livestock_latest.csv"),
}
# OSM 耕地/居民地掩膜（dasymetric 像素代理权重），裁剪版
OSM_LANDUSE_RELPATH = os.path.join(
    "data", "raw", "socioeconomy", "osm_street", "TPstreet_clip",
    "gis_osm_landuse_a_free_1.shp")
# OSM landuse 预生成多波段权重掩膜（preprocess_osm_landuse.py 产出），与矢量同源目录
OSM_MASKS_RELPATH = os.path.join(
    "data", "raw", "socioeconomy", "osm_street", "TPstreet_clip",
    "gis_osm_landuse_masks.tif")
# 权重掩膜波段顺序（须与预处理脚本 FCLASS_ORDER 一致）；缺失 fclass 波段全 0
FCLASS_ORDER = ["residential", "industrial", "commercial", "retail",
                "office", "farmland", "grass"]
# 各维度 dasymetric 权重所用的 OSM landuse fclass 组合
_METRIC_OSM_FCLASS = {
    "gdp": ("residential", "industrial", "commercial", "retail", "office"),
    "pop": ("residential",),
    "arable": ("farmland",),
    "grain": ("farmland",),
    "livestock": ("farmland", "grass"),
}
# 县域名称别名：数据名 -> 地理矢量名（geojson 含夏河县不含合作市）
NAME_ALIAS = {"合作市": "夏河县"}


def _load_geo_counties(target_crs):
    """加载 economy_counties 并 reproject，附加 _key(规范县名) 列，返回 gdf。"""
    base_dir = current_app.config["BASE_DIR"]
    geo_path = os.path.join(base_dir, "data", "raw", "geo", "economy_counties.geojson")
    gdf = gpd.read_file(geo_path)
    if gdf.crs is None:
        gdf.set_crs("EPSG:3857", inplace=True)
    gdf = gdf.to_crs(target_crs)
    gdf["_key"] = gdf["NAME_DECODED"].astype(str).apply(_norm_county_name)
    return gdf


def _load_county_metric_points(relpath, target_crs, geo_gdf=None):
    """读取清洗后最新值 CSV(county,value,year) + economy_counties 匹配。
    返回 (pts[N,2], vals[N], keys[N])，keys 与 _load_geo_counties 的 _key 对齐。
    用于 dasymetric 像素分配与"真实县域汇总"统计。"""
    base_dir = current_app.config["BASE_DIR"]
    csv_path = os.path.join(base_dir, *relpath.split(os.sep))
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"清洗经济文件不存在: {csv_path}")
    df = pd.read_csv(csv_path)
    df = df[df["county"].notna()]
    df["county"] = df["county"].astype(str).str.strip()
    df = df[df["county"] != "nan"]
    valcol = df.columns[1]  # value 列（county 之后的第二列）
    df["_v"] = pd.to_numeric(df[valcol], errors="coerce")
    df = df.dropna(subset=["_v"]).query("_v > 0")
    if len(df) == 0:
        return np.empty((0, 2)), np.empty((0,)), []
    if geo_gdf is None:
        geo_gdf = _load_geo_counties(target_crs)
    # 数据县名 -> 地理县名（别名），再规范化为匹配键
    df["_m"] = df["county"].apply(lambda n: NAME_ALIAS.get(n, n))
    df["_key"] = df["_m"].apply(_norm_county_name)
    merged = geo_gdf.merge(df[["_key", "_v", "county"]], on="_key", how="inner")
    if len(merged) == 0:
        # 兜底：PYNAME 去空格小写匹配
        geo_gdf["_kpy"] = geo_gdf["PYNAME"].astype(str).str.replace(r"\s+", "", regex=True).str.lower()
        df["_kpy"] = df["_m"].astype(str).str.replace(r"\s+", "", regex=True).str.lower()
        merged = geo_gdf.merge(df[["_kpy", "_v", "county"]], on="_kpy", how="inner")
    merged = merged.dropna(subset=["_v"])
    pts = np.array([(g.x, g.y) for g in merged.geometry], dtype=np.float64)
    vals = merged["_v"].astype(np.float64).values
    keys = merged["_key"].astype(str).tolist() if "_key" in merged.columns else merged["_kpy"].astype(str).tolist()
    return pts, vals, keys


def _load_osm_weight_masks(dst_shape, dst_transform, target_crs):
    """加载 OSM landuse 各类 fclass 的权重网格（float32, 与 dst_shape 同形）。

    加载优先级：
      1) 预生成多波段权重掩膜 `gis_osm_landuse_masks.tif`（`preprocess_osm_landuse.py` 产出），
         按 `FCLASS_ORDER` 各波段 reproject 到目标网格——避免每次 /risk_assessment 都重读
         33MB dbf + 16MB shp 并实时栅格化，且结果可复现、可溯源；
      2) 回退：直接从 OSM landuse 矢量 on-the-fly 栅格化（与原逻辑一致）。
    返回 {fclass: grid}；均不可用返回空 dict（调用方退化为 IDW/均匀分配）。
    """
    base_dir = current_app.config["BASE_DIR"]
    prebuilt = os.path.join(base_dir, *OSM_MASKS_RELPATH.split(os.sep))
    if os.path.exists(prebuilt):
        try:
            from rasterio.warp import reproject, Resampling
            with rasterio.open(prebuilt) as src:
                masks = {}
                for i, fc in enumerate(FCLASS_ORDER, start=1):
                    dst = np.zeros(dst_shape, dtype=np.float32)
                    reproject(
                        source=rasterio.band(src, i),
                        destination=dst,
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=dst_transform,
                        dst_crs=target_crs,
                        resampling=Resampling.nearest,
                    )
                    masks[fc] = dst
            current_app.logger.info(
                f"OSM landuse 权重掩膜（预生成栅格）加载: 波段={list(masks.keys())}")
            return masks
        except Exception as e:
            current_app.logger.warning(f"预生成 OSM 掩膜加载失败，回退矢量栅格化: {e}")

    # 回退：on-the-fly 栅格化（原逻辑）
    from rasterio.features import rasterize
    shp = os.path.join(base_dir, *OSM_LANDUSE_RELPATH.split(os.sep))
    if not os.path.exists(shp):
        current_app.logger.warning(f"OSM landuse 缺失: {shp}")
        return {}
    try:
        gdf = gpd.read_file(shp)
        if gdf.crs is not None and target_crs is not None and gdf.crs != target_crs:
            gdf = gdf.to_crs(target_crs)
        masks = {}
        for fc in set(sum(_METRIC_OSM_FCLASS.values(), ())):
            sub = gdf[gdf["fclass"] == fc]
            if len(sub) == 0:
                continue
            feats = [(geom.__geo_interface__, 1.0) for geom in sub.geometry]
            grid = rasterize(feats, out_shape=dst_shape, transform=dst_transform,
                             fill=0.0, dtype=np.float32)
            masks[fc] = grid
        current_app.logger.info(f"OSM landuse 权重掩膜生成(矢量): {list(masks.keys())}")
        return masks
    except Exception as e:
        current_app.logger.warning(f"OSM landuse 栅格化失败，退化为 IDW: {e}")
        return {}


def _dasymetric_distribute(county_gdf, totals_map, weight_grid, dst_shape, dst_transform):
    """将各县总值按权重网格 dasymetric 分配到像元（县域面内加权分配）。
    需要 county_gdf 为县域**面**图层；totals_map: {_key: total}。
    注：本系统 economy_counties.geojson 为**点**层，故风险计算中实际走 IDW+OSM 调制
    （见 _osm_modulate）。此函数保留供后续接入县域面图层时启用真 dasymetric。"""
    from rasterio.features import rasterize
    out = np.zeros(dst_shape, dtype=np.float32)
    feats = [(geom.__geo_interface__, k) for geom, k in zip(county_gdf.geometry, county_gdf["_key"])]
    key_grid = rasterize(feats, out_shape=dst_shape, transform=dst_transform, fill=-1, dtype=np.int32)
    for k, total in totals_map.items():
        if total is None or total <= 0:
            continue
        mask = key_grid == k
        if not np.any(mask):
            continue
        if weight_grid is not None:
            w = weight_grid.copy()
            w[~mask] = 0.0
            wsum = float(w.sum())
            if wsum <= 0:
                w = mask.astype(np.float32)
                wsum = float(w.sum())
        else:
            w = mask.astype(np.float32)
            wsum = float(w.sum())
        if wsum <= 0:
            continue
        out[mask] = total * w[mask] / wsum
    return out


def _osm_modulate(idw_grid, osm_mask):
    """用 OSM landuse 权重在 OSM 覆盖区内调制 IDW 暴露网格：
    OSM 覆盖区内暴露向 OSM 要素（居民地/耕地）集中；覆盖区外保持原 IDW。
    osm_mask: float32 网格（某类 fclass 像元=1）。归一化在调用方进行，这里只需相对形状。"""
    if osm_mask is None:
        return idw_grid
    m = osm_mask.astype(np.float32)
    mx = float(m.max())
    if mx <= 0:
        return idw_grid
    m = m / mx  # 归一化到 [0,1]
    cover = m > 0
    out = idw_grid.copy()
    out[cover] = idw_grid[cover] * m[cover]
    return out


@alert_bp.route("/risk_assessment", methods=["POST"])
def flood_risk_assessment():
    """
    洪水灾害风险评估（栅格像元级）
    风险 = 危险性 × 暴露度 × 脆弱性
      危险性 H：时段内 flood_inundation 最大值，归一化
      暴露度 E：县区 GDP 点 IDW 插值到 H 栅格（经济维度）+
                县域人口点 IDW 插值（人口维度），等权合成后归一化
      脆弱性 V：SV_QTP 县域社会经济脆弱性指数（按评估起始年就近取期、缺失县用中位数填充）；
                数据不可用时回退常数 0.5
    统计口径：stats 中的 GDP/人口为"淹没县域真实汇总值"（按县域质心落入淹没区者求和），
             而非 IDW 插值面的积分，避免量级失真。
    输入: { start_date?: "2024-07-01", end_date?: "2024-08-31", basin?: "all"|索引,
            weight_method?: "entropy"(默认) | "custom",
            risk_weights?: [wH,wE,wV] | {hazard,exposure,vulnerability},
            exposure_weights?: [w_gdp,w_pop,w_arable,w_grain,w_livestock] | {gdp,pop,arable,grain,livestock} }
          weight_method="entropy": 后端依据淹没区内各栅格取值离散程度客观赋权(熵权法)，无需传权重；
          weight_method="custom": 使用 risk_weights/exposure_weights（非负实数，后端自动归一化和=1）。
          负数/全零/维度缺失返回 400。
    """
    try:
        data = request.get_json() or {}
        start_str = data.get("start_date", "2024-07-01")
        end_str = data.get("end_date", "2024-08-31")
        basin = data.get("basin", "all")  # 'all' 或流域索引字符串
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d")
            end_date = datetime.strptime(end_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400
        if start_date > end_date:
            return jsonify({"error": "开始日期不能晚于结束日期"}), 400

        # 0. 解析权重方法（二选一：entropy 熵权法 / custom 自定义），默认 entropy。
        #    entropy：后端依据淹没区内各栅格取值离散程度客观赋权，无需前端传权重；
        #    custom：解析并校验前端权重（暴露度 5 维 / 综合风险 3 因子）。
        _RISK_WEIGHT_KEYS = ["hazard", "exposure", "vulnerability"]
        _EXPOSURE_WEIGHT_KEYS = ["gdp", "pop", "arable", "grain", "livestock"]
        weight_method = data.get("weight_method", "entropy")
        if weight_method not in ("entropy", "custom"):
            return jsonify({"error": f"weight_method 仅支持 'entropy' 或 'custom'，实际为 '{weight_method}'"}), 400
        risk_weights = None
        exposure_weights = None
        if weight_method == "custom":
            try:
                risk_weights = _parse_weights(data.get("risk_weights"), 3, _RISK_WEIGHT_KEYS, "risk_weights")
                exposure_weights = _parse_weights(data.get("exposure_weights"), 5, _EXPOSURE_WEIGHT_KEYS, "exposure_weights")
            except ValueError as e:
                return jsonify({"error": f"权重参数非法: {e}"}), 400

        # 1. 扫描 flood_inundation 文件
        flood_dir = current_app.config["FLOOD_INUNDATION_DIR"]
        runoff_dir = current_app.config.get("RUNOFF_OUTPUT_DIR")

        def _list_flood_dates(d):
            out = []
            if not os.path.isdir(d):
                return out
            for f in os.listdir(d):
                if not f.endswith("_flood_inundation.tif"):
                    continue
                dp = f.replace("_flood_inundation.tif", "")
                try:
                    out.append(datetime.strptime(dp, "%Y-%m-%d"))
                except ValueError:
                    continue
            return out

        def _has_runoff_in_range(rd):
            if not rd or not os.path.isdir(rd):
                return False
            cur = start_date
            while cur <= end_date:
                if os.path.exists(os.path.join(rd, f"{cur.strftime('%Y-%m-%d')}_runoff.tif")):
                    return True
                cur += timedelta(days=1)
            return False

        flood_all_dates = _list_flood_dates(flood_dir)

        flood_files = []
        if os.path.isdir(flood_dir):
            for f in os.listdir(flood_dir):
                if not f.endswith("_flood_inundation.tif"):
                    continue
                date_part = f.replace("_flood_inundation.tif", "")
                try:
                    fd = datetime.strptime(date_part, "%Y-%m-%d")
                except ValueError:
                    continue
                if start_date <= fd <= end_date:
                    flood_files.append((os.path.join(flood_dir, f), fd))
            flood_files.sort(key=lambda x: x[1])

        if not flood_files:
            # 结构化阻断：区分"从未生成 / 日期不匹配 / 缺上游径流"三种情形，给出精确操作指引，
            # 不再静默返回全零结果。
            if os.path.isdir(flood_dir) and flood_all_dates:
                miss_step = "range_mismatch"
                message = (f"所选评估时段（{start_str} ~ {end_str}）内未找到 flood_inundation 淹没范围数据"
                           f"（系统已存在其他时段的淹没结果）。请将评估日期调整为已模拟的时段，"
                           f"或在「灾害过程模拟 → 洪水灾害过程模拟 → 淹没模拟」中重新运行该时段的淹没模拟，"
                           f"然后返回此处重新评估。")
            elif _has_runoff_in_range(runoff_dir):
                miss_step = "flood_inundation"
                message = (f"评估前须先生成洪水淹没范围（flood_inundation）。所选时段（{start_str} ~ {end_str}）内"
                           f"已检测到径流结果，但尚未生成淹没范围。请在「灾害过程模拟 → 洪水灾害过程模拟 → 淹没模拟」"
                           f"中运行洪水淹没模拟，然后返回此处重新评估。")
            else:
                miss_step = "runoff"
                message = (f"评估前须先生成洪水淹没范围（flood_inundation），而该数据依赖径流模拟结果。"
                           f"所选时段（{start_str} ~ {end_str}）内未检测到径流结果文件。请先在"
                           f"「灾害过程模拟 → 洪水灾害过程模拟 → 径流模拟」运行径流模拟，再运行淹没模拟，"
                           f"最后返回此处评估。")
            return jsonify({
                "status": "NO_INUNDATION_DATA",
                "missing_step": miss_step,
                "message": message,
                "start_date": start_str,
                "end_date": end_str,
                "action": "run_simulate",
            }), 409

        # 2. 合成危险性：逐像元最大值
        template_profile = None
        hazard_grid = None
        used_dates = []
        for path, fd in flood_files:
            try:
                with rasterio.open(path) as src:
                    arr = src.read(1).astype(np.float32)
                    nodata = src.nodata
                    if template_profile is None:
                        template_profile = src.profile.copy()
                        hazard_grid = np.full(arr.shape, 0.0, dtype=np.float32)
                    if nodata is not None:
                        arr = np.where(arr == nodata, 0, arr)
                    np.maximum(hazard_grid, arr, out=hazard_grid)
                    used_dates.append(fd.strftime("%Y-%m-%d"))
            except Exception as e:
                current_app.logger.warning(f"跳过损坏的 flood_inundation 文件: {os.path.basename(path)}, 错误: {e}")
                continue

        if hazard_grid is None:
            return jsonify({"error": "所有 flood_inundation 文件均无法读取，请检查数据完整性"}), 400

        # 像元面积（用于统计）
        transform = template_profile.get("transform")
        crs = template_profile.get("crs")
        pw = abs(transform[0])
        ph = abs(transform[4])
        if crs is not None and crs.is_geographic:
            center_lat = (transform[5] + transform[5] - ph * hazard_grid.shape[0]) / 2
            lat_rad = np.radians(center_lat)
            mplon = 111412.84 * np.cos(lat_rad) - 93.5 * np.cos(3 * lat_rad)
            mplat = 111132.954 - 559.822 * np.cos(2 * lat_rad)
            pixel_area_m2 = (pw * mplon) * (ph * mplat)
        else:
            pixel_area_m2 = pw * ph
        pixel_area_km2 = pixel_area_m2 / 1e6

        # 流域裁剪（若指定流域）
        basin_name_used = "全部流域"
        if basin != "all":
            try:
                basin_geo_path = os.path.join(current_app.config["BASE_DIR"], "data", "raw", "geo", "TP_Basins_China.geojson")
                basin_gdf = gpd.read_file(basin_geo_path)
                idx = int(basin)
                if 0 <= idx < len(basin_gdf):
                    geom = basin_gdf.geometry.iloc[idx]
                    # 探测流域名称字段
                    props = basin_gdf.drop(columns="geometry").iloc[idx].to_dict()
                    name_field = next((k for k in ["NAME", "BASIN_NAME", "BAS_NM", "name", "BASIN", "PN", "BAS_NAME"]
                                       if props.get(k) not in (None, "")), None)
                    basin_name_used = props.get(name_field, f"流域{idx + 1}") if name_field else f"流域{idx + 1}"
                    # CRS 对齐：流域(CRS84) → tif CRS
                    if basin_gdf.crs is not None and crs is not None and basin_gdf.crs != crs:
                        geom = gpd.GeoSeries([geom], crs=basin_gdf.crs).to_crs(crs).iloc[0]
                    from rasterio.features import geometry_mask
                    in_mask = geometry_mask(
                        [geom.__geo_interface__], out_shape=hazard_grid.shape,
                        transform=transform, invert=True)
                    hazard_grid = np.where(in_mask, hazard_grid, 0)
                else:
                    current_app.logger.warning(f"流域索引 {idx} 越界，使用全部区域")
            except Exception as e:
                current_app.logger.warning(f"流域裁剪失败，使用全部区域: {e}")

        # 危险性归一化（仅在 hazard>0 即淹没区）
        hazard_valid = hazard_grid > 0
        hazard_norm = _normalize(hazard_grid, hazard_valid)

        # 3. 暴露度：县域经济维度（GDP/人口/耕地/粮食/牲畜）多指标合成 + OSM 像素代理
        #    各维度以县域最新总值(权威) 做 IDW 空间化；在 OSM landuse 覆盖区内，将暴露向
        #    OSM 要素（居民地/耕地）集中（OSM 调制）。因 economy_counties 为点层、OSM 为局部
        #    裁剪，未做县域面 dasymetric（保留 _dasymetric_distribute 供后续接入面图层启用）。
        tcr = crs if crs is not None else "EPSG:4326"
        try:
            geo_gdf = _load_geo_counties(tcr)
        except Exception as e:
            current_app.logger.error(f"加载县域矢量失败: {e}")
            return jsonify({"error": "无法加载 economy_counties 县域矢量"}), 500

        osm_masks = _load_osm_weight_masks(hazard_grid.shape, transform, tcr)
        use_osm = bool(osm_masks)

        metric_grids = {}      # mkey -> 归一化像素网格
        metric_pts = {}        # mkey -> (pts, vals) 用于真实汇总统计
        for mkey in ("gdp", "pop", "arable", "grain", "livestock"):
            try:
                pts, vals, keys = _load_county_metric_points(
                    ECON_METRIC_RELPATHS[mkey], tcr, geo_gdf=geo_gdf)
            except Exception as e:
                current_app.logger.warning(f"加载 {mkey} 最新值失败: {e}")
                pts, vals, keys = np.empty((0, 2)), np.empty((0,)), []
            metric_pts[mkey] = (pts, vals)
            if len(pts) == 0:
                metric_grids[mkey] = np.zeros(hazard_grid.shape, dtype=np.float32)
                continue
            # IDW 空间化（县域总值 -> 像元）
            grid = _idw_interpolate(pts, vals, hazard_grid.shape, transform, k=8, power=2.0)
            # OSM 像素代理：OSM 覆盖区内向居民地/耕地要素集中
            if use_osm:
                wgrid = None
                for fc in _METRIC_OSM_FCLASS.get(mkey, ()):
                    if fc in osm_masks:
                        wgrid = osm_masks[fc] if wgrid is None else wgrid + osm_masks[fc]
                if wgrid is not None:
                    grid = _osm_modulate(grid, wgrid.astype(np.float32))
            grid = np.where(hazard_valid, grid, 0.0)
            metric_grids[mkey] = _normalize(grid, hazard_valid)

        # 5 个维度加权合成：权重方法为 entropy 时先按熵权法客观赋权，custom 时沿用前端权重；
        # 随后按归一化权重加权求和（等权时退化为均值），再归一化。
        stack = np.stack([metric_grids[m] for m in ("gdp", "pop", "arable", "grain", "livestock")], axis=0)
        if exposure_weights is None:
            exposure_weights = _entropy_weights(stack, hazard_valid)
        exp_raw = np.tensordot(exposure_weights, stack, axes=(0, 0))
        exposure_norm = _normalize(exp_raw, hazard_valid)
        current_app.logger.info(
            f"暴露度合成完成: OSM调制={'是' if use_osm else '否'} 权重方法={weight_method} "
            f"维度=GDP/人口/耕地/粮食/牲畜 权重={[round(float(x), 3) for x in exposure_weights]}")

        # 4. 脆弱性：SV_QTP 县域社会经济脆弱性指数栅格化
        #    SV 数据为 5 年期（SV2000/2005/2010/2015/2020），按评估时段开始年份就近取期
        try:
            sv_gdf = _get_sv_gdf_cached(crs if crs is not None else "EPSG:4326")
            sv_avail = {c: int(c.replace("SV", "")) for c in SV_YEAR_COLUMNS
                        if sv_gdf is not None and c in sv_gdf.columns}
            if sv_gdf is not None and sv_avail:
                sv_year = min(sv_avail.values(), key=lambda y: abs(start_date.year - y))
                sv_col = f"SV{sv_year}"
                sv_gdf = sv_gdf.copy()
                sv_gdf[sv_col] = pd.to_numeric(sv_gdf[sv_col], errors="coerce")
                # SV=0（缺失县）在淹没区内的像元 → 用区域内非零 SV 中位数填充
                sv_grid = _rasterize_county_values(sv_gdf, sv_col, hazard_grid.shape, transform)
                sv_valid = hazard_valid & (sv_grid > 0)
                if np.any(sv_valid):
                    sv_med = float(np.median(sv_grid[sv_valid]))
                    sv_grid = np.where(hazard_valid & (sv_grid <= 0), sv_med, sv_grid)
                sv_grid = np.where(hazard_valid, sv_grid, 0.0)
                vulnerability_grid = _normalize(sv_grid, hazard_valid).astype(np.float32)
                vulnerability_note = (f"脆弱性采用{sv_year}年县域社会经济脆弱性指数（{sv_col}）栅格化"
                                      f"（评估时段{start_date.year}年就近取期，"
                                      f"{int(np.sum(sv_valid))} 像元，缺失县按中位数填充）")
            else:
                raise ValueError("SV_QTP 数据缺失或无可用的 SV 年份列")
        except Exception as e:
            current_app.logger.warning(f"脆弱性 SV_QTP 计算失败，回退常数 {VULNERABILITY_CONST}: {e}")
            vulnerability_grid = np.where(hazard_valid, VULNERABILITY_CONST, 0.0).astype(np.float32)
            vulnerability_note = f"脆弱性采用常数 {VULNERABILITY_CONST}（SV_QTP 数据不可用）"

        # 5. 风险合成：采用加权几何平均 Risk = H^wH · E^wE · V^wV
        #    （乘法模型 Risk = H×E×V 的规范加权推广，权重经归一化后作为各因子指数；
        #     等权时退化为几何平均，单调变换不改变五级相对分级）。
        #    权重方法为 entropy 时，顶层 H/E/V 采用"熵权×AHP先验混合"：先算客观熵权，
        #    再以主观先验锚定物理结构做凸组合（避免 Hazard/Vuln 被数据判为 0、权重漂移）；
        #    custom 时沿用前端权重。暴露度 5 维仍用纯熵权（其同质性子指标层适用）。
        risk_entropy_weights = None
        if risk_weights is None:
            risk_entropy_weights = _entropy_weights(
                np.stack([hazard_norm, exposure_norm, vulnerability_grid], axis=0), hazard_valid)
            risk_weights = _combined_ahp_entropy(
                RISK_PRIOR_WEIGHTS, risk_entropy_weights, RISK_PRIOR_STRENGTH)
        risk_grid = _weighted_geometric_mean(
            [hazard_norm, exposure_norm, vulnerability_grid], risk_weights)
        composite_model = "weighted_geometric_mean"
        # 非淹没区风险置0
        risk_grid = np.where(hazard_valid, risk_grid, 0.0)

        # 6. 分级（基于淹没区内值分位数，使各级面积均衡，统一输出等级 1-5）
        def _classify_into_levels(values, valid_mask):
            """把连续值按 valid 区域内分位数分成 1-5 级，0 表示无效"""
            level_grid = np.zeros(values.shape, dtype=np.uint8)
            valid_vals = values[valid_mask & (values > 0)]
            if valid_vals.size > 0:
                q20, q40, q60, q80 = np.percentile(valid_vals, [20, 40, 60, 80])
                level_grid[valid_mask & (values > 0) & (values < q20)] = 1
                level_grid[valid_mask & (values >= q20) & (values < q40)] = 2
                level_grid[valid_mask & (values >= q40) & (values < q60)] = 3
                level_grid[valid_mask & (values >= q60) & (values < q80)] = 4
                level_grid[valid_mask & (values >= q80)] = 5
            level_grid[~valid_mask] = 0
            return level_grid

        level_grid = _classify_into_levels(risk_grid, hazard_valid)

        # 7. 写输出栅格
        out_dir = current_app.config["RISK_RESULTS_DIR"]
        os.makedirs(out_dir, exist_ok=True)
        stamp = f"{start_str}_to_{end_str}_basin-{basin}"

        def _write_tif(name, arr, dtype):
            prof = template_profile.copy()
            prof.update(dtype=dtype, count=1, nodata=0, compress="lzw")
            p = os.path.join(out_dir, name)
            with rasterio.open(p, "w", **prof) as dst:
                dst.write(arr.astype(dtype), 1)
            os.chmod(p, 0o644)
            return f"/backend-static/risk_results/{name}"

        risk_url = _write_tif(f"{stamp}_risk.tif", level_grid, np.uint8)
        hazard_url = _write_tif(f"{stamp}_hazard.tif", _classify_into_levels(hazard_norm, hazard_valid), np.uint8)
        exposure_url = _write_tif(f"{stamp}_exposure.tif", _classify_into_levels(exposure_norm, hazard_valid), np.uint8)
        # 脆弱性：归一化连续值 → 分级 1-5（与危险性/暴露度一致），非淹没区 0
        vulnerability_level_grid = _classify_into_levels(vulnerability_grid, hazard_valid)
        vulnerability_url = _write_tif(f"{stamp}_vulnerability.tif", vulnerability_level_grid, np.uint8)

        # 8. 统计：各维度均采用"淹没县域真实汇总值"（按质心落入淹没区者求和），
        #    而非 IDW 插值面积分，避免量级失真；各级占比基于真实汇总值计算。
        total_flood_pixels = int(np.sum(hazard_valid))

        # 综合风险评分：基于「绝对连续风险」(H×E×V) 的淹没区面积加权均值，
        # 再按峰值归一化到 0-1。注意：相对分级(level_grid)各等级面积均衡，
        # 若直接对等级做面积加权平均会得到恒为 ~3 的伪评分(≈50)，故改用连续风险值。
        _rv = risk_grid[hazard_valid & (risk_grid > 0)]
        if _rv.size > 0:
            _rmax = float(_rv.max())
            risk_mean_norm = float(np.mean(_rv / _rmax)) if _rmax > 0 else 0.0
            risk_mean_raw = float(np.mean(_rv))
            risk_max = _rmax
        else:
            risk_mean_norm = 0.0
            risk_mean_raw = 0.0
            risk_max = 0.0
        _hr = int(np.sum((level_grid >= 4) & hazard_valid))
        high_risk_area_ratio = round(_hr / total_flood_pixels, 4) if total_flood_pixels > 0 else 0.0

        metric_real = {}     # mkey -> (total_real, by_level)
        metric_matched = {}  # mkey -> 匹配县数
        for mkey in ("gdp", "pop", "arable", "grain", "livestock"):
            p, v = metric_pts.get(mkey, (np.empty((0, 2)), np.empty((0,))))
            metric_matched[mkey] = int(len(p))
            if len(p) > 0:
                metric_real[mkey] = _aggregate_points_by_level(p, v, transform, level_grid, hazard_valid)
            else:
                metric_real[mkey] = (0.0, {})
        gdp_total_real, gdp_by_level = metric_real["gdp"]
        pop_total_real, pop_by_level = metric_real["pop"]
        arable_total_real, arable_by_level = metric_real["arable"]
        grain_total_real, grain_by_level = metric_real["grain"]
        livestock_total_real, livestock_by_level = metric_real["livestock"]

        level_stats = []
        for lv in RISK_LEVELS:
            lvl = lv["level"]
            n = int(np.sum(level_grid == lvl))
            gdp_sum = float(gdp_by_level.get(lvl, 0.0))
            pop_sum = float(pop_by_level.get(lvl, 0.0)) if pop_by_level is not None else None
            arable_sum = float(arable_by_level.get(lvl, 0.0))
            grain_sum = float(grain_by_level.get(lvl, 0.0))
            livestock_sum = float(livestock_by_level.get(lvl, 0.0))
            level_stats.append({
                "level": lvl,
                "name": lv["name"],
                "color": lv["color"],
                "pixels": n,
                "area_km2": round(n * pixel_area_km2, 2),
                # 各维度在「该风险等级」内的占比（基于淹没县域真实汇总值）
                "area_share": round(n / total_flood_pixels * 100, 2) if total_flood_pixels > 0 else 0.0,
                "gdp_share": round(gdp_sum / gdp_total_real * 100, 2) if gdp_total_real > 0 else 0.0,
                "pop_share": round(pop_sum / pop_total_real * 100, 2) if (pop_total_real is not None and pop_total_real > 0) else None,
                "arable_share": round(arable_sum / arable_total_real * 100, 2) if arable_total_real > 0 else 0.0,
                "grain_share": round(grain_sum / grain_total_real * 100, 2) if grain_total_real > 0 else 0.0,
                "livestock_share": round(livestock_sum / livestock_total_real * 100, 2) if livestock_total_real > 0 else 0.0,
            })

        return jsonify({
            "risk_tif_url": risk_url,
            "hazard_tif_url": hazard_url,
            "exposure_tif_url": exposure_url,
            "vulnerability_tif_url": vulnerability_url,
            "stats": {
                "levels": level_stats,
                "total_flood_area_km2": round(total_flood_pixels * pixel_area_km2, 2),
                "total_gdp_wan": round(gdp_total_real, 2),
                "total_pop": int(round(pop_total_real)) if pop_total_real is not None else None,
                "total_arable_ha": round(metric_real["arable"][0], 2),
                "total_grain_t": round(metric_real["grain"][0], 2),
                "total_livestock_head": round(metric_real["livestock"][0], 2),
                "county_points_used": metric_matched["gdp"],
                "county_points_detail": {m: metric_matched[m] for m in ("gdp", "pop", "arable", "grain", "livestock")},
                "exposure_osm_modulated": use_osm,
                "dates_used": used_dates,
                "risk_mean_norm": round(risk_mean_norm, 4),
                "risk_mean_raw": round(risk_mean_raw, 6),
                "risk_max": round(risk_max, 6),
                "high_risk_area_ratio": high_risk_area_ratio,
            },
            "meta": {
                "weight_method": weight_method,
                "method": ("综合风险 = 危险性^wH × 暴露度^wE × 脆弱性^wV（加权几何平均）；"
                           "顶层 H/E/V 权重由熵权×AHP先验混合赋权(凸组合)，暴露度5维由熵权赋权，"
                           "亦可由用户自定义覆盖"),
                "composite_model": composite_model,
                "hazard_source": "flood_inundation 时段最大值归一化",
                "exposure_source": ("县域GDP/人口/耕地/粮食/牲畜 多指标 IDW + OSM landuse 像素调制"
                                    if use_osm else
                                    "县域GDP/人口/耕地/粮食/牲畜 IDW 插值（OSM 权重缺失，未调制）"),
                "exposure_stats": "各维度均为淹没县域真实汇总值（按质心落入淹没区者求和），非 IDW 插值面积分",
                "exposure_dimensions": ["gdp", "pop", "arable", "grain", "livestock"],
                "score_method": ("综合风险评分 = 淹没区连续风险面积加权均值 ÷ 峰值，×100；"
                                 "（加权几何平均连续面，等级按 0.2/0.4/0.6/0.8 阈值划分，真实量级）"),
                "vulnerability_note": vulnerability_note,
                "risk_weights": dict(zip(_RISK_WEIGHT_KEYS, [round(float(x), 4) for x in risk_weights])),
                "risk_entropy_weights": (dict(zip(_RISK_WEIGHT_KEYS, [round(float(x), 4) for x in risk_entropy_weights]))
                                         if risk_entropy_weights is not None else None),
                "risk_prior_weights": dict(zip(_RISK_WEIGHT_KEYS, [round(float(x), 4) for x in RISK_PRIOR_WEIGHTS])),
                "risk_prior_strength": round(float(RISK_PRIOR_STRENGTH), 2),
                "exposure_weights": dict(zip(_EXPOSURE_WEIGHT_KEYS, [round(float(x), 4) for x in exposure_weights])),
                "weights_user_defined": bool(weight_method == "custom"),
                "crs": str(crs),
                "pixel_size_deg": [pw, ph],
                "date_range": f"{start_str} 至 {end_str}",
                "basin": basin_name_used,
                "dates_used_count": len(used_dates),
            },
        })

    except Exception as e:
        current_app.logger.error(f"洪水风险评估失败: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({"error": "服务器内部错误"}), 500
