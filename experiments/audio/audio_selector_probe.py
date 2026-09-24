#!/usr/bin/env python3
"""Single-pipeline HEVC plus predecoded multi-audio input-selector probe."""
import argparse, json, time, resource
from pathlib import Path
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

p = argparse.ArgumentParser()
p.add_argument('video'); p.add_argument('audio')
p.add_argument('--timeout', type=float, default=65.0)
p.add_argument('--expected-frames', type=int, default=1088)
a = p.parse_args(); Gst.init(None)

tracks = [(0, None, 'avdec_truehd'), (1, 'dcaparse', 'avdec_dca'),
          (5, 'ac3parse', 'avdec_ac3'), (2, 'ac3parse', 'avdec_ac3')]
pad_index = {track: pos for pos, (track, _, _) in enumerate(tracks)}
schedule = [(0.0, 0), (10.0, 1), (20.0, 5), (30.0, 2)]
result = {'errors': [], 'warnings': [], 'switches': [], 'decoders': {},
          'selector_inputs': {}, 'selector_output': {}, 'audio_sink': {}, 'qos': 0}
frames = [0]

def emit(x): print(json.dumps(x, ensure_ascii=False), flush=True)
def stats(): return {'buffers': 0, 'first_pts': None, 'last_pts': None,
                     'stream_ids': [], 'caps': [], 'current_stream': None, 'by_stream': {}}
def probe(bucket):
    def cb(_pad, info):
        if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
            e = info.get_event()
            if e.type == Gst.EventType.STREAM_START:
                sid = e.parse_stream_start()
                bucket['current_stream'] = sid
                if sid not in bucket['stream_ids']: bucket['stream_ids'].append(sid)
            elif e.type == Gst.EventType.CAPS:
                cap = e.parse_caps().to_string()
                if cap not in bucket['caps']: bucket['caps'].append(cap)
        if info.type & Gst.PadProbeType.BUFFER:
            b = info.get_buffer(); pts = None if b.pts == Gst.CLOCK_TIME_NONE else int(b.pts)
            bucket['buffers'] += 1
            sid = bucket['current_stream'] or '<unknown>'
            st = bucket['by_stream'].setdefault(sid, {'buffers': 0, 'first_pts': pts, 'last_pts': pts})
            st['buffers'] += 1; st['last_pts'] = pts
            if bucket['first_pts'] is None: bucket['first_pts'] = pts
            bucket['last_pts'] = pts
        return Gst.PadProbeReturn.OK
    return cb

vp = str(Path(a.video).resolve()); ap = str(Path(a.audio).resolve())
desc = (f'filesrc location={json.dumps(vp)} ! matroskademux ! h265parse ! '
        'capssetter caps="video/x-h265,colorimetry=bt2020" ! '
        'v4l2h265dec name=vdec capture-io-mode=dmabuf ! '
        'fpsdisplaysink name=fps text-overlay=false '
        'video-sink="waylandsink display=m17s-media fullscreen=true sync=true" sync=true '
        f'filesrc location={json.dumps(ap)} ! matroskademux name=aud '
        'input-selector name=sel sync-streams=true sync-mode=clock cache-buffers=true ! '
        'alsasink name=asink device=hw:0,0 sync=true ts-offset=42000000 ')
for track, parser, decoder in tracks:
    parse_part = f'{parser} ! ' if parser else ''
    desc += (f'aud.audio_{track} ! queue ! {parse_part}{decoder} name=adec{track} ! '
             'audioconvert ! audioresample ! '
             'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2 ! '
             f'queue ! sel.sink_{pad_index[track]} ')

pipe = Gst.parse_launch(desc); fps = pipe.get_by_name('fps'); sel = pipe.get_by_name('sel')
pipe.get_by_name('vdec').get_static_pad('src').add_probe(
    Gst.PadProbeType.BUFFER, lambda _p, _i: (frames.__setitem__(0, frames[0] + 1), Gst.PadProbeReturn.OK)[1])
mask = Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM
for track, _, decoder in tracks:
    ds = stats(); result['decoders'][str(track)] = {'factory': decoder, **ds}
    pipe.get_by_name(f'adec{track}').get_static_pad('src').add_probe(mask, probe(result['decoders'][str(track)]))
    ss = stats(); result['selector_inputs'][str(track)] = ss
    sel.get_static_pad(f'sink_{pad_index[track]}').add_probe(mask, probe(ss))
result['selector_output'] = stats(); sel.get_static_pad('src').add_probe(mask, probe(result['selector_output']))
result['audio_sink'] = stats(); pipe.get_by_name('asink').get_static_pad('sink').add_probe(mask, probe(result['audio_sink']))

try:
    if pipe.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE: raise RuntimeError('PAUSED failed')
    st, cur, pending = pipe.get_state(15 * Gst.SECOND)
    if st != Gst.StateChangeReturn.SUCCESS: raise RuntimeError(f'preroll failed: {st.value_nick}/{cur.value_nick}/{pending.value_nick}')
    pads = {track: sel.get_static_pad(f'sink_{pad_index[track]}') for track, _, _ in tracks}
    sel.set_property('active-pad', pads[0])
    clock = Gst.SystemClock.obtain(); pipe.use_clock(clock)
    base = clock.get_time() + 200 * Gst.MSECOND
    cpu_start = resource.getrusage(resource.RUSAGE_SELF)
    pipe.set_start_time(Gst.CLOCK_TIME_NONE); pipe.set_base_time(base)
    if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE: raise RuntimeError('PLAYING failed')
    bus = pipe.get_bus(); deadline = time.monotonic() + a.timeout; nxt = 1; eos = False; last = 0
    while time.monotonic() < deadline and not eos and not result['errors']:
        elapsed = max(0.0, (clock.get_time() - base) / Gst.SECOND)
        while nxt < len(schedule) and elapsed >= schedule[nxt][0]:
            track = schedule[nxt][1]; before = result['audio_sink']['buffers']
            sel.set_property('active-pad', pads[track])
            result['switches'].append({'elapsed': round(elapsed, 3), 'track': track,
                                       'sink_buffers_before': before}); nxt += 1
        m = bus.timed_pop_filtered(50 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR |
                                   Gst.MessageType.WARNING | Gst.MessageType.QOS)
        if m:
            if m.type == Gst.MessageType.EOS: eos = True
            elif m.type == Gst.MessageType.ERROR:
                e, d = m.parse_error(); result['errors'].append({'message': str(e), 'debug': d})
            elif m.type == Gst.MessageType.WARNING:
                w, d = m.parse_warning(); result['warnings'].append({'message': str(w), 'debug': d})
            else: result['qos'] += 1
        if elapsed - last >= 5:
            emit({'progress': {'elapsed': round(elapsed, 2), 'frames': frames[0],
                               'rendered': fps.get_property('frames-rendered'),
                               'active': sel.get_property('active-pad').get_name(),
                               'audio_buffers': result['audio_sink']['buffers']}}); last = elapsed
    usage = resource.getrusage(resource.RUSAGE_SELF)
    result.update(elapsed_seconds=round(elapsed, 3), process_cpu_percent=round(100 * (usage.ru_utime + usage.ru_stime - cpu_start.ru_utime - cpu_start.ru_stime) / max(elapsed, 0.001), 2), eos=eos, decoded_frames=frames[0], rendered_frames=fps.get_property('frames-rendered'),
                  dropped_frames=fps.get_property('frames-dropped'))
    for sw in result['switches']:
        sw['reached_audio_sink'] = result['audio_sink']['buffers'] > sw['sink_buffers_before']
    target_stream_ids = {sid for _, track in schedule
                         for sid in result['selector_inputs'][str(track)]['stream_ids']}
    result['target_stream_ids'] = sorted(target_stream_ids)
    result['all_targets_at_sink'] = (len(result['switches']) == 3 and
                                     len(target_stream_ids) == 4 and
                                     target_stream_ids.issubset(result['audio_sink']['stream_ids']) and
                                     all(result['audio_sink']['by_stream'].get(sid, {}).get('buffers', 0) > 0 for sid in target_stream_ids))
except BaseException as e:
    result['errors'].append({'message': str(e), 'type': type(e).__name__})
finally:
    pipe.set_state(Gst.State.NULL); pipe.get_state(5 * Gst.SECOND)
emit({'summary': result})
raise SystemExit(0 if not result['errors'] and result.get('eos') and
                 result.get('decoded_frames') == a.expected_frames and
                 result.get('rendered_frames') == a.expected_frames and
                 result.get('all_targets_at_sink') else 1)
