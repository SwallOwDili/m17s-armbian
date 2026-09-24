#!/usr/bin/env python3
"""Provide V4L2 with DMA-heap buffers and verify zero-copy GL import."""

import fcntl
import json
import os
import struct
import sys
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstAllocators", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstAllocators, GstVideo


DMA_HEAP_IOCTL_ALLOC = 0xC0184800


class DmaHeapPool(Gst.BufferPool):
    def __init__(self, heap_path):
        super().__init__()
        self.heap_fd = os.open(heap_path, os.O_RDWR | os.O_CLOEXEC)
        self.allocator = GstAllocators.DmaBufAllocator.new()
        self.size = 0
        self.allocations = 0

    def _alloc_fd(self, size):
        request = bytearray(struct.pack("=QIIQ", size, 0, os.O_RDWR | os.O_CLOEXEC, 0))
        fcntl.ioctl(self.heap_fd, DMA_HEAP_IOCTL_ALLOC, request, True)
        _length, dma_fd, _fd_flags, _heap_flags = struct.unpack("=QIIQ", request)
        return dma_fd

    def do_set_config(self, config):
        ok, _caps, size, _minimum, _maximum = Gst.BufferPool.config_get_params(config)
        if not ok or size <= 0:
            return False
        self.size = size
        return Gst.BufferPool.do_set_config(self, config)

    def do_alloc_buffer(self, _params):
        buffer = Gst.Buffer.new()
        for plane_size in (8337408, 4147200):
            dma_fd = self._alloc_fd(plane_size)
            memory = GstAllocators.DmaBufAllocator.alloc(self.allocator, dma_fd, plane_size)
            if memory is None:
                os.close(dma_fd)
                return Gst.FlowReturn.ERROR, None
            buffer.append_memory(memory)
        # The Meson VDEC requires the explicit NV12 plane layout to accept
        # imported buffers. Keep the offsets/stride consistent with the
        # 3840x2160 NV12 stream used by the probe.
        GstVideo.buffer_add_video_meta_full(
            buffer,
            GstVideo.VideoFrameFlags.NONE,
            GstVideo.VideoFormat.NV12,
            3840,
            2160,
            2,
            [0, 8337408, 0, 0],
            [3840, 3840, 0, 0],
        )
        self.allocations += 1
        return Gst.FlowReturn.OK, buffer


Gst.init(None)
path = sys.argv[1]
pool = DmaHeapPool("/dev/dma_heap/linux,cma")
allocation_queries = []
pipeline = Gst.parse_launch(
    f'filesrc location="{path}" ! matroskademux ! h265parse ! '
    'capssetter caps="video/x-h265,colorimetry=bt2020" ! '
    'v4l2h265dec name=decoder capture-io-mode=dmabuf-import ! identity name=heappool ! '
    'glupload name=upload ! glcolorconvert ! glshader ! fakesink sync=false'
)
decoder = pipeline.get_by_name("decoder")
upload = pipeline.get_by_name("upload")
heappool = pipeline.get_by_name("heappool")
input_memories = []
decoded_frames = 0


def provide_pool(pad, parent, query):
    if query.type != Gst.QueryType.ALLOCATION:
        return pad.query_default(parent, query)
    downstream_ok = parent.get_static_pad("src").peer_query(query)
    caps, need_pool = query.parse_allocation()
    allocation_queries.append({"caps": caps.to_string(), "need_pool": need_pool})
    size = 3840 * 2160 * 3 // 2
    if query.get_n_allocation_pools():
        query.set_nth_allocation_pool(0, pool, size, 4, 0)
    else:
        query.add_allocation_pool(pool, size, 4, 0)
    return downstream_ok


heappool.get_static_pad("sink").set_query_function_full(provide_pool)


def inspect_input(_pad, info):
    buffer = info.get_buffer()
    if len(input_memories) < 3:
        input_memories.append(
            [
                {
                    "allocator": memory.allocator.name if memory.allocator else None,
                    "dmabuf": memory.is_type("DMABuf"),
                    "size": memory.size,
                    "maxsize": memory.maxsize,
                }
                for memory in (buffer.peek_memory(i) for i in range(buffer.n_memory()))
            ]
        )
    return Gst.PadProbeReturn.OK


def count_frame(_pad, _info):
    global decoded_frames
    decoded_frames += 1
    return Gst.PadProbeReturn.OK


decoder.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, count_frame)
upload.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, inspect_input)

started = time.monotonic()
change = pipeline.set_state(Gst.State.PLAYING)
bus = pipeline.get_bus()
error = None
eos = False
deadline = started + 30
while change != Gst.StateChangeReturn.FAILURE and time.monotonic() < deadline:
    message = bus.timed_pop_filtered(
        100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS
    )
    if message and message.type == Gst.MessageType.ERROR:
        value, debug = message.parse_error()
        error = {"message": str(value), "debug": debug}
        break
    if message and message.type == Gst.MessageType.EOS:
        eos = True
        break
pipeline.set_state(Gst.State.NULL)
pipeline.get_state(5 * Gst.SECOND)
elapsed = time.monotonic() - started
result = {
    "eos": eos,
    "error": error,
    "elapsed_seconds": round(elapsed, 3),
    "decoded_frames": decoded_frames,
    "pool_allocations": pool.allocations,
    "allocation_queries": allocation_queries,
    "glupload_input_memories": input_memories,
}
print(json.dumps(result, indent=2))
raise SystemExit(0 if eos and not error and decoded_frames == 1088 else 1)
