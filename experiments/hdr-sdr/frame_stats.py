#!/usr/bin/env python3
"""Measure what the GL sink actually renders versus what it drops.

fpsdisplaysink reports "rendered/dropped" through its last-message property,
which is the only way to tell a throughput problem from a presentation problem:
the decoder pad can be ticking at 22 fps while the sink only puts a few frames
on screen.
"""

import argparse
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

parser = argparse.ArgumentParser()
parser.add_argument("--file", default="/var/tmp/movie-sample-4k-hevc-video-only.mkv")
parser.add_argument("--shader", default="/var/tmp/tonemap.frag")
parser.add_argument("--io-mode", default="dmabuf", choices=("mmap", "dmabuf"))
parser.add_argument("--seconds", type=float, default=20)
parser.add_argument("--out-width", type=int, default=0)
parser.add_argument("--sync", default="true")
parser.add_argument("--video-sink", default="glimagesink")
args = parser.parse_args()

Gst.init(None)

scale = ""
if args.out_width:
    scale = (
        "capsfilter caps=video/x-raw(memory:GLMemory),"
        f"width={args.out_width},height={args.out_width * 9 // 16} ! "
    )

description = (
    f'filesrc location="{args.file}" ! matroskademux ! h265parse ! '
    "capssetter caps=video/x-h265,colorimetry=bt2020 ! "
    f"v4l2h265dec name=dec capture-io-mode={args.io_mode} ! "
    "video/x-raw,format=NV12,colorimetry=bt2020 ! "
    f"glupload ! glcolorconvert ! glshader name=sh ! {scale}"
    f"fpsdisplaysink name=out video-sink={args.video_sink} text-overlay=false sync={args.sync}"
)
print(description, flush=True)

pipeline = Gst.parse_launch(description)
with open(args.shader) as handle:
    pipeline.get_by_name("sh").set_property("fragment", handle.read())

decoded = [0]
pipeline.get_by_name("dec").get_static_pad("src").add_probe(
    Gst.PadProbeType.BUFFER,
    lambda pad, info: (decoded.__setitem__(0, decoded[0] + 1), Gst.PadProbeReturn.OK)[1],
)

started = time.monotonic()
pipeline.set_state(Gst.State.PLAYING)
bus = pipeline.get_bus()
sink = pipeline.get_by_name("out")
next_report = started + 5
while time.monotonic() - started < args.seconds:
    message = bus.timed_pop_filtered(200 * Gst.MSECOND, Gst.MessageType.ERROR)
    if message is not None:
        print("ERROR:", message.parse_error()[0], flush=True)
        break
    now = time.monotonic()
    if now >= next_report:
        elapsed = now - started
        try:
            last = sink.get_property("last-message")
        except Exception:
            last = None
        print(f"decoded={decoded[0]} fps={decoded[0] / elapsed:.2f} | sink: {last}", flush=True)
        next_report = now + 5
pipeline.set_state(Gst.State.NULL)
print("done", flush=True)
