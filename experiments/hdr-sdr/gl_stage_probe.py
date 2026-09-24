#!/usr/bin/env python3
"""Synthetic GstGL stage-by-stage probe (no VDEC involved).

Isolates where the Mali-450 GL path loses its throughput: source generation,
upload, colorconvert, glshader, download. Every stage can be switched on/off
from the command line so a single variable is changed at a time.
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
    "void main() { vec4 c = texture2D(tex, v_texcoord);\n"
    "  gl_FragColor = vec4(c.rgb * vec3(1.1, 1.0, 0.9) + vec3(0.01), 1.0); }\n"
)

parser = argparse.ArgumentParser()
parser.add_argument("--width", type=int, default=3840)
parser.add_argument("--height", type=int, default=2160)
parser.add_argument("--format", default="NV12", choices=("NV12", "RGBA", "I420"))
parser.add_argument("--buffers", type=int, default=150)
parser.add_argument("--upload", action="store_true")
parser.add_argument("--convert", action="store_true")
parser.add_argument("--shader", action="store_true")
parser.add_argument("--download", action="store_true")
parser.add_argument("--sink", action="store_true", help="glimagesink instead of fakesink")
parser.add_argument("--passes", type=int, default=0, help="extra passthrough glshader passes")
parser.add_argument("--source", default="videotestsrc", choices=("videotestsrc", "gltestsrc"))
parser.add_argument("--out-width", type=int, default=0, help="0 = same as source")
parser.add_argument("--shader-file")
args = parser.parse_args()

if args.source == "gltestsrc":
    parts = [
        f"gltestsrc num-buffers={args.buffers} ! "
        f"video/x-raw(memory:GLMemory),width={args.width},height={args.height},framerate=30/1"
    ]
else:
    parts = [
        f"videotestsrc num-buffers={args.buffers} ! "
        f"video/x-raw,format={args.format},width={args.width},height={args.height},framerate=30/1"
    ]
if args.upload:
    parts.append("glupload")
if args.convert:
    parts.append("glcolorconvert")
if args.shader:
    parts.append("glshader name=sh")
for i in range(args.passes):
    parts.append(f"glshader name=p{i}")
if args.out_width:
    parts.append(
        f"capsfilter caps=video/x-raw(memory:GLMemory),width={args.out_width},height={args.out_width * args.height // args.width}"
    )
if args.download:
    parts.append("gldownload")
parts.append("glimagesink name=out sync=false" if args.sink else "fakesink name=out sync=false")

Gst.init(None)
pipeline = Gst.parse_launch(" ! ".join(parts))
src = PASSTHROUGH
if args.shader_file:
    with open(args.shader_file) as handle:
        src = handle.read()
shader = pipeline.get_by_name("sh")
if shader is not None:
    shader.set_property("fragment", src)
for i in range(args.passes):
    extra = pipeline.get_by_name(f"p{i}")
    if extra is not None:
        extra.set_property("fragment", PASSTHROUGH)

sink = pipeline.get_by_name("out")
frames = [0]
sink.get_static_pad("sink").add_probe(
    Gst.PadProbeType.BUFFER,
    lambda pad, info: (frames.__setitem__(0, frames[0] + 1), Gst.PadProbeReturn.OK)[1],
)

started = time.monotonic()
error = None
state = pipeline.set_state(Gst.State.PLAYING)
if state == Gst.StateChangeReturn.FAILURE:
    error = {"message": "failed to start"}
else:
    bus = pipeline.get_bus()
    while time.monotonic() - started < 60:
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
            "pipeline": " ! ".join(parts),
            "buffers": args.buffers,
            "seconds": round(elapsed, 3),
            "frames": frames[0],
            "fps": round(frames[0] / elapsed, 2) if elapsed else None,
            "error": error,
        }
    )
)
