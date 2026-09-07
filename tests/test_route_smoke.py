# -*- coding: utf-8 -*-
"""径流模拟接口的 Flask 端到端冒烟测试（走真实 HTTP 路由）。

运行：python tests/test_route_smoke.py
"""
import os
import sys
import time
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, 'webgis_backend')
# 注：支持 pytest 与 `python tests/test_route_smoke.py` 双模式运行，故保留路径注入（幂等）
for _p in (BACKEND, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.chdir(BACKEND)

# Flask 2.x 的 test_client 依赖 werkzeug.__version__，新版 werkzeug 已移除该属性
import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    from importlib.metadata import version
    try:
        werkzeug.__version__ = version('werkzeug')
    except Exception:
        werkzeug.__version__ = '3.0.0'

from app import create_app  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f'  {detail}' if detail else ''))


def post(client, path, payload):
    t0 = time.perf_counter()
    resp = client.post(path, json=payload)
    elapsed = time.perf_counter() - t0
    try:
        body = resp.get_json()
    except Exception:
        body = None
    return resp.status_code, body, elapsed


def main():
    app = create_app('default')
    app.config['TESTING'] = True

    # 必须把输出目录改到临时目录：否则测试会把结果写进真实的
    # data/webgis/outputs/runoff_results/，污染用户的实际模拟结果。
    # 曾因此混入 235×898（流域范围）的文件，与 1560×3479 的区域结果混在一起，
    # 使洪水识别时不同日期读到不同网格的文件。
    import tempfile
    app.config['RUNOFF_OUTPUT_DIR'] = tempfile.mkdtemp(prefix='runoff_smoke_')

    client = app.test_client()

    print('1. 模型列表')
    code, body, _ = post(client, '/api/simulate/models', {})
    r = client.get('/api/simulate/models')
    models = r.get_json() or []
    check('GET /api/simulate/models 返回 200', r.status_code == 200, f'{r.status_code}')
    check('模型列表非空', len(models) > 0, f'{models}')
    if not models:
        return 1
    model_name = models[0]

    print('\n2. 参数校验')
    r = client.post('/api/simulate/runoff', json={'start_date': '2024-06-01'})
    check('缺少 model 参数返回 400', r.status_code == 400, f'{r.status_code}')

    r = client.post('/api/simulate/runoff', json={
        'model': '../evil', 'start_date': '2024-06-01', 'end_date': '2024-06-03'})
    check('非法模型名返回 400', r.status_code == 400, f'{r.status_code}')

    r = client.post('/api/simulate/runoff', json={
        'model': model_name, 'start_date': '2024-06-05', 'end_date': '2024-06-01'})
    check('开始日期晚于结束日期返回 400', r.status_code == 400, f'{r.status_code}')

    print('\n3. 全域模拟（1 天）')
    code, body, dt = post(client, '/api/simulate/runoff', {
        'model': model_name, 'start_date': '2024-06-01', 'end_date': '2024-06-01'})
    ok = code == 200 and body and body.get('status') == 'success'
    check('全域单日模拟返回 200', ok, f'{code} {str(body)[:160]}')
    if ok:
        d = body['data']
        check('返回 1 个结果文件', len(d['files']) == 1, f"{len(d['files'])}")
        check('报告有效像元数', d['n_pixels'] > 0, f"{d['n_pixels']:,}")
        check('报告耗时', d['elapsed_seconds'] > 0, f"{d['elapsed_seconds']}s")
        print(f"      像元 {d['n_pixels']:,} | 耗时 {d['elapsed_seconds']}s | "
              f"跨日记忆 {d['cross_day_memory']}")

    print('\n4. 流域模拟 + warmup（3 天输出 + 30 天预热）')
    code, body, dt = post(client, '/api/simulate/runoff', {
        'model': model_name, 'start_date': '2024-06-01', 'end_date': '2024-06-03',
        'basin_id': '2', 'warmup_days': 30})
    ok = code == 200 and body and body.get('status') == 'success'
    check('流域+预热模拟返回 200', ok, f'{code} {str(body)[:200]}')
    if ok:
        d = body['data']
        check('返回 3 个结果文件', len(d['files']) == 3, f"{len(d['files'])}")
        check('warmup 生效（总天数 - 输出天数 = 30）', d['warmup_days'] == 30,
              f"{d['warmup_days']}")
        check('流域内像元数远小于全域', d['n_pixels'] < 500_000, f"{d['n_pixels']:,}")
        check('流域范围启用跨日记忆', d['cross_day_memory'] is True)
        check('输出为裁剪后的文件', all('cropped_' in f for f in d['files']),
              f"{d['files'][:1]}")
        print(f"      像元 {d['n_pixels']:,} | 预热 {d['warmup_days']} 天 | "
              f"耗时 {d['elapsed_seconds']}s | 填充 {d['nodata_values_filled']:,}")

    print('\n5. 跨新旧数据切换点（2024-04-13 ~ 2024-04-15）')
    code, body, dt = post(client, '/api/simulate/runoff', {
        'model': model_name, 'start_date': '2024-04-13', 'end_date': '2024-04-15'})
    ok = code == 200 and body and body.get('status') == 'success'
    check('跨切换点模拟返回 200', ok, f'{code} {str(body)[:160]}')
    if ok:
        d = body['data']
        # 研究区边界（TP_China）生效后，新旧两代数据的有效掩膜完全一致：
        # 像元数稳定在 3,125,430，跨切换点不再需要任何前向填充。
        # 这正是收敛到中国境内带来的额外收益。
        check('像元数稳定在中国境内 3,125,430', d['n_pixels'] == 3_125_430,
              f"{d['n_pixels']:,}")
        check('两代掩膜一致，前向填充归零', d['nodata_values_filled'] == 0,
              f"{d['nodata_values_filled']:,}")

    print(f'\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}')
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print(f'  - {f}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
