"""Run with pytest --confcutdir=tests/audio_validation tests/audio_validation."""

import ast
import importlib.util
import logging
import operator
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'libs'))
spec = importlib.util.spec_from_file_location('audio_validation', ROOT / 'bazarr/subtitles/audio_validation.py')
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)


@pytest.fixture
def media():
    rng = np.random.default_rng(27)
    intervals, now = [], 0
    while now < 2400:
        now += rng.uniform(0.3, 3)
        end = now + rng.uniform(0.5, 5)
        intervals.append((now, end))
        now = end
    intervals = np.array(intervals)
    starts = np.round((2400 - timing.WINDOW_SECONDS) * np.array(timing.FRACTIONS), 1)
    activity = []
    for start in starts:
        times = start + np.arange(timing.WINDOW_SECONDS * timing.HZ) / timing.HZ
        sample = np.any((times[:, None] >= intervals[:, 0]) & (times[:, None] < intervals[:, 1]), axis=1)
        activity.append(sample.astype(float))
    return 2400, starts, np.array(activity), intervals


@pytest.mark.parametrize('offset,rate', [(0, 1), (35, 1), (-45, 1), (10, 25 / 24)])
def test_accepts_offset_and_speed_variations(media, offset, rate):
    duration, starts, audio, intervals = media
    result = timing.evaluate_activity(duration, starts, audio, (intervals - offset) / rate, search=True)
    assert result['accepted']
    assert abs(result['offset_seconds'] - offset) < 0.5
    assert result['rate'] == pytest.approx(rate)


@pytest.mark.parametrize('offset,rate', [(35, 1), (-45, 1), (10, 25 / 24)])
def test_actual_timing_rejects_unapplied_corrections(media, offset, rate):
    duration, starts, audio, intervals = media
    assert not timing.evaluate_activity(duration, starts, audio, (intervals - offset) / rate)['accepted']


@pytest.mark.parametrize('initial_pass,aligned_pass', [(True, True), (False, True), (False, False)])
def test_validate_then_sync_then_validate(monkeypatch, initial_pass, aligned_pass):
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=True, audio_stream=0))))
    monkeypatch.setitem(sys.modules, 'app.get_args', SimpleNamespace(args=SimpleNamespace(config_dir='unused')))
    monkeypatch.setitem(sys.modules, 'utilities.binaries', SimpleNamespace(get_binary=str))
    monkeypatch.setattr(timing, 'parse_intervals', lambda text: text)
    monkeypatch.setattr(timing, 'extract_activity', lambda *args: (2400, [], []))
    monkeypatch.setattr(timing, 'embedded_reference_candidates', lambda *args: [])
    calls = []
    def evaluate(*args):
        calls.append(args[-1])
        return {'accepted': initial_pass if len(calls) == 1 else aligned_pass, 'reason': 'timing_not_confirmed'}
    monkeypatch.setattr(timing, 'evaluate_activity', evaluate)
    def align(*args):
        assert not initial_pass
        return 'corrected'
    monkeypatch.setattr(timing, 'align_subtitle', align)
    subtitle = SimpleNamespace(text='original', content=b'original', encoding='gbk', _guessed_encoding='gbk')
    assert timing.validate_download(SimpleNamespace(original_path='video'), subtitle) == (initial_pass or aligned_pass)
    assert calls == (['original'] if initial_pass else ['original', 'corrected'])
    assert subtitle.content == (b'corrected' if not initial_pass and aligned_pass else b'original')
    assert subtitle.audio_timing_validated == (initial_pass or aligned_pass)
    if not initial_pass and aligned_pass:
        assert subtitle.encoding == subtitle._guessed_encoding == 'utf-8'


def test_embedded_reference_is_used_before_audio_alignment(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=True, audio_stream=0))))
    monkeypatch.setitem(sys.modules, 'app.get_args', SimpleNamespace(args=SimpleNamespace(config_dir='config')))
    monkeypatch.setitem(sys.modules, 'utilities.binaries', SimpleNamespace(get_binary=str))
    monkeypatch.setattr(timing, 'parse_intervals', lambda text: text)
    monkeypatch.setattr(timing, 'extract_activity', lambda *args: (2400, [], []))
    monkeypatch.setattr(timing, 'evaluate_activity', lambda *args: {
        'accepted': False, 'reason': 'timing_not_confirmed'})
    monkeypatch.setattr(timing, 'embedded_reference_candidates',
                        lambda *args: [('embedded.en.srt', 'reference intervals', 2)])
    calls = []

    def align(*args):
        calls.append(args[-1] if len(args) == 5 else None)
        return 'embedded corrected'

    monkeypatch.setattr(timing, 'align_subtitle', align)
    monkeypatch.setattr(timing, 'evaluate_reference_alignment', lambda *args: {
        'accepted': True, 'reason': 'timing_match'})
    subtitle = SimpleNamespace(text='original', content=b'original', language=SimpleNamespace(alpha3='zho'))
    assert timing.validate_download(SimpleNamespace(original_path='video'), subtitle)
    assert calls == ['embedded.en.srt']
    assert subtitle.content == b'embedded corrected'


def test_failed_embedded_alignment_falls_back_to_audio(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=True, audio_stream=0))))
    monkeypatch.setitem(sys.modules, 'app.get_args', SimpleNamespace(args=SimpleNamespace(config_dir='config')))
    monkeypatch.setitem(sys.modules, 'utilities.binaries', SimpleNamespace(get_binary=str))
    monkeypatch.setattr(timing, 'parse_intervals', lambda text: text)
    monkeypatch.setattr(timing, 'extract_activity', lambda *args: (2400, [], []))
    results = iter(({'accepted': False, 'reason': 'timing_not_confirmed'},
                    {'accepted': True, 'reason': 'timing_match'}))
    monkeypatch.setattr(timing, 'evaluate_activity', lambda *args: next(results))
    monkeypatch.setattr(timing, 'embedded_reference_candidates',
                        lambda *args: [('broken.en.srt', 'reference intervals', 2)])

    def align(*args):
        if len(args) == 5:
            raise ValueError('bad embedded track')
        return 'audio corrected'

    monkeypatch.setattr(timing, 'align_subtitle', align)
    subtitle = SimpleNamespace(text='original', content=b'original', language=SimpleNamespace(alpha3='zho'))
    assert timing.validate_download(SimpleNamespace(original_path='video'), subtitle)
    assert subtitle.content == b'audio corrected'


def test_reference_alignment_distinguishes_corrected_timing(media):
    duration, starts, _, intervals = media
    accepted = timing.evaluate_reference_alignment(duration, starts, intervals, intervals)
    rejected = timing.evaluate_reference_alignment(duration, starts, intervals, intervals - 35)
    assert accepted['accepted']
    assert not rejected['accepted']


def test_embedded_reference_selection_prefers_language_then_english_and_limits_tracks(tmp_path, monkeypatch):
    import json
    streams = [
        {'index': 8, 'codec_name': 'hdmv_pgs_subtitle', 'tags': {'language': 'zho'}},
        {'index': 9, 'codec_name': 'subrip', 'tags': {'language': 'zho', 'title': 'Forced'}},
        {'index': 6, 'codec_name': 'subrip', 'tags': {'language': 'fra'}},
        {'index': 7, 'codec_name': 'subrip', 'tags': {'language': 'deu'}},
        {'index': 4, 'codec_name': 'subrip', 'tags': {'language': 'eng'}},
        {'index': 5, 'codec_name': 'ass', 'tags': {'language': 'chi'}},
    ]
    monkeypatch.setattr(timing, '_run', lambda command: json.dumps({'streams': streams}).encode())
    extracted = []

    def extract(video, track_id, cache, binary):
        extracted.append(track_id)
        path = tmp_path / f'{track_id}.srt'
        path.write_text('subtitle', encoding='utf-8')
        return str(path)

    monkeypatch.setitem(sys.modules, 'subtitles', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'subtitles.embedded_translation', SimpleNamespace(
        extract_embedded_subtitle=extract))
    monkeypatch.setattr(timing, 'parse_intervals', lambda text: np.array([(0.0, 700.0)]))
    references = timing.embedded_reference_candidates('video.mkv', tmp_path, str, 'zho')
    assert extracted == [5, 4, 6]
    assert [item[2] for item in references] == [5, 4, 6]


def test_sync_failure_leaves_original_and_cleans_temporary_files(monkeypatch):
    import subprocess
    paths = []
    def fail(command, **kwargs):
        source = Path(command[command.index('-i') + 1])
        paths.append(source)
        assert source.exists()
        assert kwargs['timeout'] == 300
        raise subprocess.TimeoutExpired(command, 300)
    monkeypatch.setattr(timing.subprocess, 'run', fail)
    with pytest.raises(subprocess.TimeoutExpired):
        timing.align_subtitle('video', '1\n00:00:01,000 --> 00:00:02,000\nTest\n', str)
    assert paths and not paths[0].parent.exists()


def test_validated_download_skips_later_movie_and_episode_sync():
    tree = ast.parse((ROOT / 'bazarr/subtitles/processing.py').read_text(encoding='utf-8'))
    guards = [node.test for node in ast.walk(tree) if isinstance(node, ast.If)
              and 'audio_timing_validated' in ast.unparse(node.test)]
    assert len(guards) == 2
    for guard in guards:
        code = compile(ast.Expression(guard), '<sync-guard>', 'eval')
        assert not eval(code, {'subtitle': SimpleNamespace(audio_timing_validated=True),
                               'sync_checker': lambda _: pytest.fail('Must skip repeat sync')})
        assert eval(code, {'subtitle': SimpleNamespace(), 'sync_checker': lambda _: True})


def test_sync_error_rejects_without_mutating_candidate(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=True, audio_stream=0))))
    monkeypatch.setitem(sys.modules, 'app.get_args', SimpleNamespace(args=SimpleNamespace(config_dir='unused')))
    monkeypatch.setitem(sys.modules, 'utilities.binaries', SimpleNamespace(get_binary=str))
    monkeypatch.setattr(timing, 'parse_intervals', lambda _: [])
    monkeypatch.setattr(timing, 'extract_activity', lambda *args: (2400, [], []))
    monkeypatch.setattr(timing, 'evaluate_activity', lambda *args: {'accepted': False, 'reason': 'timing_not_confirmed'})
    def fail(*args):
        raise TimeoutError()
    monkeypatch.setattr(timing, 'align_subtitle', fail)
    subtitle = SimpleNamespace(text='original', content=b'original')
    assert not timing.validate_download(SimpleNamespace(original_path='video'), subtitle)
    assert subtitle.content == b'original'
    assert not subtitle.audio_timing_validated


def test_rejects_unrelated_speech_and_empty_audio(media):
    duration, starts, audio, intervals = media
    rng = np.random.default_rng(75)
    shuffled = intervals.copy()
    shuffled[:, 0] = rng.uniform(0, 2390, len(intervals))
    shuffled[:, 1] = shuffled[:, 0] + rng.uniform(0.4, 5, len(intervals))
    assert not timing.evaluate_activity(duration, starts, audio, shuffled)['accepted']
    assert not timing.evaluate_activity(duration, starts, np.zeros_like(audio), intervals)['accepted']
    assert not timing.evaluate_activity(duration, starts, np.ones_like(audio), intervals)['accepted']


def test_one_matching_window_cannot_pass(media):
    duration, starts, audio, intervals = media
    changed = audio.copy()
    for index in range(1, len(changed)):
        changed[index] = np.roll(changed[index], 191 * index)
    assert not timing.evaluate_activity(duration, starts, changed, intervals)['accepted']


def test_parses_srt_and_ass_without_text_language_dependency():
    import pysubs2
    subtitles = pysubs2.SSAFile()
    for i in range(35):
        subtitles.append(pysubs2.SSAEvent(start=i * 3000, end=i * 3000 + 1500, text='Dialogue'))
    expected = timing.parse_intervals(subtitles.to_string('srt'))
    subtitles.append(pysubs2.SSAEvent(start=1, end=2000, text='Ignored', type='Comment'))
    np.testing.assert_equal(timing.parse_intervals(subtitles.to_string('ass')), expected)
    with pytest.raises(ValueError):
        timing.parse_intervals('')


def test_cache_reuses_audio_and_invalidates_on_video_change(tmp_path, monkeypatch):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    calls = []
    def run(command):
        calls.append(command)
        if command[0] == 'ffprobe':
            return b'{"format":{"duration":2400}}'
        return b'\0' * (timing.WINDOW_SECONDS * 16000 * 2)
    monkeypatch.setattr(timing, '_run', run)
    first = timing.extract_activity(video, tmp_path / 'cache', str)
    second = timing.extract_activity(video, tmp_path / 'cache', str)
    assert len(calls) == 6
    np.testing.assert_equal(first[2], second[2])
    video.write_bytes(b'changed-video')
    timing.extract_activity(video, tmp_path / 'cache', str)
    assert len(calls) == 12


def production_function(path, name, namespace):
    """Exercise the actual download function without starting Bazarr's database/jobs."""
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name]
    node = next((node for node in nodes if node.args.args and node.args.args[0].arg == 'self'), nodes[0])
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


class Episode:
    name = 'test-video'


class Language:
    basename = 'en'


def candidate(number, provider='test'):
    return SimpleNamespace(id=number, language=Language(), matches={'series', 'season', 'episode'},
                           hearing_impaired_verifiable=False, hash_verifiable=True,
                           release_info='test', provider_name=provider)


def pool_method():
    return production_function('custom_libs/subliminal_patch/core.py', 'download_best_subtitles',
                               {'Episode': Episode, 'MAX_SCORES': {'episode': 360},
                                'compute_score': lambda *args: (360, 360),
                                'operator': operator, 'logger': logging.getLogger('test')})


def test_rejected_candidate_falls_through_even_with_only_one():
    first, second = candidate(1), candidate(2)
    tried = []
    def download(subtitle):
        tried.append(subtitle.id)
        return True
    pool = SimpleNamespace(download_subtitle=download, providers=['test'])
    accepted = pool_method()(pool, [first, second], Episode(), [first.language], only_one=True,
                             subtitle_validator=lambda video, subtitle: subtitle.id == 2)
    assert accepted == [second]
    assert tried == [1, 2]


def test_whisper_fallback_cannot_bypass_validator():
    subtitle = candidate(1, 'whisperai')
    pool = SimpleNamespace(download_subtitle=lambda subtitle: True, providers=['whisperai'])
    accepted = pool_method()(pool, [subtitle], Episode(), [subtitle.language], min_score=400,
                             fallback_allowed=True, subtitle_validator=lambda *_: False)
    assert accepted == []


def test_unconfigured_callback_preserves_download_behavior():
    subtitle = candidate(1)
    pool = SimpleNamespace(download_subtitle=lambda subtitle: True, providers=['test'])
    assert pool_method()(pool, [subtitle], Episode(), [subtitle.language]) == [subtitle]


def test_manual_rejection_prevents_save():
    subtitle = SimpleNamespace(language=SimpleNamespace(), is_valid=lambda: True)
    settings = SimpleNamespace(general=SimpleNamespace(utf8_encode=False, subzero_mods=''))
    def unexpected_save(*args, **kwargs):
        raise AssertionError('Rejected subtitle must not reach save_subtitles')
    function = production_function('bazarr/subtitles/manual.py', 'manual_download_subtitle', {
        'logging': logging, 'os': __import__('os'), 'settings': settings,
        'subtitle_cache': {'cached': subtitle}, 'get_array_from': lambda _: [],
        'get_video': lambda *args, **kwargs: Episode(), 'force_unicode': str,
        'download_subtitles': lambda *args: None, '_get_pool': lambda *args: None,
        'validate_download': lambda *args: False, 'save_subtitles': unexpected_save,
    })
    result = function('video', 'en', 'False', 'False', 'cached', 'test', 'scene', 'title', 'series', False, 1)
    assert 'validation' in result


def test_disabled_filter_does_not_access_media(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=False))))
    assert timing.validate_download(None, None)


def test_enabled_filter_fails_closed_on_extraction_error(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=True, audio_stream=0))))
    monkeypatch.setitem(sys.modules, 'app.get_args', SimpleNamespace(args=SimpleNamespace(config_dir='unused')))
    monkeypatch.setitem(sys.modules, 'utilities.binaries', SimpleNamespace(get_binary=str))
    monkeypatch.setattr(timing, 'parse_intervals', lambda _: [])
    def fail(*args):
        raise TimeoutError('private path or credentials must not be logged')
    monkeypatch.setattr(timing, 'extract_activity', fail)
    assert not timing.validate_download(SimpleNamespace(original_path='video'), SimpleNamespace(text='text'))


def test_failed_extraction_backs_off_across_candidates(tmp_path, monkeypatch):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    calls = []
    def fail(command):
        calls.append(command)
        raise TimeoutError()
    monkeypatch.setattr(timing, '_run', fail)
    with pytest.raises(TimeoutError):
        timing.extract_activity(video, tmp_path / 'cache', str)
    with pytest.raises(ValueError, match='temporarily unavailable'):
        timing.extract_activity(video, tmp_path / 'cache', str)
    assert len(calls) == 1
