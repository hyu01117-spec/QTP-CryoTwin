# -*- coding: utf-8 -*-
"""步骤 1：径流生成。

输入：气象驱动（4 个变量，逐日 tif）+ LSTM 模型权重
输出：逐日径流 tif（命名 {YYYY-MM-DD}_runoff.tif），与参考影像同网格

实现参考本项目 Training/inference/predict.py：
逐像元取 4 变量时间序列 -> 过两层 LSTM -> 写盘。

用法：
    python step1_runoff.py                      # 使用 config.py 默认配置
    python step1_runoff.py --start 2024-01-01 --end 2024-08-31
    python step1_runoff.py --meteor_dir D:/data/era5 --model D:/models/lstm.pt

注意：
- 本脚本为"简单正确"实现（逐像元循环），像元数多时较慢；
  大区域请先用流域边界裁剪驱动数据，或改用批量向量化实现。
- 若你已有径流结果，可跳过本步直接运行 step2_hazard.py。
"""
import argparse
import glob
import os
import time
from datetime import datetime, timedelta

import numpy as np
import rasterio
import torch

import config


# ---- 模型定义：与本项目训练结构一致（两层 LSTM + FC + ReLU）----
class RunoffLSTM(torch.nn.Module):
    def __init__(self, input_size=4, hidden_size=100, num_layers=2, dropout=0.0):
        super().__init__()
        self.lstm = torch.nn.LSTM(
            input_size=input_size, hidden_size=hidden_size,
            num_layers=num_layers, batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0)
        self.fc = torch.nn.Linear(hidden_size, 1)
        self.relu = torch.nn.ReLU()

    def forward(self, x, state=None):
        out, state = self.lstm(x, state)
        return self.relu(self.fc(out).squeeze(-1)), state


def list_dates(meteor_dir, variables, start=None, end=None):
    """按变量 0 的文件列出日期（YYYYMMDD），并按区间过滤。"""
    files = sorted(glob.glob(os.path.join(meteor_dir, variables[0], "*_clipped.tif")))
    dates = [os.path.basename(f).replace("_clipped.tif", "") for f in files]
    dates = sorted(set(dates))
    if start:
        s = start.replace("-", "")
        dates = [d for d in dates if d >= s]
    if end:
        e = end.replace("-", "")
        dates = [d for d in dates if d <= e]
    return dates


def main():
    ap = argparse.ArgumentParser(description="径流生成：气象驱动 + LSTM -> 逐日径流 tif")
    ap.add_argument("--meteor_dir", default=str(config.METEOR_DIR))
    ap.add_argument("--model", default=str(config.LSTM_MODEL))
    ap.add_argument("--out_dir", default=str(config.RUNOFF_DIR))
    ap.add_argument("--variables", nargs="+", default=list(config.VARIABLES))
    ap.add_argument("--start", required=True, help="起止日期，必填，如 2024-01-01")
    ap.add_argument("--end", required=True, help="结束日期，必填，如 2024-12-31")
    ap.add_argument("--flip", action="store_true", default=config.FLIP_DIRECTION,
                    help="数值方向翻转（本项目 predict.py 行为）")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"使用设备: {device}")

    model = RunoffLSTM(input_size=len(args.variables)).to(device)
    state = torch.load(args.model, map_location=device, weights_only=True)
    if isinstance(state, dict) and "state_dict" in state:
        state = state["state_dict"]
    model.load_state_dict(state)
    model.eval()

    dates = list_dates(args.meteor_dir, args.variables, args.start, args.end)
    print(f"待推理日期: {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")
    if not dates:
        raise SystemExit("没有找到驱动数据，请检查 --meteor_dir 与日期区间")

    # 参考影像（变量 0 的第一天）
    ref_path = os.path.join(args.meteor_dir, args.variables[0], f"{dates[0]}_clipped.tif")
    with rasterio.open(ref_path) as ref:
        H, W, profile = ref.height, ref.width, ref.profile
        profile.update(count=1, dtype=rasterio.float32, compress="lzw")

    t0 = time.time()
    # 逐日推理（每像元：4 变量时间序列 -> LSTM）
    for d in dates:
        # 读该日 4 个变量
        stacks = []
        for var in args.variables:
            p = os.path.join(args.meteor_dir, var, f"{d}_clipped.tif")
            with rasterio.open(p) as src:
                img = src.read(1).astype(np.float32)
                img = np.where(img == src.nodata, np.nan, img) if src.nodata is not None else img
                stacks.append(img)
        stack = np.stack(stacks, axis=-1)          # [H, W, V]

        pred = np.full((H, W), np.nan, dtype=np.float32)
        rows, cols = np.where(np.all(np.isfinite(stack), axis=-1))
        n = len(rows)
        if n > 0:
            x = torch.tensor(stack[rows, cols], dtype=torch.float32).unsqueeze(0).to(device)
            with torch.no_grad():
                y, _ = model(x)
            pred[rows, cols] = y.squeeze(0).cpu().numpy()

        if args.flip:
            vmin, vmax = np.nanmin(pred), np.nanmax(pred)
            pred = vmax - (pred - vmin)

        date_out = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        out_path = os.path.join(args.out_dir, f"{date_out}_runoff.tif")
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(pred, 1)
        print(f"  {date_out}  有效像元 {n:,} / {H*W:,}")

    print(f"完成，共 {len(dates)} 天，耗时 {time.time()-t0:.1f} s，输出到 {args.out_dir}")


if __name__ == "__main__":
    main()
