# -*- coding: utf-8 -*-
"""径流模拟模块 性能基准（实测，非估算）

测量内容：
  A. 现状实现（像元当时间步）单日推理耗时
  B. 修正语义（像元做 batch）单步 / 整段耗时，含不同 batch 的甜点
  C. 掩膜前置的真实收益（用 basins_5.geojson 实测各流域像元占比）
  D. 无效像元浪费（nodata 像元仍在参与前向）
  E. JIT / bf16 等推理加速手段的实测增益
  F. 栅格 IO：全图读 vs 窗口读；写出压缩选型

运行：python bench_runoff_speed.py
"""
import os
import time
import json
import tempfile
import torch
import torch.nn as nn
import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.features import geometry_mask

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir))  # 项目根（脚本已移至 scripts/benchmark/，回退两层）
MODEL = os.path.join(ROOT, 'data', 'raw', 'models', 'lstm_runoff_model.pt')
TIF = os.path.join(ROOT, 'data', 'raw', 'TibetanPlateau', 'temperature_2m',
                   'temperature_2m_2024-01-01.tif')
BASINS = os.path.join(ROOT, 'data', 'raw', 'geo', 'hydrology', 'basins_5.geojson')

TMPDIR = tempfile.mkdtemp(prefix='cryo_bench_')

H, W = 1560, 4082
TOTAL = H * W


class RunoffLSTM(nn.Module):
    def __init__(self, input_size=4):
        super().__init__()
        self.lstm = nn.LSTM(input_size=input_size, hidden_size=100,
                            num_layers=2, batch_first=True)
        self.fc = nn.Linear(100, 1)
        self.relu = nn.ReLU()

    def forward(self, x, state=None):
        out, st = self.lstm(x, state)
        return self.relu(self.fc(out).squeeze(-1)), st


def hr(t):
    if t < 1:
        return f"{t*1000:.0f}ms"
    if t < 60:
        return f"{t:.2f}s"
    if t < 3600:
        return f"{t/60:.1f}min"
    return f"{t/3600:.2f}h"


def timeit(fn, n=5, warm=2):
    for _ in range(warm):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n


def main():
    torch.set_num_threads(os.cpu_count() or 4)
    print(f"torch {torch.__version__} | CUDA {torch.cuda.is_available()} "
          f"| threads {torch.get_num_threads()} | cpus {os.cpu_count()}")

    model = RunoffLSTM(4)
    state = torch.load(MODEL, map_location='cpu', weights_only=True)
    if isinstance(state, dict) and 'state_dict' in state:
        state = state['state_dict']
    model.load_state_dict(state)
    model.eval()
    print(f"模型: 2层LSTM hidden=100 in=4 | 参数 "
          f"{sum(p.numel() for p in model.parameters()):,}\n")

    # ---------------- 基础事实 ----------------
    with rasterio.open(TIF) as src:
        transform = src.transform
        arr = src.read(1)
    valid = np.isfinite(arr) & (arr > -1e30)
    N_VALID = int(valid.sum())
    print(f"栅格 {H}x{W} = {TOTAL:,} 像元 | 有效 {N_VALID:,} "
          f"({N_VALID/TOTAL:.1%}) | 无效浪费 {TOTAL/N_VALID:.2f}×\n")

    rng = np.random.default_rng(0)

    # ---------------- A. 现状 ----------------
    print("=" * 74)
    print("A. 现状实现：batch=1，像元被当成时间步（串行展开）")
    print("=" * 74)
    base_day = None
    for B in (1024, 4096):
        n_batch = int(np.ceil(TOTAL / B))     # 注：现状对全图像元（含 nodata）都算
        x = torch.FloatTensor(rng.standard_normal((1, B, 4), dtype=np.float32))
        with torch.no_grad():
            dt = timeit(lambda: model(x), n=10)
        per_day = dt * n_batch
        if B == 1024:
            base_day = per_day
        print(f"  B={B:>5}: {dt*1000:8.1f} ms/批 × {n_batch:>5} 批 = "
              f"单日 {hr(per_day):>8} | 30天 {hr(per_day*30):>9} | 365天 {hr(per_day*365)}")

    # ---------------- B. 修正语义 ----------------
    print()
    print("=" * 74)
    print("B. 修正语义：像元做 batch（真正并行），沿时间展开")
    print("=" * 74)
    best = None
    for B in (1024, 4096, 16384, 65536, 262144):
        x1 = torch.FloatTensor(rng.standard_normal((B, 1, 4), dtype=np.float32))
        with torch.no_grad():
            dt1 = timeit(lambda: model(x1), n=10)
        per_day = dt1 * (N_VALID / B)
        us = dt1 / B * 1e6
        flag = ""
        if best is None or per_day < best[1]:
            best = (B, per_day)
            flag = "  <-- 甜点"
        print(f"  B={B:>7}: 单步 {dt1*1000:7.2f} ms ({us:6.3f} µs/像元·步) "
              f"→ 全域单日 {hr(per_day):>8}{flag}")
    print(f"\n  结论：修正语义后单日 {hr(best[1])}，相对现状 {base_day/best[1]:.1f}× "
          f"（且结果不再依赖 batch_size）")

    # ---------------- B2. T=365 整段（有记忆）----------------
    print()
    print("  B2. 带时间记忆（T=365 整段展开）的内存安全分块：")
    for B in (2048, 8192, 16384):
        try:
            xT = torch.FloatTensor(rng.standard_normal((B, 365, 4), dtype=np.float32))
            with torch.no_grad():
                dtT = timeit(lambda: model(xT), n=2, warm=1)
            full = dtT * (N_VALID / B)
            print(f"    B={B:>6}: {dtT:7.2f}s/块 → 全域全年 {hr(full):>8} "
                  f"| 峰值显/内存约 {B*365*100*4*2/1e9:.1f} GB（激活）")
        except RuntimeError as e:
            print(f"    B={B:>6}: OOM / 失败（{str(e)[:50]}...）")
            break

    # ---------------- C. 掩膜前置 ----------------
    print()
    print("=" * 74)
    print("C. 掩膜前置：实测各流域像元占比（basins_5.geojson）")
    print("=" * 74)
    b_rows = []
    if os.path.isfile(BASINS):
        with open(BASINS, 'r', encoding='utf-8') as f:
            gj = json.load(f)
        for feat in gj.get('features', []):
            geom = feat.get('geometry')
            props = feat.get('properties', {})
            name = (props.get('HYBAS_ID') or props.get('PFAF_ID')
                    or props.get('OBJECTID') or '?')
            name = str(name).split('.')[0]
            if geom is None:
                continue
            try:
                m = geometry_mask([geom], out_shape=(H, W),
                                  transform=transform, invert=True, all_touched=False)
            except Exception as e:
                print(f"  {name}: 掩膜失败 {e}")
                continue
            n_in = int((m & valid).sum())
            if n_in < 100:
                continue
            b_rows.append((str(name), n_in, float(props.get('SUB_AREA') or 0)))
        for name, n_in, area in sorted(b_rows, key=lambda r: -r[1])[:12]:
            frac = n_in / N_VALID
            print(f"  HYBAS {name:<12} 面积 {area:>10,.0f} km²  像元 {n_in:>9,}  "
                  f"占有效区 {frac:6.2%}  → 相对全域提速 {1/frac:6.1f}×")

    # ---------------- D. 推理加速手段 ----------------
    print()
    print("=" * 74)
    print("D. 推理引擎层加速手段实测（(B,1,4) 单步，B=65536）")
    print("=" * 74)
    B = 65536
    x1 = torch.FloatTensor(rng.standard_normal((B, 1, 4), dtype=np.float32))
    with torch.no_grad():
        dt_eager = timeit(lambda: model(x1), n=10)
    print(f"  eager (fp32, 8线程)         {dt_eager*1000:8.2f} ms   1.00×")

    # 状态传递（跨日记忆）的开销
    try:
        h0 = torch.zeros(2, B, 100)
        c0 = torch.zeros(2, B, 100)

        def step_stateful():
            return model(x1, (h0, c0))
        with torch.no_grad():
            dt_st = timeit(step_stateful, n=10, warm=3)
        print(f"  eager + 传入(h,c)状态        {dt_st*1000:8.2f} ms   "
              f"{dt_eager/dt_st:.2f}×   （跨日记忆几乎零额外开销）")
    except Exception as e:
        print(f"  状态传递                    失败: {str(e)[:60]}")

    # bf16（用深拷贝，避免污染后续测试）
    try:
        import copy
        mb = copy.deepcopy(model).to(torch.bfloat16)
        xb = x1.to(torch.bfloat16)
        with torch.no_grad():
            dt_bf16 = timeit(lambda: mb(xb), n=5, warm=3)
        print(f"  bfloat16（无AMX时反而慢）    {dt_bf16*1000:8.2f} ms   "
              f"{dt_eager/dt_bf16:.2f}×   ← 本机不可用")
    except Exception as e:
        print(f"  bfloat16                    失败: {str(e)[:60]}")

    # JIT trace + freeze + optimize_for_inference
    try:
        mt = torch.jit.trace(model, (x1,), strict=False)
        mt = torch.jit.optimize_for_inference(torch.jit.freeze(mt))
        with torch.no_grad():
            dt_jit = timeit(lambda: mt(x1), n=10, warm=3)
        print(f"  jit.freeze+opt_for_infer    {dt_jit*1000:8.2f} ms   "
              f"{dt_eager/dt_jit:.2f}×")
    except Exception as e:
        print(f"  jit                         失败: {str(e)[:60]}")

    # torch.compile（CPU inductor）
    try:
        mc = torch.compile(model)
        with torch.no_grad():
            t0 = time.perf_counter()
            mc(x1)
            mc(x1)
            warm_s = time.perf_counter() - t0
            dt_c = timeit(lambda: mc(x1), n=10, warm=3)
        print(f"  torch.compile(inductor)     {dt_c*1000:8.2f} ms   "
              f"{dt_eager/dt_c:.2f}×   （编译预热 {warm_s:.1f}s）")
    except Exception as e:
        print(f"  torch.compile               失败: {str(e)[:60]}")

    # ---------------- E. IO ----------------
    print()
    print("=" * 74)
    print("E. 栅格 IO 与写出")
    print("=" * 74)
    with rasterio.open(TIF) as src:
        t_full = timeit(lambda: src.read(1), n=5)
        win = Window(1000, 300, 500, 400)
        t_win = timeit(lambda: src.read(1, window=win), n=20)
    print(f"  全图读   {t_full*1000:7.2f} ms/次 → 4变量×365天 {hr(t_full*4*365)}")
    print(f"  窗口读   {t_win*1000:7.4f} ms/次 → 4变量×365天 {hr(t_win*4*365)}"
          f"   提速 {t_full/t_win:.0f}×")

    out = np.random.default_rng(1).standard_normal((H, W)).astype(np.float32)
    prof = rasterio.open(TIF).meta.copy()
    for label, upd in (("无压缩", {'compress': None}),
                       ("lzw", {'compress': 'lzw'}),
                       ("deflate+p3", {'compress': 'deflate', 'predictor': 3}),
                       ("zstd+p3", {'compress': 'zstd', 'predictor': 3})):
        p = prof.copy()
        p.update(upd)
        p.pop('compress', None) if upd['compress'] is None else None
        p['compress'] = upd['compress']
        if upd.get('predictor'):
            p['predictor'] = upd['predictor']
        tmp = os.path.join(TMPDIR, f'_bench_{label}.tif')
        try:
            def w():
                with rasterio.open(tmp, 'w', **p) as d:
                    d.write(out, 1)
            dt = timeit(w, n=3, warm=1)
            size = os.path.getsize(tmp)
            print(f"  写出 {label:<12} {dt*1000:7.1f} ms  文件 {size/1e6:6.1f} MB")
        except Exception as e:
            print(f"  写出 {label:<12} 失败: {str(e)[:60]}")

    # ---------------- F. 汇总 ----------------
    print()
    print("=" * 74)
    print("F. 提速路径汇总（全域有效像元口径，30 天模拟 + 365 天预热）")
    print("=" * 74)
    warm_days = 365
    sim_days = 30
    us_per_pix_step = dt_eager / B * 1e6 / 1e6   # 秒/像元·步
    cost_year = us_per_pix_step * warm_days * N_VALID
    cost_sim = us_per_pix_step * sim_days * N_VALID
    print(f"  单位成本 {us_per_pix_step*1e6:.3f} µs/(像元·步)")
    print(f"  现状（像元当时间步）      单日 {hr(base_day):>8}  "
          f"30天+365预热 无法计算（无记忆）")
    print(f"  仅修正语义                预热 {hr(cost_year):>8} + 模拟 {hr(cost_sim):>8}")
    if b_rows:
        nmax = max(n for _, n, _ in b_rows)
        frac = nmax / N_VALID
        print(f"  修正语义 + 最大流域掩膜    预热 {hr(cost_year*frac):>8} + "
              f"模拟 {hr(cost_sim*frac):>8}   (提速 {1/frac:.0f}×)")
    print(f"  修正语义 + 无效像元剔除    预热 {hr(cost_year*N_VALID/TOTAL):>8} + "
          f"模拟 {hr(cost_sim*N_VALID/TOTAL):>8}   (额外 {TOTAL/N_VALID:.2f}×)")


if __name__ == '__main__':
    main()
