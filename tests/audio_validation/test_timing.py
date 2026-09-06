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
    result = timing.evaluate_activity(duration, starts, audio, (intervals - offset) / rate)
    assert result['accepted']
    assert abs(result['offset_seconds'] - offset) < 0.5
    assert result['rate'] == pytest.approx(rate)


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
