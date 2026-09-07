#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""逐像元 LSTM 径流推理 —— 低内存流式版。

为什么要重写 predict.py
------------------------
原实现有两个硬伤，在 16 GB / 无 GPU 的机器上直接导致"跑不动"：

1. 内存：把「全年 × 全变量 × 全像元」一次性 stack 进内存
       4 变量 × 365 天 × 1560 × 3507 × 4 B ≈ 31.9 GB   → 必然 OOM
2. 速度：逐像元调用模型（约 547 万次前向 + 同等次数的 Python 层 gather）

本脚本的改动
------------
1. **分块流式**：按"像元瓦片"读取与推理，峰值内存由 --tile-pixels / --batch-pixels 控制，
   默认约 1 GB，与栅格总大小解耦。
2. **批量推理**：沿像元维组批，把 547 万次 kernel launch 降到数千次。
3. **批量推理**：沿像元维组批，把 547 万次 kernel launch 降到数千次。
4. **句柄复用**：rasterio 数据集一次性打开后做窗口读取。
   本机实测：复用句柄 0.8 ms/次，反复开关 8.3 ms/次，差 10 倍。
5. **有效像元掩膜**：nodata 像元直接跳过，不参与推理（原实现只跳过、但仍要 gather）。
6. **区域限定**：--bbox 或 --basin 只算指定范围。全研究区 637 万像元里
   四变量交集有效约 58%，再限定到单个流域通常只剩百分之几 —— 这是数量级的提速。
7. torch 线程数对齐 CPU 核数（原默认 4，本机 8 核）。

关于 --seq-window（时间分窗）
----------------------------
最初的设计意图是"分窗后激活值变小，从而可以把像元批放大十几倍"。
**本机实测结论相反**：CPU 上 LSTM 的瓶颈不是批并行度（hidden 只有 100，矩阵很小），
分窗反而多出 Python 循环与状态搬运开销。全年 T=365 实测：

    不分窗 batch=701   → 1.265 ms/像元，峰值 1887 MB
    分窗30 batch=8533  → 2.002 ms/像元，峰值 2195 MB   （慢 58%）
    分窗60 batch=4266  → 1.828 ms/像元，峰值 2195 MB   （慢 45%）

因此默认 **不分窗**。--seq-window 保留仅作实验用（显存受限的 GPU 场景可能仍有用）。

批大小怎么选
------------
实测（T=365，2.3 万有效像元）：

    batch=256  → 1.404 ms/像元，峰值 1193 MB
    batch=512  → 1.411 ms/像元，峰值 1539 MB
    batch=701  → 1.265 ms/像元，峰值 1887 MB   ← 最快
    batch=1024 → 1.472 ms/像元，峰值 2203 MB
    batch=2048 → OOM（试图分配 2.6 GB 失败）

即：**批大小对速度影响不到 15%，对内存却是线性影响**。
16 GB 机器上建议 batch=512（--act-budget-mb 256），把内存让给读取缓冲。

数值约定
--------
与旧实现一致：每个像元是 batch=1、长度 T 的独立序列（seq=时间, feature=变量）。

唯一的输出差异：旧实现无条件对结果做 `max - (x - min)` 翻转（逐时相空间反转，
破坏量纲，无法与阈值场比较）。本脚本**默认不翻转**；如需复现旧行为请显式加 --flip。

用法示例
--------
    # 全高原（默认，内存约 1 GB）
    python predict_streaming.py

    # 只算叶尔羌流域（强烈推荐，快 1~2 个数量级）
    python predict_streaming.py --basin data/raw/geo/TP_Basins_China.geojson --basin-name 叶尔羌河

    # 先看资源规划再决定要不要跑（不读数据、不算）
    python predict_streaming.py --dry-run
"""

import argparse
import glob
import math
import os
import re
import sys
import time

import numpy as np
import rasterio
from rasterio.windows import Window
import torch
import torch.nn as nn

try:
    from tqdm import tqdm
except ImportError:  # tqdm 缺失时退化为无进度条
    def tqdm(x, **kwargs):
        return x


# ===================== 模型定义（与训练保持一致）=====================

class RunoffLSTM(nn.Module):
    """两层 LSTM + FC + ReLU。

    与旧实现的差异仅在于 forward 额外返回 (h, c)，用于时间分窗时的状态传递；
    参数结构与 state_dict 完全一致，可直接加载 lstm_runoff_model.pt。
    """

    def __init__(self, input_size, hidden_size=100, num_layers=2, dropout=0.0):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )
        self.fc = nn.Linear(hidden_size, 1)
        self.relu = nn.ReLU()

    def forward(self, x, state=None):
        out, state = self.lstm(x, state)
        return self.relu(self.fc(out).squeeze(-1)), state


# ===================== 数据读取 =====================

def discover_dates(input_dir, variables, pattern):
    """从第一个变量的目录里解析出所有日期（升序）。"""
    var_dir = os.path.join(input_dir, variables[0])
    files = sorted(glob.glob(os.path.join(var_dir, '*.tif')))
    if not files:
        raise FileNotFoundError(f'未找到任何 tif：{var_dir}')

    regex = re.compile(re.escape(pattern).replace(r'\{var\}', variables[0])
                       .replace(r'\{date\}', r'(?P<date>\d{4}-\d{2}-\d{2})'))
    dates = []
    for f in files:
        m = regex.match(os.path.basename(f))
        if m:
            dates.append(m.group('date'))
    if not dates:
        raise ValueError(f'文件名与 --pattern "{pattern}" 不匹配，'
                         f'示例文件名：{os.path.basename(files[0])}')
    return sorted(set(dates))


def check_grid_consistency(datasets, variables, dates, ref_fp):
    """校验所有数据集与参考网格一致。

    历史坑：temperature_2m 的前 104 天曾被裁剪成 3507 列，其余变量是 4082 列，
    导致 simulate.py 的 np.stack 直接抛 ValueError（被宽 except 吞掉，接口 500）。
    这里提前拦住并给出可执行的修复命令。
    """
    bad = []
    for vi, row in enumerate(datasets):
        for ti, src in enumerate(row):
            fp = (src.height, src.width, src.transform)
            if fp != ref_fp:
                bad.append((variables[vi], dates[ti], src.height, src.width))
    if bad:
        v, d, h, w = bad[0]
        raise ValueError(
            f'栅格网格不一致：共 {len(bad)} 个文件，例如 {v}/{d} 为 {h}×{w}，'
            f'而基准为 {ref_fp[0]}×{ref_fp[1]}。\n'
            f'  请先执行：python fix_grid_consistency.py --check\n'
            f'  确认后执行：python fix_grid_consistency.py --fix')


def open_all_datasets(input_dir, variables, dates, pattern):
    """一次性打开全部数据集并复用句柄：ds[var_idx][t_idx]。"""
    ds = []
    for vi, var in enumerate(variables):
        row = []
        for ti, date in enumerate(dates):
            path = os.path.join(input_dir, var, pattern.format(var=var, date=date))
            if not os.path.isfile(path):
                raise FileNotFoundError(f'缺少数据文件：{path}')
            row.append(rasterio.open(path))
        ds.append(row)
    ref = ds[0][0]
    check_grid_consistency(ds, variables, dates,
                           (ref.height, ref.width, ref.transform))
    return ds


def build_basin_mask(geom, height, width, transform):
    """把流域几何栅格化为布尔掩膜（True = 流域内）。"""
    from rasterio.features import geometry_mask
    return geometry_mask([geom], out_shape=(height, width),
                         transform=transform, invert=True)


def bbox_to_window(bbox_str, transform, height, width):
    """把 "lon0,lat0,lon1,lat1" 转成 (r0, r1, c0, c1) 栅格窗口。"""
    parts = [float(x) for x in bbox_str.split(',')]
    if len(parts) != 4:
        raise ValueError('--bbox 需要 4 个数：lon0,lat0,lon1,lat1')
    lon0, lat0, lon1, lat1 = parts
    lon0, lon1 = min(lon0, lon1), max(lon0, lon1)
    lat0, lat1 = min(lat0, lat1), max(lat0, lat1)

    c0 = int(np.floor((lon0 - transform.c) / transform.a))
    c1 = int(np.ceil((lon1 - transform.c) / transform.a))
    r0 = int(np.floor((transform.f - lat1) / (-transform.e)))
    r1 = int(np.ceil((transform.f - lat0) / (-transform.e)))

    r0, r1 = max(0, r0), min(height, r1)
    c0, c1 = max(0, c0), min(width, c1)
    if r0 >= r1 or c0 >= c1:
        raise ValueError(f'--bbox {bbox_str} 与研究区无交集')
    return r0, r1, c0, c1


def load_geometry(basin_path, basin_name, basin_index):
    """从 GeoJSON/Shapefile 中取出指定流域的 geometry（EPSG:4326）。"""
    import json
    from rasterio.warp import transform_geom

    if basin_path.lower().endswith('.geojson') or basin_path.lower().endswith('.json'):
        with open(basin_path, 'r', encoding='utf-8') as f:
            gj = json.load(f)
        features = gj.get('features', [])
    else:
        import geopandas as gpd
        gdf = gpd.read_file(basin_path)
        if gdf.crs is not None:
            gdf = gdf.to_crs('EPSG:4326')
        features = json.loads(gdf.to_json()).get('features', [])

    if not features:
        raise ValueError(f'流域文件里没有要素：{basin_path}')

    if basin_index is not None:
        return features[basin_index]['geometry']
    if basin_name:
        for feat in features:
            props = feat.get('properties', {})
            if basin_name in [str(v) for v in props.values() if v is not None]:
                return feat['geometry']
        raise ValueError(f'未找到名为 {basin_name} 的流域')

    return features[0]['geometry']


# ===================== 瓦片切分 =====================

def iter_tiles(r_start, r_end, tile_rows):
    """按行切瓦片（列方向不切，保证窗口读取是整行，IO 最省）。"""
    for r0 in range(r_start, r_end, tile_rows):
        r1 = min(r0 + tile_rows, r_end)
        yield r0, r1


def tile_row_extent(mask, r0, r1):
    """返回该行段内有效像元覆盖的列范围 [c0, c1)；整段无效返回 None。"""
    strip = mask[r0:r1]
    if not strip.any():
        return None
    cols = np.where(strip.any(axis=0))[0]
    return int(cols[0]), int(cols[-1]) + 1


# ===================== 推理 =====================

@torch.no_grad()
def infer_tile(model, cube, batch_pixels, seq_window, device):
    """对一个瓦片做推理。

    cube: (T, n_rows, n_cols, V) float32
    返回: (T, n_rows, n_cols) float32，无效像元为 NaN
    """
    T, nr, nc, V = cube.shape

    # 有效像元掩膜：任一时间、任一变量为 NaN 则整条序列作废（与旧实现一致）
    valid = ~np.isnan(cube).any(axis=(0, 3))          # (nr, nc)
    out = np.full((T, nr, nc), np.nan, dtype=np.float32)
    if not valid.any():
        return out

    # (T, P, V) -> (P, T, V)，只搬有效像元
    X = cube[:, valid]                                 # (T, P, V)
    X = np.ascontiguousarray(X.transpose(1, 0, 2))     # (P, T, V)
    P = X.shape[0]

    res = np.empty((P, T), dtype=np.float32)

    for s in range(0, P, batch_pixels):
        e = min(s + batch_pixels, P)
        xb = torch.from_numpy(X[s:e]).to(device)

        if seq_window and seq_window < T:
            # 时间分窗 + 状态传递：与一次性展开整条序列数学等价
            h = c = None
            for t0 in range(0, T, seq_window):
                y, (h, c) = model(xb[:, t0:t0 + seq_window, :],
                                  None if h is None else (h, c))
                res[s:e, t0:t0 + y.shape[1]] = y.cpu().numpy()
                h, c = h.detach(), c.detach()
        else:
            y, _ = model(xb)
            res[s:e] = y.cpu().numpy()

    out[:, valid] = res.T                              # (T, P) -> (T, nr, nc)
    return out


# ===================== 写出 =====================

def write_outputs(out_mm, dates, output_dir, profile, flip):
    """把 memmap 里的结果写成逐日 GeoTIFF。"""
    os.makedirs(output_dir, exist_ok=True)
    T = len(dates)
    paths = []
    for t in tqdm(range(T), desc='写出逐日结果', unit='d'):
        arr = np.array(out_mm[t])                      # (H, W) float32

        if flip:
            # 旧实现的翻转：逐时相空间反转。会破坏量纲，仅用于复现历史结果。
            mn, mx = np.nanmin(arr), np.nanmax(arr)
            if np.isfinite(mn) and np.isfinite(mx):
                arr = (mx - (arr - mn)).astype(np.float32)

        p = os.path.join(output_dir, f'{dates[t]}_runoff.tif')
        with rasterio.open(p, 'w', **profile) as dst:
            dst.write(arr, 1)
        paths.append(p)
    return paths


# ===================== 主流程 =====================

def parse_args(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, '..', '..'))
    default_input = os.path.join(root, 'data', 'raw', 'TibetanPlateau')

    ap = argparse.ArgumentParser(
        description='逐像元 LSTM 径流推理（低内存流式版）',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    ap.add_argument('--input', default=default_input, help='驱动数据根目录（下含各变量子目录）')
    ap.add_argument('--variables', nargs='+',
                    default=['temperature_2m', 'temperature_2m_max',
                             'total_precipitation_max', 'total_precipitation_sum'])
    ap.add_argument('--pattern', default='{var}_{date}.tif',
                    help='文件名模板；旧数据用 {date}_clipped.tif')
    ap.add_argument('--model', default=os.path.join(root, 'data', 'raw', 'models',
                                                    'lstm_runoff_model.pt'))
    ap.add_argument('--output', default=os.path.join(root, 'data', 'processed', 'runoff_tifs'))
    ap.add_argument('--start-date', default=None, help='只处理该日期及之后（默认全部）')
    ap.add_argument('--end-date', default=None, help='只处理该日期及之前（默认全部）')

    ap.add_argument('--tile-pixels', type=int, default=65536,
                    help='单个瓦片的像元数，决定读取缓冲大小')
    ap.add_argument('--batch-pixels', type=int, default=0,
                    help='单次前向的像元数，决定 LSTM 激活值内存；0=按预算自动推')
    ap.add_argument('--seq-window', type=int, default=0,
                    help='时间分窗长度（步）。0=不分窗；建议 30~60 以放大批大小')
    ap.add_argument('--act-budget-mb', type=float, default=256.0,
                    help='LSTM 激活值内存预算（MB），用于自动推 batch-pixels')

    ap.add_argument('--bbox', default=None,
                    help='只算该经纬度范围，格式 "lon0,lat0,lon1,lat1"，'
                         '例："74,35.5,80,40" 覆盖叶尔羌河一带')
    ap.add_argument('--basin', default=None, help='流域边界 GeoJSON/Shapefile 路径')
    ap.add_argument('--basin-name', default=None, help='流域名称（在属性中模糊匹配）')
    ap.add_argument('--basin-index', type=int, default=None, help='流域要素索引')

    ap.add_argument('--flip', action='store_true',
                    help='复现旧实现的数值翻转（破坏量纲，仅用于对照）')
    ap.add_argument('--limit-rows', type=int, default=0, help='只处理前 N 行（调试用）')
    ap.add_argument('--dry-run', action='store_true', help='只打印资源规划，不读数据不计算')
    ap.add_argument('--tmp', default=None, help='结果 memmap 临时目录（默认系统临时目录）')

    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # 让 torch 用满 CPU（默认只开 4 线程，本机 8 核）
    n_threads = os.cpu_count() or 4
    torch.set_num_threads(n_threads)

    variables = args.variables
    V = len(variables)

    dates = discover_dates(args.input, variables, args.pattern)
    if args.start_date:
        dates = [d for d in dates if d >= args.start_date]
    if args.end_date:
        dates = [d for d in dates if d <= args.end_date]
    if not dates:
        raise SystemExit('筛选后没有可用日期')
    T = len(dates)

    first = os.path.join(args.input, variables[0],
                         args.pattern.format(var=variables[0], date=dates[0]))
    with rasterio.open(first) as ref:
        H, W = ref.height, ref.width
        transform = ref.transform
        profile = ref.profile.copy()
    profile.update(count=1, dtype=rasterio.float32, compress='lzw', nodata=np.nan)

    if args.limit_rows:
        H = min(H, args.limit_rows)

    # ---- 有效区掩膜（bbox / 流域 / 全图）----
    if args.bbox:
        r0, r1, c0, c1 = bbox_to_window(args.bbox, transform, H, W)
        mask = np.zeros((H, W), dtype=bool)
        mask[r0:r1, c0:c1] = True
        scope = f'bbox {args.bbox}（行 {r0}-{r1}，列 {c0}-{c1}）'
    elif args.basin:
        geom = load_geometry(args.basin, args.basin_name, args.basin_index)
        mask = build_basin_mask(geom, H, W, transform)
        scope = f'流域 {args.basin_name or args.basin_index or ""}'
    else:
        mask = np.ones((H, W), dtype=bool)
        scope = '全研究区'

    use_mask = bool(args.basin or args.bbox)

    # ---- 子窗口：把计算与缓存都收窄到掩膜的外接矩形 ----
    rows_any = np.where(mask.any(axis=1))[0]
    cols_any = np.where(mask.any(axis=0))[0]
    r_start, r_end = int(rows_any[0]), int(rows_any[-1]) + 1
    c_start, c_end = int(cols_any[0]), int(cols_any[-1]) + 1
    Hsub, Wsub = r_end - r_start, c_end - c_start

    # 把输出 profile 平移到子窗口，保证写出结果的空间位置仍然正确
    a, b, c_, d, e, f = (transform.a, transform.b, transform.c,
                         transform.d, transform.e, transform.f)
    sub_transform = type(transform)(a, b, c_ + c_start * a + r_start * b,
                                    d, e, f + c_start * d + r_start * e)
    profile.update(height=Hsub, width=Wsub, transform=sub_transform)

    # ---- 尺寸与预算规划 ----
    tile_rows = max(1, args.tile_pixels // Wsub)
    seq_window = args.seq_window if args.seq_window and args.seq_window < T else T

    if args.batch_pixels:
        batch_pixels = args.batch_pixels
    else:
        # 激活值粗估：每步每像元约 hidden*5 个 float（输出 + 4 个门）
        hidden = 100
        per_pixel = seq_window * hidden * 5 * 4
        batch_pixels = max(256, int(args.act_budget_mb * 1024 * 1024 / per_pixel))
    batch_pixels = min(batch_pixels, max(256, tile_rows * Wsub))

    read_buf_mb = tile_rows * Wsub * T * V * 4 / 1024 ** 2
    act_mb = batch_pixels * seq_window * 100 * 5 * 4 / 1024 ** 2
    out_mm_mb = T * Hsub * Wsub * 4 / 1024 ** 2
    n_tiles = math.ceil(Hsub / tile_rows)

    print('=' * 62)
    print('  径流推理 · 低内存流式版')
    print('=' * 62)
    print(f'  全栅格        : {H} × {W} = {H*W:,} 像元')
    print(f'  计算范围      : {scope}')
    print(f'    实际子窗口  : 行 {r_start}-{r_end}，列 {c_start}-{c_end}'
          f'（{Hsub} × {Wsub} = {Hsub*Wsub:,}）')
    print(f'    掩膜内像元  : {int(mask.sum()):,}')
    print(f'  时间          : {T} 天  {dates[0]} ~ {dates[-1]}')
    print(f'  变量          : {V} 个')
    print(f'  瓦片          : {tile_rows} 行/块，共 {n_tiles} 块')
    print(f'  像元批        : {batch_pixels:,}（时间分窗 {seq_window} 步）')
    print(f'  CPU 线程      : {n_threads}')
    print(f'  读取缓冲      : ~{read_buf_mb:.0f} MB')
    print(f'  激活值        : ~{act_mb:.0f} MB')
    print(f'  结果缓存(磁盘): ~{out_mm_mb/1024:.2f} GB（内存外，落 --tmp 目录）')
    print(f'  预估峰值内存  : ~{(read_buf_mb*2 + act_mb + 300)/1024:.2f} GB')
    print('=' * 62)

    if args.dry_run:
        print('  --dry-run：仅规划，未执行计算。')
        return 0

    # ---- 结果缓存（磁盘 memmap，避免常驻 8 GB 内存）----
    tmp_dir = args.tmp or os.path.join(os.path.dirname(args.output), '_tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    mm_path = os.path.join(tmp_dir, 'runoff_cache.dat')
    out_mm = np.memmap(mm_path, dtype='float32', mode='w+', shape=(T, Hsub, Wsub))
    out_mm[:] = np.nan

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = RunoffLSTM(input_size=V).to(device)
    try:
        state = torch.load(args.model, map_location=device, weights_only=True)
    except TypeError:
        state = torch.load(args.model, map_location=device)
    if isinstance(state, dict) and 'state_dict' in state:
        state = state['state_dict']
    model.load_state_dict(state)
    model.eval()
    print(f'  模型已加载    : {os.path.basename(args.model)}（{device}）')

    ds = open_all_datasets(args.input, variables, dates, args.pattern)
    print(f'  数据集句柄    : {V*T} 个（复用，避免反复开关）')

    t_start = time.time()
    done_px = 0
    total_px = int(mask.sum())

    try:
        for r0, r1 in tqdm(list(iter_tiles(r_start, r_end, tile_rows)),
                           desc='推理', unit='块'):
            extent = tile_row_extent(mask, r0, r1)
            if extent is None:
                out_mm[:, r0 - r_start:r1 - r_start, :] = np.nan
                continue
            c0, c1 = extent
            nr, nc = r1 - r0, c1 - c0

            # (T, nr, nc, V)
            cube = np.empty((T, nr, nc, V), dtype=np.float32)
            for vi in range(V):
                for ti in range(T):
                    src = ds[vi][ti]
                    band = src.read(1, window=Window(c0, r0, nc, nr))
                    if src.nodata is not None:
                        band = np.where(band == src.nodata, np.nan, band)
                    cube[ti, :, :, vi] = band
            cube = cube.astype(np.float32, copy=False)

            # 掩膜外的像元直接判无效，不进模型
            if use_mask:
                cube[:, ~mask[r0:r1, c0:c1], :] = np.nan

            out = infer_tile(model, cube, batch_pixels, seq_window, device)
            out_mm[:, r0 - r_start:r1 - r_start, c0 - c_start:c1 - c_start] = out
            done_px += int(np.isfinite(out[0]).sum())
    finally:
        for row in ds:
            for d in row:
                try:
                    d.close()
                except Exception:
                    pass

    elapsed = time.time() - t_start
    print(f'\n  推理完成：{elapsed/60:.1f} 分钟，有效像元 {done_px:,}/{total_px:,}')

    out_mm.flush()
    write_outputs(out_mm, dates, args.output, profile, args.flip)
    del out_mm
    try:
        os.remove(mm_path)
    except OSError:
        pass

    print(f'  结果目录：{args.output}')
    print(f'  总耗时  ：{(time.time()-t_start)/60:.1f} 分钟')
    return 0


if __name__ == '__main__':
    sys.exit(main())
