#!/usr/bin/env python3
"""Capture one decoded NV12 frame (plus its layout) with a clean shutdown."""

import argparse
import json
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo

parser = argparse.ArgumentParser()
parser.add_argument("--file", default="/var/tmp/movie-sample-4k-hevc-video-only.mkv")
parser.add_argument("--frame", type=int, default=120)
parser.add_argument("--prefix", default="/var/tmp/frame")
parser.add_argument("--io-mode", default="mmap", choices=("mmap", "dmabuf"))
parser.add_argument("--timeout", type=float, default=120.0)
args = parser.parse_args()

Gst.init(None)
pipeline = Gst.parse_launch(
    f'filesrc location="{args.file}" ! matroskademux ! h265parse ! '
    'capssetter caps=video/x-h265,colorimetry=bt2020 ! '
    f'v4l2h265dec name=decoder capture-io-mode={args.io_mode} ! '
    'fakesink name=out sync=false'
)
decoder = pipeline.get_by_name("decoder")
state = {"count": 0, "saved": None, "error": None}


def on_buffer(pad, info):
    state["count"] += 1
    if state["count"] != args.frame:
        return Gst.PadProbeReturn.OK
    buffer = info.get_buffer()
    ok, mapping = buffer.map(Gst.MapFlags.READ)
    if not ok:
        state["error"] = "map failed"
        return Gst.PadProbeReturn.OK
    try:
        meta = GstVideo.buffer_get_video_meta(buffer)
        caps = pad.get_current_caps()
        structure = caps.get_structure(0) if caps else None
        if meta is None:
            if structure is None:
                state["error"] = "no caps"
                return Gst.PadProbeReturn.OK
            width = structure.get_value("width")
            height = structure.get_value("height")
            luma_stride = width
            chroma_stride = width
            luma_offset = 0
            chroma_offset = width * height
        else:
            width, height = meta.width, meta.height
            luma_stride = meta.stride[0]
            chroma_stride = meta.stride[1]
            luma_offset = meta.offset[0]
            chroma_offset = meta.offset[1]
        luma_size = luma_stride * height
        chroma_size = chroma_stride * ((height + 1) // 2)
        data = bytes(mapping.data)
        with open(f"{args.prefix}.nv12", "wb") as handle:
            handle.write(data[luma_offset : luma_offset + luma_size])
            handle.write(data[chroma_offset : chroma_offset + chroma_size])
        layout = {
            "frame": args.frame,
            "caps": pad.get_current_caps().to_string(),
            "buffer_size": buffer.get_size(),
            "mapped_size": mapping.size,
            "video_meta": {
                "format": GstVideo.VideoFormat.to_string(meta.format) if meta else "NV12",
                "width": width,
                "height": height,
                "from_video_meta": meta is not None,
                "y_stride": luma_stride,
                "uv_stride": chroma_stride,
                "y_offset": luma_offset,
                "uv_offset": chroma_offset,
            },
            "nv12_file": f"{args.prefix}.nv12",
        }
        with open(f"{args.prefix}.json", "w") as handle:
            json.dump(layout, handle, indent=2)
        state["saved"] = layout
    finally:
        buffer.unmap(mapping)
    return Gst.PadProbeReturn.EOS


decoder.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer)
started = time.monotonic()
if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    state["error"] = "failed to start"
else:
    bus = pipeline.get_bus()
    while time.monotonic() - started < args.timeout:
        if state["saved"] or state["error"]:
            break
        message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message is None:
            continue
        if message.type == Gst.MessageType.ERROR:
            state["error"] = str(message.parse_error()[0])
        break
pipeline.set_state(Gst.State.NULL)
print(json.dumps({"decoded": state["count"], "saved": state["saved"], "error": state["error"]}, indent=2))
