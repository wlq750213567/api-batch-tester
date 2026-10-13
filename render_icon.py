# -*- coding: utf-8 -*-
"""把图标设计渲染成 512x512 PNG（纯标准库：无第三方依赖、无外部渲染器）

用有符号距离场(SDF)做解析抗锯齿，画家算法从底到顶叠层。
"""
import math
import struct
import zlib

W = H = 512
AA_HALF = 0.5


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else (hi if v > hi else v)


def aa(d):
    """给定 SDF 距离，返回 1px 宽的覆盖率。"""
    return clamp(AA_HALF - d)


def sd_rrect(px, py, x, y, w, h, r):
    qx = abs(px - (x + w * 0.5)) - (w * 0.5 - r)
    qy = abs(py - (y + h * 0.5)) - (h * 0.5 - r)
    ax = qx if qx > 0.0 else 0.0
    ay = qy if qy > 0.0 else 0.0
    m = qx if qx > qy else qy
    return math.hypot(ax, ay) + (m if m < 0.0 else 0.0) - r


def sd_circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def sd_seg(px, py, x1, y1, x2, y2):
    dx = x2 - x1
    dy = y2 - y1
    L2 = dx * dx + dy * dy
    t = clamp(((px - x1) * dx + (py - y1) * dy) / L2) if L2 else 0.0
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def render(out_size=512):
    global AA_HALF
    S = out_size / 512.0
    AA_HALF = 0.5 / S  # 保持视觉边缘宽度一致

    BG0 = (79.0, 70.0, 229.0)
    BG1 = (14.0, 165.0, 233.0)
    BD0 = (52.0, 211.0, 153.0)
    BD1 = (5.0, 150.0, 105.0)
    WHT = 255.0

    out = bytearray()
    inv = 1.0 / S
    for y in range(out_size):
        py = (y + 0.5) * inv
        for x in range(out_size):
            px = (x + 0.5) * inv

            # ---- 背景圆角矩形（渐变） ----
            d = sd_rrect(px, py, 0, 0, 512, 512, 112)
            if d >= 0.5:
                out += b"\x00\x00\x00\x00"
                continue
            cov = clamp(0.5 - d)
            t = (px + py) * (1.0 / 1024.0)
            cr = BG0[0] + (BG1[0] - BG0[0]) * t
            cg = BG0[1] + (BG1[1] - BG0[1]) * t
            cb = BG0[2] + (BG1[2] - BG0[2]) * t

            # ---- 卡片填充 ----
            dcard = sd_rrect(px, py, 104, 104, 304, 304, 52)
            a = aa(dcard) * 0.14
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a

            # ---- 卡片描边 ----
            a = aa(abs(dcard) - 4.5) * 0.47
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a

            # ---- 三条接口行 ----
            a = aa(sd_rrect(px, py, 146, 170, 132, 24, 12))
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a
            a = aa(sd_rrect(px, py, 146, 238, 180, 24, 12)) * 0.78
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a
            a = aa(sd_rrect(px, py, 146, 306, 104, 24, 12)) * 0.52
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a

            # ---- 右侧状态点 ----
            a = aa(sd_circle(px, py, 352, 182, 13)) * 0.92
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a
            a = aa(sd_circle(px, py, 352, 250, 13)) * 0.60
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a
            a = aa(sd_circle(px, py, 352, 318, 13)) * 0.37
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a

            # ---- 通过徽章（绿渐变圆） ----
            a = aa(sd_circle(px, py, 376, 376, 86))
            if a > 0.0:
                bt = clamp((py - 290.0) / 172.0)
                br = BD0[0] + (BD1[0] - BD0[0]) * bt
                bgc = BD0[1] + (BD1[1] - BD0[1]) * bt
                bb = BD0[2] + (BD1[2] - BD0[2]) * bt
                cr += (br - cr) * a
                cg += (bgc - cg) * a
                cb += (bb - cb) * a

            # ---- 徽章白环 ----
            a = aa(abs(sd_circle(px, py, 376, 376, 76)) - 10.0)
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a

            # ---- 对勾 ----
            s = min(sd_seg(px, py, 334, 378, 366, 410), sd_seg(px, py, 366, 410, 420, 344))
            a = aa(s - 14.0)
            if a > 0.0:
                cr += (WHT - cr) * a
                cg += (WHT - cg) * a
                cb += (WHT - cb) * a

            out += bytes(
                (
                    int(clamp(cr, 0, 255)),
                    int(clamp(cg, 0, 255)),
                    int(clamp(cb, 0, 255)),
                    int(clamp(cov * 255.0, 0, 255)),
                )
            )
    return out


def write_png(path, width, height, rgba):
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter type 0
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


if __name__ == "__main__":
    import os
    import sys
    import time

    target = sys.argv[1] if len(sys.argv) > 1 else "icon-512.png"
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 512
    started = time.time()
    pixels = render(size)
    write_png(target, size, size, pixels)
    print("渲染完成: %s %dx%d (%.1f KB, %.1fs)" % (target, size, size, os.path.getsize(target) / 1024.0, time.time() - started))
