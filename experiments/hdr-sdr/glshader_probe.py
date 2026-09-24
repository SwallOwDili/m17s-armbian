#!/usr/bin/env python3
"""Minimal glshader throughput probe (synthetic 1080p RGBA source)."""

import json
import os
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

SHADER = (
    "precision highp float;\n"
    "varying vec2 v_texcoord;\n"
    "uniform sampler2D tex;\n"
    "void main() { gl_FragColor = texture2D(tex, v_texcoord); }\n"
)

Gst.init(None)
width = os.environ.get("PROBE_WIDTH", "1920")
height = os.environ.get("PROBE_HEIGHT", "1080")
buffers = os.environ.get("PROBE_BUFFERS", "150")
pipeline = Gst.parse_launch(
    f"videotestsrc num-buffers={buffers} ! "
    f"video/x-raw,format=RGBA,width={width},height={height},framerate=30/1 ! "
    "glupload ! glshader name=sh ! gldownload ! fakesink sync=false"
)
pipeline.get_by_name("sh").set_property("fragment", SHADER)

frames = [0]
pipeline.get_by_name("sh").get_static_pad("sink").add_probe(
    Gst.PadProbeType.BUFFER,
    lambda pad, info: (frames.__setitem__(0, frames[0] + 1), Gst.PadProbeReturn.OK)[1],
)

started = time.monotonic()
error = None
state = pipeline.set_state(Gst.State.PLAYING)
if state == Gst.StateChangeReturn.FAILURE:
    error = "failed to start"
else:
    bus = pipeline.get_bus()
    deadline = started + 20
    while time.monotonic() < deadline:
        message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message is None:
            continue
        if message.type == Gst.MessageType.ERROR:
            error = str(message.parse_error()[0])
        break
elapsed = time.monotonic() - started
pipeline.set_state(Gst.State.NULL)
print(json.dumps({"frames": frames[0], "seconds": round(elapsed, 3), "fps": round(frames[0] / elapsed, 2), "error": error}))
