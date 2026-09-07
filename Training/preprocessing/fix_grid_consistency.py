#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""驱动栅格网格一致性校验与修复。

发现的问题
----------
`data/raw/TibetanPlateau` 下四个变量的空间网格并不一致：

    temperature_2m            2024-01-01 ~ 2024-04-13  (104 天)  1560×3507  left=73.1857
                              2024-04-14 ~ 2024-12-30  (261 天)  1560×4082  left=68.0204
    temperature_2m_max        全年 365 天                         1560×4082  left=68.0204
    total_precipitation_max   全年 365 天                         1560×4082  left=68.0204
    total_precipitation_sum   全年 365 天                         1560×4082  left=68.0204

即 **temperature_2m 的前 104 天缺失了西部 68.02°E–73.19°E 的 575 列**。

影响
----
`simulate.py:304` 会把各变量 flatten 后 `np.stack(input_data, axis=1)`，
长度 5,470,920 与 6,396,540 无法堆叠，直接抛

    ValueError: all input arrays must have the same shape

该异常被外层宽 `except` 吞掉，表现为接口 500 或该日期被静默跳过。
只要 start_date 落在 2024-01-01 ~ 2024-04-13，在线径流模拟必然失败。

修复策略
--------
基准网格 = 出现次数最多的 (height, width, transform) 组合（本例为 1560×4082）。
对不一致的文件分两种情况处理：

1. **纯列/行偏移**（分辨率、行数相同，只有 left/right 不同）：
   直接按像素偏移搬运到基准网格，空缺填 nodata。**零信息损失**，本例正是此情况
   （4082 - 3507 = 575 列，73.1857° - 68.0204° = 5.1653°，5.1653 / 0.008983 = 575.0）。
2. **其他情况**：用 `rasterio.warp.reproject` 重采样到基准网格（有重采样误差，会警告）。

安全性
------
修复采用"先移走、再原地写"：原始文件被 **move**（非复制，不额外占磁盘）到
`--backup` 目录，可随时回滚。默认先 `--check`，确认无误后再 `--fix`。
"""

import argparse
import collections
import glob
import os
import shutil
import sys

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(x, **kwargs):
        return x

DEFAULT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'raw', 'TibetanPlateau'))


def _fingerprint(path):
    with rasterio.open(path) as src:
        return (path, (src.height, src.width, src.transform, src.crs, src.res))


def scan(root):
    """扫描所有变量/所有日期的网格指纹。返回 {var: [(path, fp), ...]}。

    兼容两种目录结构：
      - 变量子目录式：root/<变量>/<日期>.tif（驱动数据）
      - 平铺式：root/*.tif（阈值场目录）
    """
    entries = sorted(os.listdir(root))
    subdirs = [e for e in entries if os.path.isdir(os.path.join(root, e))]
    flat_tifs = [e for e in entries if e.lower().endswith('.tif')]

    out = {}
    if subdirs:
        for var in subdirs:
            vdir = os.path.join(root, var)
            items = []
            for name in sorted(os.listdir(vdir)):
                if not name.lower().endswith('.tif'):
                    continue
                items.append(_fingerprint(os.path.join(vdir, name)))
            if items:
                out[var] = items
    if flat_tifs:
        out[os.path.basename(root.rstrip(os.sep)) or 'flat'] = [
            _fingerprint(os.path.join(root, n)) for n in flat_tifs]
    return out


def fingerprints(scan_result):
    """统计 (H, W, transform, crs) 的出现次数。"""
    counter = collections.Counter()
    for var, items in scan_result.items():
        for p, fp in items:
            counter[fp] += 1
    return counter


def shift_to_grid(src_arr, src_transform, dst_shape, dst_transform, fill):
    """把同分辨率的 src_arr 按整数像素偏移搬到 dst 网格，空缺填 fill。

    仅适用于分辨率相同、网格对齐的情形；否则返回 None 交由调用方走重投影。
    """
    sh, sw = src_arr.shape
    dh, dw = dst_shape
    if abs(src_transform.a - dst_transform.a) > 1e-9 or \
       abs(abs(src_transform.e) - abs(dst_transform.e)) > 1e-9:
        return None

    # 计算整数像素偏移（列、行）
    col_off = (src_transform.c - dst_transform.c) / dst_transform.a
    row_off = (src_transform.f - dst_transform.f) / dst_transform.e
    if abs(col_off - round(col_off)) > 1e-6 or abs(row_off - round(row_off)) > 1e-6:
        return None
    col_off, row_off = int(round(col_off)), int(round(row_off))

    out = np.full((dh, dw), fill, dtype=src_arr.dtype)

    # 目标区间
    d_r0, d_r1 = max(0, row_off), min(dh, row_off + sh)
    d_c0, d_c1 = max(0, col_off), min(dw, col_off + sw)
    if d_r0 >= d_r1 or d_c0 >= d_c1:
        return None
    # 源区间（对应裁剪）
    s_r0, s_r1 = d_r0 - row_off, d_r1 - row_off
    s_c0, s_c1 = d_c0 - col_off, d_c1 - col_off

    out[d_r0:d_r1, d_c0:d_c1] = src_arr[s_r0:s_r1, s_c0:s_c1]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='驱动栅格网格一致性校验与修复')
    ap.add_argument('--root', default=DEFAULT_ROOT, help='驱动数据根目录')
    ap.add_argument('--check', action='store_true', help='只校验并报告，不做任何修改')
    ap.add_argument('--fix', action='store_true', help='执行修复（原始文件移动到备份目录）')
    ap.add_argument('--backup', default=None, help='备份目录，默认 <root>_gridfix_backup_<日期>')
    ap.add_argument('--dry-run', action='store_true', help='修复演习：报告将做什么但不落盘')
    ap.add_argument('--reference', default=None,
                    help='指定基准网格文件（替换"取众数"的自动判定）。'
                         '用于把阈值场这类独立目录对齐到研究区网格，例：'
                         '--reference data/raw/TibetanPlateau/temperature_2m/'
                         'temperature_2m_2024-07-01.tif')
    ap.add_argument('--to-nan-nodata', action='store_true',
                    help='修复同时把 nodata 统一为 NaN（原 nodata 值替换为 NaN）。'
                         '本项目同时存在 -3.4e38 / None(实际 NaN) / 0 三种无效值标记，'
                         '是下游掩膜失效的根源')
    args = ap.parse_args(argv)

    if not args.check and not args.fix:
        args.check = True

    root = args.root
    if not os.path.isdir(root):
        raise SystemExit(f'目录不存在：{root}')

    print('扫描：', root)
    scan_result = scan(root)
    counter = fingerprints(scan_result)

    print('\n== 网格指纹统计 ==')
    for fp, n in counter.most_common():
        h, w, tf, crs, res = fp
        print(f'  {n:5d} 个文件  {h}×{w}  left={tf.c:.4f}  top={tf.f:.4f}  res={res[0]:.6f}  crs={crs}')

    if args.reference:
        # 以指定文件为基准：典型场景是阈值场（1557×3478, left=73.446）
        # 需要对齐到研究区网格（1560×4082, left=68.020），列偏移 604 像元。
        if not os.path.isfile(args.reference):
            raise SystemExit(f'基准文件不存在：{args.reference}')
        with rasterio.open(args.reference) as ref:
            base_fp = (ref.height, ref.width, ref.transform, ref.crs, ref.res)
        base_h, base_w, base_tf, base_crs, base_res = base_fp
        total = sum(counter.values())
        print(f'\n基准网格（由 --reference 指定）：{base_h}×{base_w}  '
              f'left={base_tf.c:.4f}  top={base_tf.f:.4f}')
        print(f'  基准文件：{os.path.basename(args.reference)}')
    else:
        base_fp, base_n = counter.most_common(1)[0]
        base_h, base_w, base_tf, base_crs, base_res = base_fp
        total = sum(counter.values())
        print(f'\n基准网格：{base_h}×{base_w}（{base_n}/{total} 个文件，'
              f'占 {100*base_n/total:.1f}%）')

    # 找出不一致的文件
    bad = []
    for var, items in scan_result.items():
        for p, fp in items:
            if fp != base_fp:
                bad.append((var, p, fp))
    print(f'不一致文件：{len(bad)} 个')

    if not bad:
        print('\n所有栅格网格一致，无需修复。')
        return 0

    by_var = collections.Counter(v for v, _, _ in bad)
    print('  按变量：', dict(by_var))
    for var, p, fp in bad[:5]:
        h, w, tf, crs, res = fp
        print(f'    例：{var}/{os.path.basename(p)}  {h}×{w} left={tf.c:.4f}')

    if args.check:
        print('\n--check 模式：未做任何修改。确认无误后加 --fix 执行修复。')
        return 1

    # ---------- 执行修复 ----------
    import datetime
    backup = args.backup or f'{root}_gridfix_backup_{datetime.date.today():%Y%m%d}'

    print(f'\n开始修复，备份目录：{backup}')
    if args.dry_run:
        print('（--dry-run：不会真正移动或写入）')

    shifted = resampled = 0
    for var, p, fp in tqdm(bad, desc='修复', unit='file'):
        h, w, tf, crs, res = fp
        with rasterio.open(p) as src:
            arr = src.read(1)
            profile = src.profile.copy()
            nodata = src.nodata

        fill = nodata if nodata is not None else np.nan
        new = shift_to_grid(arr, tf, (base_h, base_w), base_tf, fill)

        method = '列/行偏移（无损）'
        if new is None:
            method = '重投影（有重采样误差）'
            new = np.full((base_h, base_w), fill, dtype=arr.dtype)
            reproject(
                source=arr, destination=new,
                src_transform=tf, src_crs=crs, src_nodata=nodata,
                dst_transform=base_tf, dst_crs=base_crs, dst_nodata=nodata,
                resampling=Resampling.nearest)
            resampled += 1
        else:
            shifted += 1

        if args.dry_run:
            continue

        # 先移走原文件（move 不额外占磁盘），再原地写出修复版。
        # 同时移走同名的 .tfw / .ovr 等附属文件：tfw 是 world file，
        # 会覆盖 GeoTIFF 内部记录的 transform，修复后若残留会让对齐前功尽弃。
        dst_backup_dir = os.path.join(backup, var)
        os.makedirs(dst_backup_dir, exist_ok=True)
        stem = os.path.splitext(p)[0]
        for extra in glob.glob(stem + '.*'):
            shutil.move(extra, os.path.join(dst_backup_dir, os.path.basename(extra)))

        profile.update(height=base_h, width=base_w, transform=base_tf, crs=base_crs)

        if args.to_nan_nodata:
            # 统一 nodata 为 NaN：本项目同时存在 -3.4e38 / None(实际是 NaN) / 0
            # 三种无效值标记，是下游掩膜失效的根源。
            if nodata is not None:
                new = np.where(new == nodata, np.nan, new).astype(arr.dtype)
            profile['nodata'] = np.nan

        with rasterio.open(p, 'w', **profile) as dst:
            dst.write(new, 1)

    print(f'\n修复完成：偏移搬运 {shifted} 个，重投影 {resampled} 个')
    if not args.dry_run:
        print(f'原始文件已移至：{backup}')
        print('回滚方法：把备份目录里的文件 move 回原目录即可。')

    # 复检
    print('\n== 复检 ==')
    counter2 = fingerprints(scan(root))
    for fp, n in counter2.most_common():
        h, w, tf, crs, res = fp
        print(f'  {n:5d} 个文件  {h}×{w}  left={tf.c:.4f}')
    print('一致' if len(counter2) == 1 else f'仍有 {len(counter2)} 种网格，请检查')
    return 0


if __name__ == '__main__':
    sys.exit(main())
