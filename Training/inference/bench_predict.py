#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""新旧推理实现对比基准（真实数据）。

在同一个数据子集上分别跑：
  A. 旧实现（predict.py）：全年 stack 进内存 + 逐像元 Python 循环
  B. 新实现（predict_streaming.py）：分块流式 + 像元批量 + 时间分窗状态传递

输出：耗时、峰值内存、数值最大偏差，并按子集线性外推到全研究区。

用法：
    python bench_predict.py                      # 默认 8 行 × 20 天
    python bench_predict.py --rows 4 --days 10   # 更快
    python bench_predict.py --rows 16 --days 30  # 更准
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import rasterio
from rasterio.windows import Window

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from predict_streaming import RunoffLSTM, infer_tile, discover_dates, open_all_datasets  # noqa: E402

VARIABLES = ['temperature_2m', 'temperature_2m_max',
             'total_precipitation_max', 'total_precipitation_sum']


class OldRunoffLSTM(nn.Module):
    """旧 predict.py 里的模型定义（forward 只返回张量）。"""

    def __init__(self, input_size):
        super().__init__()
        self.lstm = nn.LSTM(input_size=input_size, hidden_size=100,
                            num_layers=2, batch_first=True, dropout=0.2)
        self.fc = nn.Linear(100, 1)
        self.relu = nn.ReLU()

    def forward(self, x):
        out, _ = self.lstm(x)
        return self.relu(self.fc(out).squeeze(-1))


# ---------- 内存测量（Windows）----------

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
    ]


_ctypes_ready = False


def _init_ctypes():
    global _ctypes_ready
    if _ctypes_ready:
        return
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    ctypes.windll.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    _ctypes_ready = True


def mem_mb():
    _init_ctypes()
    c = PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    if not ok:
        return -1.0
    return c.PeakWorkingSetSize / 1024 ** 2


def reset_peak():
    mem_mb()


# ---------- 两种实现 ----------

def run_old(ds, rows, W, T, V, model, device, row_offset=0):
    """旧实现：先 stack 成 (V, T, rows, W)，再逐像元推理。"""
    win = Window(0, row_offset, W, rows)
    var_stacks = []
    for row in ds:
        stack = []
        for src in row:
            img = src.read(1, window=win)
            if src.nodata is not None:
                img = np.where(img == src.nodata, np.nan, img)
            stack.append(img)
        var_stacks.append(np.stack(stack, axis=0))          # (T, rows, W)

    results = np.full((T, rows, W), np.nan, dtype=np.float32)
    n_done = 0
    with torch.no_grad():
        for i in range(rows):
            for j in range(W):
                ps = np.stack([var_stacks[k][:, i, j] for k in range(V)], axis=-1)
                if np.isnan(ps).any():
                    continue
                x = torch.tensor(ps, dtype=torch.float32).unsqueeze(0).to(device)
                results[:, i, j] = model(x).squeeze(0).cpu().numpy()
                n_done += 1
    return results, n_done


def run_new(ds, rows, W, T, V, model, device, batch_pixels, seq_window, row_offset=0):
    """新实现：组 cube (T, rows, cols, V) 后批量推理。"""
    win = Window(0, row_offset, W, rows)
    cube = np.empty((T, rows, W, V), dtype=np.float32)
    for vi, row in enumerate(ds):
        for ti, src in enumerate(row):
            band = src.read(1, window=win)
            if src.nodata is not None:
                band = np.where(band == src.nodata, np.nan, band)
            cube[ti, :, :, vi] = band
    out = infer_tile(model, cube, batch_pixels, seq_window, device)
    return out, int(np.isfinite(out[0]).sum())


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, '..', '..'))
    default_input = os.path.join(root, 'data', 'raw', 'TibetanPlateau')

    ap = argparse.ArgumentParser()
    ap.add_argument('--input', default=default_input)
    ap.add_argument('--model', default=os.path.join(root, 'data', 'raw', 'models',
                                                    'lstm_runoff_model.pt'))
    ap.add_argument('--rows', type=int, default=8, help='子集行数')
    ap.add_argument('--row-offset', type=int, default=700,
                    help='起始行（默认 700 = 研究区中部，有效像元密集）')
    ap.add_argument('--days', type=int, default=20, help='子集天数')
    ap.add_argument('--batch-pixels', type=int, default=8192)
    ap.add_argument('--seq-window', type=int, default=0, help='0=不分窗（与旧实现严格一致）')
    ap.add_argument('--skip-old', action='store_true', help='跳过旧实现（太慢时）')
    args = ap.parse_args()

    torch.set_num_threads(os.cpu_count() or 4)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    all_dates = discover_dates(args.input, VARIABLES, '{var}_{date}.tif')
    dates = all_dates[:args.days]
    T = len(dates)
    V = len(VARIABLES)
    rows = args.rows

    with rasterio.open(os.path.join(args.input, VARIABLES[0],
                                    f'{VARIABLES[0]}_{dates[0]}.tif')) as ref:
        H_full, W = ref.height, ref.width

    print('=' * 70)
    print('  新旧推理实现对比基准（真实数据）')
    print('=' * 70)
    print(f'  全研究区      : {H_full} × {W} = {H_full*W:,} 像元')
    print(f'  基准子集      : {rows} 行 × {W} 列 = {rows*W:,} 像元，{T} 天')
    print(f'  CPU 线程      : {torch.get_num_threads()}   设备：{device}')
    print('=' * 70)

    ds = open_all_datasets(args.input, VARIABLES, dates, '{var}_{date}.tif')

    # 模型
    old_model = OldRunoffLSTM(input_size=V).to(device)
    state = torch.load(args.model, map_location=device, weights_only=True)
    old_model.load_state_dict(state)
    old_model.eval()

    new_model = RunoffLSTM(input_size=V).to(device)
    new_model.load_state_dict(state)
    new_model.eval()

    results = {}

    if not args.skip_old:
        reset_peak()
        t0 = time.time()
        old_out, old_n = run_old(ds, rows, W, T, V, old_model, device, args.row_offset)
        old_t = time.time() - t0
        old_m = mem_mb()
        results['old'] = (old_out, old_t, old_m, old_n)
        print(f'\n[A] 旧实现  predict.py')
        print(f'    耗时      : {old_t:.2f} s')
        print(f'    峰值内存  : {old_m:.0f} MB（子集）')
        print(f'    有效像元  : {old_n:,} / {rows*W:,}')

    reset_peak()
    t0 = time.time()
    new_out, new_n = run_new(ds, rows, W, T, V, new_model, device,
                             args.batch_pixels, args.seq_window, args.row_offset)
    new_t = time.time() - t0
    new_m = mem_mb()
    results['new'] = (new_out, new_t, new_m, new_n)
    print(f'\n[B] 新实现  predict_streaming.py（batch={args.batch_pixels}, '
          f'分窗={args.seq_window or "不分窗"}）')
    print(f'    耗时      : {new_t:.2f} s')
    print(f'    峰值内存  : {new_m:.0f} MB（子集）')
    print(f'    有效像元  : {new_n:,} / {rows*W:,}')

    # 数值一致性
    if 'old' in results:
        a, b = results['old'][0], results['new'][0]
        both = np.isfinite(a) & np.isfinite(b)
        only_a = np.isfinite(a) & ~np.isfinite(b)
        only_b = ~np.isfinite(a) & np.isfinite(b)
        print(f'\n[一致性]')
        print(f'    双方都有效: {int(both.sum()):,} 像元；仅旧有效: {int(only_a.sum()):,}；'
              f'仅新有效: {int(only_b.sum()):,}')
        if both.sum():
            d = np.abs(a[both] - b[both])
            print(f'    最大绝对偏差: {d.max():.3e}   平均: {d.mean():.3e}')
            print(f'    相对偏差(相对旧值均值): {d.max()/max(1e-12, np.abs(a[both]).mean()):.3e}')
            print(f'    结论: {"数值一致（差异在 float32 累加误差内）" if d.max() < 1e-3 else "数值不一致，需排查"}')

    # 外推到全研究区
    print(f'\n[外推到全研究区 · 全年 365 天]')
    total_px = H_full * W
    old_full_gb = V * 365 * H_full * W * 4 / 1024 ** 3
    print(f'  内存：')
    print(f'    旧实现  : 需 stack {old_full_gb:.1f} GB  → 16 GB 机器上必然 OOM')
    print(f'    新实现  : 由 --tile-pixels / --batch-pixels 控制，'
          f'默认约 1.5 GB（见 predict_streaming.py --dry-run）')
    if 'old' in results:
        per_px = results['old'][1] / max(1, results['old'][3])
        est_old = per_px * total_px
        print(f'  耗时（按有效像元线性外推）：')
        print(f'    旧实现  : {est_old/3600:,.1f} 小时  '
              f'（{per_px*1000:.2f} ms/像元 × {total_px:,}）')
    per_px_new = results['new'][1] / max(1, results['new'][3])
    est_new = per_px_new * total_px
    print(f'    新实现  : {est_new/3600:,.1f} 小时  '
          f'（{per_px_new*1000:.3f} ms/像元 × {total_px:,}）')
    if 'old' in results:
        print(f'    提速    : {results["old"][1]/max(1e-9, results["new"][1]):.0f} ×')
    print(f'  注：外推未计 IO 差异；实际运行建议加 --basin 只算流域。')

    for row in ds:
        for d in row:
            d.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
