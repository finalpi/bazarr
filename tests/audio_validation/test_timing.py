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


def stub_audio_selection(monkeypatch, audio_index=0):
    monkeypatch.setattr(timing, 'select_audio_reference', lambda *args, **kwargs: {
        'audio_index': audio_index, 'stream_index': audio_index + 1,
        'language': 'eng', 'title': 'Original', 'reason': 'original_language'}, raising=False)


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








def test_reference_alignment_distinguishes_corrected_timing(media):
    duration, starts, _, intervals = media
    accepted = timing.evaluate_reference_alignment(duration, starts, intervals, intervals)
    rejected = timing.evaluate_reference_alignment(duration, starts, intervals, intervals - 35)
    assert accepted['accepted']
    assert not rejected['accepted']






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


def test_native_track_uses_a_separate_cache_and_relative_ffmpeg_mapping(tmp_path, monkeypatch):
    video = tmp_path / 'video.mkv'
    video.write_bytes(b'video')
    commands = []

    def run(command):
        commands.append(command)
        if command[0] == 'ffprobe':
            return b'{"format":{"duration":2400}}'
        return b'\0' * (timing.WINDOW_SECONDS * 16000 * 2)

    monkeypatch.setattr(timing, '_run', run)
    timing.extract_activity(video, tmp_path / 'cache', str, audio_stream=0)
    timing.extract_activity(video, tmp_path / 'cache', str, audio_stream=10)
    timing.extract_activity(video, tmp_path / 'cache', str, audio_stream=10)
    assert len(commands) == 12
    maps = [command[command.index('-map') + 1] for command in commands if command[0] == 'ffmpeg']
    assert maps == ['0:a:0'] * 5 + ['0:a:10'] * 5
    assert len(list((tmp_path / 'cache').glob('timing-*.npz'))) == 2


def test_probe_wrapper_selects_real_original_language_helper_without_using_default_track(monkeypatch):
    import json
    import types

    package = types.ModuleType('diagnostic_subtitles')
    package.__path__ = [str(ROOT / 'bazarr/subtitles')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    probe_spec = importlib.util.spec_from_file_location(
        package.__name__ + '.audio_validation', ROOT / 'bazarr/subtitles/audio_validation.py')
    probe = importlib.util.module_from_spec(probe_spec)
    probe_spec.loader.exec_module(probe)
    streams = [{'index': index, 'codec_type': 'audio',
                'tags': {'language': 'rus'}, 'disposition': {'default': int(index == 1)}}
               for index in range(1, 11)]
    streams += [{'index': 11, 'codec_type': 'audio',
                 'tags': {'language': 'eng', 'title': 'Original (Theatrical Mix)'}},
                {'index': 12, 'codec_type': 'audio',
                 'tags': {'language': 'eng', 'title': 'Commentary'}, 'disposition': {'default': 1}}]
    commands = []

    def run(command):
        commands.append(command)
        return json.dumps({'streams': streams}).encode()

    monkeypatch.setattr(probe, '_run', run)
    selected = probe.select_audio_reference(
        SimpleNamespace(original_path='movie.mkv', original_language='English'), str)
    assert (selected['audio_index'], selected['stream_index'], selected['language']) == (10, 11, 'eng')
    assert commands[0][commands[0].index('-select_streams') + 1] == 'a'


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




def test_disabled_filter_does_not_access_media(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=False))))
    monkeypatch.setattr(timing, 'select_audio_reference',
                        lambda *args: pytest.fail('Disabled validation must not inspect media'), raising=False)
    assert timing.validate_download(None, None)


def test_enabled_filter_fails_closed_on_extraction_error(monkeypatch):
    stub_audio_selection(monkeypatch)
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=True, audio_stream=0))))
    monkeypatch.setitem(sys.modules, 'app.get_args', SimpleNamespace(args=SimpleNamespace(config_dir='unused')))
    monkeypatch.setitem(sys.modules, 'utilities.binaries', SimpleNamespace(get_binary=str))
    monkeypatch.setattr(timing, 'parse_intervals', lambda _: [])
    def fail(*args, **kwargs):
        raise TimeoutError('private path or credentials must not be logged')
    monkeypatch.setattr(timing, 'extract_activity', fail)
    assert not timing.validate_download(SimpleNamespace(original_path='video'), SimpleNamespace(text=full_srt()))




def test_original_audio_selection_failure_rejects_without_extracting_or_changing_candidate(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=True, audio_stream=0))))
    monkeypatch.setitem(sys.modules, 'app.get_args', SimpleNamespace(args=SimpleNamespace(config_dir='unused')))
    monkeypatch.setitem(sys.modules, 'utilities.binaries', SimpleNamespace(get_binary=str))
    monkeypatch.setattr(timing, 'parse_intervals', lambda text: [])

    def fail_selection(*args, **kwargs):
        raise ValueError('Original dialogue track unavailable')

    monkeypatch.setattr(timing, 'select_audio_reference', fail_selection, raising=False)
    extract = Mock(side_effect=AssertionError('No audio may be extracted after failed selection'))
    align = Mock(side_effect=AssertionError('No synchronization may run after failed selection'))
    monkeypatch.setattr(timing, 'extract_activity', extract)
    monkeypatch.setattr(timing, 'apply_timing_transform', align)
    subtitle = SimpleNamespace(text=full_srt(), content=b'original', encoding='gbk', _guessed_encoding='gbk',
                               audio_timing_validated=False)
    original = vars(subtitle).copy()
    assert not timing.validate_download(SimpleNamespace(original_path='video'), subtitle)
    assert subtitle.content == original['content']
    assert subtitle.encoding == original['encoding']
    assert subtitle._guessed_encoding == original['_guessed_encoding']
    assert subtitle.audio_timing_failure_reason == 'validation_unavailable'
    extract.assert_not_called()
    align.assert_not_called()


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



def full_srt(intervals=None):
    if intervals is None:
        intervals = np.array([(i * 40 + 1, i * 40 + 3) for i in range(60)])
    subtitles = timing.pysubs2.SSAFile()
    for left, right in intervals:
        subtitles.append(timing.pysubs2.SSAEvent(start=round(left * 1000), end=round(right * 1000), text='Dialogue'))
    return subtitles.to_string('srt')


def stub_runtime(monkeypatch, media, audio_index=10):
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=True))))
    monkeypatch.setitem(sys.modules, 'app.get_args', SimpleNamespace(args=SimpleNamespace(config_dir='unused')))
    monkeypatch.setitem(sys.modules, 'utilities.binaries', SimpleNamespace(get_binary=str))
    stub_audio_selection(monkeypatch, audio_index)
    monkeypatch.setattr(timing, 'extract_activity', lambda *args, **kwargs: media[:3])
    monkeypatch.setattr(timing, '_reference_verification', lambda *args, **kwargs:
                        (None, {'accepted': False, 'reason': 'reference_inconclusive'}))


@pytest.mark.parametrize('offset,rate', [(0, 1), (35, 1), (-45, 1), (10, 25 / 24)])
def test_sampled_sync_applies_transform_and_revalidates_before_commit(monkeypatch, media, offset, rate):
    duration, starts, activity, correct_intervals = media
    stub_runtime(monkeypatch, media)
    raw_intervals = (correct_intervals - offset) / rate
    raw_intervals = raw_intervals[raw_intervals[:, 0] >= 0]
    text = full_srt(raw_intervals)
    subtitle = SimpleNamespace(text=text, content=text.encode(), language=SimpleNamespace(forced=False))
    stages = []
    assert timing.validate_download(SimpleNamespace(original_path='video', duration=duration), subtitle,
                                    progress=lambda stage, details: stages.append(stage))
    corrected = timing.parse_intervals(subtitle.content.decode())
    assert timing.evaluate_activity(duration, starts, activity, corrected)['accepted']
    assert subtitle.audio_timing_validated
    assert subtitle.audio_timing_failure_reason is None
    assert 'timing_accepted' in stages
    if offset or rate != 1:
        assert 'timing_search' in stages and 'timing_verify' in stages
        np.testing.assert_allclose(corrected, raw_intervals * rate + offset, atol=.15)
    else:
        assert subtitle.content == text.encode()
        assert 'timing_search' not in stages


def test_short_candidate_rejected_before_media_reads_with_diagnostic_reason(monkeypatch, media, caplog):
    stub_runtime(monkeypatch, media)
    monkeypatch.setattr(timing, 'select_audio_reference', lambda *a, **kw: pytest.fail('Short file must be rejected first'))
    text = full_srt(np.array([(i * 4, i * 4 + 2) for i in range(63)]))
    subtitle = SimpleNamespace(text=text, content=text.encode(), provider_name='subhd', id='SiteId',
                               selected_archive_member='movie.CHS&ENG.srt', language=SimpleNamespace(forced=False))
    assert not timing.validate_download(SimpleNamespace(original_path='video', duration=7733.12), subtitle)
    assert subtitle.content == text.encode()
    assert 'coverage' in subtitle.audio_timing_failure_reason or 'fragment' in subtitle.audio_timing_failure_reason
    assert 'SiteId' in caplog.text and 'movie.CHS&ENG.srt' in caplog.text
    assert 'elapsed=' in caplog.text


@pytest.mark.parametrize('seed', [6, 24, 75, 104, 500])
def test_offset_rate_search_rejects_unrelated_subtitle(monkeypatch, media, seed):
    duration, _, _, correct = media
    rng = np.random.default_rng(seed)
    wrong = correct.copy()
    wrong[:, 0] = rng.uniform(0, duration - 5, len(wrong))
    wrong[:, 1] = wrong[:, 0] + rng.uniform(.5, 4, len(wrong))
    stub_runtime(monkeypatch, media)
    text = full_srt(wrong)
    subtitle = SimpleNamespace(text=text, content=text.encode())
    assert not timing.validate_download(SimpleNamespace(original_path='video', duration=duration), subtitle)
    assert subtitle.content == text.encode()
    assert not subtitle.audio_timing_validated


def test_failed_post_correction_check_never_mutates_subtitle(monkeypatch, media):
    stub_runtime(monkeypatch, media)
    result = iter([{'accepted': False, 'reason': 'timing_not_confirmed'},
                   {'accepted': True, 'reason': 'timing_match', 'rate': 1, 'offset_seconds': 35},
                   {'accepted': False, 'reason': 'timing_not_confirmed'}])
    monkeypatch.setattr(timing, 'evaluate_activity', lambda *a, **kw: next(result))
    text = full_srt()
    subtitle = SimpleNamespace(text=text, content=text.encode(), encoding='gbk', _guessed_encoding='gbk')
    assert not timing.validate_download(SimpleNamespace(original_path='video', duration=2400), subtitle)
    assert subtitle.content == text.encode()
    assert subtitle.encoding == subtitle._guessed_encoding == 'gbk'
    assert subtitle.audio_timing_failure_reason == 'corrected_timing_not_confirmed'


def test_transform_preserves_ass_styles_and_text():
    subs = timing.pysubs2.SSAFile()
    subs.styles['Dialogue'] = timing.pysubs2.SSAStyle(fontname='Test font', fontsize=28)
    subs.events.append(timing.pysubs2.SSAEvent(start=10000, end=13000, text=r'{\b1}Chinese\NEnglish', style='Dialogue'))
    aligned = timing.apply_timing_transform(subs.to_string('ass'), 25 / 24, -2)
    actual = timing.pysubs2.SSAFile.from_string(aligned)
    assert actual.styles['Dialogue'].fontname == 'Test font'
    assert actual.events[0].text == subs.events[0].text
    assert actual.events[0].start == pytest.approx(10000 * 25 / 24 - 2000, abs=10)
    assert actual.events[0].end == pytest.approx(13000 * 25 / 24 - 2000, abs=10)


def test_timeout_logs_type_and_stage_without_exception_message(monkeypatch, media, caplog):
    stub_runtime(monkeypatch, media)
    secret = 'https://signed.example/download?token=private'
    def timeout(*args, **kwargs):
        raise timing.subprocess.TimeoutExpired(secret, 180)
    monkeypatch.setattr(timing, 'extract_activity', timeout)
    subtitle = SimpleNamespace(text=full_srt(), content=b'original')
    assert not timing.validate_download(SimpleNamespace(original_path='video', duration=2400), subtitle)
    assert subtitle.audio_timing_failure_reason == 'validation_timeout'
    assert secret not in caplog.text and 'validation_timeout' in caplog.text
    assert subtitle.content == b'original'


def test_audio_samples_use_seek_and_bounded_duration_with_shared_deadline(tmp_path, monkeypatch):
    video = tmp_path / 'video.mkv'
    video.write_bytes(b'video')
    commands = []
    def run(command, **kwargs):
        commands.append((command, kwargs))
        if command[0] == 'ffprobe':
            return b'{"format":{"duration":2400}}'
        return b'\0' * (timing.WINDOW_SECONDS * 16000 * 2)
    monkeypatch.setattr(timing, '_run', run)
    stages = []
    timing.extract_activity(video, tmp_path / 'cache', str, 10,
                            deadline=timing.time.monotonic() + 170,
                            progress=lambda stage, details: stages.append((stage, details)))
    ffmpeg = [(cmd, kw) for cmd, kw in commands if cmd[0] == 'ffmpeg']
    assert len(ffmpeg) == 5
    for cmd, kwargs in ffmpeg:
        assert cmd.index('-ss') < cmd.index('-i')
        assert cmd[cmd.index('-t') + 1] == str(timing.WINDOW_SECONDS)
        assert cmd[cmd.index('-map') + 1] == '0:a:10'
        assert 0 < kwargs['timeout'] <= 35
    assert len([item for item in stages if item[0] == 'audio_sample']) == 5


def test_exhausted_budget_never_spawns_process():
    with pytest.raises(TimeoutError):
        timing._run_with_budget(['ffmpeg'], deadline=timing.time.monotonic() - 1)


def test_url_provider_ids_and_credentials_are_redacted_from_context(monkeypatch, media, caplog):
    stub_runtime(monkeypatch, media)
    caplog.set_level(logging.INFO)
    subtitle = SimpleNamespace(text=full_srt(), content=b'original',
                               provider_name='zimuku', id='https://example/sub?token=private',
                               selected_archive_member='subtitle.srt api_key=private')
    assert not timing.validate_download(SimpleNamespace(original_path='video', duration=media[0]), subtitle)
    assert 'https://example' not in caplog.text
    assert 'token=private' not in caplog.text and 'api_key=private' not in caplog.text
    assert '[redacted' in caplog.text


def test_vtt_notes_do_not_become_audio_timing_cues():
    subs = timing.pysubs2.SSAFile()
    for index in range(35):
        subs.append(timing.pysubs2.SSAEvent(start=index * 3000, end=index * 3000 + 1500, text='Dialogue'))
    actual = subs.to_string('vtt')
    with_notes = actual.replace('WEBVTT\n\n', 'WEBVTT\n\nNOTE timestamp examples\n'
                                '00:10:00.000 --> 00:11:00.000\nNot dialogue\n\n')
    np.testing.assert_equal(timing.parse_intervals(with_notes), timing.parse_intervals(actual))


def test_inconclusive_vad_can_use_independent_dialogue_evidence_without_retiming(monkeypatch, media):
    stub_runtime(monkeypatch, media)
    monkeypatch.setattr(timing, 'evaluate_activity', lambda *args, **kwargs:
                        {'accepted': False, 'reason': 'timing_not_confirmed'})
    text = full_srt()
    reference = []
    def verify(*args, **kwargs):
        reference.append(args)
        return text, {'accepted': True, 'reason': 'reference_dialogue_match', 'anchors': 20}
    monkeypatch.setattr(timing, '_reference_verification', verify)
    subtitle = SimpleNamespace(text=text, content=text.encode(), encoding='utf-8')
    assert timing.validate_download(SimpleNamespace(original_path='video', duration=2400), subtitle)
    assert subtitle.content == text.encode()
    assert subtitle.audio_timing_method == 'embedded_original_dialogue'
    assert reference[0][2]['audio_index'] == 10


def test_reference_timeout_is_not_reported_as_a_proven_timing_mismatch(monkeypatch, media):
    stub_runtime(monkeypatch, media)
    monkeypatch.setattr(timing, 'evaluate_activity', lambda *args, **kwargs:
                        {'accepted': False, 'reason': 'timing_not_confirmed'})
    def timeout(*args, **kwargs):
        raise TimeoutError()
    monkeypatch.setattr(timing, '_reference_verification', timeout)
    subtitle = SimpleNamespace(text=full_srt(), content=b'original')
    assert not timing.validate_download(SimpleNamespace(original_path='video', duration=2400), subtitle)
    assert subtitle.audio_timing_failure_reason == 'validation_timeout'
    assert subtitle.content == b'original'
