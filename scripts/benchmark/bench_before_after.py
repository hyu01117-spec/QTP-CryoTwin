# -*- coding: utf-8 -*-
"""径流模拟：新旧实现在同一台机器、同一份数据上的实测对比。

对比三种口径（均含 读盘 → 推理 → 写盘 全链路）：
  A. 旧实现：全像元都算、像元被当成时间步、batch=1024
  B. 旧语义 + 只算有效像元：隔离"掩膜"这一项的单独收益
  C. 新引擎：修正语义（像元做 batch）+ 只算有效像元 + batch=16384
  D. 新引擎 + 流域掩膜前置 + warmup：真实业务场景

运行：python bench_before_after.py
"""
import os
import sys
import time
import tempfile

import numpy as np
import rasterio
import torch

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir))  # 项目根（脚本已移至 scripts/benchmark/，回退两层）
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'webgis_backend'))

from modules.runoff_engine import RunoffLSTM, simulate_runoff  # noqa: E402


class OldStyleRunoffLSTM(RunoffLSTM):
    """旧 simulate.py 里的模型签名：forward(x) 只返回张量。

    参数结构与 RunoffLSTM 完全一致，仅用于复刻旧实现的调用方式。
    """

    def forward(self, x):
        out, _ = self.lstm(x)
        return self.relu(self.fc(out).squeeze(-1))

DATA_DIR = os.path.join(ROOT, 'data', 'raw', 'TibetanPlateau')
MODEL_PATH = os.path.join(ROOT, 'data', 'raw', 'models', 'lstm_runoff_model.pt')
VARIABLES = ['temperature_2m', 'temperature_2m_max',
             'total_precipitation_max', 'total_precipitation_sum']

torch.set_num_threads(os.cpu_count() or 4)


def hr(t):
    return f"{t:.1f}s" if t < 90 else f"{t/60:.2f}min"


def _load(cls):
    m = cls(input_size=len(VARIABLES))
    st = torch.load(MODEL_PATH, map_location='cpu', weights_only=True)
    if isinstance(st, dict) and 'state_dict' in st:
        st = st['state_dict']
    m.load_state_dict(st)
    m.eval()
    return m


def load_model():
    """返回 (新引擎模型, 旧签名模型)，两者权重完全相同。"""
    return _load(RunoffLSTM), _load(OldStyleRunoffLSTM)


def read_day(date):
    """读某天全部变量，返回 (V, H, W) 与 nodata 值。"""
    arrs, nods = [], []
    for v in VARIABLES:
        p = os.path.join(DATA_DIR, v, f'{v}_{date}.tif')
        with rasterio.open(p) as src:
            arrs.append(src.read(1))
            nods.append(src.nodata)
    return np.stack(arrs, axis=0), nods


def run_old(date, model, out_dir, valid_only=False, batch_size=1024):
    """忠实复刻旧 simulate.py 的逐日推理循环。

    旧实现要点：batch=1，把 batch_size 个像元当成 batch_size 个时间步
    （.unsqueeze(0) → (1, B, V)）；对全部像元（含 nodata）都做前向，
    算完再用 valid_mask 挑出有效值。
    """
    cube, nods = read_day(date)
    V, H, W = cube.shape
    nodata = nods[0] if nods[0] is not None else -9999

    flats = []
    for vi in range(V):
        band = cube[vi]
        band = np.nan_to_num(band, nan=nodata)
        flats.append(band.flatten())
    input_array = np.stack(flats, axis=1)              # (H*W, V)
    valid_mask = ~np.any(input_array == nodata, axis=1)

    work_idx = np.flatnonzero(valid_mask) if valid_only else None
    total = int(valid_mask.sum()) if valid_only else H * W

    output = np.full(H * W, np.nan, dtype=np.float32)
    n_batch = 0
    with torch.no_grad():
        if valid_only:
            for s in range(0, total, batch_size):
                e = min(s + batch_size, total)
                idx = work_idx[s:e]
                x = torch.FloatTensor(input_array[idx]).unsqueeze(0)
                y = model(x).cpu().numpy()[0]
                output[idx] = y
                n_batch += 1
        else:
            for i in range(0, H * W, batch_size):
                e = min(i + batch_size, H * W)
                batch_data = input_array[i:e]
                bvm = valid_mask[i:e]
                if not np.any(bvm):
                    continue
                x = torch.FloatTensor(batch_data).unsqueeze(0)
                y = model(x).cpu().numpy()[0]
                output[i:e][bvm] = y[bvm]
                n_batch += 1

    out = output.reshape(H, W).astype(np.float32)
    p = os.path.join(out_dir, f'{"oldv" if valid_only else "old"}_{date}.tif')
    with rasterio.open(p, 'w', driver='GTiff', height=H, width=W, count=1,
                       dtype='float32', compress='zstd', predictor=3,
                       nodata=np.nan) as dst:
        dst.write(out, 1)
    return n_batch, total


def main():
    model, old_model = load_model()
    dates = ['2024-06-01', '2024-06-02']
    out_dir = tempfile.mkdtemp(prefix='bench_ba_')
    results = {}

    print(f'对比日期: {dates}（{len(dates)} 天）| 线程 {torch.get_num_threads()}')
    print(f'输出目录: {out_dir}\n')

    print('=' * 68)
    print('A. 旧实现（全像元都算，像元当时间步，batch=1024）')
    print('=' * 68)
    t0 = time.perf_counter()
    for d in dates:
        run_old(d, old_model, out_dir, valid_only=False)
    t_a = (time.perf_counter() - t0) / len(dates)
    print(f'  单日 {hr(t_a)}  |  30 天 {hr(t_a*30)}')
    results['A'] = t_a

    print()
    print('=' * 68)
    print('B. 旧语义 + 只算有效像元（隔离掩膜收益）')
    print('=' * 68)
    t0 = time.perf_counter()
    for d in dates:
        run_old(d, old_model, out_dir, valid_only=True)
    t_b = (time.perf_counter() - t0) / len(dates)
    print(f'  单日 {hr(t_b)}  |  提速 {t_a/t_b:.1f}×（仅掩膜）')
    results['B'] = t_b

    print()
    print('=' * 68)
    print('C. 新引擎（修正语义 + 有效像元 + batch=16384）')
    print('=' * 68)
    day_files = {d: {v: os.path.join(DATA_DIR, v, f'{v}_{d}.tif') for v in VARIABLES}
                 for d in dates}
    ref = day_files[dates[0]][VARIABLES[0]]
    t0 = time.perf_counter()
    for d in dates:
        simulate_runoff(model=model, device=torch.device('cpu'), variables=VARIABLES,
                        day_files={d: day_files[d]}, all_dates=[d], out_dates=[d],
                        ref_path=ref, output_dir=out_dir, basin_geometry=None,
                        batch_pixels=16384)
    t_c = (time.perf_counter() - t0) / len(dates)
    print(f'  单日 {hr(t_c)}  |  提速 {t_a/t_c:.1f}×（相对旧实现）')
    results['C'] = t_c

    print()
    print('=' * 68)
    print('D. 新引擎 + 流域掩膜前置 + warmup 180 天（真实业务场景）')
    print('=' * 68)
    import json
    gj_path = os.path.join(ROOT, 'data', 'raw', 'geo', 'hydrology', 'TP_Basins_China.geojson')
    with open(gj_path, encoding='utf-8') as f:
        geom = json.load(f)['features'][2]['geometry']

    import datetime
    end = datetime.date(2024, 6, 3)
    warm_start = end - datetime.timedelta(days=180 + 2)
    all_d = [(warm_start + datetime.timedelta(days=i)).strftime('%Y-%m-%d')
             for i in range(183)]
    out_d = all_d[-3:]
    df = {d: {v: os.path.join(DATA_DIR, v, f'{v}_{d}.tif') for v in VARIABLES}
          for d in all_d}
    t0 = time.perf_counter()
    st = simulate_runoff(model=model, device=torch.device('cpu'), variables=VARIABLES,
                         day_files=df, all_dates=all_d, out_dates=out_d,
                         ref_path=ref, output_dir=out_dir, basin_geometry=geom,
                         batch_pixels=16384)
    t_d = time.perf_counter() - t0
    print(f'  流域 {st["n_pixels"]:,} 像元 | 预热 180 天 + 输出 3 天 | '
          f'总耗时 {hr(t_d)}')
    print(f'  跨日记忆 {st["cross_day_memory"]} | 状态 {st["state_bytes"]/1e6:.0f} MB')
    print(f'  其中模拟段约 {hr(t_d * 3/183)}（预热可缓存复用）')

    print()
    print('=' * 68)
    print('汇总（单日，全域口径；30 天模拟）')
    print('=' * 68)
    print(f'  A 旧实现            {hr(results["A"]):>9} / 日   30 天 {hr(results["A"]*30):>9}   {1:>6.1f}×')
    print(f'  B +只算有效像元     {hr(results["B"]):>9} / 日   30 天 {hr(results["B"]*30):>9}   '
          f'{results["A"]/results["B"]:>6.1f}×')
    print(f'  C 新引擎            {hr(results["C"]):>9} / 日   30 天 {hr(results["C"]*30):>9}   '
          f'{results["A"]/results["C"]:>6.1f}×')
    print(f'  D 流域+预热180天    总 {hr(t_d):>9}（{st["n_pixels"]:,} 像元）')


if __name__ == '__main__':
    main()
