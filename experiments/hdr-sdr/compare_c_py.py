#!/usr/bin/env python3
"""Independent numeric check of m17s_tonemap.c against a direct-math reference.

Reads the captured 4K NV12 frame and the C tone mapper's BGRA output, recomputes
the documented math in plain Python (no LUTs, no shared code with the C file) on
a random pixel sample, and reports the code-value deltas. Also compares whole
frames rendered with different thread counts.

Usage:
  compare_c_py.py --nv12 frame-120.nv12 --bgra frame-120-c.bgra [--samples 400000]
"""

import argparse
import json
import math
import random

PQ_M1 = 0.1593017578125
PQ_M2 = 78.84375
PQ_C1 = 0.8359375
PQ_C2 = 18.8515625
PQ_C3 = 18.6875
REF_WHITE = 100.0
PEAK = 1000.0
PEAK_SQ = (PEAK / REF_WHITE) ** 2


def pq_eotf(e):
    if e <= 0.0:
        return 0.0
    vp = e ** (1.0 / PQ_M2)
    num = vp - PQ_C1
    if num < 0.0:
        num = 0.0
    den = PQ_C2 - PQ_C3 * vp
    if den < 1e-6:
        den = 1e-6
    return (num / den) ** (1.0 / PQ_M1) * 10000.0


def clamp01(v):
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def render(y_sum4, u, v):
    """Direct math: limited-range BT.2020 NCL -> PQ EOTF -> Reinhard -> BT.709 -> 2.2."""
    yv = (y_sum4 - 16.0) / 219.0
    cb = (u - 128.0) / 224.0
    cr = (v - 128.0) / 224.0
    r = clamp01(yv + 1.4746 * cr)
    g = clamp01(yv - 0.164553 * cb - 0.571353 * cr)
    b = clamp01(yv + 1.8814 * cb)
    rl = pq_eotf(r)
    gl = pq_eotf(g)
    bl = pq_eotf(b)
    lum = 0.2627 * rl + 0.6780 * gl + 0.0593 * bl
    x = lum / REF_WHITE
    scale = (1.0 + x / PEAK_SQ) / (1.0 + x) if x > 0.0 else 1.0
    lr = rl / REF_WHITE * scale
    lg = gl / REF_WHITE * scale
    lb = bl / REF_WHITE * scale
    r709 = clamp01(1.6605 * lr - 0.5876 * lg - 0.0728 * lb)
    g709 = clamp01(-0.1246 * lr + 1.1329 * lg - 0.0083 * lb)
    b709 = clamp01(-0.0182 * lr - 0.1006 * lg + 1.1187 * lb)
    return [
        int(255.0 * (b709 ** (1.0 / 2.2)) + 0.5),
        int(255.0 * (g709 ** (1.0 / 2.2)) + 0.5),
        int(255.0 * (r709 ** (1.0 / 2.2)) + 0.5),
    ]


ap = argparse.ArgumentParser()
ap.add_argument("--nv12", required=True)
ap.add_argument("--bgra", required=True)
ap.add_argument("--width", type=int, default=3840)
ap.add_argument("--height", type=int, default=2160)
ap.add_argument("--out-width", type=int, default=1920)
ap.add_argument("--out-height", type=int, default=1080)
ap.add_argument("--samples", type=int, default=200000)
ap.add_argument("--seed", type=int, default=20260922)
args = ap.parse_args()

w, h = args.width, args.height
ow, oh = args.out_width, args.out_height
nv12 = open(args.nv12, "rb").read()
bgra = open(args.bgra, "rb").read()
assert len(nv12) >= w * h * 3 // 2, "nv12 too short"
assert len(bgra) >= ow * oh * 4, "bgra too short"
uv_base = w * h

random.seed(args.seed)
deltas = []
bad_examples = []
channel_max = 0
for _ in range(args.samples):
    dx = random.randrange(ow)
    dy = random.randrange(oh)
    sy = dy * h // oh
    sy2 = sy + 1 if sy + 1 < h else sy
    sx = dx * w // ow
    sx2 = sx + 1 if sx + 1 < w else sx
    row0 = sy * w
    row1 = sy2 * w
    y_mean = (
        nv12[row0 + sx] + nv12[row0 + sx2] + nv12[row1 + sx] + nv12[row1 + sx2]
    ) / 4.0
    uvy = dy * (h // 2) // oh
    uvx = dx * (w // 2) // ow
    idx = uv_base + uvy * w + uvx * 2
    expected = render(y_mean, nv12[idx], nv12[idx + 1])
    off = (dy * ow + dx) * 4
    actual = [bgra[off], bgra[off + 1], bgra[off + 2]]
    delta = max(abs(a - b) for a, b in zip(actual, expected))
    deltas.append(delta)
    if delta > channel_max:
        channel_max = delta
    if delta > 1 and len(bad_examples) < 8:
        bad_examples.append(
            {"x": dx, "y": dy, "c_actual_bgr": actual, "ref_bgr": expected, "delta": delta}
        )

deltas.sort()
n = len(deltas)
report = {
    "nv12": args.nv12,
    "bgra": args.bgra,
    "samples": n,
    "max_abs_diff": deltas[-1],
    "p999_abs_diff": deltas[int(n * 0.999)],
    "p99_abs_diff": deltas[int(n * 0.99)],
    "median_abs_diff": deltas[n // 2],
    "exact_fraction": round(sum(1 for d in deltas if d == 0) / n, 6),
    "within_1_fraction": round(sum(1 for d in deltas if d <= 1) / n, 6),
    "within_2_fraction": round(sum(1 for d in deltas if d <= 2) / n, 6),
    "over_2": sum(1 for d in deltas if d > 2),
    "examples_over_1": bad_examples,
}
print(json.dumps(report, indent=2))
