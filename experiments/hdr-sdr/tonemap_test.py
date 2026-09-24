#!/usr/bin/env python3
"""Run the C tone mapper over a captured NV12 frame and time it."""

import argparse
import ctypes
import json
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument("--nv12", default="/var/tmp/frame.nv12")
parser.add_argument("--layout", default="/var/tmp/frame.json")
parser.add_argument("--library", default="/var/tmp/m17s_tonemap.so")
parser.add_argument("--out", default="/var/tmp/frame.bgra")
parser.add_argument("--out-width", type=int, default=1920)
parser.add_argument("--out-height", type=int, default=1080)
parser.add_argument("--threads", type=int, default=3)
parser.add_argument("--peak", type=float, default=1000.0)
parser.add_argument("--repeat", type=int, default=24)
args = parser.parse_args()

lib = ctypes.CDLL(args.library)
lib.m17s_tonemap_new.restype = ctypes.c_void_p
lib.m17s_tonemap_new.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_int]
lib.m17s_tonemap_convert.restype = ctypes.c_int
lib.m17s_tonemap_convert.argtypes = [
    ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
]

layout = json.load(open(args.layout)) if args.layout else None
with open(args.nv12, "rb") as handle:
    nv12 = handle.read()
width, height = 3840, 2160
y_stride, uv_stride = width, width
if layout and layout.get("video_meta"):
    meta = layout["video_meta"]
    width, height = meta["width"], meta["height"]
    y_stride = meta["stride"][0]
    uv_stride = meta["stride"][1]

y_plane = ctypes.create_string_buffer(nv12[: y_stride * height], y_stride * height)
uv_size = uv_stride * ((height + 1) // 2)
uv_plane = ctypes.create_string_buffer(nv12[y_stride * height : y_stride * height + uv_size], uv_size)
out_stride = args.out_width * 4
out = ctypes.create_string_buffer(out_stride * args.out_height)

tm = lib.m17s_tonemap_new(100.0, args.peak, args.threads)
if not tm:
    raise SystemExit("m17s_tonemap_new failed")

durations = []
for _ in range(args.repeat):
    start = time.monotonic()
    rc = lib.m17s_tonemap_convert(
        tm, y_plane, y_stride, uv_plane, uv_stride, width, height,
        args.out_width, args.out_height, out, out_stride,
    )
    durations.append(time.monotonic() - start)
if rc != 0:
    raise SystemExit("convert failed")
with open(args.out, "wb") as handle:
    handle.write(out.raw[: out_stride * args.out_height])

durations.sort()
print(json.dumps({
    "library": args.library,
    "source": {"width": width, "height": height, "y_stride": y_stride, "uv_stride": uv_stride},
    "output": {"width": args.out_width, "height": args.out_height, "format": "BGRA"},
    "threads": args.threads,
    "peak_nits": args.peak,
    "repeats": args.repeat,
    "median_ms": round(durations[len(durations) // 2] * 1000, 2),
    "best_ms": round(durations[0] * 1000, 2),
    "fps_headroom": round(1.0 / durations[len(durations) // 2], 2),
    "out": args.out,
}, indent=2))
