#!/usr/bin/env python3
"""Probe hardware HEVC video plus legacy playbin current-audio switching."""
import argparse
import json
from pathlib import Path
import resource
import time

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst


def parse_schedule(value):
    entries = []
    try:
        for item in value.split(','):
            when, track = item.split(':', 1)
            entries.append((float(when), int(track)))
    except ValueError as error:
        raise argparse.ArgumentTypeError('expected WHEN:TRACK[,WHEN:TRACK...]') from error
    if not entries or any(when < 0 or track < 0 for when, track in entries):
        raise argparse.ArgumentTypeError('schedule times and track numbers must be non-negative')
    if entries != sorted(entries):
        raise argparse.ArgumentTypeError('schedule entries must be ordered by time')
    return entries


parser = argparse.ArgumentParser()
parser.add_argument('video', help='Raw 4K HEVC video-only Matroska file')
parser.add_argument('audio', help='Matroska file containing the original audio tracks')
selection = parser.add_mutually_exclusive_group()
selection.add_argument('--schedule', type=parse_schedule, default=parse_schedule('0:0,10:1,20:5,30:2'))
selection.add_argument('--audio-track', type=int, help='Play one audio track for the whole run')
parser.add_argument('--duration', type=float, default=45.5)
parser.add_argument('--timeout', type=float, default=65.0)
parser.add_argument('--audio-offset-ns', type=int, default=42_000_000)
parser.add_argument('--expected-frames', type=int, default=1088)
args = parser.parse_args()
if args.audio_track is not None and args.audio_track < 0:
    parser.error('--audio-track must be non-negative')
if args.duration <= 0 or args.timeout <= 0 or args.timeout < args.duration:
    parser.error('--duration must be positive and --timeout must be at least --duration')

Gst.init(None)
video = audio = None
result = {'errors': [], 'warnings': [], 'eos': [], 'switches': [], 'selections': [],
          'audio_streams': [], 'audio_decoders': [], 'decoder_outputs': {},
          'audio_sink_input': {'buffers': 0, 'first_pts': None, 'last_pts': None,
                               'stream_id': None, 'by_stream': {}},
          'qos': {'count': 0, 'last': None}}
frames = [0]


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def message_record(message):
    source = message.src.get_name() if message.src is not None else None
    if message.type == Gst.MessageType.ERROR:
        error, debug = message.parse_error()
        result['errors'].append({'pipeline': source, 'message': str(error), 'debug': debug})
    elif message.type == Gst.MessageType.WARNING:
        warning, debug = message.parse_warning()
        result['warnings'].append({'pipeline': source, 'message': str(warning), 'debug': debug})


def tag_string(tags, name):
    if tags is None:
        return None
    found, value = tags.get_string(name)
    return value if found else None


def audio_stream_records(collection):
    records = []
    for position in range(collection.get_size()):
        stream = collection.get_stream(position)
        if not stream.get_stream_type() & Gst.StreamType.AUDIO:
            continue
        tags = stream.get_tags()
        caps = stream.get_caps()
        records.append({'index': len(records), 'collection_index': position,
                        'id': stream.get_stream_id(),
                        'caps': caps.to_string() if caps else None,
                        'codec': tag_string(tags, Gst.TAG_AUDIO_CODEC),
                        'language': tag_string(tags, Gst.TAG_LANGUAGE_CODE)})
    return records


def wait_paused(pipeline, label):
    if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError(f'{label} pipeline failed to enter PAUSED')
    status, current, pending = pipeline.get_state(int(args.timeout * Gst.SECOND))
    if status != Gst.StateChangeReturn.SUCCESS or current != Gst.State.PAUSED:
        raise RuntimeError(f'{label} preroll failed: result={status.value_nick}, '
                           f'state={current.value_nick}, pending={pending.value_nick}')


try:
    video_path = Path(args.video).resolve()
    audio_path = Path(args.audio).resolve()
    video_description = (
        f'filesrc location={json.dumps(str(video_path))} ! matroskademux ! h265parse ! '
        'capssetter caps="video/x-h265,colorimetry=bt2020" ! '
        'v4l2h265dec name=decoder capture-io-mode=dmabuf ! '
        'fpsdisplaysink name=fps text-overlay=false '
        'video-sink="waylandsink display=m17s-media fullscreen=true sync=true" sync=true'
    )
    video = Gst.parse_launch(video_description)
    decoder = video.get_by_name('decoder')
    fps = video.get_by_name('fps')

    def count_frame(_pad, _info):
        frames[0] += 1
        return Gst.PadProbeReturn.OK

    decoder.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, count_frame)

    audio = Gst.ElementFactory.make('playbin', 'audio-playbin')
    if audio is None:
        raise RuntimeError('could not create playbin')
    audio.set_property('uri', audio_path.as_uri())
    audio.set_property('flags', 2)  # GST_PLAY_FLAG_AUDIO only
    audio_sink = Gst.parse_bin_from_description(
        'audioconvert ! audioresample ! '
        'audio/x-raw,format=S16LE,rate=48000,channels=2 ! '
        f'alsasink name=audio-output device=hw:0,0 sync=true ts-offset={args.audio_offset_ns}',
        True)
    audio.set_property('audio-sink', audio_sink)
    audio_output = audio_sink.get_by_name('audio-output')
    audio_output_pad = audio_output.get_static_pad('sink')

    def audio_output_probe(_pad, info):
        stats = result['audio_sink_input']
        if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.STREAM_START:
                stream_id = event.parse_stream_start()
                stats['stream_id'] = stream_id
                running_time = None
                if 'base_time' in globals():
                    running_time = max(0, int(clock.get_time() - base_time))
                stream_stats = stats['by_stream'].setdefault(
                    stream_id, {'buffers': 0, 'first_pts': None, 'last_pts': None,
                                'stream_start_running_time_ns': []})
                stream_stats['stream_start_running_time_ns'].append(running_time)
        if info.type & Gst.PadProbeType.BUFFER:
            buffer = info.get_buffer()
            stats['buffers'] += 1
            pts = int(buffer.pts) if buffer.pts != Gst.CLOCK_TIME_NONE else None
            if stats['first_pts'] is None:
                stats['first_pts'] = pts
            stats['last_pts'] = pts
            stream_id = stats['stream_id'] or '<unknown>'
            stream_stats = stats['by_stream'].setdefault(
                stream_id, {'buffers': 0, 'first_pts': None, 'last_pts': None,
                            'stream_start_running_time_ns': []})
            stream_stats['buffers'] += 1
            if stream_stats['first_pts'] is None:
                stream_stats['first_pts'] = pts
            stream_stats['last_pts'] = pts
        return Gst.PadProbeReturn.OK

    audio_output_pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM,
                               audio_output_probe)

    def element_added(_bin, _sub_bin, element):
        factory = element.get_factory()
        if factory is None:
            return
        klass = factory.get_metadata(Gst.ELEMENT_METADATA_KLASS) or ''
        if 'Decoder' in klass and 'Audio' in klass:
            record = {'name': element.get_name(), 'factory': factory.get_name(), 'klass': klass}
            if record not in result['audio_decoders']:
                result['audio_decoders'].append(record)
                emit({'audio_decoder_added': record})
                pad = element.get_static_pad('src')
                if pad is not None:
                    output = {'factory': record['factory'], 'buffers': 0, 'first_pts': None,
                              'last_pts': None, 'stream_id': None}
                    result['decoder_outputs'][record['name']] = output

                    def decoded_event(_pad, info):
                        event = info.get_event()
                        if event is not None and event.type == Gst.EventType.CAPS:
                            entry = {'name': record['name'], 'factory': record['factory'],
                                     'caps': event.parse_caps().to_string()}
                            result.setdefault('audio_decoded_caps', []).append(entry)
                            emit({'audio_decoded_caps': entry})
                        elif event is not None and event.type == Gst.EventType.STREAM_START:
                            output['stream_id'] = event.parse_stream_start()
                        return Gst.PadProbeReturn.OK

                    def decoded_buffer(_pad, info):
                        buffer = info.get_buffer()
                        if buffer is not None:
                            output['buffers'] += 1
                            pts = int(buffer.pts) if buffer.pts != Gst.CLOCK_TIME_NONE else None
                            if output['first_pts'] is None:
                                output['first_pts'] = pts
                            output['last_pts'] = pts
                        return Gst.PadProbeReturn.OK

                    pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, decoded_event)
                    pad.add_probe(Gst.PadProbeType.BUFFER, decoded_buffer)

    audio.connect('deep-element-added', element_added)
    schedule = [(0.0, args.audio_track)] if args.audio_track is not None else args.schedule
    selected_indices = set()

    def current_audio_changed(playbin, _pspec):
        index = playbin.get_property('current-audio')
        selected_indices.add(index)
        record = {'elapsed_seconds': None, 'selected_index': index}
        result['selections'].append(record)
        emit({'current_audio_changed': record})

    audio.connect('notify::current-audio', current_audio_changed)

    clock = Gst.SystemClock.obtain()
    for pipeline in (video, audio):
        pipeline.use_clock(clock)
        pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
    wait_paused(video, 'video')
    wait_paused(audio, 'audio')

    n_audio = audio.get_property('n-audio')
    if n_audio <= 0:
        raise RuntimeError('playbin reported no audio tracks during preroll')
    initial_current_audio = audio.get_property('current-audio')
    selected_indices.add(initial_current_audio)
    result['selections'].append({'elapsed_seconds': 0.0, 'phase': 'preroll-current',
                                 'selected_index': initial_current_audio})
    invalid = [track for _, track in schedule if track >= n_audio]
    if invalid:
        raise RuntimeError(f'scheduled audio track outside n-audio={n_audio}: {invalid}')

    required_indices = {track for _, track in schedule}
    current_requested = schedule[0][1]

    def select_track(track, elapsed, allow_preroll_match=False):
        audio.set_property('current-audio', track)
        actual = audio.get_property('current-audio')
        record = {'elapsed_seconds': round(elapsed, 3), 'requested_index': track,
                  'actual_index': actual, 'selection_matched': actual == track}
        result['switches'].append(record)
        emit({'audio_switch_requested': record})

    select_track(current_requested, 0.0, allow_preroll_match=True)
    base_time = clock.get_time() + 200 * Gst.MSECOND
    for pipeline in (video, audio):
        pipeline.set_base_time(base_time)
    for label, pipeline in (('video', video), ('audio', audio)):
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f'{label} pipeline failed to enter PLAYING')

    wall_start = time.monotonic()
    cpu_start = resource.getrusage(resource.RUSAGE_SELF)
    deadline = wall_start + args.timeout
    next_switch = 1
    last_progress = wall_start
    buses = (('video', video.get_bus()), ('audio', audio.get_bus()))
    eos_seen = set()
    while time.monotonic() < deadline:
        now = time.monotonic()
        elapsed = max(0.0, (clock.get_time() - base_time) / Gst.SECOND)
        while next_switch < len(schedule) and elapsed >= schedule[next_switch][0]:
            current_requested = schedule[next_switch][1]
            select_track(current_requested, elapsed)
            next_switch += 1
        for label, bus in buses:
            while True:
                message = bus.pop_filtered(Gst.MessageType.EOS | Gst.MessageType.ERROR |
                                           Gst.MessageType.WARNING | Gst.MessageType.QOS)
                if message is None:
                    break
                if message.type == Gst.MessageType.EOS:
                    eos_seen.add(label)
                    if label not in result['eos']:
                        result['eos'].append(label)
                elif message.type == Gst.MessageType.QOS:
                    result['qos']['count'] += 1
                    result['qos']['last'] = {
                        'pipeline': label,
                        'source': message.src.get_name() if message.src else None,
                        'timestamp': int(message.timestamp),
                        'seqnum': message.get_seqnum(),
                    }
                else:
                    message_record(message)
                emit({'bus_message': {'pipeline': label, 'type': str(message.type)}})
        if result['errors'] or eos_seen == {'video', 'audio'}:
            break
        if now - last_progress >= 5:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            cpu = 100 * (usage.ru_utime + usage.ru_stime - cpu_start.ru_utime - cpu_start.ru_stime) / max(elapsed, 1e-9)
            emit({'progress': {'elapsed_seconds': round(elapsed, 3), 'decoded_frames': frames[0],
                               'rendered_frames': fps.get_property('frames-rendered'),
                               'dropped_frames': fps.get_property('frames-dropped'),
                               'n_audio': n_audio,
                               'requested_audio': current_requested,
                               'current_audio': audio.get_property('current-audio'),
                               'process_cpu_percent': round(cpu, 2)}})
            last_progress = now
        time.sleep(0.05)

    elapsed = max(0.0, (clock.get_time() - base_time) / Gst.SECOND)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    result.update(elapsed_seconds=round(elapsed, 3), decoded_frames=frames[0],
                  rendered_frames=fps.get_property('frames-rendered'),
                  dropped_frames=fps.get_property('frames-dropped'),
                  n_audio=n_audio, requested_audio=current_requested,
                  current_audio=audio.get_property('current-audio'),
                  process_cpu_percent=round(100 * (usage.ru_utime + usage.ru_stime -
                                                    cpu_start.ru_utime - cpu_start.ru_stime) /
                                            max(elapsed, 1e-9), 2),
                  expected_frames=args.expected_frames,
                  decoded_count_matches=frames[0] == args.expected_frames,
                  rendered_count_matches=fps.get_property('frames-rendered') == args.expected_frames,
                  duration_reached=elapsed >= args.duration,
                  both_eos=eos_seen == {'video', 'audio'},
                  required_audio_indices=sorted(required_indices),
                  selected_audio_indices=sorted(selected_indices),
                  all_target_indices_selected=required_indices.issubset(selected_indices),
                  all_switch_requests_matched=all(switch['selection_matched']
                                                  for switch in result['switches']),
                  timed_out=eos_seen != {'video', 'audio'} and time.monotonic() >= deadline)
except KeyboardInterrupt:
    result['errors'].append({'message': 'interrupted'})
except BaseException as error:
    result['errors'].append({'message': str(error), 'type': type(error).__name__})
finally:
    for pipeline in (audio, video):
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
                pipeline.get_state(5 * Gst.SECOND)
            except BaseException as error:
                result['errors'].append({'message': f'pipeline cleanup failed: {error}',
                                         'type': type(error).__name__})

emit({'summary': result})
raise SystemExit(0 if (not result['errors'] and result.get('both_eos', False) and
                       result.get('decoded_count_matches', False) and
                       result.get('rendered_count_matches', False) and
                       result.get('all_target_indices_selected', False)) else 1)
