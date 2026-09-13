# -*- coding: utf-8 -*-
r"""生成「青藏高原冰冻圈灾害数字孪生系统」桌面图标。

绘制方式：纯 PIL 矢量式绘制 + 4 倍超采样，输出多尺寸 ICO，小尺寸自动简化细节。

用法：
    .runtime\python\python.exe assets\branding\make_app_icon.py --outdir assets\branding
"""

import argparse
import io
import math
import os
import struct

from PIL import Image, ImageDraw

# 系统前端配色（webgis_frontend/css/styles.css）
NAVY_TOP = (0x0A, 0x14, 0x24)
NAVY_BOT = (0x15, 0x2A, 0x4E)
ICE = (0x7D, 0xD3, 0xFC)
ICE_LIGHT = (0xBA, 0xE6, 0xFD)
CYAN = (0x67, 0xE8, 0xF9)
WHITE = (0xE6, 0xED, 0xF7)
BLUE = (0x3B, 0x82, 0xF6)
BLUE_DEEP = (0x25, 0x63, 0xEB)

SIZES = (256, 128, 64, 48, 32, 24, 16)


def _stops_color(stops, t):
    """按 (位置, RGBA) 列表插值。"""
    t = min(max(t, 0.0), 1.0)
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if p0 <= t <= p1:
            k = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
            return tuple(int(round(c0[j] + (c1[j] - c0[j]) * k)) for j in range(4))
    return stops[-1][1]


def vertical_gradient(size, stops):
    col = Image.new("RGBA", (1, size))
    px = col.load()
    for y in range(size):
        px[0, y] = _stops_color(stops, y / max(1, size - 1))
    return col.resize((size, size), Image.Resampling.BILINEAR)


def radial_glow(size, cx, cy, radius, color, alpha):
    n = 256
    mask = Image.new("L", (n, n), 0)
    px = mask.load()
    for y in range(n):
        for x in range(n):
            d = math.hypot((x + 0.5) / n - cx, (y + 0.5) / n - cy) / radius
            if d < 1.0:
                px[x, y] = int(255 * alpha * (1.0 - d) ** 2)
    mask = mask.resize((size, size), Image.Resampling.BICUBIC)
    layer = Image.new("RGBA", (size, size), tuple(color) + (0,))
    layer.putalpha(mask)
    return layer


def fill_polygon(canvas, points, stops):
    """用竖直渐变填充多边形。"""
    size = canvas.width
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    canvas.paste(vertical_gradient(size, stops), (0, 0), mask)


def draw_dot_lattice(canvas, step=6, radius_ratio=0.008, alpha=26):
    """背景数据点阵，强化数字孪生质感。"""
    size = canvas.width
    d = ImageDraw.Draw(canvas)
    r = size * radius_ratio
    for i in range(1, step):
        for j in range(1, step):
            cx, cy = size * i / step, size * j / step
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ICE + (alpha,))


def draw_mesh_peak(canvas, detail=True):
    """方案 A：低多边形网格雪山（数字孪生 + 冰冻圈）。"""
    size = canvas.width
    d = ImageDraw.Draw(canvas)
    P = lambda x, y: (x * size, y * size)
    lw = max(1, int(size * 0.013))

    # 侧后方山脊
    d.polygon([P(-0.02, 0.80), P(0.24, 0.46), P(0.50, 0.80)], fill=(0x26, 0x41, 0x70, 210))
    d.polygon([P(0.48, 0.80), P(0.74, 0.40), P(1.02, 0.80)], fill=(0x1D, 0x35, 0x5E, 220))

    apex, base_l, base_r = (0.50, 0.22), (0.11, 0.79), (0.89, 0.79)
    tri = [P(*apex), P(*base_l), P(*base_r)]
    fill_polygon(
        canvas,
        tri,
        [
            (0.00, WHITE + (238,)),
            (0.30, ICE + (232,)),
            (0.62, BLUE + (226,)),
            (1.00, BLUE_DEEP + (222,)),
        ],
    )
    canvas.alpha_composite(radial_glow(size, 0.50, 0.42, 0.46, BLUE, 0.60))

    # 低多边形三角网格
    cols = 4 if detail else 2
    rows = 3 if detail else 2
    grid_alpha = 120 if detail else 175
    node_r = size * (0.014 if detail else 0.020)

    def lerp(a, b, t):
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    # 从顶点射向底边的骨架线
    for j in range(cols + 1):
        bp = lerp(base_l, base_r, j / cols)
        d.line([P(*apex), P(*bp)], fill=CYAN + (grid_alpha,), width=lw)

    # 等高网格（水平横线）+ 交点节点
    for k in range(1, rows + 1):
        t = k / (rows + 1)
        pts = []
        for j in range(cols + 1):
            bp = lerp(base_l, base_r, j / cols)
            pts.append(lerp(apex, bp, t))
        d.line([P(*p) for p in pts], fill=CYAN + (grid_alpha,), width=lw)
        if detail:
            for p in pts:
                cx, cy = P(*p)
                d.ellipse([cx - node_r, cy - node_r, cx + node_r, cy + node_r], fill=WHITE + (225,))

    d.line(tri + [tri[0]], fill=ICE_LIGHT + (255,), width=max(1, int(size * 0.024)), joint="curve")

    for t, alpha, half in ((0.075, 150, 0.40), (0.155, 80, 0.27)):
        y = 0.79 + t
        d.line(
            [P(0.50 - half, y), P(0.50 + half, y)],
            fill=ICE_LIGHT + (alpha,),
            width=max(1, int(size * 0.010)),
        )


def draw_snowflake_contour(canvas, detail=True):
    """方案 B：雪花 + 等值线环（冰冻圈 + 灾害数据）。"""
    size = canvas.width
    d = ImageDraw.Draw(canvas)
    P = lambda x, y: (x * size, y * size)
    canvas.alpha_composite(radial_glow(size, 0.50, 0.50, 0.52, BLUE, 0.55))

    rings = 4 if detail else 3
    for i in range(rings, 0, -1):
        r = 0.115 + i * 0.062
        d.ellipse(
            [P(0.50 - r, 0.50 - r), P(0.50 + r, 0.50 + r)],
            outline=ICE + (45 + 30 * (rings - i),),
            width=max(1, int(size * 0.010)),
        )

    lw = max(1, int(size * 0.024))
    for a in range(6):
        ang = math.radians(a * 60)
        dx, dy = math.cos(ang), math.sin(ang)
        d.line(
            [P(0.50 - dx * 0.30, 0.50 - dy * 0.30), P(0.50 + dx * 0.30, 0.50 + dy * 0.30)],
            fill=ICE_LIGHT + (250,),
            width=lw,
        )
        if not detail:
            continue
        for s in (0.15, 0.22):
            bx, by = 0.50 + dx * s, 0.50 + dy * s
            for side in (-1, 1):
                bang = ang + side * math.radians(52)
                ex, ey = bx + math.cos(bang) * 0.075, by + math.sin(bang) * 0.075
                d.line([P(bx, by), P(ex, ey)], fill=CYAN + (215,), width=max(1, int(lw * 0.6)))
    cx, cy = P(0.50, 0.50)
    r = size * 0.042
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE + (255,))


def draw_risk_dial(canvas, detail=True):
    """方案 C：雪山 + 风险刻度环（灾害监测）。"""
    size = canvas.width
    d = ImageDraw.Draw(canvas)
    P = lambda x, y: (x * size, y * size)
    canvas.alpha_composite(radial_glow(size, 0.50, 0.52, 0.52, BLUE, 0.55))

    cx, cy = P(0.50, 0.54)
    ring = size * 0.40
    d.ellipse([cx - ring, cy - ring, cx + ring, cy + ring], outline=BLUE + (170,), width=max(1, int(size * 0.022)))
    ticks = 12 if detail else 8
    for i in range(ticks):
        ang = 2 * math.pi * i / ticks - math.pi / 2
        r0, r1 = ring * 0.86, ring * 0.99
        strong = i % (3 if detail else 2) == 0
        d.line(
            [cx + math.cos(ang) * r0, cy + math.sin(ang) * r0, cx + math.cos(ang) * r1, cy + math.sin(ang) * r1],
            fill=(CYAN if strong else ICE) + (235 if strong else 150,),
            width=max(1, int(size * (0.026 if strong else 0.016))),
        )
    d.polygon([P(0.22, 0.74), P(0.42, 0.42), P(0.58, 0.74)], fill=BLUE + (235,))
    d.polygon([P(0.44, 0.74), P(0.62, 0.34), P(0.82, 0.74)], fill=ICE_LIGHT + (250,))
    d.line(
        [P(0.44, 0.74), P(0.62, 0.34), P(0.82, 0.74)],
        fill=WHITE + (255,),
        width=max(1, int(size * 0.02)),
        joint="curve",
    )
    if detail:
        y = 0.74
        d.line([P(0.62 - 0.06, y), P(0.62 - 0.165, y)], fill=CYAN + (190,), width=max(1, int(size * 0.014)))
        d.line([P(0.62 + 0.06, y), P(0.62 + 0.09, y)], fill=CYAN + (190,), width=max(1, int(size * 0.014)))


VARIANTS = {"a": draw_mesh_peak, "b": draw_snowflake_contour, "c": draw_risk_dial}


def render(size, variant, ss=4):
    big = size * ss
    canvas = Image.new("RGBA", (big, big), NAVY_TOP + (255,))
    canvas.alpha_composite(vertical_gradient(big, [(0.0, NAVY_TOP + (255,)), (1.0, NAVY_BOT + (255,))]))
    draw_dot_lattice(canvas, step=10, radius_ratio=0.0058, alpha=17)
    VARIANTS[variant](canvas, detail=size >= 64)

    d = ImageDraw.Draw(canvas)
    inset = big * 0.012
    d.rounded_rectangle(
        [inset, inset, big - inset, big - inset],
        radius=big * 0.215,
        outline=ICE + (70,),
        width=max(1, int(big * 0.008)),
    )

    tile = Image.new("L", (big, big), 0)
    ImageDraw.Draw(tile).rounded_rectangle([0, 0, big - 1, big - 1], radius=big * 0.225, fill=255)
    canvas.putalpha(Image.composite(canvas.getchannel("A"), Image.new("L", (big, big), 0), tile))
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(frames, path):
    blobs = []
    for _, im in frames:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        blobs.append(buf.getvalue())
    offset = 6 + 16 * len(frames)
    out = [struct.pack("<HHH", 0, 1, len(frames))]
    for (size, _), blob in zip(frames, blobs):
        dim = 0 if size >= 256 else size
        out.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    for blob in blobs:
        out.append(blob)
    with open(path, "wb") as fh:
        fh.write(b"".join(out))


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=here)
    ap.add_argument("--preview-dir", default="")
    ap.add_argument("--default", default="a", choices=sorted(VARIANTS))
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    ico_paths = {}
    for key in sorted(VARIANTS):
        write_ico([(s, render(s, key)) for s in SIZES], os.path.join(args.outdir, "app-icon-%s.ico" % key))
        ico_paths[key] = os.path.join(args.outdir, "app-icon-%s.ico" % key)
        if args.preview_dir:
            os.makedirs(args.preview_dir, exist_ok=True)
            render(256, key).save(os.path.join(args.preview_dir, "icon-%s.png" % key))

    with open(ico_paths[args.default], "rb") as src:
        data = src.read()
    with open(os.path.join(args.outdir, "app-icon.ico"), "wb") as dst:
        dst.write(data)
    render(512, args.default).save(os.path.join(args.outdir, "app-icon.png"))
    print("ico    :", os.path.join(args.outdir, "app-icon.ico"))
    print("png    :", os.path.join(args.outdir, "app-icon.png"))
    for key, path in sorted(ico_paths.items()):
        print("variant:", key, path)


if __name__ == "__main__":
    main()
