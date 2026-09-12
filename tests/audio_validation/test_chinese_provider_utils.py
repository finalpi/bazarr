from subliminal_patch.chinese import select_archive_entry, subtitle_language_hint, title_variants
import io
import subprocess
from zipfile import ZipFile
from unittest.mock import patch

from subliminal_patch.providers.localsibling import _language_matches, _same_work
from subliminal_patch.providers.r3sub import R3subProvider
from subliminal_patch.providers.subhd import SubhdProvider, SubhdSubtitle, _detect_subtitle_format, _extract_download
from subzero.language import Language
from subliminal import Episode


class Video:
    series = 'Maria Holic'
    title = 'A Flirtatious Kiss'
    alternative_series = ['玛利亚狂热', 'Maria+Holic']


def test_archive_selection_prefers_exact_episode_then_simplified_ass():
    names = [
        'show.S01E02.CHS.ass',
        'show.S01E01.CHT.ass',
        'show.S01E01.CHS.srt',
        'show.S01E01.CHS.ass',
        'show.S01E01.English.ass',
    ]
    assert select_archive_entry(names, season=1, episode=1, desired_language='zh') == 'show.S01E01.CHS.ass'


def test_archive_selection_supports_absolute_episode_and_rejects_incomplete_files():
    names = ['show.E01.CHS.ass', 'show.E14.CHS.ass', 'show.E14.sample.ass']
    assert select_archive_entry(names, season=2, episode=1, absolute_episode=14,
                                desired_language='zh') == 'show.E14.CHS.ass'


def test_archive_selection_rejects_a_different_named_episode():
    assert select_archive_entry(
        ['show.S01E02.CHS.ass'], season=1, episode=1, desired_language='zh') is None


def test_archive_selection_supports_common_anime_and_x_episode_names():
    names = ['Show - 02 [1080p].ass', 'Show - 01 [1080p].ass']
    assert select_archive_entry(names, season=1, episode=1, desired_language='zh') == names[1]
    assert select_archive_entry(['Show.1x03.CHS.srt'], season=1, episode=3,
                                desired_language='zh') == 'Show.1x03.CHS.srt'


def test_language_and_title_hints_are_deduplicated():
    assert subtitle_language_hint('name.中英双语.ass') == 'bilingual'
    assert title_variants(Video()) == ['Maria Holic', '玛利亚狂热']


def test_subhd_search_parser_keeps_the_longest_release_name():
    body = '''
      <div class="row"><a href="/a/Ab12">玛利亚狂热</a>
      <a class="view-text" href="/a/Ab12">Maria.Holic.S01E01.1080p</a>
      <span>简体</span><span>ASS</span></div>
      <div class="row"><a href="/a/Cd34">Other.S01E01.CHT</a></div>
    '''
    assert SubhdProvider._parse_results(body) == [
        ('Ab12', '玛利亚狂热 Maria.Holic.S01E01.1080p 简体 ASS'),
        ('Cd34', 'Other.S01E01.CHT'),
    ]


def test_subhd_title_only_result_does_not_claim_an_episode_match():
    video = Episode('/tmp/The.Simpsons.S37E01.mkv', 'The Simpsons', 37, 1,
                    alternative_series=['辛普森一家'])
    subtitle = SubhdSubtitle(
        Language('zho', 'CN'), 'movie', 'https://subhd.me/a/movie',
        '辛普森一家 The Simpsons Movie 2007 BluRay 1080p', video)
    matches = subtitle.get_matches(video)
    assert 'series' in matches
    assert 'season' not in matches
    assert 'episode' not in matches


def test_r3sub_search_parser_extracts_id_and_file_list():
    body = '''
      <div class="movie movie--preview">
        <a class="movie__title" href="show.php?id=R3a1">玛利亚狂热</a>
        <p class="movie__option">Maria Holic (2009)</p>
        <button data-fname="Maria.Holic.S01E01.CHT.ass">download</button>
      </div>
    '''
    assert R3subProvider._parse_search(body) == [
        ('R3a1', '玛利亚狂热 Maria Holic (2009) download', ['Maria.Holic.S01E01.CHT.ass'])
    ]


def test_subhd_archive_extracts_exact_episode_and_keeps_declared_format():
    output = io.BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('show.S01E02.CHS.ass', '[Script Info]\nwrong')
        archive.writestr('show.S01E01.CHS.srt', '1\n00:00:01,000 --> 00:00:02,000\n正确\n')
    video = type('EpisodeVideo', (), {'season': 1, 'episode': 1, 'absolute_episode': None})()
    subtitle = SubhdSubtitle(Language('zho', 'CN'), 'x', 'https://subhd.me/a/x', 'show', video)
    content, subtitle_format = _extract_download(output.getvalue(), subtitle)
    assert b'-->' in content
    assert subtitle_format == 'srt'


def test_subhd_archive_falls_back_when_preferred_ass_is_malformed():
    output = io.BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('show.S01E01.CHS.ass',
                         '[Script Info]\n[V4+ Styles]\n'
                         'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, '
                         'OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, '
                         'ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, '
                         'MarginR, MarginV, Encoding\n'
                         'Style: Default,Arial,20,&H00H202020,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,'
                         '100,100,0,0,1,1,0,2,10,10,10,1\n'
                         '[Events]\nFormat: Layer, Start, End, Style, Text\n'
                         'Dialogue: 0,0:00:01.00,0:00:02.00,Default,错误\n')
        archive.writestr('show.S01E01.CHS.srt',
                         '1\n00:00:01,000 --> 00:00:02,000\n正确\n')
    video = type('EpisodeVideo', (), {'season': 1, 'episode': 1, 'absolute_episode': None})()
    subtitle = SubhdSubtitle(Language('zho', 'CN'), 'x', 'https://subhd.me/a/x', 'show', video)
    content, subtitle_format = _extract_download(output.getvalue(), subtitle)
    assert b'-->' in content
    assert subtitle_format == 'srt'


def test_subhd_detects_utf16_ass_even_when_the_archive_extension_is_wrong():
    content = '[Script Info]\r\n[Events]\r\n'.encode('utf-16')
    assert _detect_subtitle_format(content, 'srt') == 'ass'


def test_local_sibling_language_filter_distinguishes_simplified_and_traditional():
    assert _language_matches('show.zh.ass', Language('zho', 'CN'))
    assert not _language_matches('show.zh.ass', Language('zho', 'TW'))
    assert _language_matches('show.zh-TW.ass', Language('zho', 'TW'))


def test_local_sibling_requires_the_same_title():
    assert _same_work(Video(), {'title': 'Maria Holic'})
    assert not _same_work(Video(), {'title': 'Another Show'})


def test_r3sub_interstitial_parser_reads_actual_second_hop_values():
    body = '<form action="/jpdown1.php"><input name="id" value="abc"><input name="lang" value="tw"></form>'
    assert R3subProvider._form_values(body) == {'id': 'abc', 'lang': 'tw'}


def test_r3sub_uses_dedicated_proxy_only_after_direct_cloudflare_403():
    provider = R3subProvider(email='configured', password='configured')
    provider._cookie_jar = '/tmp/cookies'
    provider._proxy_url = 'http://host.docker.internal:7890'
    forbidden = subprocess.CalledProcessError(22, ['curl'], stderr=b'HTTP 403')
    success = subprocess.CompletedProcess(['curl'], 0, stdout=b'ok', stderr=b'')
    with patch('subliminal_patch.providers.r3sub.subprocess.run', side_effect=[forbidden, success]) as run:
        assert provider._curl('https://forum.r3sub.com/entry/signin') == b'ok'
    assert provider._use_proxy is True
    assert '-x' not in run.call_args_list[0].args[0]
    assert run.call_args_list[1].args[0][1:3] == ['-x', provider._proxy_url]
