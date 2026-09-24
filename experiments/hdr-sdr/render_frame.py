#!/usr/bin/env python3
"""Render one decoded 4K frame through the GL chain and dump both the decoder
output (NV12) and the GL shader output (RGBA) for the same frame number.

Used to prove the tone-mapping shader against an independent reference.
"""

import argparse
import json
import os
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


parser = argparse.ArgumentParser()
parser.add_argument("--file", default="/var/tmp/movie-sample-4k-hevc-video-only.mkv")
parser.add_argument("--frame", type=int, default=120)
parser.add_argument("--shader", required=True)
parser.add_argument("--out-prefix", default="/var/tmp/frame")
parser.add_argument("--scale", default="1920x1080", help="glcolorscale output, or 'none'")
parser.add_argument("--io-mode", default="mmap", choices=("mmap", "dmabuf"))
parser.add_argument("--timeout", type=float, default=120.0)
args = parser.parse_args()

with open(args.shader) as handle:
    fragment = handle.read()

Gst.init(None)
scale = ""
if args.scale != "none":
    width, height = args.scale.split("x")
    scale = (
        " ! glcolorscale ! capsfilter "
        f"caps=video/x-raw(memory:GLMemory),width={width},height={height}"
    )
description = (
    f'filesrc location="{args.file}" ! matroskademux ! h265parse ! '
    'capssetter caps=video/x-h265,colorimetry=bt2020 ! '
    'v4l2h265dec name=decoder capture-io-mode=' + args.io_mode + ' ! '
    'glupload ! glcolorconvert' + scale + ' ! glshader name=sh ! '
    'gldownload name=downloader ! fakesink name=out sync=false'
)
pipeline = Gst.parse_launch(description)
pipeline.get_by_name("sh").set_property("fragment", fragment)
decoder = pipeline.get_by_name("decoder")
downloader = pipeline.get_by_name("downloader")
out = pipeline.get_by_name("out")

saved = {}
counters = {"decoded": 0, "rendered": 0}


def save_nv12(buffer, path):
    ok, mapping = buffer.map(Gst.MapFlags.READ)
    if not ok:
        return {"error": "nv12 map failed"}
    try:
        with open(path, "wb") as handle:
            handle.write(bytes(mapping.data))
        return {"path": path, "size": mapping.size, "caps": decoder.get_static_pad("src").get_current_caps().to_string()}
    finally:
        buffer.unmap(mapping)


def save_rgba(buffer, path):
    ok, mapping = buffer.map(Gst.MapFlags.READ)
    if not ok:
        return {"error": "rgba map failed"}
    try:
        with open(path, "wb") as handle:
            handle.write(bytes(mapping.data))
        caps = downloader.get_static_pad("src").get_current_caps()
        return {"path": path, "size": mapping.size, "caps": caps.to_string() if caps else None}
    finally:
        buffer.unmap(mapping)


def on_decoder(_pad, info):
    counters["decoded"] += 1
    if counters["decoded"] == args.frame:
        saved["nv12"] = save_nv12(info.get_buffer(), f"{args.out_prefix}.nv12")
    return Gst.PadProbeReturn.OK


def on_downloader(_pad, info):
    counters["rendered"] += 1
    if counters["rendered"] == args.frame:
        saved["rgba"] = save_rgba(info.get_buffer(), f"{args.out_prefix}.rgba")
    return Gst.PadProbeReturn.OK


decoder.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_decoder)
downloader.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_downloader)

started = time.monotonic()
error = None
state = pipeline.set_state(Gst.State.PLAYING)
if state == Gst.StateChangeReturn.FAILURE:
    error = {"message": "pipeline failed to start"}
else:
    bus = pipeline.get_bus()
    while time.monotonic() - started < args.timeout:
        if counters["rendered"] >= args.frame:
            break
        message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message is None:
            continue
        if message.type == Gst.MessageType.ERROR:
            value, debug = message.parse_error()
            error = {"message": str(value), "debug": debug}
        break
pipeline.set_state(Gst.State.NULL)
print(
    json.dumps(
        {
            "frame": args.frame,
            "decoded": counters["decoded"],
            "rendered": counters["rendered"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "scale": args.scale,
            "shader": os.path.basename(args.shader),
            "saved": saved,
            "error": error,
        },
        indent=2,
    )
)
