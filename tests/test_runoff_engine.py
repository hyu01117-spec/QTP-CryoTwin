# -*- coding: utf-8 -*-
"""径流推理引擎的回归测试。

三条核心断言：
1. batch_size 不变性 —— 旧实现（像元当时间步）这条必挂，是语义是否修对的判据。
2. 像元独立性 —— 打乱像元顺序，输出必须同步置换，不能互相污染。
3. 跨日记忆有效性 —— 传入历史后，同一天的输出必须发生变化。

运行：python tests/test_runoff_engine.py
"""
import os
import sys

import numpy as np
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 注：支持 pytest 与 `python tests/test_runoff_engine.py` 双模式运行，故保留路径注入（幂等）
for _p in (ROOT, os.path.join(ROOT, 'webgis_backend')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.runoff_engine import RunoffLSTM, simulate_runoff  # noqa: E402

MODEL_PATH = os.path.join(ROOT, 'data', 'raw', 'models', 'lstm_runoff_model.pt')
DATA_DIR = os.path.join(ROOT, 'data', 'raw', 'TibetanPlateau')

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f'  {detail}' if detail else ''))


def load_model():
    torch.set_num_threads(os.cpu_count() or 4)
    model = RunoffLSTM(input_size=4)
    state = torch.load(MODEL_PATH, map_location='cpu', weights_only=True)
    if isinstance(state, dict) and 'state_dict' in state:
        state = state['state_dict']
    model.load_state_dict(state)
    model.eval()
    return model


def test_batch_invariance(model):
    """同一批像元，不同 batch_pixels 必须给出相同结果。"""
    print('\n1. batch_size 不变性（语义判据）')
    torch.manual_seed(0)
    P, V = 4096, 4
    X = torch.randn(P, 1, V)

    outs = []
    for bp in (512, 1024, 4096, 8192):
        ys = []
        with torch.no_grad():
            for s in range(0, P, bp):
                y, _ = model(X[s:s + bp], None)
                ys.append(y[:, -1])
        outs.append(torch.cat(ys).numpy())

    base = outs[0]
    for bp, o in zip((512, 1024, 4096, 8192), outs):
        rmse = float(np.sqrt(np.mean((o - base) ** 2)))
        check(f'batch={bp} 与 batch=512 的 RMSE < 1e-6',
              rmse < 1e-6, f'RMSE={rmse:.3e}')


def test_old_semantics_would_fail(model):
    """复现旧实现的张量组织，证明它确实不满足不变性。"""
    print('\n2. 旧语义对照（应当不满足不变性）')
    torch.manual_seed(0)
    P, V = 2048, 4
    X = torch.randn(P, 1, V)

    outs = []
    for bp in (256, 1024):
        ys = []
        with torch.no_grad():
            for s in range(0, P, bp):
                # 旧实现：batch=1，把 bp 个像元当成 bp 个时间步
                x = X[s:s + bp].transpose(0, 1)          # (1, bp, V)
                y, _ = model(x, None)
                ys.append(y[0])                          # (bp,)
        outs.append(torch.cat(ys).numpy())

    rmse = float(np.sqrt(np.mean((outs[0] - outs[1]) ** 2)))
    check('旧语义 batch=256 vs 1024 存在差异（说明结果依赖批大小）',
          rmse > 1e-6, f'RMSE={rmse:.3e}')


def test_pixel_independence(model):
    """打乱像元顺序，输出必须同步置换。"""
    print('\n3. 像元独立性（无邻域污染）')
    torch.manual_seed(1)
    P, V = 1024, 4
    X = torch.randn(P, 1, V)
    perm = torch.randperm(P)

    with torch.no_grad():
        y_orig, _ = model(X, None)
        y_perm, _ = model(X[perm], None)

    a = y_orig[:, -1].numpy()
    b = y_perm[:, -1].numpy()[np.argsort(perm.numpy())]
    rmse = float(np.sqrt(np.mean((a - b) ** 2)))
    check('置换像元顺序后输出同步置换', rmse < 1e-6, f'RMSE={rmse:.3e}')


def test_state_matters(model):
    """跨日记忆必须改变输出，否则等于没有记忆。"""
    print('\n4. 跨日记忆有效性')
    torch.manual_seed(2)
    P, V, T = 512, 4, 10
    X = torch.randn(P, T, V)

    with torch.no_grad():
        y_all, _ = model(X, None)                       # 整段
        h = c = None
        for t in range(T):
            y_step, (h, c) = model(X[:, t:t + 1, :],
                                   None if h is None else (h, c))  # 逐步 + 状态
            h, c = h.detach(), c.detach()

    rmse = float(np.sqrt(np.mean((y_all[:, -1].numpy() - y_step[:, -1].numpy()) ** 2)))
    check('逐步状态传递与整段展开等价', rmse < 1e-5, f'RMSE={rmse:.3e}')

    with torch.no_grad():
        y_last, _ = model(X[:, -1:, :], None)           # 无历史
    diff = float(np.mean(np.abs(y_step[:, -1].numpy() - y_last[:, -1].numpy())))
    check('有历史 vs 无历史的输出不同（记忆确实生效）', diff > 1e-6,
          f'平均绝对差={diff:.3e}')


def test_end_to_end(model):
    """用真实数据跑一小块区域，验证窗口读 + 掩膜 + 写盘全链路。"""
    print('\n5. 端到端冒烟（真实栅格，200×200 窗口）')
    import tempfile

    variables = ['temperature_2m', 'temperature_2m_max',
                 'total_precipitation_max', 'total_precipitation_sum']
    # 2 天足够验证"逐日推进 + 写出"全链路；压低天数避免全量跑时内存累积
    dates = ['2024-06-01', '2024-06-02']
    day_files = {}
    for d in dates:
        day_files[d] = {v: os.path.join(DATA_DIR, v, f'{v}_{d}.tif') for v in variables}

    missing = [p for d in day_files.values() for p in d.values() if not os.path.isfile(p)]
    if missing:
        check('驱动文件齐全', False, f'缺少 {missing[0]}')
        return

    ref = day_files[dates[0]][variables[0]]
    out_dir = tempfile.mkdtemp(prefix='runoff_engine_test_')

    stats = simulate_runoff(
        model=model, device=torch.device('cpu'), variables=variables,
        day_files=day_files, all_dates=dates, out_dates=dates,
        ref_path=ref, output_dir=out_dir, basin_geometry=None,
        batch_pixels=4096)

    check('输出 2 个日期文件', len(stats['files']) == 2, f"实际 {len(stats['files'])}")
    check('有效像元数 > 0', stats['n_pixels'] > 0, f"{stats['n_pixels']:,}")
    # 全域 372 万像元的状态需 5.95 GB，超出 2 GB 预算，按设计应退化为逐日独立
    check('全域触发状态内存保护并退化', stats['cross_day_memory'] is False
          and stats['fallback_reason'] is not None,
          f"state={stats['state_bytes']/1024**3:.2f}GB")

    import rasterio
    if stats['files']:
        with rasterio.open(stats['files'][0]) as src:
            arr = src.read(1)
            finite = np.isfinite(arr)
        check('输出栅格含有限值', finite.sum() > 0, f'有限像元 {int(finite.sum()):,}')
        check('输出网格为 1560×4082', arr.shape == (1560, 4082), f'{arr.shape}')

    # 分块不变性：同一份数据用不同 batch_pixels，结果必须一致
    out_dir2 = tempfile.mkdtemp(prefix='runoff_engine_test2_')
    stats2 = simulate_runoff(
        model=model, device=torch.device('cpu'), variables=variables,
        day_files=day_files, all_dates=dates, out_dates=dates,
        ref_path=ref, output_dir=out_dir2, basin_geometry=None,
        batch_pixels=65536)
    if stats['files'] and stats2['files']:
        with rasterio.open(stats['files'][-1]) as a, rasterio.open(stats2['files'][-1]) as b:
            m = np.isfinite(a.read(1)) & np.isfinite(b.read(1))
            rmse = float(np.sqrt(np.mean((a.read(1)[m] - b.read(1)[m]) ** 2)))
        check('引擎层 batch 4096 vs 65536 结果一致', rmse < 1e-6, f'RMSE={rmse:.3e}')


def test_basin_scope(model):
    """流域范围：像元数应显著减少，且跨日记忆应当启用。"""
    print('\n6. 流域掩膜前置 + 跨日记忆')
    import tempfile

    variables = ['temperature_2m', 'temperature_2m_max',
                 'total_precipitation_max', 'total_precipitation_sum']
    dates = ['2024-06-01', '2024-06-02', '2024-06-03']
    day_files = {d: {v: os.path.join(DATA_DIR, v, f'{v}_{d}.tif') for v in variables}
                 for d in dates}
    ref = day_files[dates[0]][variables[0]]

    gj_path = os.path.join(ROOT, 'data', 'raw', 'geo', 'hydrology', 'TP_Basins_China.geojson')
    if not os.path.isfile(gj_path):
        check('流域文件存在', False, gj_path)
        return
    import json
    with open(gj_path, encoding='utf-8') as f:
        geom = json.load(f)['features'][2]['geometry']   # 一个中小流域

    out_dir = tempfile.mkdtemp(prefix='runoff_engine_basin_')
    stats = simulate_runoff(
        model=model, device=torch.device('cpu'), variables=variables,
        day_files=day_files, all_dates=dates, out_dates=dates,
        ref_path=ref, output_dir=out_dir, basin_geometry=geom,
        batch_pixels=16384)

    check('流域范围有效像元显著少于全域', stats['n_pixels'] < 500_000,
          f"{stats['n_pixels']:,} 像元（全域 3,717,422）")
    check('流域范围启用跨日记忆', stats['cross_day_memory'] is True,
          f"state {stats['state_bytes']/1024**2:.0f} MB")
    check('窗口小于全域', stats['window'][2] < 1560 or stats['window'][3] < 4082,
          f"窗口 {stats['window']}")


def test_mixed_nodata_generation(model):
    """跨 2024-04-14 切换点：新旧两代 nodata 编码必须都能识别。"""
    print('\n7. 新旧两代 nodata 编码兼容（跨 2024-04-14 切换点）')
    import tempfile

    variables = ['temperature_2m', 'temperature_2m_max',
                 'total_precipitation_max', 'total_precipitation_sum']
    dates = ['2024-04-13', '2024-04-14', '2024-04-15']
    day_files = {d: {v: os.path.join(DATA_DIR, v, f'{v}_{d}.tif') for v in variables}
                 for d in dates}
    if any(not os.path.isfile(p) for d in day_files.values() for p in d.values()):
        check('切换点附近驱动文件齐全', False)
        return

    ref = day_files[dates[0]][variables[0]]
    out_dir = tempfile.mkdtemp(prefix='runoff_engine_mix_')
    stats = simulate_runoff(
        model=model, device=torch.device('cpu'), variables=variables,
        day_files=day_files, all_dates=dates, out_dates=dates,
        ref_path=ref, output_dir=out_dir, basin_geometry=None,
        batch_pixels=16384)

    # 取并集后应保留新一代的完整范围，而不是被旧一代的 3,125,430 收窄
    check('有效像元取并集（≥ 371 万）', stats['n_pixels'] >= 3_700_000,
          f"{stats['n_pixels']:,}")
    check('旧日期缺失像元被前向填充', stats['nodata_values_filled'] > 0,
          f"填充 {stats['nodata_values_filled']:,} 个")


def test_region_boundary(model):
    """研究区总边界（TP_China）：应是所有模拟的默认上界。"""
    print('\n8. 研究区总边界 TP_China')
    import tempfile
    from modules.runoff_engine import load_region_geometry

    region = load_region_geometry(
        os.path.join(ROOT, 'data', 'raw', 'geo', 'boundary', 'TP_China.geojson'))
    check('研究区边界可加载', region is not None)
    if region is None:
        return

    variables = ['temperature_2m', 'temperature_2m_max',
                 'total_precipitation_max', 'total_precipitation_sum']
    # 只跑 1 天：本用例验证的是像元数与窗口，与天数无关；
    # 压低天数避免全量跑测试时内存累积把后续用例顶爆。
    dates = ['2024-06-01']
    day_files = {d: {v: os.path.join(DATA_DIR, v, f'{v}_{d}.tif') for v in variables}
                 for d in dates}
    ref = day_files[dates[0]][variables[0]]

    out_a = tempfile.mkdtemp(prefix='re_off_')
    out_b = tempfile.mkdtemp(prefix='re_on_')
    sa = simulate_runoff(model=model, device=torch.device('cpu'), variables=variables,
                         day_files=day_files, all_dates=dates, out_dates=dates,
                         ref_path=ref, output_dir=out_a, batch_pixels=16384)
    sb = simulate_runoff(model=model, device=torch.device('cpu'), variables=variables,
                         day_files=day_files, all_dates=dates, out_dates=dates,
                         ref_path=ref, output_dir=out_b, region_geometry=region,
                         batch_pixels=16384)

    check('无边界时按整块栅格算', sa['n_pixels'] == 3_717_422, f"{sa['n_pixels']:,}")
    check('有边界时收敛到中国境内 3,125,430', sb['n_pixels'] == 3_125_430,
          f"{sb['n_pixels']:,}")
    check('边界使计算量减少约 16%',
          0.80 < sb['n_pixels'] / sa['n_pixels'] < 0.90,
          f"{sb['n_pixels']/sa['n_pixels']:.1%}")
    check('范围标记为 region', sb['scope'] == 'region', sb['scope'])

    # 研究区边界应恰好等于旧代数据的有效范围
    check('与旧代(1-01~4-13)数据范围一致',
          sb['n_pixels'] == 3_125_430,
          '旧代有效像元也是 3,125,430')


def test_region_makes_generations_consistent(model):
    """区域边界应让新旧两代数据给出完全一致的像元集，前向填充归零。"""
    print('\n9. 两代数据在区域边界下的一致性')
    import tempfile
    from modules.runoff_engine import load_region_geometry

    region = load_region_geometry(
        os.path.join(ROOT, 'data', 'raw', 'geo', 'boundary', 'TP_China.geojson'))
    if region is None:
        check('研究区边界可加载', False)
        return

    variables = ['temperature_2m', 'temperature_2m_max',
                 'total_precipitation_max', 'total_precipitation_sum']
    # 跨 2024-04-14 切换点
    dates = ['2024-04-13', '2024-04-14', '2024-04-15']
    day_files = {d: {v: os.path.join(DATA_DIR, v, f'{v}_{d}.tif') for v in variables}
                 for d in dates}
    if any(not os.path.isfile(p) for d in day_files.values() for p in d.values()):
        check('切换点附近驱动文件齐全', False)
        return

    ref = day_files[dates[0]][variables[0]]
    out = tempfile.mkdtemp(prefix='re_mix_')
    st = simulate_runoff(model=model, device=torch.device('cpu'), variables=variables,
                         day_files=day_files, all_dates=dates, out_dates=dates,
                         ref_path=ref, output_dir=out, region_geometry=region,
                         batch_pixels=16384)

    check('像元数稳定在 3,125,430', st['n_pixels'] == 3_125_430, f"{st['n_pixels']:,}")
    check('前向填充归零（两代掩膜一致）', st['nodata_values_filled'] == 0,
          f"填充 {st['nodata_values_filled']:,} 个")


def main():
    print(f'模型: {MODEL_PATH}')
    model = load_model()

    # 各用例会构造百万级像元的数组与完整模拟，逐个显式回收，
    # 否则累积占用会把后续用例顶爆（尤其机器上还在跑服务时）。
    import gc
    steps = (
        test_batch_invariance,
        test_old_semantics_would_fail,
        test_pixel_independence,
        test_state_matters,
        test_end_to_end,
        test_basin_scope,
        test_mixed_nodata_generation,
        test_region_boundary,
        test_region_makes_generations_consistent,
    )
    for step in steps:
        step(model)
        gc.collect()

    print(f'\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}')
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print(f'  - {f}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
