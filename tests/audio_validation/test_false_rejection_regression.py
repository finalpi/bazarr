"""Integration regression: weak speech activity is not proof of a mismatch.

The observed VAD statistics are numeric only. All dialogue is synthetic; no
movie dialogue or downloaded subtitle file is included in the repository.
"""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pysubs2
import pytest
from subzero.language import Language

from test_reference_alignment import SENTENCES
from test_subtitle_rejections import database_factory


ROOT = Path(__file__).resolve().parents[2]
DURATION = 3600
STARTS = [360, 1080, 1800, 2520, 3240]
LOW_VAD = {'accepted': False, 'reason': 'timing_not_confirmed', 'score': 0.2227465,
           'peak_z': 4.0657031, 'offset_seconds': 0.0, 'rate': 1.0, 'windows': 5,
           'window_scores': [0.254839, 0.191955, 0.289308, 0.122784, 0.254846]}


def subtitles(kind='bilingual', offset=0, rate=1, linger=0, split=False):
    references = []
    file = pysubs2.SSAFile()
    for window, start in enumerate(STARTS, 1):
        for index in range(5):
            left = start + index * 8
            sentence = SENTENCES[len(references)]
            references.append(dict(start=left, end=left + 3.5, text=sentence, window=window))
            body = ('中文译文\n' + sentence if kind == 'bilingual' else
                    '这些是完整的中文字幕内容' if kind == 'chinese' else
                    'Unrelated announcements use entirely different words about computer maintenance.')
            if split and kind == 'bilingual':
                words = sentence.split()
                chunks = [' '.join(words[:len(words) // 2]), ' '.join(words[len(words) // 2:])]
            else:
                chunks = [body]
            for part, chunk in enumerate(chunks):
                first = left + part * 3.5 / len(chunks)
                last = left + (part + 1) * 3.5 / len(chunks)
                file.append(pysubs2.SSAEvent(start=round((first - offset) / rate * 1000),
                                            end=round(((last - offset) / rate + linger) * 1000), text=chunk))
        # Additional translated cues provide a normal full-subtitle population.
        file.append(pysubs2.SSAEvent(start=round((start + 45 - offset) / rate * 1000),
                                    end=round((start + 47 - offset) / rate * 1000), text='另一段中文对白'))
        file.append(pysubs2.SSAEvent(start=round((start + 51 - offset) / rate * 1000),
                                    end=round((start + 53 - offset) / rate * 1000), text='更多中文对白'))
    return file.to_string('srt'), references


@pytest.fixture
def pipeline(monkeypatch, database_factory, tmp_path):
    db = database_factory(tmp_path / 'rejections.sqlite')
    package = ModuleType('subtitle_regression')
    package.__path__ = [str(ROOT / 'bazarr/subtitles')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, package.__name__ + '.rejections', db.module)
    for name in ('audio_validation', 'reference_alignment', 'reference_sampling'):
        path = ROOT / 'bazarr/subtitles' / (name + '.py')
        spec = importlib.util.spec_from_file_location(package.__name__ + '.' + name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        setattr(package, name, module)
    timing = package.audio_validation
    monkeypatch.setitem(sys.modules, 'app.config', SimpleNamespace(
        settings=SimpleNamespace(audio_validation=SimpleNamespace(enabled=True))))
    monkeypatch.setitem(sys.modules, 'app.get_args', SimpleNamespace(args=SimpleNamespace(config_dir=str(tmp_path))))
    monkeypatch.setitem(sys.modules, 'utilities.binaries', SimpleNamespace(get_binary=str))
    reference = {'audio_index': 10, 'stream_index': 11, 'language': 'eng', 'title': 'Original'}
    monkeypatch.setattr(timing, 'select_audio_reference', Mock(return_value=reference))
    monkeypatch.setattr(timing, 'extract_activity', Mock(return_value=(
        DURATION, np.array(STARTS), np.zeros((5, timing.WINDOW_SECONDS * timing.HZ)))))
    monkeypatch.setattr(timing, 'evaluate_activity', Mock(return_value=dict(LOW_VAD)))
    sample = Mock()
    monkeypatch.setattr(package.reference_sampling, 'extract_reference_cues', sample)
    path = tmp_path / 'movie.mkv'
    path.write_bytes(b'media metadata fixture')
    video = SimpleNamespace(original_path=str(path), original_language='English', duration=DURATION, radarrId=49)
    return SimpleNamespace(timing=timing, sampler=sample, db=db, video=video)


def candidate(text):
    return SimpleNamespace(text=text, content=text.encode(), encoding='utf-8', _guessed_encoding='utf-8',
                           language=Language('zho'), id='fixture-site-id', provider_name='subhd')


@pytest.mark.parametrize('linger,split', [(0, False), (0.5, False), (2.5, False), (0, True)])
def test_low_vad_with_independent_dialogue_is_accepted_without_false_rejection(pipeline, linger, split):
    text, references = subtitles(linger=linger, split=split)
    pipeline.sampler.return_value = (references, {'stream_index': 17})
    subtitle = candidate(text)
    assert pipeline.timing.validate_download(pipeline.video, subtitle)
    assert subtitle.audio_timing_method == 'embedded_original_dialogue'
    assert subtitle.content == text.encode()
    assert pipeline.db.rows() == []


@pytest.mark.parametrize('offset,rate', [(35, 1), (-45, 1), (10, 25 / 24)])
def test_proven_corrections_are_rechecked_and_no_failed_record_is_written(pipeline, offset, rate):
    text, references = subtitles(offset=offset, rate=rate)
    pipeline.sampler.return_value = (references, {'stream_index': 17})
    subtitle = candidate(text)
    assert pipeline.timing.validate_download(pipeline.video, subtitle)
    assert subtitle.content != text.encode()
    assert pipeline.db.rows() == []


def test_pure_chinese_low_vad_is_unverified_not_a_permanent_mismatch(pipeline):
    text, _ = subtitles(kind='chinese')
    subtitle = candidate(text)
    assert not pipeline.timing.validate_download(pipeline.video, subtitle)
    assert subtitle.audio_timing_failure_reason == 'timing_not_confirmed'
    assert subtitle.content == text.encode()
    pipeline.sampler.assert_not_called()
    assert pipeline.db.rows() == []


def test_false_vad_shift_cannot_override_dialogue_proving_original_timestamps(pipeline):
    text, references = subtitles()
    pipeline.sampler.return_value = (references, {'stream_index': 17})
    pipeline.timing.evaluate_activity.side_effect = [
        dict(LOW_VAD),
        {'accepted': True, 'reason': 'timing_match', 'rate': 1.0, 'offset_seconds': 35},
        {'accepted': True, 'reason': 'timing_match'},
    ]
    subtitle = candidate(text)
    assert pipeline.timing.validate_download(pipeline.video, subtitle)
    assert subtitle.content == text.encode()
    assert subtitle.audio_timing_method == 'embedded_original_dialogue'
    assert pipeline.db.rows() == []


@pytest.mark.parametrize('failure', ['missing_reference', 'few_anchors', 'unrelated_dialogue', 'timeout', 'bad_origin'])
def test_insufficient_or_unusable_reference_never_becomes_a_permanent_mismatch(pipeline, failure):
    text, references = subtitles(kind='unrelated' if failure == 'unrelated_dialogue' else 'bilingual')
    if failure in ('missing_reference', 'bad_origin'):
        pipeline.sampler.return_value = ([], {'reason': 'reference_unavailable', 'detail': failure})
    elif failure == 'timeout':
        pipeline.sampler.side_effect = TimeoutError()
    else:
        pipeline.sampler.return_value = (references[:6] if failure == 'few_anchors' else references,
                                        {'stream_index': 17})
    subtitle = candidate(text)
    assert not pipeline.timing.validate_download(pipeline.video, subtitle)
    assert subtitle.content == text.encode()
    assert subtitle.audio_timing_failure_reason in ('timing_not_confirmed', 'validation_timeout')
    assert pipeline.db.rows() == []
