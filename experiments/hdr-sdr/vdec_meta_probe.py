#!/usr/bin/env python3
"""Print the real buffer layout crossing the VDEC -> GL boundary.

The V4L2 capture device only advertises Amlogic's private NM12 format, so the
question that decides whether HDR tone mapping can run on the GPU at all is
what actually arrives at glupload: which caps, which GstVideoMeta (stride,
plane offsets), and whether the memory is GL memory or system memory.

Runs one short pipeline per stage and dumps the first few buffers.
"""

import json
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo

Gst.init(None)

DEC = (
    "filesrc location=/var/tmp/movie-sample-4k-hevc-video-only.mkv ! matroskademux ! h265parse ! "
    "capssetter caps=video/x-h265,colorimetry=bt2020 ! v4l2h265dec capture-io-mode=mmap name=dec"
)

STAGES = {
    "decoder_only": DEC + " ! fakesink name=out sync=false",
    "after_upload": DEC + " ! glupload name=up ! fakesink name=out sync=false",
    "after_convert": DEC + " ! glupload ! glcolorconvert name=cc ! fakesink name=out sync=false",
}

report = {}

for stage, desc in STAGES.items():
    pipeline = Gst.parse_launch(desc)
    samples = []

    def probe(_pad, info, target=stage):
        buf = info.get_buffer()
        if buf is None or len(samples) >= 3:
            return Gst.PadProbeReturn.OK
        entry = {
            "caps": _pad.get_current_caps().to_string() if _pad.get_current_caps() else None,
            "size": buf.get_size(),
            "n_memory": buf.get_n_memory(),
            "memory": [],
        }
        for i in range(buf.get_n_memory()):
            mem = buf.peek_memory(i)
            entry["memory"].append(
                {
                    "allocator": mem.get_allocator().get_name() if mem.get_allocator() else None,
                    "size": mem.get_size(),
                    "is_gl": GstVideo is not None and "GLMemory" in str(type(mem)),
                }
            )
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
        samples.append(entry)
        return Gst.PadProbeReturn.OK

    target = pipeline.get_by_name({"decoder_only": "dec", "after_upload": "up", "after_convert": "cc"}[stage])
    if target is None:
        target = pipeline.get_by_name("out")
    watch = target.get_static_pad("src") if target.get_static_pad("src") else target.get_static_pad("sink")
    watch.add_probe(Gst.PadProbeType.BUFFER, probe)

    pipeline.set_state(Gst.State.PLAYING)
    bus = pipeline.get_bus()
    deadline = time.monotonic() + 6
    error = None
    while time.monotonic() < deadline:
        msg = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg is not None:
            if msg.type == Gst.MessageType.ERROR:
                error = str(msg.parse_error()[0])
            break
    pipeline.set_state(Gst.State.NULL)
    report[stage] = {"samples": samples, "error": error}

print(json.dumps(report, indent=2))
