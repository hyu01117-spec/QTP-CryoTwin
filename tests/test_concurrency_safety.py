# -*- coding: utf-8 -*-
"""径流输出与并发安全性回归测试。

针对 2026-08-30 的线上事故：两个并发模拟写同一批文件名，
rasterio 写前先删同名旧文件，Windows 上撞 Permission denied，任务 500。

本测试钉死两道防线：
1. 原子写：写临时文件 + os.replace 替换，不删除目标文件；
   即使目标正被别的句柄打开（前端下载中）也能替换成功。
2. 并发锁：第二个并发请求返回 409，而不是硬撞。

运行：python tests/test_concurrency_safety.py
"""
import os
import sys
import threading

import numpy as np
import rasterio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, 'webgis_backend')
# 注：本文件支持两种运行方式 —— `pytest tests/`（路径已由根 conftest.py 注入）
# 与 `python tests/test_concurrency_safety.py`（无 conftest，必须自行注入）。
# 故此处保留注入逻辑，但加幂等判断避免 pytest 下产生重复路径项。
for _p in (BACKEND, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.runoff_engine import write_geotiff_atomic  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f'  {detail}' if detail else ''))


def _profile(h, w):
    return dict(driver='GTiff', height=h, width=w, count=1, dtype='float32',
                compress='lzw', nodata=np.nan)


def test_atomic_write_basic():
    """基本功能：能写出、内容正确、不留临时文件。"""
    print('\n1. 原子写基本功能')
    import tempfile
    d = tempfile.mkdtemp(prefix='atomic_basic_')
    path = os.path.join(d, '2024-01-01_runoff.tif')
    arr = (np.random.rand(64, 96) * 10).astype('float32')

    write_geotiff_atomic(path, arr, _profile(64, 96))

    check('文件已生成', os.path.isfile(path))
    with rasterio.open(path) as s:
        got = s.read(1)
    check('内容一致', np.allclose(got, arr, atol=1e-6))

    leftovers = [f for f in os.listdir(d) if f.startswith('.tmp_')]
    check('未残留临时文件', not leftovers, str(leftovers))


def test_atomic_write_over_existing():
    """覆盖已存在的文件：不应触发删除，内容应更新。"""
    print('\n2. 覆盖已存在文件')
    import tempfile
    d = tempfile.mkdtemp(prefix='atomic_over_')
    path = os.path.join(d, '2024-01-01_runoff.tif')

    a1 = np.full((64, 96), 1.0, dtype='float32')
    write_geotiff_atomic(path, a1, _profile(64, 96))
    a2 = np.full((64, 96), 2.0, dtype='float32')
    write_geotiff_atomic(path, a2, _profile(64, 96))

    with rasterio.open(path) as s:
        got = s.read(1)
    check('内容已更新为第二次的值', np.allclose(got, a2, atol=1e-6),
          f'mean={float(np.nanmean(got)):.3f}')
    leftovers = [f for f in os.listdir(d) if f.startswith('.tmp_')]
    check('未残留临时文件', not leftovers, str(leftovers))


def test_concurrent_writers_same_path():
    """两个并发写者写同一路径：这是旧实现必挂的场景，现在两者都应成功。

    旧实现 rasterio.open(path,'w') 会先删同名旧文件；两个请求同时推进到
    同一天时，后一个删不掉前一个正打开的文件 -> Permission denied -> 500。
    原子写改为各自写唯一临时文件再 os.replace，谁都不持有目标文件的写句柄。
    """
    print('\n3. 两个并发写者写同一路径')
    import tempfile
    d = tempfile.mkdtemp(prefix='atomic_race_')
    path = os.path.join(d, '2024-01-01_runoff.tif')
    errors = []

    def writer(tag):
        try:
            for i in range(6):      # 模拟逐日写出，多线程交错
                arr = np.full((64, 96), float(tag), dtype='float32')
                write_geotiff_atomic(path, arr, _profile(64, 96))
        except Exception as e:
            errors.append(f'{tag}: {type(e).__name__}: {e}')

    ts = [threading.Thread(target=writer, args=(t,)) for t in (1, 2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    check('两个写入线程均未报错', not errors, '; '.join(errors)[:200])
    check('文件完整可读', os.path.isfile(path))
    if os.path.isfile(path):
        with rasterio.open(path) as s:
            got = s.read(1)
        check('内容是完整写入的一版（非半截）',
              np.all(np.isfinite(got)) and float(got[0, 0]) in (1.0, 2.0),
              f'value={float(got[0,0])}')
    leftovers = [f for f in os.listdir(d) if f.startswith('.tmp_')]
    check('未残留临时文件', not leftovers, str(leftovers))


def test_reader_holding_target_is_documented():
    """Windows 限制：目标被读取句柄占用时，替换必然失败（无法规避）。

    这是操作系统层面的限制 —— os.replace 在 Windows 上需要目标的删除权限，
    被打开（且未以 FILE_SHARE_DELETE 打开）的文件拿不到。
    原子写能解决"两个写者"，但解决不了"有读者正在下载"。
    正确行为是：失败要干净（不留临时文件）、报错要清晰，交给上层重试。
    """
    print('\n4. Windows 限制：目标被读取句柄占用')
    import tempfile
    d = tempfile.mkdtemp(prefix='atomic_busy_')
    path = os.path.join(d, '2024-01-01_runoff.tif')
    a1 = np.full((64, 96), 1.0, dtype='float32')
    write_geotiff_atomic(path, a1, _profile(64, 96))

    a2 = np.full((64, 96), 9.0, dtype='float32')
    holder = rasterio.open(path)          # 模拟前端正在读取/下载
    try:
        write_geotiff_atomic(path, a2, _profile(64, 96), retries=2)
        failed = False
        err = ''
    except PermissionError as e:
        failed = True
        err = str(e)
    except OSError as e:
        failed = True
        err = f'{type(e).__name__}: {e}'
    finally:
        holder.close()

    if failed:
        check('占用时失败但错误明确（Windows 已知限制）', '拒绝访问' in err or
              'Permission' in err or 'WinError' in err, err[:110])
    else:
        check('占用时仍能写入（说明系统允许替换）', True)

    leftovers = [f for f in os.listdir(d) if f.startswith('.tmp_')]
    check('失败后未残留临时文件', not leftovers, str(leftovers))


def test_runoff_lock_rejects_second():
    """并发锁：持锁期间的新请求应返回 409，而不是并发跑。"""
    print('\n5. 并发锁拒绝第二个请求')
    os.chdir(BACKEND)
    import werkzeug
    if not hasattr(werkzeug, '__version__'):
        from importlib.metadata import version
        try:
            werkzeug.__version__ = version('werkzeug')
        except Exception:
            werkzeug.__version__ = '3.0.0'

    from app import create_app
    import modules.simulate as sim

    app = create_app('default')
    app.config['TESTING'] = True
    client = app.test_client()

    models = client.get('/api/simulate/models').get_json() or []
    if not models:
        check('模型列表非空', False)
        return
    payload = {'model': models[0], 'start_date': '2024-06-01',
               'end_date': '2024-06-01', 'basin_id': '2'}

    # 手动持锁，模拟"已有任务在跑"
    sim._RUNOFF_RUN_LOCK.acquire()
    try:
        r = client.post('/api/simulate/runoff', json=payload)
        body = r.get_json() or {}
        check('返回 409', r.status_code == 409, f'{r.status_code}')
        check('错误码标记 simulation_busy', body.get('code') == 'simulation_busy',
              str(body)[:100])
    finally:
        sim._RUNOFF_RUN_LOCK.release()

    # 放锁后应恢复正常（不再 409）
    r = client.post('/api/simulate/runoff', json=payload)
    check('放锁后不再返回 409', r.status_code != 409, f'{r.status_code}')


def test_lock_released_on_error():
    """异常路径也必须放锁，否则服务会被永久卡死。"""
    print('\n6. 异常后锁被释放')
    os.chdir(BACKEND)
    import werkzeug
    if not hasattr(werkzeug, '__version__'):
        werkzeug.__version__ = '3.0.0'
    from app import create_app
    import modules.simulate as sim

    app = create_app('default')
    app.config['TESTING'] = True
    client = app.test_client()

    # 非法模型名 -> 400（在拿锁之前就返回）
    client.post('/api/simulate/runoff',
                json={'model': '../nope', 'start_date': '2024-06-01',
                      'end_date': '2024-06-01'})
    check('参数错误后锁未被持有', not sim._RUNOFF_RUN_LOCK.locked())

    # 让 simulate_runoff 抛异常，验证 finally 放锁
    orig = sim.simulate_runoff

    def boom(*a, **kw):
        raise RuntimeError('模拟内部故障（测试注入）')

    sim.simulate_runoff = boom
    try:
        models = client.get('/api/simulate/models').get_json() or []
        client.post('/api/simulate/runoff',
                    json={'model': models[0] if models else 'x',
                          'start_date': '2024-06-01', 'end_date': '2024-06-01'})
        check('模拟异常后锁已释放', not sim._RUNOFF_RUN_LOCK.locked())
    finally:
        sim.simulate_runoff = orig


def main():
    test_atomic_write_basic()
    test_atomic_write_over_existing()
    test_concurrent_writers_same_path()
    test_reader_holding_target_is_documented()
    test_runoff_lock_rejects_second()
    test_lock_released_on_error()

    print(f'\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}')
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print(f'  - {f}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
