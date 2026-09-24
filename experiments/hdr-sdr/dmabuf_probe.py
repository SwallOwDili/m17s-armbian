#!/usr/bin/env python3
"""Inspect the memory backing the first decoded V4L2 frames."""

import json
import sys
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo


Gst.init(None)
path = sys.argv[1]
io_mode = sys.argv[2] if len(sys.argv) > 2 else "dmabuf"
pipeline = Gst.parse_launch(
    f'filesrc location="{path}" ! matroskademux ! h265parse ! '
    'capssetter caps="video/x-h265,colorimetry=bt2020" ! '
    f'v4l2h265dec name=decoder capture-io-mode={io_mode} ! fakesink sync=false'
)
decoder = pipeline.get_by_name("decoder")
samples = []


def inspect_buffer(pad, info):
    buffer = info.get_buffer()
    video_meta = GstVideo.buffer_get_video_meta(buffer)
    memories = []
    for index in range(buffer.n_memory()):
        memory = buffer.peek_memory(index)
        allocator = memory.allocator
        memories.append(
            {
                "index": index,
                "allocator": allocator.name if allocator else None,
                "dmabuf": memory.is_type("DMABuf"),
                "fd_memory": memory.is_type("fdmem"),
                "maxsize": memory.maxsize,
            }
        )
    samples.append(
        {
            "caps": pad.get_current_caps().to_string(),
            "size": buffer.get_size(),
            "video_meta": None if video_meta is None else {
                "format": GstVideo.VideoFormat.to_string(video_meta.format),
                "width": video_meta.width,
                "height": video_meta.height,
                "planes": video_meta.n_planes,
                "offset": list(video_meta.offset[:video_meta.n_planes]),
                "stride": list(video_meta.stride[:video_meta.n_planes]),
            },
            "memories": memories,
        }
    )
    return Gst.PadProbeReturn.OK


decoder.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, inspect_buffer)
pipeline.set_state(Gst.State.PLAYING)
bus = pipeline.get_bus()
deadline = time.monotonic() + 10
error = None
while time.monotonic() < deadline and len(samples) < 3:
    message = bus.timed_pop_filtered(
        100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS
    )
    if message and message.type == Gst.MessageType.ERROR:
        value, debug = message.parse_error()
        error = {"message": str(value), "debug": debug}
        break
    if message and message.type == Gst.MessageType.EOS:
        break
pipeline.set_state(Gst.State.NULL)
pipeline.get_state(5 * Gst.SECOND)
print(json.dumps({"capture_io_mode": io_mode, "samples": samples[:3], "error": error}, indent=2))
raise SystemExit(0 if samples and not error else 1)
