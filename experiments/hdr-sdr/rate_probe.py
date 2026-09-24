#!/usr/bin/env python3
"""Measure sustained frames per second of a video pipeline on the M17S.

Counts buffers on the decoder source pad over a fixed wall-clock window so that
pipelines which never reach EOS still produce a comparable number.
"""

import argparse
import json
import resource
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


def cma_free_kib():
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("CmaFree"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


PASSTHROUGH_FRAGMENT = """
varying vec2 v_texcoord;
uniform sampler2D tex;
void main() { gl_FragColor = texture2D(tex, v_texcoord); }
"""

DECODER = (
    'filesrc location="{path}" ! matroskademux ! h265parse ! '
    'capssetter caps=video/x-h265,colorimetry=bt2020 ! '
    'v4l2h265dec name=decoder capture-io-mode={io_mode}'
)

VARIANTS = {
    "decode": DECODER + " ! fakesink name=out sync=false",
    "wayland": DECODER
    + " ! waylandsink display=m17s-media fullscreen=true sync=false",
    "upload": DECODER + " ! glupload ! gldownload ! fakesink name=out sync=false",
    "convert4k": DECODER
    + " ! glupload ! glcolorconvert ! gldownload ! fakesink name=out sync=false",
    "scale_1080": DECODER
    + " ! glupload ! glcolorconvert ! glshader name=sh ! "
    "capsfilter caps=video/x-raw(memory:GLMemory),width=1920,height=1080 ! "
    "gldownload ! fakesink name=out sync=false",
    "shader_4k": DECODER
    + " ! glupload ! glcolorconvert ! glshader name=sh ! "
    "gldownload ! fakesink name=out sync=false",
    "glsink_1080": DECODER
    + " ! glupload ! glcolorconvert ! glshader name=sh ! "
    "glimagesink name=out sync=false",
    "glsink_4k_noshader": DECODER
    + " ! glupload ! glcolorconvert ! glimagesink name=out sync=false",
}


parser = argparse.ArgumentParser()
parser.add_argument("variant", choices=sorted(VARIANTS))
parser.add_argument("--file", default="/var/tmp/movie-sample-4k-hevc-video-only.mkv")
parser.add_argument("--seconds", type=float, default=15.0)
parser.add_argument("--shader", help="read a fragment shader from this file")
parser.add_argument("--map-output", action="store_true", help="map and checksum buffers entering the sink")
parser.add_argument("--io-mode", default="mmap", choices=("mmap", "dmabuf", "dmabuf-import"))
args = parser.parse_args()

Gst.init(None)
description = VARIANTS[args.variant].format(path=args.file, io_mode=args.io_mode)
pipeline = Gst.parse_launch(description)
shader = pipeline.get_by_name("sh")
if shader is not None:
    if args.shader:
        with open(args.shader) as handle:
            shader.set_property("fragment", handle.read())
    else:
        shader.set_property("fragment", PASSTHROUGH_FRAGMENT)

frames = [0]
decoder = pipeline.get_by_name("decoder")
decoder.get_static_pad("src").add_probe(
    Gst.PadProbeType.BUFFER,
    lambda pad, info: (frames.__setitem__(0, frames[0] + 1), Gst.PadProbeReturn.OK)[1],
)

usage_start = resource.getrusage(resource.RUSAGE_SELF)
cma_before = cma_free_kib()

checksum = [0]
if args.map_output:

    def checksum_buffer(_pad, info):
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        ok, mapping = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            checksum[0] ^= sum(mapping.data[:4096])
        finally:
            buffer.unmap(mapping)
        return Gst.PadProbeReturn.OK

    sink_element = pipeline.get_by_name("out")
    if sink_element is None:
        sink_element = pipeline.get_by_name("fps")
    target_pad = sink_element.get_static_pad("sink") if sink_element else None
    if target_pad is not None:
        target_pad.add_probe(Gst.PadProbeType.BUFFER, checksum_buffer)

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
        break
elapsed = time.monotonic() - started
pipeline.set_state(Gst.State.NULL)
usage = resource.getrusage(resource.RUSAGE_SELF)
cpu = (usage.ru_utime + usage.ru_stime - usage_start.ru_utime - usage_start.ru_stime)
print(
    json.dumps(
        {
            "variant": args.variant,
            "windows_seconds": round(elapsed, 3),
            "decoded_frames": frames[0],
            "fps": round(frames[0] / elapsed, 2) if elapsed else None,
            "process_cpu_percent": round(100 * cpu / elapsed, 1) if elapsed else None,
            "cma_free_kib_before": cma_before,
            "cma_free_kib_after": cma_free_kib(),
            "output_checksum": checksum[0] if args.map_output else None,
            "error": error,
        }
    )
)
