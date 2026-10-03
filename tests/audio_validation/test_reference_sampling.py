"""Reference text and timestamps come only from a bounded original-English track."""

import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('reference_sampling', ROOT / 'bazarr/subtitles/reference_sampling.py')
sampling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sampling)
STARTS = [100, 300, 500, 700, 900]


def stream(index=17, language='eng', title='English Full', codec='subrip', **flags):
    return dict(index=index, codec_type='subtitle', codec_name=codec,
                tags={'language': language, 'title': title}, disposition=flags)


def hexdump(text):
    payload = text.encode('utf-8')
    lines = []
    for offset in range(0, len(payload), 16):
        chunk = payload[offset:offset + 16]
        pairs = ' '.join(chunk[index:index + 2].hex() for index in range(0, len(chunk), 2))
        ascii_preview = ''.join(chr(byte) if 32 <= byte < 127 else '.' for byte in chunk)
        lines.append(f'{offset:08x}: {pairs:<39}  {ascii_preview}')
    return '\n' + '\n'.join(lines) + '\n'


def packet(start, text='Yes.', duration=2, index=17):
    return dict(stream_index=index, pts_time=str(start), duration_time=str(duration), data=hexdump(text))


@pytest.fixture
def media(tmp_path):
    path = tmp_path / 'The.Game.1997.mkv'
    path.write_bytes(b'video fixture')
    return path


def run_fixture(monkeypatch, origin=0, streams=None, samples=None):
    calls = []
    streams = [stream()] if streams is None else streams
    samples = [[packet(start + origin + 1)] for start in STARTS] if samples is None else samples
    sample_index = 0

    def run(command, timeout):
        nonlocal sample_index
        calls.append((command, timeout))
        if '-show_packets' not in command:
            return {'streams': streams, 'format': {'start_time': str(origin)}}
        result = {'packets': samples[sample_index]}
        sample_index += 1
        return result

    monkeypatch.setattr(sampling, '_run', run)
    return calls


def extract(media, tmp_path, starts=STARTS, **kwargs):
    return sampling.extract_reference_cues(media, tmp_path / 'cache', str, {'language': 'eng'},
                                           starts, 1200, **kwargs)


@pytest.mark.parametrize('text', ['Yes.', 'A', 'All I need is a little help.', 'é <i>English</i> &amp; 中文'])
def test_standard_hex_dump_decodes_short_odd_length_and_multiline_packets(text):
    expected = 'é English & 中文' if '<i>' in text else text
    assert sampling._decode_packet_text(hexdump(text)) == expected


def test_original_full_track_wins_and_five_reads_have_absolute_ends_and_budgets(monkeypatch, media, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    streams = [stream(18, title='English SDH', hearing_impaired=1),
               stream(20, title='English Commentary'), stream(16, language='rus'),
               stream(19, title='English', hearing_impaired=1), stream()]
    calls = run_fixture(monkeypatch, streams=streams)
    stages = []
    cues, metadata = extract(media, tmp_path, deadline=sampling.time.monotonic() + 140,
                             progress=lambda stage, details: stages.append((stage, details)))
    assert len(cues) == 5
    assert {cue['window'] for cue in cues} == {1, 2, 3, 4, 5}
    assert metadata['stream_index'] == 17 and metadata['reason'] == 'reference_available'
    assert metadata['window_seconds'] == 60 and not metadata['cache_hit']
    assert len(calls) == 6
    for (command, timeout), start in zip(calls[1:], STARTS):
        assert command[command.index('-select_streams') + 1] == '17'
        interval = command[command.index('-read_intervals') + 1]
        begin, end = interval.split('%')
        assert float(begin) == start - sampling.MAX_CUE_SECONDS
        assert float(end) == start + 60
        assert not end.startswith('+')
        assert '-show_data' in command
        assert 0 < timeout <= 25
    assert calls[0][1] <= 15
    assert [details['index'] for stage, details in stages if stage == 'reference_sample'] == [1, 2, 3, 4, 5]
    assert stages[0] == ('reference_cache', {'cache_hit': False})
    assert 'track=17' in caplog.text and 'cues=' in caplog.text and 'elapsed=' in caplog.text
    assert 'Yes.' not in caplog.text


def test_pts_are_video_relative_and_real_boundaries_filter_imprecise_seek(monkeypatch, media, tmp_path):
    origin = 7.5
    packets = [packet(origin + 95, 'Crossing left edge', 10), packet(origin + 101, 'Yes.'),
               packet(origin + 75, 'Too early'), packet(origin + 160, 'At right edge'),
               packet(origin + 100, 'Other stream', index=18), packet(origin + 102, 'Too long', 21),
               packet(origin + 102, 'No positive duration', 0)]
    packets += [dict(packet(origin + 102), pts_time='nan'), dict(packet(origin + 102), duration_time='N/A')]
    calls = run_fixture(monkeypatch, origin=origin, samples=[packets])
    cues, metadata = extract(media, tmp_path, starts=[100])
    assert [(cue['start'], cue['end'], cue['text']) for cue in cues] == [
        (95, 105, 'Crossing left edge'), (101, 103, 'Yes.')]
    assert metadata['origin'] == origin
    assert calls[1][0][calls[1][0].index('-read_intervals') + 1] == '87.500000%167.500000'


@pytest.mark.parametrize('bad_stream', [
    stream(title='English Full commentary'), stream(comment=1), stream(title='English Forced'), stream(forced=1),
    stream(language='rus'), stream(codec='ass'), stream(codec='hdmv_pgs_subtitle'),
])
def test_ineligible_tracks_are_never_sampled(monkeypatch, media, tmp_path, bad_stream):
    calls = run_fixture(monkeypatch, streams=[bad_stream])
    assert extract(media, tmp_path) == ([], {'reason': 'reference_unavailable'})
    assert len(calls) == 1


def test_non_english_original_audio_does_not_probe_or_use_english_subtitles(monkeypatch, media, tmp_path):
    run = Mock(side_effect=AssertionError('No probe for a non-English original'))
    monkeypatch.setattr(sampling, '_run', run)
    assert sampling.extract_reference_cues(media, tmp_path, str, {'language': 'rus'}, STARTS, 1200) == (
        [], {'reason': 'reference_unavailable'})
    run.assert_not_called()


def test_default_sdh_cannot_beat_non_sdh_and_uppercase_tags_are_supported(monkeypatch, media, tmp_path):
    original = stream(index=17, title='English Full')
    original['tags'] = {'LANGUAGE': 'ENG', 'TITLE': 'English Full'}
    run_fixture(monkeypatch, streams=[stream(18, title='English SDH', default=1), original])
    assert extract(media, tmp_path)[1]['stream_index'] == 17


def test_cache_hit_skips_packet_reads_and_atomic_write_leaves_no_tempfile(monkeypatch, media, tmp_path):
    calls = run_fixture(monkeypatch)
    expected, metadata = extract(media, tmp_path)
    assert not metadata['cache_hit']
    cache, = (tmp_path / 'cache').glob('reference-*.json')
    assert cache.stat().st_size <= sampling.MAX_CACHE_BYTES
    assert not list((tmp_path / 'cache').glob('*.tmp'))
    calls.clear()
    cues, metadata = extract(media, tmp_path)
    assert cues == expected and metadata['cache_hit']
    assert len(calls) == 1 and '-show_packets' not in calls[0][0]


@pytest.mark.parametrize('corruption', ['json', 'array', 'empty_cues', 'excessive_file', 'invalid_timestamp', 'invalid_window',
                                      'excessive_cues', 'excessive_text', 'duplicate', 'wrong_identity'])
def test_corrupted_cache_is_resampled(monkeypatch, media, tmp_path, corruption):
    calls = run_fixture(monkeypatch)
    expected, _ = extract(media, tmp_path)
    cache, = (tmp_path / 'cache').glob('reference-*.json')
    saved = json.loads(cache.read_text(encoding='utf-8'))
    if corruption == 'json':
        cache.write_text('{broken', encoding='utf-8')
    elif corruption == 'array':
        cache.write_text('[]', encoding='utf-8')
    elif corruption == 'excessive_file':
        cache.write_bytes(b'x' * (sampling.MAX_CACHE_BYTES + 1))
    else:
        if corruption == 'invalid_timestamp':
            saved['cues'][0]['start'] = float('nan')
        elif corruption == 'invalid_window':
            saved['cues'][0]['window'] = 6
        elif corruption == 'excessive_cues':
            saved['cues'] = saved['cues'] * 241
        elif corruption == 'empty_cues':
            saved['cues'] = []
        elif corruption == 'excessive_text':
            saved['cues'][0]['text'] = 'x' * (sampling.MAX_TEXT_BYTES + 1)
        elif corruption == 'duplicate':
            saved['cues'].append(saved['cues'][0])
        else:
            saved['identity']['stream_index'] = 18
        cache.write_text(json.dumps(saved), encoding='utf-8')
    calls = run_fixture(monkeypatch)
    cues, metadata = extract(media, tmp_path)
    assert cues == expected and not metadata['cache_hit']
    assert len(calls) == 6


@pytest.mark.parametrize('changed', ['path', 'size', 'mtime', 'stream', 'origin', 'windows'])
def test_cache_identity_changes_require_new_samples(monkeypatch, media, tmp_path, changed):
    run_fixture(monkeypatch)
    extract(media, tmp_path)
    starts, origin, streams = STARTS, 0, [stream()]
    if changed == 'path':
        other = tmp_path / 'Other.mkv'
        other.write_bytes(media.read_bytes())
        media = other
    elif changed == 'size':
        media.write_bytes(media.read_bytes() + b'more')
    elif changed == 'mtime':
        stat = media.stat()
        os.utime(media, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    elif changed == 'stream':
        streams = [stream(index=18)]
    elif changed == 'origin':
        origin = 10
    else:
        starts = [start + 1 for start in STARTS]
    samples = [[packet(start + origin + 1, index=streams[0]['index'])] for start in starts]
    calls = run_fixture(monkeypatch, origin=origin, streams=streams, samples=samples)
    assert not extract(media, tmp_path, starts=starts)[1]['cache_hit']
    assert len(calls) == 6


def test_overlapping_windows_deduplicate_short_dialogue_without_size_filter(monkeypatch, media, tmp_path):
    samples = [[packet(111, 'A')], [packet(111, 'A'), packet(115, 'No.')]]
    run_fixture(monkeypatch, samples=samples)
    cues, _ = extract(media, tmp_path, starts=[100, 110])
    assert [(cue['text'], cue['window']) for cue in cues] == [('A', 1), ('No.', 2)]


def test_expired_budget_and_cache_lock_never_start_packet_read(monkeypatch, media, tmp_path):
    calls = run_fixture(monkeypatch)
    with pytest.raises(TimeoutError):
        extract(media, tmp_path, deadline=sampling.time.monotonic() - 1)
    assert calls == []

    locked = SimpleNamespace(acquire=Mock(return_value=False), release=Mock())
    monkeypatch.setattr(sampling, 'LOCKS', [locked] * 16)
    with pytest.raises(TimeoutError, match='cache'):
        extract(media, tmp_path, deadline=sampling.time.monotonic() + 10)
    assert len(calls) == 1
    assert 0 < locked.acquire.call_args.kwargs['timeout'] <= 10
    locked.release.assert_not_called()


def test_process_timeout_is_propagated_without_caching_partial_results(monkeypatch, media, tmp_path):
    calls = run_fixture(monkeypatch)
    original = sampling._run

    def run(command, timeout):
        if '-show_packets' in command:
            raise subprocess.TimeoutExpired('ffprobe', timeout)
        return original(command, timeout)

    monkeypatch.setattr(sampling, '_run', run)
    with pytest.raises(subprocess.TimeoutExpired):
        extract(media, tmp_path)
    assert not list((tmp_path / 'cache').glob('*'))


def test_empty_windows_return_unavailable_and_leave_no_cache(monkeypatch, media, tmp_path):
    calls = run_fixture(monkeypatch, samples=[[]] * 5)
    cues, metadata = extract(media, tmp_path)
    assert cues == [] and metadata['reason'] == 'reference_unavailable'
    assert len(calls) == 6
    assert not list((tmp_path / 'cache').glob('*'))


def test_cue_limit_and_text_limit_raise_instead_of_returning_unbounded_evidence(monkeypatch, media, tmp_path):
    samples = [[packet(101 + index / 100, str(index), 1) for index in range(sampling.MAX_CUES + 1)]]
    run_fixture(monkeypatch, samples=samples)
    with pytest.raises(ValueError, match='Too many'):
        extract(media, tmp_path, starts=[100])
    assert not list((tmp_path / 'cache').glob('*'))
    with pytest.raises(ValueError, match='text'):
        sampling._decode_packet_text(hexdump('x' * (sampling.MAX_TEXT_BYTES + 1)))


def test_real_runner_uses_no_shell_and_honors_timeout(monkeypatch):
    run = Mock(return_value=SimpleNamespace(stdout=b'{"packets":[]}'))
    monkeypatch.setattr(sampling.subprocess, 'run', run)
    assert sampling._run(['ffprobe', '-show_packets', 'movie.mkv'], 7.5) == {'packets': []}
    assert run.call_args.kwargs['timeout'] == 7.5
    assert run.call_args.kwargs['check'] and run.call_args.kwargs['capture_output']
    assert 'shell' not in run.call_args.kwargs


def test_numpy_window_plan_is_normalized_to_json_safe_floats(monkeypatch, media, tmp_path):
    run_fixture(monkeypatch)
    cues, metadata = extract(media, tmp_path, starts=np.asarray(STARTS, dtype=np.float64))
    assert len(cues) == 5
    assert metadata['window_starts'] == STARTS
    assert all(type(start) is float for start in metadata['window_starts'])
    cache, = (tmp_path / 'cache').glob('reference-*.json')
    assert json.loads(cache.read_text())['identity']['windows'][0] == [100.0, 160.0]


def test_later_windows_receive_only_remaining_global_budget(monkeypatch, media, tmp_path):
    clock, calls = [0], []
    monkeypatch.setattr(sampling.time, 'monotonic', lambda: clock[0])

    def run(command, timeout):
        calls.append((command, timeout))
        if '-show_packets' not in command:
            clock[0] += 5
            return {'streams': [stream()], 'format': {'start_time': 0}}
        index = len(calls) - 2
        clock[0] += 10
        return {'packets': [packet(STARTS[index] + 1)]}

    monkeypatch.setattr(sampling, '_run', run)
    extract(media, tmp_path, deadline=60)
    assert [timeout for _, timeout in calls] == [15, 25, 25, 25, 25, 15]


def test_total_cache_size_is_bounded_even_when_individual_cues_are_valid(monkeypatch, media, tmp_path):
    run_fixture(monkeypatch, samples=[[
        packet(101 + index / 100, 'x' * sampling.MAX_TEXT_BYTES, 1) for index in range(300)
    ]])
    with pytest.raises(ValueError, match='cache exceeds'):
        extract(media, tmp_path, starts=[100])
    assert not list((tmp_path / 'cache').glob('*'))


def test_atomic_replace_failure_cleans_tempfile(monkeypatch, media, tmp_path):
    run_fixture(monkeypatch)
    monkeypatch.setattr(sampling.os, 'replace', Mock(side_effect=OSError('cannot replace cache')))
    with pytest.raises(OSError):
        extract(media, tmp_path)
    assert not list((tmp_path / 'cache').glob('*'))
