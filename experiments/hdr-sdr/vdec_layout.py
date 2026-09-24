#!/usr/bin/env python3
"""Pull real samples across the VDEC -> GL boundary with an appsink.

Prints caps, memory allocator, plane count, strides and plane offsets so the
actual layout (Amlogic NM12 vs plain NV12, alignment, tiling) is visible
instead of inferred.
"""

import json
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo

Gst.init(None)

DEC = (
    "filesrc location=/var/tmp/movie-sample-4k-hevc-video-only.mkv ! matroskademux ! h265parse ! "
    "capssetter caps=video/x-h265,colorimetry=bt2020 ! v4l2h265dec capture-io-mode=mmap"
)

STAGE = sys.argv[1] if len(sys.argv) > 1 else "dec"
PIPELINES = {
    "dec": DEC + " ! appsink name=snk max-buffers=4 drop=true sync=false",
    "upload": DEC + " ! glupload ! appsink name=snk max-buffers=4 drop=true sync=false",
    "upload_cc": DEC + " ! glupload ! glcolorconvert ! appsink name=snk max-buffers=4 drop=true sync=false",
}

pipeline = Gst.parse_launch(PIPELINES[STAGE])
appsink = pipeline.get_by_name("snk")
out = []


def on_sample(sink):
    if len(out) >= 3:
        return Gst.FlowReturn.OK
    sample = sink.pull_sample()
    if sample is None:
        loop.quit()
        return Gst.FlowReturn.EOS
    buf = sample.get_buffer()
    caps = sample.get_caps()
    entry = {
        "caps": caps.to_string(),
        "buffer_size": buf.get_size(),
        "n_memory": buf.get_n_memory(),
        "memory": [
            {
                "allocator": (buf.peek_memory(i).get_allocator().get_name()
                              if buf.peek_memory(i).get_allocator() else None),
                "size": buf.peek_memory(i).get_size(),
            }
            for i in range(buf.get_n_memory())
        ],
    }
    meta = GstVideo.VideoMeta.get(buf)
    if meta is not None:
        entry["video_meta"] = {
            "format": meta.get_format().to_string(),
            "width": meta.get_width(),
            "height": meta.get_height(),
            "n_planes": meta.get_n_planes(),
            "stride": [meta.get_stride(p) for p in range(meta.get_n_planes())],
            "offset": [meta.get_offset(p) for p in range(meta.get_n_planes())],
            "flags": int(meta.get_flags()),
        }
    out.append(entry)
    if len(out) >= 3:
        loop.quit()
    return Gst.FlowReturn.OK


loop = GLib.MainLoop()
appsink.connect("new-sample", on_sample)
pipeline.set_state(Gst.State.PLAYING)
GLib.timeout_add_seconds(12, lambda: (loop.quit(), False)[1])
loop.run()
pipeline.set_state(Gst.State.NULL)
print(json.dumps({"stage": STAGE, "samples": out}, indent=2))
