# -*- coding: utf-8 -*-
"""输出栅格的"前端可读性"回归测试。

背景：2026-08-30 把输出压缩改成 zstd 后，前端报
"Unknown compression method identifier: 50000"。
原因：GDAL 的 zstd 一律写 TIFF tag 259 = 50000（与 predictor 取值无关），
而 webgis_frontend/js/geotiff.js 只注册了 50001，读不了 50000。

本测试把两件事钉死：
1. 后端写出的 TIFF 压缩标识必须落在前端支持的集合内；
2. 若本机有 node，直接用前端那个 geotiff.js 解码，验证能读且数值无损。

运行：python tests/test_output_frontend_readable.py
"""
import os
import struct
import subprocess
import sys
import tempfile

import numpy as np
import rasterio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 注：支持 pytest 与 `python tests/xxx.py` 双模式运行，故保留路径注入（幂等，见 conftest.py）
for _p in (ROOT, os.path.join(ROOT, 'webgis_backend')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.runoff_engine import simulate_runoff  # noqa: E402

# webgis_frontend/js/geotiff.js 实际注册的解码器标识（实测确认）
FRONTEND_SUPPORTED = {1, 5, 7, 8, 32773, 34887, 50001}

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f'  {detail}' if detail else ''))


def tiff_tag259(path):
    """直读 TIFF IFD，取 Compression 标签（259）的真实数值。"""
    with open(path, 'rb') as f:
        d = f.read()
    bo = '<' if d[:2] == b'II' else '>'
    off = struct.unpack(bo + 'I', d[4:8])[0]
    n = struct.unpack(bo + 'H', d[off:off + 2])[0]
    for i in range(n):
        e = off + 2 + i * 12
        tag, typ, cnt = struct.unpack(bo + 'HHI', d[e:e + 8])
        if tag == 259:
            if typ == 3 and cnt == 1:
                return struct.unpack(bo + 'H', d[e + 8:e + 10])[0]
            return struct.unpack(bo + 'I', d[e + 8:e + 12])[0]
    return None


def find_node():
    for c in ('node', 'node.exe'):
        try:
            out = subprocess.run([c, '--version'], capture_output=True, timeout=10)
            if out.returncode == 0:
                return c
        except Exception:
            continue
    return None


def make_like_runoff(h, w):
    """造一份接近真实径流场的数据：研究区外 NaN，区内平滑。"""
    yy, xx = np.mgrid[0:h, 0:w].astype('float32')
    rng = np.random.default_rng(0)
    base = np.clip(20 * np.exp(-((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / 6e5), 0, None)
    base = base.astype('float32') + (rng.standard_normal((h, w)) * 0.3).astype('float32')
    outside = ((np.abs(yy - h / 2) / (h / 2)) ** 2
               + (np.abs(xx - w / 2) / (w / 2)) ** 2) > 0.85
    return np.where(outside, np.nan, base).astype('float32')


def main():
    ref = make_like_runoff(1560, 4082)
    tmp = tempfile.mkdtemp(prefix='tif_readable_')

    # 用引擎真实的写出路径产出一个文件（跑一天，只用 1 个变量占位会很慢，
    # 所以这里直接构造与引擎 profile 一致的写出参数，避免依赖驱动数据）
    from modules.runoff_engine import build_pixel_index
    src = os.path.join(ROOT, 'data', 'raw', 'TibetanPlateau', 'temperature_2m',
                       'temperature_2m_2024-06-01.tif')
    if not os.path.isfile(src):
        check('驱动文件存在', False, src)
        return 1

    grid = build_pixel_index(src, None)
    profile = grid['profile'].copy()
    profile.update(driver='GTiff', dtype='float32', count=1,
                   compress='lzw', nodata=np.nan)

    out_path = os.path.join(tmp, 'runoff_test.tif')
    with rasterio.open(out_path, 'w', **profile) as dst:
        dst.write(ref, 1)

    print('1. TIFF 压缩标识检查')
    tag = tiff_tag259(out_path)
    check('压缩标识在前端支持集合内', tag in FRONTEND_SUPPORTED,
          f'tag259={tag}（支持 {sorted(FRONTEND_SUPPORTED)}）')
    check('不是 zstd 的 50000', tag != 50000, f'tag259={tag}')

    print('\n2. 用前端 geotiff.js 实际解码')
    node = find_node()
    if not node:
        print('  [跳过] 未找到 node，跳过浏览器端解码验证')
    else:
        js = os.path.join(ROOT, 'tests', 'verify_geotiff_node.js')
        r = subprocess.run([node, js, out_path], capture_output=True,
                           text=True, timeout=180)
        if r.returncode != 0 or not r.stdout.strip():
            check('前端 geotiff.js 能解码', False,
                  (r.stdout or r.stderr).strip()[:200])
        else:
            import json
            info = json.loads(r.stdout.strip())
            check('前端 geotiff.js 能解码', info.get('ok') is True,
                  info.get('error', ''))
            if info.get('ok'):
                check('解码尺寸正确',
                      info.get('width') == 4082 and info.get('height') == 1560,
                      f"{info.get('width')}x{info.get('height')}")

    print('\n3. 数值无损（Python 端自读比对）')
    with rasterio.open(out_path) as s:
        got = s.read(1)
    finite = np.isfinite(ref)
    rmse = float(np.sqrt(np.mean((got[finite] - ref[finite]) ** 2)))
    check('写出再读回 RMSE < 1e-6', rmse < 1e-6, f'RMSE={rmse:.3e}')
    nan_ok = bool(np.array_equal(np.isnan(got), np.isnan(ref)))
    check('NaN 掩膜位置完全一致', nan_ok)

    print(f'\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}')
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print(f'  - {f}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
