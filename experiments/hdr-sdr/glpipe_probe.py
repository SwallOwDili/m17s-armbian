#!/usr/bin/env python3
"""Run an arbitrary VDEC -> GL pipeline for a fixed window and report fps.

Combines rate_probe.py's measurement style (buffer probes on the decoder src
pad and on the sink pad) with a freely specified GL stage string, so each stage
combination and V4L2 io-mode can be compared without editing the script.
"""

import argparse
import json
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

PASSTHROUGH = (
    "precision highp float;\n"
    "varying vec2 v_texcoord;\n"
    "uniform sampler2D tex;\n"
    "void main() { gl_FragColor = texture2D(tex, v_texcoord); }\n"
)

parser = argparse.ArgumentParser()
parser.add_argument("--stages", required=True,
                    help='e.g. "glupload ! glcolorconvert ! glshader name=sh ! glimagesink name=out"')
parser.add_argument("--io-mode", default="mmap", choices=("mmap", "dmabuf", "dmabuf-import"))
parser.add_argument("--file", default="/var/tmp/movie-sample-4k-hevc-video-only.mkv")
parser.add_argument("--seconds", type=float, default=8.0)
parser.add_argument("--shader")
parser.add_argument("--sink-name", default="out")
args = parser.parse_args()

DECODER = (
    'filesrc location="{path}" ! matroskademux ! h265parse ! '
    "capssetter caps=video/x-h265,colorimetry=bt2020 ! "
    "v4l2h265dec name=decoder capture-io-mode={io}"
).format(path=args.file, io=args.io_mode)

description = DECODER + " ! " + args.stages
Gst.init(None)
pipeline = Gst.parse_launch(description)

shader = pipeline.get_by_name("sh")
if shader is not None:
    if args.shader:
        with open(args.shader) as handle:
            shader.set_property("fragment", handle.read())
    else:
        shader.set_property("fragment", PASSTHROUGH)

decoded = [0]
pipeline.get_by_name("decoder").get_static_pad("src").add_probe(
    Gst.PadProbeType.BUFFER,
    lambda pad, info: (decoded.__setitem__(0, decoded[0] + 1), Gst.PadProbeReturn.OK)[1],
)
shown = [0]
sink = pipeline.get_by_name(args.sink_name)
watch = sink.get_static_pad("sink") if sink else None
if watch is not None:
    watch.add_probe(
        Gst.PadProbeType.BUFFER,
        lambda pad, info: (shown.__setitem__(0, shown[0] + 1), Gst.PadProbeReturn.OK)[1],
    )

started = time.monotonic()
error = None
state = pipeline.set_state(Gst.State.PLAYING)
if state == Gst.StateChangeReturn.FAILURE:
    error = {"message": "pipeline failed to start"}
else:
    bus = pipeline.get_bus()
    while time.monotonic() - started < args.seconds:
        message = bus.timed_pop_filtered(
            100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS
        )
        if message is None:
            continue
        if message.type == Gst.MessageType.ERROR:
            value, debug = message.parse_error()
            error = {"message": str(value), "debug": debug}
        break
elapsed = time.monotonic() - started
pipeline.set_state(Gst.State.NULL)
print(
    json.dumps(
        {
            "stages": args.stages,
            "io_mode": args.io_mode,
            "seconds": round(elapsed, 3),
            "decoded_frames": decoded[0],
            "sink_frames": shown[0],
            "decoded_fps": round(decoded[0] / elapsed, 2) if elapsed else None,
            "sink_fps": round(shown[0] / elapsed, 2) if elapsed else None,
            "error": error,
        }
    )
)
