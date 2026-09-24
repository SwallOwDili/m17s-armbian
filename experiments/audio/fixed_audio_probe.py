#!/usr/bin/env python3
"""Run an explicit V4L2 decoder with frame counts; never select software decode."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import resource
import time

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo

parser = argparse.ArgumentParser()
parser.add_argument('file')
parser.add_argument('--codec', choices=('h264', 'hevc', 'vp9'), required=True)
parser.add_argument('--loops', type=int, default=1)
parser.add_argument('--timeout', type=float, default=30)
parser.add_argument('--sink', choices=('kms', 'wayland', 'fake'), default='kms')
parser.add_argument('--container', choices=('mp4', 'mkv'))
parser.add_argument('--expected-frames', type=int)
parser.add_argument('--audio-mka')
parser.add_argument('--audio-track', type=int, default=1)
parser.add_argument('--audio-codec', choices=('dts','truehd','ac3'), default='dts')
parser.add_argument('--audio-wav', help='Optional stereo PCM WAV on the same pipeline clock')
parser.add_argument('--audio-offset-ns', type=int, default=0, help='ALSA presentation offset relative to WAV time zero')
parser.add_argument('--audio-device', default='hw:0,0')
parser.add_argument('--fade-seconds', type=float, default=0.0)
parser.add_argument('--media-duration', type=float)
parser.add_argument('--diagnostic-hdr-caps', action='store_true', help='Override compressed-stream colorimetry for throughput diagnosis only; NOT HDR tone mapping')
parser.add_argument('--hide-console-plane', action='store_true', help='Temporarily disable the opaque Meson primary plane and restore it on exit')
parser.add_argument('--capture-frame', type=int, metavar='N', help='Capture decoded frame N once (1-based) as raw NV12 plus layout JSON')
parser.add_argument('--capture-prefix', default='captured-frame', help='Output prefix for --capture-frame')
args = parser.parse_args()
if args.capture_frame is not None and args.capture_frame < 1:
    parser.error('--capture-frame must be at least 1')
if args.hide_console_plane and args.sink != 'kms':
    parser.error('--hide-console-plane requires --sink kms')
if args.diagnostic_hdr_caps and args.codec != 'hevc':
    parser.error('--diagnostic-hdr-caps is only supported for the HEVC diagnostic')

Gst.init(None)
demux, parse, decoder = {
    'h264': ('qtdemux', 'h264parse', 'v4l2h264dec'),
    'hevc': ('qtdemux', 'h265parse', 'v4l2h265dec'),
    'vp9': ('matroskademux', 'vp9parse', 'v4l2vp9dec'),
}[args.codec]
if args.container:
    demux = 'qtdemux' if args.container == 'mp4' else 'matroskademux'
sink_template = {
    'kms': 'kmssink driver-name=meson sync=true skip-vsync=true',
    'wayland': 'waylandsink display=m17s-media fullscreen=true sync=true',
    'fake': 'fakesink sync=false',
}[args.sink]
metadata_override = 'capssetter caps="video/x-h265,colorimetry=bt2020" ! ' if args.diagnostic_hdr_caps else ''
description_template = (f'filesrc location={json.dumps(str(Path(args.file).resolve()))} ! {demux} ! '
                        f'{parse} ! {metadata_override}{decoder} name=decoder capture-io-mode=dmabuf ! '
                        f'fpsdisplaysink name=fps text-overlay=false video-sink="{sink_template}" '
                        f'sync={str(args.sink != "fake").lower()}')
if args.audio_wav:
    description_template += (f' filesrc location={json.dumps(str(Path(args.audio_wav).resolve()))} ! '
                             'wavparse ! audioconvert ! audioresample ! '
                             'audio/x-raw,format=S16LE,rate=48000,channels=2 ! '
                             f'volume name=audio_fade ! alsasink name=audio device={json.dumps(args.audio_device)} '
                             f'sync=true ts-offset={args.audio_offset_ns}')


if args.audio_mka:
    audio_parse, audio_decoder = {'dts': ('dcaparse', 'avdec_dca'),
                                  'truehd': ('identity', 'avdec_truehd'),
                                  'ac3': ('ac3parse', 'avdec_ac3')}[args.audio_codec]
    description_template += (f' filesrc location={json.dumps(str(Path(args.audio_mka).resolve()))} ! '
                             f'matroskademux name=aud aud.audio_{args.audio_track} ! queue ! '
                             f'{audio_parse} ! {audio_decoder} name=audio_decoder ! audioconvert ! audioresample ! '
                             'audio/x-raw,format=S16LE,rate=48000,channels=2 ! '
                             f'volume name=audio_fade ! alsasink name=audio device={json.dumps(args.audio_device)} '
                             f'sync=true ts-offset={args.audio_offset_ns}')

def drm_call(libdrm, name, *call_args):
    ctypes.set_errno(0)
    rc = getattr(libdrm, name)(*call_args)
    if rc != 0:
        raise OSError(ctypes.get_errno(), f'{name} failed')

def prepare_drm():
    state = Path('/sys/kernel/debug/dri/0/state').read_text()
    primary = re.search(r'plane\[(\d+)\]: meson_primary_plane\n(.*?)(?=plane\[)', state, re.S)
    crtc = re.search(r'crtc\[(\d+)\]: meson_crtc', state)
    overlay = re.search(r'plane\[(\d+)\]: meson_overlay_plane', state)
    connector = re.search(r'connector\[(\d+)\]: HDMI-A-1', state)
    if not primary or not crtc or not overlay or not connector:
        raise RuntimeError('Expected Meson primary/overlay plane, CRTC, and HDMI connector not found')
    body = primary.group(2)
    fb = re.search(r'\n\s*fb=(\d+)', body)
    if (not fb or int(fb[1]) == 0 or 'crtc-pos=1920x1080+0+0' not in body or
            'src-pos=1920.000000x1080.000000+0.000000+0.000000' not in body):
        raise RuntimeError('Refusing to alter an unexpected primary plane geometry')
    libdrm = ctypes.CDLL('libdrm.so.2', use_errno=True)
    libdrm.drmSetMaster.argtypes = [ctypes.c_int]
    libdrm.drmSetMaster.restype = ctypes.c_int
    libdrm.drmSetClientCap.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64]
    libdrm.drmSetClientCap.restype = ctypes.c_int
    libdrm.drmModeSetPlane.argtypes = ([ctypes.c_int] + [ctypes.c_uint32] * 4 + [ctypes.c_int32] * 2 + [ctypes.c_uint32] * 6)
    libdrm.drmModeSetPlane.restype = ctypes.c_int
    fd = os.open('/dev/dri/card0', os.O_RDWR | os.O_CLOEXEC)
    try:
        drm_call(libdrm, 'drmSetMaster', fd)
        drm_call(libdrm, 'drmSetClientCap', fd, 2, 1)
    except BaseException:
        os.close(fd)
        raise
    restore = [fd, int(primary[1]), int(crtc[1]), int(fb[1]), 0, 0, 0, 1920, 1080, 0, 0, 1920 << 16, 1080 << 16]
    disable = [fd, int(primary[1]), int(crtc[1]), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    return libdrm, fd, restore, disable, int(overlay[1]), int(connector[1])

def capture_buffer(buffer, caps, frame_number):
    prefix = Path(args.capture_prefix)
    raw_path = prefix.parent / f'{prefix.name}.{frame_number}.nv12'
    json_path = prefix.parent / f'{prefix.name}.{frame_number}.json'
    success, map_info = buffer.map(Gst.MapFlags.READ)
    if not success:
        raise RuntimeError('read-only map failed (the DMABUF may not be CPU-mappable)')
    try:
        raw_path.write_bytes(map_info.data)
        meta = GstVideo.buffer_get_video_meta(buffer)
        layout = {'frame': frame_number, 'caps': caps.to_string() if caps else None,
                  'buffer_size': buffer.get_size(), 'mapped_size': map_info.size,
                  'video_meta': None if meta is None else {
                      'format': GstVideo.VideoFormat.to_string(meta.format),
                      'width': meta.width, 'height': meta.height, 'n_planes': meta.n_planes,
                      'offset': list(meta.offset[:meta.n_planes]), 'stride': list(meta.stride[:meta.n_planes])},
                  'raw_file': str(raw_path)}
        json_path.write_text(json.dumps(layout, indent=2) + '\n')
    finally:
        buffer.unmap(map_info)
    print(json.dumps({'capture': {'raw': str(raw_path), 'layout': str(json_path)}}), flush=True)

results = []
actual_pipelines = []
for iteration in range(args.loops):
    pipeline = drm_fd = libdrm = restore_plane = disable_plane = None
    primary_hidden = False
    frames = [0]
    capture_attempted = [False]
    result = {'iteration': iteration + 1, 'eos': False, 'errors': [], 'warnings': []}
    iteration_description = description_template
    try:
        if args.hide_console_plane:
            libdrm, drm_fd, restore_plane, disable_plane, overlay_id, connector_id = prepare_drm()
            iteration_description = description_template.replace('kmssink driver-name=meson', f'kmssink fd={drm_fd} plane-id={overlay_id} connector-id={connector_id} force-modesetting=false')
        actual_pipelines.append(iteration_description)
        pipeline = Gst.parse_launch(iteration_description)
        dec, fps, bus = pipeline.get_by_name('decoder'), pipeline.get_by_name('fps'), pipeline.get_bus()
        audio_fade = pipeline.get_by_name('audio_fade')
        if audio_fade is not None and args.fade_seconds > 0:
            audio_fade.set_property('volume', 0.0)
        def count_frame(pad, info):
            frames[0] += 1
            if args.capture_frame == frames[0] and not capture_attempted[0]:
                capture_attempted[0] = True
                try:
                    capture_buffer(info.get_buffer(), pad.get_current_caps(), frames[0])
                except Exception as error:
                    print(json.dumps({'capture_error': str(error), 'frame': frames[0]}), flush=True)
            return Gst.PadProbeReturn.OK
        dec.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, count_frame)
        audio_frames = [0]
        adec = pipeline.get_by_name('audio_decoder')
        if adec is not None:
            def count_audio(_pad, _info):
                audio_frames[0] += 1
                return Gst.PadProbeReturn.OK
            adec.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, count_audio)
        if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('pipeline failed to enter PAUSED')
        state_change, current, pending = pipeline.get_state(int(args.timeout * Gst.SECOND))
        if state_change != Gst.StateChangeReturn.SUCCESS or current != Gst.State.PAUSED:
            raise RuntimeError(f'pipeline preroll failed: result={state_change.value_nick}, state={current.value_nick}, pending={pending.value_nick}')
        if disable_plane is not None:
            drm_call(libdrm, 'drmModeSetPlane', *disable_plane)
            primary_hidden = True
            print(json.dumps({'console_plane_hidden': restore_plane[1], 'restore_framebuffer': restore_plane[3]}), flush=True)
        start, last_progress = time.monotonic(), time.monotonic()
        cpu_start = resource.getrusage(resource.RUSAGE_SELF)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('pipeline failed to enter PLAYING')
        while time.monotonic() - start < args.timeout:
            now = time.monotonic()
            elapsed_now = now - start
            if audio_fade is not None and args.fade_seconds > 0:
                level = min(1.0, elapsed_now / args.fade_seconds)
                if args.media_duration is not None:
                    level = min(level, max(0.0, (args.media_duration - elapsed_now) /
                                           args.fade_seconds))
                audio_fade.set_property('volume', level)
            if now - last_progress >= 5:
                print(json.dumps({'progress': {'iteration': iteration + 1, 'elapsed_seconds': round(now - start, 3), 'decoded_frames': frames[0], 'rendered_frames': fps.get_property('frames-rendered'), 'dropped_frames': fps.get_property('frames-dropped')}}), flush=True)
                last_progress = now
            message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR | Gst.MessageType.WARNING)
            if message is None:
                continue
            if message.type == Gst.MessageType.EOS:
                result['eos'] = True
                break
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                result['errors'].append({'message': str(error), 'debug': debug})
                break
            warning, debug = message.parse_warning()
            result['warnings'].append({'message': str(warning), 'debug': debug})
        if not result['eos'] and not result['errors']:
            result['errors'].append({'message': 'timeout'})
        elapsed = time.monotonic() - start
        usage = resource.getrusage(resource.RUSAGE_SELF)
        caps = dec.get_static_pad('src').get_current_caps()
        if adec is not None:
            acaps = adec.get_static_pad('src').get_current_caps()
            result.update(audio_decoder=adec.get_factory().get_name(), audio_decoded_buffers=audio_frames[0],
                          audio_decoded_caps=acaps.to_string() if acaps else None)
        rendered, dropped = fps.get_property('frames-rendered'), fps.get_property('frames-dropped')
        result.update(decoded_frames=frames[0], rendered_frames=rendered, dropped_frames=dropped,
                      sink_dropped_matches=dropped == 0, elapsed_seconds=round(elapsed, 3),
                      process_cpu_percent=round(100 * (usage.ru_utime + usage.ru_stime - cpu_start.ru_utime - cpu_start.ru_stime) / max(elapsed, 1e-9), 2),
                      decoded_caps=caps.to_string() if caps else None)
        if args.expected_frames is not None:
            decoded_matches = frames[0] == args.expected_frames
            rendered_matches = rendered == args.expected_frames
            result.update(expected_frames=args.expected_frames,
                          decoded_count_matches=decoded_matches,
                          rendered_count_matches=rendered_matches,
                          frame_count_matches=decoded_matches and rendered_matches)
        result['clean_pass'] = (result['eos'] and not result['errors'] and not result['warnings'] and result['sink_dropped_matches'] and result.get('decoded_count_matches', True) and result.get('rendered_count_matches', True))
        print(json.dumps({'before_cleanup': result}), flush=True)
    except KeyboardInterrupt:
        result['errors'].append({'message': 'interrupted'})
    except BaseException as error:
        result['errors'].append({'message': str(error), 'type': type(error).__name__})
    finally:
        try:
            if pipeline is not None:
                try:
                    pipeline.set_state(Gst.State.NULL)
                    pipeline.get_state(5 * Gst.SECOND)
                except BaseException as error:
                    result['errors'].append({'message': f'pipeline cleanup failed: {error}',
                                             'type': type(error).__name__})
            if primary_hidden:
                try:
                    ctypes.set_errno(0)
                    restore_rc = libdrm.drmModeSetPlane(*restore_plane)
                    restore_errno = ctypes.get_errno() if restore_rc else 0
                    print(json.dumps({'console_plane_restored': restore_rc == 0,
                                      'errno': restore_errno}), flush=True)
                    if restore_rc != 0:
                        result['errors'].append({'message': 'failed to restore console plane',
                                                 'errno': restore_errno})
                except BaseException as error:
                    result['errors'].append({'message': f'console plane restore failed: {error}',
                                             'type': type(error).__name__})
        finally:
            if drm_fd is not None:
                os.close(drm_fd)
    result['clean_pass'] = bool(result.get('clean_pass', False) and not result['errors'])
    results.append(result)
    print(json.dumps({'iteration_result': result}), flush=True)
    if result['errors']:
        break

print(json.dumps({'pipeline': actual_pipelines[-1] if actual_pipelines else None,
                  'pipelines': actual_pipelines, 'file': Path(args.file).name,
                  'diagnostic_colorimetry_override': 'bt2020' if args.diagnostic_hdr_caps else None,
                  'results': results}), flush=True)
raise SystemExit(0 if len(results) == args.loops and all(r['clean_pass'] for r in results) else 1)
