from pathlib import Path
import importlib.util

import pysubs2
import pytest
from subzero.language import Language

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'translation_hi_filter', ROOT / 'bazarr/subtitles/tools/translate/hi_filter.py')
hi_filter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hi_filter)


def test_removes_sound_cues_but_keeps_dialogue_and_source(tmp_path):
    source = tmp_path / 'source.srt'
    source.write_text(
        '1\n00:00:01,000 --> 00:00:02,000\n[DOOR SLAMS]\n\n'
        '2\n00:00:02,000 --> 00:00:03,000\n[MUSIC]\n[LAUGHTER]\n\n'
        '3\n00:00:03,000 --> 00:00:04,000\n♪ OH, BABY, YOU ♪\n\n'
        '4\n00:00:04,000 --> 00:00:05,000\n[LAUGHS] Hello, Marge.\n\n'
        '5\n00:00:05,000 --> 00:00:06,000\n♪ BUT YOU SAY\nHE IS JUST A FRIEND ♪\nGODDAMN YOU!\n\n'
        '6\n00:00:06,000 --> 00:00:07,000\n♪♪\nWe should leave now.\n', encoding='utf-8')
    original = source.read_bytes()

    with hi_filter.translation_source(str(source), Language('eng'), True) as filtered_path:
        filtered = pysubs2.load(filtered_path, encoding='utf-8')
        assert '[DOOR SLAMS]' not in Path(filtered_path).read_text(encoding='utf-8')
        assert '[MUSIC]' not in Path(filtered_path).read_text(encoding='utf-8')
        assert '♪' not in Path(filtered_path).read_text(encoding='utf-8')
        assert any('OH, BABY, YOU' in cue.text for cue in filtered)
        assert any('BUT YOU SAY' in cue.text and 'HE IS JUST A FRIEND' in cue.text
                   and 'GODDAMN YOU!' in cue.text for cue in filtered)
        assert any('Hello, Marge.' in cue.text for cue in filtered)
        assert any('We should leave now.' in cue.text for cue in filtered)
        assert all(cue.start >= 3000 for cue in filtered)
    assert not Path(filtered_path).exists()
    assert source.read_bytes() == original


def test_all_sound_cues_fail_before_translation(tmp_path):
    source = tmp_path / 'sound-only.srt'
    source.write_text('1\n00:00:01,000 --> 00:00:02,000\n[DOOR SLAMS]\n', encoding='utf-8')
    with pytest.raises(ValueError, match='no subtitles|No dialogue'):
        with hi_filter.translation_source(str(source), Language('eng'), True):
            pytest.fail('Sound-only subtitles must not be translated')


def test_disabled_filter_uses_original_source(tmp_path):
    source = tmp_path / 'source.srt'
    source.write_text('1\n00:00:01,000 --> 00:00:02,000\n[DOOR SLAMS]\n', encoding='utf-8')
    with hi_filter.translation_source(str(source), None) as path:
        assert path == str(source)
