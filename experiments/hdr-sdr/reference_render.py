#!/usr/bin/env python3
"""Independent HDR10 -> SDR reference renderer for M17S captures.

Reads a captured 4K 8-bit NV12 frame plus the matching raw RGBA dump from the
box, applies the documented tone-mapping math on the CPU (pure Python), and
writes a PNG. Also reports the difference between the CPU reference and the
GPU/shader output so the shader can be checked numerically.
"""

import argparse
import json
import math
import random
import struct
import zlib

PQ_M1 = 0.1593017578125
PQ_M2 = 78.84375
PQ_C1 = 0.8359375
PQ_C2 = 18.8515625
PQ_C3 = 18.6875
REF_WHITE = 100.0
TONE_PEAK = 1000.0


def pq_eotf(value):
    if value <= 0.0:
        return 0.0
    vp = value ** (1.0 / PQ_M2)
    num = max(vp - PQ_C1, 0.0)
    den = max(PQ_C2 - PQ_C3 * vp, 1e-6)
    return (num / den) ** (1.0 / PQ_M1) * 10000.0


def tonemap_rgb(nits):
    """Luminance-preserving Reinhard curve, then BT.2020 -> BT.709 -> gamma 2.2."""
    lum = max(
        0.2627 * nits[0] + 0.6780 * nits[1] + 0.0593 * nits[2], 0.0
    )
    x = lum / REF_WHITE
    peak = TONE_PEAK / REF_WHITE
    mapped = x * (1.0 + x / (peak * peak)) / (1.0 + x)
    scale = mapped / x if x > 1e-6 else 1.0
    linear = [c / REF_WHITE * scale for c in nits]
    r709 = (
        1.6605 * linear[0] - 0.5876 * linear[1] - 0.0728 * linear[2]
    )
    g709 = (
        -0.1246 * linear[0] + 1.1329 * linear[1] - 0.0083 * linear[2]
    )
    b709 = (
        -0.0182 * linear[0] - 0.1006 * linear[1] + 1.1187 * linear[2]
    )
    out = []
    for channel in (r709, g709, b709):
        value = min(max(channel, 0.0), 1.0)
        out.append(int(round(255.0 * (value ** (1.0 / 2.2)))))
    return out


def nv12_to_rgb_limited(y_value, u_value, v_value, matrix):
    yp = (y_value - 16) / 219.0
    cb = (u_value - 128) / 224.0
    cr = (v_value - 128) / 224.0
    if matrix == "bt2020":
        r = yp + 1.4746 * cr
        g = yp - 0.164553 * cb - 0.571353 * cr
        b = yp + 1.8814 * cb
    else:
        r = yp + 1.402 * cr
        g = yp - 0.344136 * cb - 0.714136 * cr
        b = yp + 1.772 * cb
    return [
        math.pow(min(max(r, 0.0), 1.0), 1.0),
        math.pow(min(max(g, 0.0), 1.0), 1.0),
        math.pow(min(max(b, 0.0), 1.0), 1.0),
    ]


def write_png(path, width, height, rows):
    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw.extend(row)
    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(chunk(b"IHDR", header))
        handle.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        handle.write(chunk(b"IEND", b""))


parser = argparse.ArgumentParser()
parser.add_argument("--nv12", required=True)
parser.add_argument("--rgba", help="raw RGBA dump from the box for the same frame")
parser.add_argument("--width", type=int, default=3840)
parser.add_argument("--height", type=int, default=2160)
parser.add_argument("--matrix", choices=("bt2020", "bt601"), default="bt2020")
parser.add_argument("--png", default="/var/tmp/reference.png")
parser.add_argument("--samples", type=int, default=4000)
args = parser.parse_args()

width, height = args.width, args.height
out_width, out_height = width // 2, height // 2
with open(args.nv12, "rb") as handle:
    nv12 = handle.read()
y_plane = nv12[: width * height]
uv_offset = width * height
uv_plane = nv12[uv_offset : uv_offset + width * height // 2]

random.seed(20260922)
pixels = set()
while len(pixels) < args.samples:
    pixels.add((random.randrange(out_width), random.randrange(out_height)))

reference = {}
for x, y in pixels:
    y_sum = 0
    for dy in (0, 1):
        row = (2 * y + dy) * width + 2 * x
        y_sum += y_plane[row] + y_plane[row + 1]
    y_value = y_sum / 4.0
    uv_index = y * out_width + x
    u_value = uv_plane[2 * uv_index]
    v_value = uv_plane[2 * uv_index + 1]
    rgb = nv12_to_rgb_limited(y_value, u_value, v_value, args.matrix)
    nits = [pq_eotf(c) for c in rgb]
    reference[(x, y)] = tonemap_rgb(nits)

report = {
    "nv12": args.nv12,
    "matrix": args.matrix,
    "samples": len(reference),
}

if args.rgba:
    with open(args.rgba, "rb") as handle:
        rgba = handle.read()
    diffs = []
    exact = 0
    for (x, y), expected in reference.items():
        offset = (y * out_width + x) * 4
        actual = list(rgba[offset : offset + 3])
        delta = max(abs(a - b) for a, b in zip(actual, expected))
        diffs.append(delta)
        if delta == 0:
            exact += 1
    diffs.sort()
    report.update(
        {
            "rgba": args.rgba,
            "max_abs_diff": diffs[-1],
            "p99_abs_diff": diffs[int(len(diffs) * 0.99)],
            "median_abs_diff": diffs[len(diffs) // 2],
            "exact_match_fraction": round(exact / len(diffs), 4),
            "within_2_codes_fraction": round(
                sum(1 for value in diffs if value <= 2) / len(diffs), 4
            ),
        }
    )

if args.png:
    rows = []
    for y in range(out_height):
        row = bytearray()
        uv_row = y * out_width
        for x in range(out_width):
            y_sum = 0
            base = 2 * y * width + 2 * x
            y_sum += y_plane[base] + y_plane[base + 1]
            base += width
            y_sum += y_plane[base] + y_plane[base + 1]
            uv_index = uv_row + x
            rgb = nv12_to_rgb_limited(
                y_sum / 4.0, uv_plane[2 * uv_index], uv_plane[2 * uv_index + 1], args.matrix
            )
            row.extend(tonemap_rgb([pq_eotf(c) for c in rgb]))
        rows.append(bytes(row))
    write_png(args.png, out_width, out_height, rows)
    report["png"] = args.png

print(json.dumps(report, indent=2))
