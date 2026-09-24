#!/usr/bin/env python3
"""Play 4K HDR10 as SDR on the M17S panel, with a restart-based loop.

Pipeline: VDEC (dmabuf) -> glupload -> glcolorconvert -> tonemap shader -> GL sink.
The shader property takes inline GLSL rather than a path, so gst-launch cannot
be used directly.

The loop does not seek back to zero: after the first pass this decoder/mediak
combination does not resume reliably from a post-EOS seek, so each round builds
a fresh pipeline instead.

Run on the box:
  sudo -E python3 play_hdr_sdr.py --loop
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
parser.add_argument("--seconds", type=float, default=0, help="per-round limit, 0 = until EOS")
parser.add_argument("--loop", action="store_true")
parser.add_argument("--rounds", type=int, default=0, help="0 = unlimited when looping")
parser.add_argument("--sink", default="glimagesink")
parser.add_argument("--colorimetry", default="bt2020", choices=("none", "bt2020", "bt601"))
parser.add_argument("--audio-file", default="", help="separate audio container (e.g. .mka)")
parser.add_argument("--audio-track", default="audio_1", help="demux pad name inside the audio file")
parser.add_argument("--audio-parser", default="dcaparse")
parser.add_argument("--audio-decoder", default="avdec_dca")
parser.add_argument("--audio-offset", type=int, default=42000000, help="ts-offset in ns")
parser.add_argument("--audio-device", default="hw:0,0")
args = parser.parse_args()

Gst.init(None)

AUDIO_CHAIN = ""
if args.audio_file:
    AUDIO_CHAIN = (
        f' filesrc location="{args.audio_file}" ! matroskademux name=aud '
        f"aud.{args.audio_track} ! queue ! {args.audio_parser} ! {args.audio_decoder} ! "
        "audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! "
        f"alsasink name=audio device={args.audio_device} sync=true ts-offset={args.audio_offset}"
    )

label = "" if args.colorimetry == "none" else f"video/x-raw,format=NV12,colorimetry={args.colorimetry} ! "
DESCRIPTION = (
    f'filesrc location="{args.file}" ! matroskademux ! h265parse ! '
    "capssetter caps=video/x-h265,colorimetry=bt2020 ! "
    f"v4l2h265dec name=dec capture-io-mode={args.io_mode} ! "
    f"{label}"
    "glupload ! glcolorconvert ! glshader name=sh ! "
    f"{args.sink} name=out sync=true"
    + (" fullscreen=true" if args.sink == "waylandsink" else "")
    + AUDIO_CHAIN
)

SHADER_SRC = None
if args.shader:
    with open(args.shader) as handle:
        SHADER_SRC = handle.read()


def round_once(round_no):
    pipeline = Gst.parse_launch(DESCRIPTION)
    if SHADER_SRC is not None:
        pipeline.get_by_name("sh").set_property("fragment", SHADER_SRC)
    counter = [0]
    pipeline.get_by_name("dec").get_static_pad("src").add_probe(
        Gst.PadProbeType.BUFFER,
        lambda pad, info: (counter.__setitem__(0, counter[0] + 1), Gst.PadProbeReturn.OK)[1],
    )
    started = time.monotonic()
    pipeline.set_state(Gst.State.PLAYING)
    bus = pipeline.get_bus()
    error = None
    stopped = False
    next_report = started + 5
    while not stopped:
        message = bus.timed_pop_filtered(
            200 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS
        )
        now = time.monotonic()
        if now >= next_report:
            elapsed = now - started
            print(f"round={round_no} frames={counter[0]} elapsed={elapsed:.1f}s "
                  f"fps={counter[0] / elapsed:.2f}", flush=True)
            next_report = now + 5
        if message is not None:
            if message.type == Gst.MessageType.ERROR:
                value, debug = message.parse_error()
                error = f"{value} | {debug}"
                print(f"ERROR: {error}", flush=True)
            stopped = True
        elif args.seconds and now - started >= args.seconds:
            stopped = True
    pipeline.set_state(Gst.State.NULL)
    elapsed = time.monotonic() - started
    print(f"round={round_no} ended frames={counter[0]} elapsed={elapsed:.1f}s "
          f"fps={counter[0] / elapsed:.2f}", flush=True)
    return error


if args.loop:
    round_no = 0
    while True:
        round_no += 1
        round_once(round_no)
        if args.rounds and round_no >= args.rounds:
            break
        time.sleep(1)
else:
    print(DESCRIPTION, flush=True)
    round_once(1)
print("stopped", flush=True)
