# -*- coding: utf-8 -*-
"""
E 模块经济原始数据预处理 pipeline
================================
将 data/raw/socioeconomy 下的原始 xlsx 清洗、单位统一、宽表逆透视、
县域名称映射、缺失就近取期，输出符合系统规范的可直接用数据文件到：
  - GDP(最新/年序列)        -> data/raw/economy/
  - 人口/耕地/粮食/牲畜(最新) -> data/processed/
  - 统一长表 + 名称别名表     -> data/raw/economy/ 与 data/processed/

输入：
  D1 青藏经济安全指数数据集(2000,2010,2020;县).xlsx   (GDP亿元/人口万人/粮食吨/二三产万元/储蓄万元)
  D2 青藏经济安全指数计算结果(2000,2010,2020;县).xlsx  (地市级经济安全得分)
  D3 青藏高原1980-2020年县域人口数据.xlsx              (P1980..P2020 人)
  D4 青藏高原1980-2020年县域牲畜存栏量数据.xlsx         (SU1980..SU2020 头)
  D5 青藏高原1980-2020年县域粮食数据.xlsx              (G1980..G2020 吨)
  D6 青藏高原1980-2020年县域粮食耕地面积数据.xlsx       (A1980..A2020 公顷)
  GDP_data.csv (既有, 2000-2015, 县域GDP年序列)        -> 作为 GDP 年序列主源
  economy_counties.geojson (全国县域面, NAME_DECODED)   -> 匹配校验

注：D7 Cattle/Sheep .xls 需要 xlrd（离线环境不可用），已由 D4 总牲畜量覆盖，跳过。
"""
import os, re, json, sys
import pandas as pd

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))  # 项目根（由脚本位置回退一层）
ECO = os.path.join(BASE, "data", "raw", "socioeconomy")
GDP_CSV = os.path.join(BASE, "data", "raw", "economy", "GDP_data.csv")
GEOJSON = os.path.join(BASE, "data", "raw", "geo", "socioeconomic", "economy_counties.geojson")
OUT_ECON = os.path.join(BASE, "data", "raw", "economy")
OUT_PROC = os.path.join(BASE, "data", "processed")
# 县域经济派生 CSV 统一落在 processed/economy/ 下（与 raw/economy 对称）
OUT_PROC_ECONOMY = os.path.join(OUT_PROC, "economy")
os.makedirs(OUT_ECON, exist_ok=True)
os.makedirs(OUT_PROC, exist_ok=True)
os.makedirs(OUT_PROC_ECONOMY, exist_ok=True)

YEAR_RE = re.compile(r"((?:19|20)\d{2})")

def norm_name(n):
    s = str(n).strip()
    for suf in ("回族自治县","藏族自治县","蒙古族自治县","哈萨克自治县","裕固族自治县",
                "土家族苗族自治县","布依族苗族自治县","壮族苗族自治县","自治县",
                "市辖区","自治州","地区","市","县","区"):
        s = s.replace(suf, "")
    return s.replace(" ", "").replace("\u3000", "")

# 县域名称别名：数据名 -> 地理矢量名（依据各数据集“数据说明”）
# 夏河县与合作市：1998 合作市由夏河县分出；各数据集“数据说明”写明合并为“夏河县”。
# 经核验，本系统 economy_counties.geojson 含“夏河县”而不含“合作市”，
# 故将 D1 中的“合作市”映射到地理矢量的“夏河县”（D3-D6 本身即用“夏河县”，可精确匹配）。
NAME_ALIAS = {
    "合作市": "夏河县",
}

# ----------------------------------------------------------------------------
# 1) 读取 geojson 规范县名集合（用于匹配校验）
# ----------------------------------------------------------------------------
with open(GEOJSON, encoding="utf-8") as f:
    gj = json.load(f)
geo_names = set()
geo_norm = {}
for ft in gj["features"]:
    nm = ft["properties"].get("NAME_DECODED")
    if nm:
        geo_names.add(str(nm).strip())
        geo_norm[norm_name(nm)] = str(nm).strip()

def resolve_geo(name):
    """数据县名 -> 地理矢量标准名；返回 (std_name, method)。method: exact/alias/norm/None"""
    if name in geo_names:
        return name, "exact"
    nn = norm_name(name)
    if nn in geo_norm:
        return geo_norm[nn], "norm"
    if name in NAME_ALIAS and NAME_ALIAS[name] in geo_names:
        return NAME_ALIAS[name], "alias"
    return None, None

# ----------------------------------------------------------------------------
# 2) D1 青藏经济安全指数(县) + D2 经济安全得分
# ----------------------------------------------------------------------------
def load_d1():
    p = os.path.join(ECO, "青藏经济安全指数数据集（2000, 2010, 2020; 地市与县）",
                     "青藏经济安全指数数据集(2000,2010,2020;县).xlsx")
    recs = {}  # (county, year) -> dict
    for sh in ("2000","2010","2020"):
        df = pd.read_excel(p, sheet_name=sh, header=0)
        year = int(sh)
        for _, r in df.iterrows():
            county = str(r.get("县名", "")).strip()
            if not county or county == "nan":
                continue
            d = {
                "prefecture": str(r.get("地市","")).strip(),
                "region": str(r.get("地区","")).strip(),
                "gdp_wan": float(r.get("GDP（亿元）")) * 10000.0,        # 亿元 -> 万元
                "pop_persons": float(r.get("人口（万人）")) * 10000.0,     # 万人 -> 人
                "grain_output_t": float(r.get("粮食产量（吨）")),
                "savings_wan": float(r.get("城乡居民储蓄存款余额（万元）")),
                "sec_industry_wan": float(r.get("第二产业增加值（万元）")),
                "ter_industry_wan": float(r.get("第三产业增加值（万元）")),
            }
            recs[(county, year)] = d
    return recs

def load_d2():
    p = os.path.join(ECO, "青藏经济安全指数数据集（2000, 2010, 2020; 地市与县）",
                     "青藏经济安全指数计算结果(2000,2010,2020;县).xlsx")
    # 地市级经济安全得分： (prefecture, year) -> score
    m = {}
    for sh in ("2000","2010","2020"):
        df = pd.read_excel(p, sheet_name=sh, header=0)
        year = int(sh)
        for _, r in df.iterrows():
            city = str(r.get("城市","")).strip()
            try:
                score = float(r.get("经济安全得分"))
            except (TypeError, ValueError):
                continue
            m[(city, year)] = score
    return m

# ----------------------------------------------------------------------------
# 3) 宽表年序列 (D3/D4/D5/D6)
# ----------------------------------------------------------------------------
def load_wide(sheet_path, sheet_name, county_header_candidates):
    df = pd.read_excel(sheet_path, sheet_name=sheet_name, header=0)
    # 县名列：表头精确匹配候选，或首列为 'Name'/'县名'
    county_col = None
    for c in df.columns:
        if str(c).strip() in county_header_candidates:
            county_col = c; break
    if county_col is None:
        # 退回首列
        county_col = df.columns[0]
    year_cols = {}
    for c in df.columns:
        if c == county_col:
            continue
        m = YEAR_RE.search(str(c))
        if m and re.match(r"^[A-Za-z]*\d{4}$", str(c).strip()):
            year_cols[c] = int(m.group(1))
    out = {}  # county -> {year: value}
    for _, r in df.iterrows():
        name = str(r[county_col]).strip()
        if not name or name == "nan":
            continue
        out.setdefault(name, {})
        for c, y in year_cols.items():
            v = r[c]
            if pd.notna(v):
                try:
                    out[name][y] = float(v)
                except (TypeError, ValueError):
                    pass
    return out

D3 = load_wide(os.path.join(ECO, "青藏高原县域每年人口、粮食、粮食播种面积和年末牲畜量数据集（1980-2020）",
                            "青藏高原1980-2020年县域人口数据.xlsx"), "人口", {"Name","县名","name"})
D4 = load_wide(os.path.join(ECO, "青藏高原县域每年人口、粮食、粮食播种面积和年末牲畜量数据集（1980-2020）",
                            "青藏高原1980-2020年县域牲畜存栏量数据.xlsx"), "牲畜存栏量", {"Name","县名","name"})
D5 = load_wide(os.path.join(ECO, "青藏高原县域每年人口、粮食、粮食播种面积和年末牲畜量数据集（1980-2020）",
                            "青藏高原1980-2020年县域粮食数据.xlsx"), "粮食", {"Name","县名","name"})
D6 = load_wide(os.path.join(ECO, "青藏高原县域每年人口、粮食、粮食播种面积和年末牲畜量数据集（1980-2020）",
                            "青藏高原1980-2020年县域粮食耕地面积数据.xlsx"), "粮食耕地面积", {"Name","县名","name"})

# ----------------------------------------------------------------------------
# 4) GDP 年序列：以既有 GDP_data.csv 为主(2000-2015)，D1 2020 补充
# ----------------------------------------------------------------------------
def load_gdp_csv():
    df = pd.read_csv(GDP_CSV)
    year_cols = [c for c in df.columns if str(c).isdigit()]
    out = {}  # county -> {year: gdp_wan}
    def unit_to_wan(row):
        cells = [str(row[y]) for y in year_cols if pd.notna(row[y])]
        has_yi = any("亿元" in c for c in cells)
        factor = 10000.0 if has_yi else 1.0
        vals = {}
        for y in year_cols:
            v = row[y]
            if pd.isna(v):
                continue
            s = str(v).strip()
            try:
                if "亿元" in s:
                    vals[int(y)] = float(s.replace("亿元","").strip())*10000
                elif "万元" in s:
                    vals[int(y)] = float(s.replace("万元","").strip())
                else:
                    vals[int(y)] = float(s)*factor
            except ValueError:
                pass
        return vals
    for _, r in df.iterrows():
        county = str(r["county"]).strip()
        if not county or county == "nan":
            continue
        out[county] = unit_to_wan(r)
    return out

GDP_CSV_MAP = load_gdp_csv()
D1_REC = load_d1()
D2_MAP = load_d2()

# ----------------------------------------------------------------------------
# 5) 生成“最新年”标量文件
# ----------------------------------------------------------------------------
def latest_of(d):
    """d: {year: value} -> (year, value) 取最近年份"""
    if not d:
        return None, None
    y = max(d.keys())
    return y, d[y]

def build_latest(src_map, unit_note):
    rows = []
    for county, ym in src_map.items():
        y, v = latest_of(ym)
        if v is None or v <= 0:
            continue
        rows.append((county, round(v, 4), y))
    return rows

# 人口最新 (D3)
pop_latest = build_latest(D3, "人")
# 牲畜最新 (D4)
live_latest = build_latest(D4, "头")
# 粮食最新 (D5)
grain_latest = build_latest(D5, "吨")
# 耕地最新 (D6)
arable_latest = build_latest(D6, "公顷")

# GDP 最新：合并 GDP_data.csv(<=2015) 与 D1(2020/2010/2000)
gdp_latest = {}
for c, ym in GDP_CSV_MAP.items():
    y, v = latest_of(ym)
    if v and v > 0:
        gdp_latest[c] = (y, v)
for (c, yr), d in D1_REC.items():
    if d["gdp_wan"] > 0:
        if c not in gdp_latest or yr > gdp_latest[c][0]:
            gdp_latest[c] = (yr, d["gdp_wan"])
gdp_latest_rows = [(c, round(v,2), y) for c,(y,v) in gdp_latest.items()]

# 写最新标量文件
pd.DataFrame(pop_latest, columns=["county","pop","year"]).to_csv(
    os.path.join(OUT_PROC_ECONOMY, "county_population_latest.csv"), index=False, encoding="utf-8-sig")
pd.DataFrame(grain_latest, columns=["county","grain_t","year"]).to_csv(
    os.path.join(OUT_PROC_ECONOMY, "county_grain_latest.csv"), index=False, encoding="utf-8-sig")
pd.DataFrame(arable_latest, columns=["county","arable_area_ha","year"]).to_csv(
    os.path.join(OUT_PROC_ECONOMY, "county_arable_latest.csv"), index=False, encoding="utf-8-sig")
pd.DataFrame(live_latest, columns=["county","livestock_head","year"]).to_csv(
    os.path.join(OUT_PROC_ECONOMY, "county_livestock_latest.csv"), index=False, encoding="utf-8-sig")
pd.DataFrame(gdp_latest_rows, columns=["county","gdp_wan","year"]).to_csv(
    os.path.join(OUT_ECON, "county_gdp_latest.csv"), index=False, encoding="utf-8-sig")

# ----------------------------------------------------------------------------
# 6) 统一长表 county_economy_long.csv
# ----------------------------------------------------------------------------
long_rows = []  # county,prefecture,region,year,indicator,value,unit
def add(county, pref, region, year, ind, val, unit):
    if val is None or (isinstance(val,float) and (val!=val or val<=0)):
        return
    long_rows.append((county, pref or "", region or "", int(year), ind, round(float(val),4), unit))

# D1 基准年(2000/2010/2020)
for (c, yr), d in D1_REC.items():
    pref, region = d["prefecture"], d["region"]
    add(c, pref, region, yr, "gdp_wan", d["gdp_wan"], "万元")
    add(c, pref, region, yr, "pop_persons", d["pop_persons"], "人")
    add(c, pref, region, yr, "grain_output_t", d["grain_output_t"], "吨")
    add(c, pref, region, yr, "savings_wan", d["savings_wan"], "万元")
    add(c, pref, region, yr, "sec_industry_wan", d["sec_industry_wan"], "万元")
    add(c, pref, region, yr, "ter_industry_wan", d["ter_industry_wan"], "万元")
    score = D2_MAP.get((pref, yr))
    if score is not None:
        add(c, pref, region, yr, "econ_security_score", score, "无量纲")

# D3-D6 年序列
for c, ym in D3.items():
    for y, v in ym.items(): add(c, "", "", y, "pop_persons", v, "人")
for c, ym in D4.items():
    for y, v in ym.items(): add(c, "", "", y, "livestock_head", v, "头")
for c, ym in D5.items():
    for y, v in ym.items(): add(c, "", "", y, "grain_t", v, "吨")
for c, ym in D6.items():
    for y, v in ym.items(): add(c, "", "", y, "arable_area_ha", v, "公顷")
# GDP 年序列(已有 GDP_data.csv 2000-2015) 补充基准年之间的年度
for c, ym in GDP_CSV_MAP.items():
    for y, v in ym.items(): add(c, "", "", y, "gdp_wan", v, "万元")

long_df = pd.DataFrame(long_rows, columns=["county","prefecture","region","year","indicator","value","unit"])
long_df = long_df.sort_values(["county","year","indicator"]).reset_index(drop=True)
long_df.to_csv(os.path.join(OUT_ECON, "county_economy_long.csv"), index=False, encoding="utf-8-sig")

# 名称别名表
alias_df = pd.DataFrame(
    [("合作市","夏河县","1998年合作市由夏河县分出；数据集合并单元为夏河县，本系统geojson含夏河县不含合作市，故D1合作市→夏河县")],
    columns=["source_name","target_name","reason"])
alias_df.to_csv(os.path.join(OUT_PROC_ECONOMY, "county_name_alias.csv"), index=False, encoding="utf-8-sig")

# ----------------------------------------------------------------------------
# 7) 校验报告
# ----------------------------------------------------------------------------
def coverage(rows):
    total = len(rows)
    matched = 0; methods = {}
    for c, *_ in rows:
        std, meth = resolve_geo(c)
        if std:
            matched += 1
            methods[meth] = methods.get(meth,0)+1
    return total, matched, methods

report = []
report.append("================ 经济数据预处理校验报告 ================")
report.append(f"输出目录: {OUT_ECON} (GDP/长表), {OUT_PROC_ECONOMY} (人口/耕地/粮食/牲畜/别名)")
report.append("")
for label, rows in [("GDP(最新)", gdp_latest_rows), ("人口(最新)", pop_latest),
                    ("耕地(最新)", arable_latest), ("粮食(最新)", grain_latest),
                    ("牲畜(最新)", live_latest)]:
    t, m, meth = coverage(rows)
    report.append(f"[{label}] 数据县数={t}  匹配地理矢量={m} ({100*m/t:.1f}%)  方式={meth}")
report.append("")
report.append(f"[统一长表] 总行数={len(long_df)}  县域数={long_df['county'].nunique()}  指标={sorted(long_df['indicator'].unique())}")
report.append(f"[统一长表] 年份范围={long_df['year'].min()}~{long_df['year'].max()}")
report.append("")
# 与既有文件一致性
old_pop = pd.read_csv(os.path.join(OUT_PROC_ECONOMY, "county_population_latest.csv"))
old_pop_set = set(old_pop["county"].tolist())
new_pop_set = set([c for c,_,_ in pop_latest])
report.append(f"[人口] 既有文件县数={len(old_pop_set)}  新生成县数={len(new_pop_set)}  "
              f"新增={len(new_pop_set-old_pop_set)} 丢失={len(old_pop_set-new_pop_set)}")
old_gdp = pd.read_csv(GDP_CSV)
old_gdp_set = set(old_gdp["county"].tolist())
new_gdp_set = set([c for c,_,_ in gdp_latest_rows])
report.append(f"[GDP] 既有CSV县数={len(old_gdp_set)}  新最新值县数={len(new_gdp_set)}  "
              f"新增={len(new_gdp_set-old_gdp_set)} 丢失={len(old_gdp_set-new_gdp_set)}")
# 别名命中情况
report.append("")
report.append("[别名映射命中] 夏河县 -> 合作市:")
xia = [c for c,_,_ in pop_latest if c=="夏河县"]
report.append(f"  D3-D6 中含'夏河县'行数(人口/牲畜/粮食/耕地任一): "
              f"{sum(1 for src in (D3,D4,D5,D6) if '夏河县' in src)}")
rep_text = "\n".join(report)
print(rep_text)
with open(os.path.join(OUT_PROC, "_preprocess_validation.txt"), "w", encoding="utf-8") as f:
    f.write(rep_text)

print("\nDONE. Files written.")
