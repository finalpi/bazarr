import io
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from subliminal import Episode
from subzero.language import Language

from subliminal_patch.chinese import rank_archive_entries, select_archive_entry, subtitle_language_hint
from subliminal_patch.providers.assrt import AssrtSubtitle
from subliminal_patch.providers.localsibling import _language_matches
from subliminal_patch.providers.subhd import SubhdSubtitle, _extract_download
from subliminal_patch.providers import subhd


SIMPLIFIED = Language('zho', 'CN')
TRADITIONAL = Language('zho', 'TW')


def srt(text, script='zh'):
    chinese = '這次我們說的話會讓他們覺得很好' if script == 'zt' else '这次我们说的话会让他们觉得很好'
    return ('1\n00:00:01,000 --> 00:00:02,000\n' + chinese + '\n' + text + '\n').encode('utf-8')


def zip_bytes(files):
    output = io.BytesIO()
    with ZipFile(output, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def episode():
    video = Episode('/tmp/Show.S02E05.mkv', 'Show', 2, 5)
    video.absolute_episode = 12
    return video


def subhd_subtitle(language=SIMPLIFIED):
    return SubhdSubtitle(language, 'pack', 'https://subhd.me/a/pack', 'Show S02', episode())


@pytest.mark.parametrize('language,winner', [
    ('zh-CN', 'show.S02E05.CHS&ENG.srt'), ('zh-TW', 'show.S02E05.CHT&ENG.srt')])
def test_same_episode_prefers_target_script_bilingual_even_over_other_format_and_monolingual(language, winner):
    names = ['show.S02E05.CHS.ass', 'show.S02E05.CHT.ass',
             'show.S02E05.CHS&ENG.srt', 'show.S02E05.CHT&ENG.srt', 'show.S02E05.bilingual.ass']
    for ordered_names in (names, list(reversed(names))):
        assert select_archive_entry(ordered_names, season=2, episode=5, desired_language=language) == winner


@pytest.mark.parametrize('language,bilingual,monolingual', [
    ('zh-CN', 'show.S02E05.CHT&ENG.srt', 'show.S02E05.CHS.ass'),
    ('zh-TW', 'show.S02E05.CHS&ENG.srt', 'show.S02E05.CHT.ass'),
])
def test_opposite_script_bilingual_still_precedes_target_script_monolingual(language, bilingual, monolingual):
    assert select_archive_entry([monolingual, bilingual], season=2, episode=5,
                                desired_language=language) == bilingual


@pytest.mark.parametrize('filename,script', [
    ('show.S02E05.CHS&ENG.srt', 'zh'),
    ('show.S02E05.CHT&ENG.srt', 'zt'),
    ('show.S02E05.ZHT&ENG.ass', 'zt'),
    ('show.S02E05.简英.srt', 'zh'),
    ('show.S02E05.簡英.srt', 'zh'),
    ('show.S02E05.繁英.srt', 'zt'),
])
def test_bilingual_hint_preserves_the_public_hint_and_exposes_the_chinese_script(filename, script):
    assert subtitle_language_hint(filename) == 'bilingual'
    assert subtitle_language_hint(filename, include_bilingual=False) == script


def test_plain_english_is_not_bilingual_and_target_script_single_language_choice_is_preserved():
    assert subtitle_language_hint('show.S02E05.eng.srt') == 'foreign'
    names = ['show.S02E05.eng.ass', 'show.S02E05.CHS.srt', 'show.S02E05.CHT.srt']
    assert select_archive_entry(names, season=2, episode=5, desired_language='zh-CN') == names[1]
    assert select_archive_entry(names, season=2, episode=5, desired_language='zh-TW') == names[2]


def test_wrong_episode_season_and_incomplete_bilingual_do_not_override_a_valid_same_episode_subtitle():
    correct = 'show.S02E05.CHS.srt'
    names = ['show.S02E04.CHS&ENG.ass', 'show.S03E05.CHS&ENG.ass',
             'show.S02E05.sample.CHS&ENG.ass', correct]
    assert rank_archive_entries(names, season=2, episode=5, desired_language='zh-CN') == [correct]


def test_forced_and_hearing_impaired_preferences_still_distinguish_otherwise_equal_bilingual_tracks():
    regular = 'show.S02E05.CHS&ENG.srt'
    forced = 'show.S02E05.forced.CHS&ENG.srt'
    hi = 'show.S02E05.hi.CHS&ENG.srt'
    assert select_archive_entry([forced, hi, regular], season=2, episode=5, desired_language='zh-CN') == regular
    assert select_archive_entry([regular, forced], season=2, episode=5,
                                desired_language='zh-CN', forced=True) == forced
    assert select_archive_entry([regular, hi], season=2, episode=5,
                                desired_language='zh-CN', hearing_impaired=True) == hi


@pytest.mark.parametrize('filename,simplified,traditional', [
    ('show.CHS&ENG.srt', True, False),
    ('show.CHT&ENG.srt', False, True),
    ('show.ZHT&ENG.srt', False, True),
    ('show.中英双语.srt', True, False),
])
def test_local_reuse_keeps_bilingual_script_boundaries(filename, simplified, traditional):
    assert _language_matches(filename, SIMPLIFIED) is simplified
    assert _language_matches(filename, TRADITIONAL) is traditional


@pytest.mark.parametrize('language,winner', [
    (SIMPLIFIED, 'simplified bilingual\nEnglish'), (TRADITIONAL, 'traditional bilingual\nEnglish')])
def test_nested_episode_archive_selects_target_script_bilingual_and_never_reads_other_episodes(
        language, winner, monkeypatch):
    inner = zip_bytes({
        'show.S02E05.chs.srt': srt('simplified monolingual'),
        'show.S02E05.cht.srt': srt('traditional monolingual', script='zt'),
        'show.S02E05.chs&eng.srt': srt('simplified bilingual\nEnglish'),
        'show.S02E05.cht&eng.srt': srt('traditional bilingual\nEnglish', script='zt'),
    })
    outer = zip_bytes({
        'show.S02E04.zip': b'wrong episode archive must not be read',
        'show.S02E05.zip': inner,
        'show.S03E12.zip': b'absolute episode must not override another season',
    })
    reads = []
    original_open = ZipFile.open

    def record_read(archive, name, *args, **kwargs):
        reads.append(name)
        return original_open(archive, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, 'open', record_read)
    assert _extract_download(outer, subhd_subtitle(language)) == (
        srt(winner, script='zt' if language == TRADITIONAL else 'zh'), 'srt')
    assert reads[0] == 'show.S02E05.zip'
    assert len(reads) == 2
    assert 'show.S02E04.zip' not in reads and 'show.S03E12.zip' not in reads


@pytest.mark.parametrize('remaining_bilingual,bilingual_text,expected', [
    ('show.S02E05.CHS&ENG.srt', 'valid simplified bilingual\nEnglish', 'valid simplified bilingual\nEnglish'),
    ('show.S02E05.CHT&ENG.srt', 'valid traditional bilingual\nEnglish', 'valid monolingual'),
    (None, None, 'valid monolingual'),
])
def test_invalid_preferred_bilingual_falls_back_within_the_requested_script(
        remaining_bilingual, bilingual_text, expected):
    files = {
        'show.S02E05.CHS&ENG.ass': b'[Script Info]\n[Events]\nDialogue: malformed\n',
        'show.S02E05.CHS.srt': srt('valid monolingual'),
    }
    if remaining_bilingual:
        files[remaining_bilingual] = srt(bilingual_text, script='zt' if '.CHT' in remaining_bilingual else 'zh')
    assert _extract_download(zip_bytes(files), subhd_subtitle()) == (srt(expected), 'srt')


@pytest.mark.parametrize('inner_filename', ['chs&eng.srt', 'show.S02E05.CHS&ENG.srt'])
def test_outer_monolingual_does_not_hide_a_bilingual_inside_the_same_episode_archive(inner_filename):
    inner = zip_bytes({
        inner_filename: srt('bilingual in E05 archive\nEnglish'),
        'show.S02E04.CHS&ENG.ass': b'wrong episode even inside the correct outer archive',
    })
    outer = zip_bytes({
        'show.S02E05.CHS.srt': srt('outer monolingual'),
        'show.S02E05.zip': inner,
    })
    assert _extract_download(outer, subhd_subtitle()) == (srt('bilingual in E05 archive\nEnglish'), 'srt')


def test_outer_bilingual_is_not_replaced_by_a_monolingual_inside_the_episode_archive():
    inner = zip_bytes({'show.S02E05.CHS.srt': srt('inner monolingual')})
    outer = zip_bytes({
        'show.S02E05.CHS&ENG.srt': srt('outer bilingual\nEnglish'),
        'show.S02E05.zip': inner,
    })
    assert _extract_download(outer, subhd_subtitle()) == (srt('outer bilingual\nEnglish'), 'srt')


def test_budget_exhaustion_before_reading_nested_archive_retains_a_valid_direct_subtitle(monkeypatch):
    direct = srt('valid direct monolingual')
    inner = zip_bytes({'show.S02E05.CHS&ENG.srt': srt('unreadable within the remaining budget\nEnglish')})
    outer = zip_bytes({'show.S02E05.CHS.srt': direct, 'show.S02E05.zip': inner})
    monkeypatch.setattr(subhd, '_MAX_EXTRACTED_BYTES', len(direct))
    assert _extract_download(outer, subhd_subtitle()) == (direct, 'srt')


def test_wrong_inner_episode_bilingual_does_not_override_correct_outer_monolingual():
    inner = zip_bytes({'show.S02E04.CHS&ENG.srt': srt('wrong episode bilingual')})
    outer = zip_bytes({
        'show.S02E05.CHS.srt': srt('correct outer monolingual'),
        'show.S02E05.zip': inner,
    })
    assert _extract_download(outer, subhd_subtitle()) == (srt('correct outer monolingual'), 'srt')


def assrt_detail(language, filenames, monkeypatch):
    files = [{'f': name, 'url': 'https://subtitles.example/%d' % index} for index, name in enumerate(filenames)]
    body = {'sub': {'subs': [{'filelist': files}]}}
    session = SimpleNamespace(get=Mock(return_value=SimpleNamespace(
        json=lambda: body, raise_for_status=lambda: None)))
    subtitle = AssrtSubtitle(language, 'test-subtitle', 'Show.S02E05', session, 'test-token', 60)
    subtitle.get_matches(episode())
    monkeypatch.setattr('subliminal_patch.providers.assrt.sleep', lambda seconds: None)
    return subtitle._get_detail()


@pytest.mark.parametrize('language,winner', [
    (SIMPLIFIED, 'show.S02E05.CHS&ENG.srt'), (TRADITIONAL, 'show.S02E05.CHT&ENG.srt')])
def test_assrt_chinese_selects_bilingual_of_the_target_script_after_episode_filter(language, winner, monkeypatch):
    names = ['show.S02E04.CHS&ENG.ass', 'show.S02E05.CHS.ass', 'show.S02E05.CHT.ass',
             'show.S02E05.CHS&ENG.srt', 'show.S02E05.CHT&ENG.srt']
    assert assrt_detail(language, names, monkeypatch)['f'] == winner


def test_assrt_english_selection_keeps_the_existing_language_selection(monkeypatch):
    names = ['show.S02E04.ENG.ass', 'show.S02E05.ENG.srt', 'show.S02E05.CHS&ENG.ass']
    assert assrt_detail(Language('eng'), names, monkeypatch)['f'] == 'show.S02E05.ENG.srt'


def test_assrt_chinese_unrankable_names_keep_the_existing_detail_fallback(monkeypatch):
    names = ['unknown.txt', 'unidentified.data']
    assert assrt_detail(SIMPLIFIED, names, monkeypatch)['f'] == 'unknown.txt'
