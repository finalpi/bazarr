import io
import subprocess
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlparse
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from subliminal import Episode, Movie
from subzero.language import Language

from subliminal_patch.providers import subhd
from subliminal_patch.providers.subhd import SubhdProvider
from subliminal_patch.providers.subhd import SubhdSubtitle, _extract_download


TITLE = "It's Always Sunny in Philadelphia"
LANGUAGE = Language('zho', 'CN')


def video(keyword=None):
    episode = Episode('/tmp/Its.Always.Sunny.In.Philadelphia.S02E05.avi', TITLE, 2, 5,
                      alternative_series=['费城永远阳光灿烂'])
    episode.absolute_episode = 12
    if keyword:
        episode.search_keyword = keyword
    return episode


def page(rows=(), links=()):
    body = ''.join('<div class="row"><a href="/a/%s">%s</a></div>' % row for row in rows)
    body += ''.join('<a class="page-link" href="%s">page</a>' % link for link in links)
    return body.encode('utf-8')


def query_path(query, suffix=''):
    return '/search/' + quote(query, safe='') + suffix


def test_default_search_falls_back_from_episode_to_the_target_season_pack():
    provider = SubhdProvider()
    provider._request = Mock(side_effect=[page(), page([
        ('sePyJ8', '费城永远阳光灿烂 第二季 ' + TITLE + ' S02 @TLF'),
        ('wrong', TITLE + ' S18'),
    ])])
    results = provider.list_subtitles(video(), {LANGUAGE})
    assert [subtitle.id for subtitle in results] == ['sePyJ8']
    assert [unquote(urlparse(call.args[0]).path) for call in provider._request.call_args_list] == [
        '/search/' + TITLE + ' S02E05', '/search/' + TITLE + ' S02']
    assert {'series', 'season', 'episode'} <= results[0].get_matches(video())


def test_raw_wrong_season_results_do_not_stop_later_queries_or_call_other_mirrors():
    provider = SubhdProvider()
    provider._request = Mock(side_effect=[
        page([('wrong', TITLE + ' S18')]), page([('correct', TITLE + ' S02')])])
    assert [subtitle.id for subtitle in provider.list_subtitles(video(), {LANGUAGE})] == ['correct']
    assert len(provider._request.call_args_list) == 2
    assert {urlparse(call.args[0]).netloc for call in provider._request.call_args_list} == {'subhd.me'}


def test_title_keyword_reaches_a_matching_season_on_the_next_page():
    provider = SubhdProvider()
    provider._request = Mock(side_effect=[
        page([('wrong', TITLE + ' S18')], [query_path(TITLE, '/2'), query_path(TITLE, '/3')]),
        page([('sePyJ8', TITLE + ' S02 @TLF')]),
    ])
    results = provider.list_subtitles(video(TITLE), {LANGUAGE})
    assert [subtitle.id for subtitle in results] == ['sePyJ8']
    assert [urlparse(call.args[0]).path for call in provider._request.call_args_list] == [
        query_path(TITLE), query_path(TITLE, '/2')]


def test_title_keyword_scans_three_pages_then_adds_a_season_query():
    provider = SubhdProvider()
    provider._request = Mock(side_effect=[
        page([('wrong', TITLE + ' S18')], [query_path(TITLE, '/2'), query_path(TITLE, '/3')]),
        page([('wrong', TITLE + ' S17')], [query_path(TITLE, '/3')]),
        page([('wrong', TITLE + ' S16')]),
        page([('sePyJ8', TITLE + ' S02')]),
    ])
    assert [subtitle.id for subtitle in provider.list_subtitles(video(TITLE), {LANGUAGE})] == ['sePyJ8']
    assert [unquote(urlparse(call.args[0]).path) for call in provider._request.call_args_list] == [
        '/search/' + TITLE, '/search/' + TITLE + '/2', '/search/' + TITLE + '/3', '/search/' + TITLE + ' S02']


def test_page_one_is_an_alias_of_the_initial_url_and_does_not_use_a_page_slot():
    provider = SubhdProvider()
    keyword = TITLE + ' S02'
    links = [query_path(keyword, '/1'), query_path(keyword, '/2'), query_path(keyword, '/3')]
    provider._request = Mock(return_value=page([('wrong', TITLE + ' S18')], links))
    assert provider.list_subtitles(video(keyword), {LANGUAGE}) == []
    assert [urlparse(call.args[0]).path for call in provider._request.call_args_list] == [
        query_path(keyword), query_path(keyword, '/2'), query_path(keyword, '/3')]


def test_pagination_accepts_only_the_same_host_and_decoded_search_query():
    search_url = 'https://subhd.me' + query_path(TITLE)
    links = [
        "/search/It's%20Always%20Sunny%20in%20Philadelphia/2",
        query_path(TITLE, '/02'),
        'https://attacker.example' + query_path(TITLE, '/3'),
        'https://subhd.one' + query_path(TITLE, '/3'),
        '/search/Another.Title/3',
        '/a/download',
        query_path(TITLE, '/3') + '?redirect=elsewhere',
        'http://subhd.me' + query_path(TITLE, '/3'),
    ]
    assert SubhdProvider._search_page_links(page(links=links).decode('utf-8'), search_url) == [
        search_url + '/2']


def test_cyclic_page_links_are_visited_once_and_never_expand_beyond_three_pages():
    provider = SubhdProvider()
    keyword = TITLE + ' S02'
    links = [query_path(keyword), query_path(keyword, '/2'), query_path(keyword, '/3'),
             query_path(keyword, '/4')]
    provider._request = Mock(return_value=page([('wrong', TITLE + ' S18')], links))
    assert provider.list_subtitles(video(keyword), {LANGUAGE}) == []
    assert [urlparse(call.args[0]).path for call in provider._request.call_args_list] == [
        query_path(keyword), query_path(keyword, '/2'), query_path(keyword, '/3')]


def test_mirrors_are_tried_after_request_failure_and_only_until_a_valid_empty_page():
    provider = SubhdProvider()
    provider._request = Mock(side_effect=[
        subprocess.CalledProcessError(22, ['curl']), page(), page([('correct', TITLE + ' S02')]),
    ])
    assert [subtitle.id for subtitle in provider.list_subtitles(video(), {LANGUAGE})] == ['correct']
    assert [urlparse(call.args[0]).netloc for call in provider._request.call_args_list] == [
        'subhd.me', 'subhd.one', 'subhd.me']


def test_total_request_budget_includes_failed_mirror_requests():
    provider = SubhdProvider()
    provider._request = Mock(side_effect=OSError('connection failed'))
    assert provider.list_subtitles(video(), {LANGUAGE}) == []
    assert provider._request.call_count == subhd._MAX_SEARCH_REQUESTS == 12


@pytest.mark.parametrize('keyword', [TITLE + ' S02', TITLE + ' S02E05', TITLE + ' Season 2',
                                    TITLE + ' second season', '费城永远阳光灿烂 第二季'])
def test_explicit_season_or_episode_keywords_are_not_modified_or_duplicated(keyword):
    assert SubhdProvider._search_queries(video(keyword)) == [keyword]


def test_default_and_movie_queries_preserve_title_and_media_identity():
    episode = video()
    assert SubhdProvider._search_queries(episode)[:3] == [TITLE + ' S02E05', TITLE + ' S02', TITLE]
    assert (episode.series, episode.season, episode.episode, episode.absolute_episode) == (TITLE, 2, 5, 12)
    movie = Movie('/tmp/Movie.mkv', 'Movie', year=2005)
    assert SubhdProvider._search_queries(movie) == ['Movie']


def test_duplicate_results_are_deduplicated_and_wrong_episode_season_and_title_are_excluded():
    provider = SubhdProvider()
    provider._request = Mock(return_value=page([
        ('correct', TITLE + ' S02'), ('correct', TITLE + ' S02'),
        ('wrongSeason', TITLE + ' S03E05'), ('wrongEpisode', TITLE + ' S02E04'),
        ('wrongTitle', 'Another Show S02E05'),
    ]))
    results = provider.list_subtitles(video(), {LANGUAGE, Language('zho', 'TW')})
    assert len(results) == 2
    assert {subtitle.id for subtitle in results} == {'correct'}
    assert {str(subtitle.language) for subtitle in results} == {'zh-CN', 'zh-TW'}


def zip_bytes(files):
    output = io.BytesIO()
    with ZipFile(output, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def subtitle():
    return SubhdSubtitle(LANGUAGE, 'sePyJ8', '/a/sePyJ8', TITLE + ' S02 @TLF', video())


SRT = b'1\n00:00:01,000 --> 00:00:02,000\ncorrect E05 dialogue\n'


def track_zip_reads(monkeypatch):
    original_open = ZipFile.open
    reads = []

    def open_member(archive, name, *args, **kwargs):
        reads.append(name)
        return original_open(archive, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, 'open', open_member)
    return reads


def test_nested_ten_episode_pack_reads_only_e05_and_keeps_absolute_episode_rules(monkeypatch):
    inner_name = 'Its.Always.Sunny.In.Philadelphia.S02E05-NODLABS@TLF.chs.srt'
    inner = zip_bytes({inner_name: SRT})
    files = {}
    for episode in range(1, 11):
        extension = 'zip' if episode % 2 else 'rar'
        files['Its.Always.Sunny.In.Philadelphia.S02E%02d-NODLABS@TLF.%s' % (episode, extension)] = (
            inner if episode == 5 else b'must never be decompressed')
    outer = zip_bytes(files)
    reads = track_zip_reads(monkeypatch)
    extracted, format_ = _extract_download(outer, subtitle())
    assert extracted == SRT
    assert format_ == 'srt'
    assert reads == ['Its.Always.Sunny.In.Philadelphia.S02E05-NODLABS@TLF.zip', inner_name]
    assert subtitle().video.absolute_episode == 12


def test_nested_wrong_season_wrong_episode_and_unmarked_archives_are_never_read(monkeypatch):
    outer = zip_bytes({
        'show.S01E05.zip': b'wrong season',
        'show.S02E04.rar': b'wrong episode',
        'show.S03E12.zip': b'absolute episode cannot override an explicit season',
        'show.S02E12.zip': b'absolute episode cannot override an explicit episode',
        'subtitles.zip': b'no episode evidence',
    })
    reads = track_zip_reads(monkeypatch)
    assert _extract_download(outer, subtitle()) == (None, None)
    assert reads == []


def test_inner_subtitle_absolute_episode_cannot_override_an_explicit_season_or_episode(monkeypatch):
    inner = zip_bytes({'show.S03E12.chs.srt': SRT, 'show.S02E12.chs.srt': SRT})
    outer = zip_bytes({'show.S02E05.zip': inner})
    reads = track_zip_reads(monkeypatch)
    assert _extract_download(outer, subtitle()) == (None, None)
    assert reads == ['show.S02E05.zip']


def test_absolute_episode_without_a_season_marker_keeps_the_existing_anime_match():
    inner = zip_bytes({'show.E12.chs.srt': SRT})
    outer = zip_bytes({'show.E12.zip': inner})
    assert _extract_download(outer, subtitle()) == (SRT, 'srt')


def test_three_archive_levels_work_but_a_fourth_level_is_not_read(monkeypatch):
    nested = zip_bytes({'show.S02E05.chs.srt': SRT})
    for _ in range(2):
        nested = zip_bytes({'show.S02E05.zip': nested})
    assert _extract_download(nested, subtitle()) == (SRT, 'srt')
    too_deep = zip_bytes({'show.S02E05.zip': nested})
    reads = track_zip_reads(monkeypatch)
    assert _extract_download(too_deep, subtitle()) == (None, None)
    assert reads == ['show.S02E05.zip', 'show.S02E05.zip']


def test_nested_archive_uses_the_original_chinese_language_and_format_ranking():
    import pysubs2
    ass = pysubs2.SSAFile()
    ass.append(pysubs2.SSAEvent(start=1000, end=2000, text='preferred simplified ASS'))
    ass_bytes = ass.to_string(format_='ass').encode('utf-8')
    inner = zip_bytes({
        'show.S02E05.CHT.ass': ass_bytes.replace(b'preferred simplified ASS', b'wrong traditional ASS'),
        'show.S02E05.CHS.srt': SRT,
        'show.S02E05.CHS.ass': ass_bytes,
        'show.S02E04.CHS.ass': ass_bytes,
    })
    assert _extract_download(zip_bytes({'show.S02E05.zip': inner}), subtitle()) == (ass_bytes, 'ass')


def test_zip_member_size_is_checked_before_it_is_decompressed(monkeypatch):
    outer = zip_bytes({'show.S02E05.chs.srt': SRT})
    reads = track_zip_reads(monkeypatch)
    monkeypatch.setattr(subhd, '_MAX_ARCHIVE_MEMBER_BYTES', len(SRT) - 1)
    assert _extract_download(outer, subtitle()) == (None, None)
    assert reads == []


def test_total_nested_budget_is_checked_before_decompressing_the_final_subtitle(monkeypatch):
    inner = zip_bytes({'show.S02E05.chs.srt': SRT})
    outer = zip_bytes({'show.S02E05.zip': inner})
    reads = track_zip_reads(monkeypatch)
    monkeypatch.setattr(subhd, '_MAX_EXTRACTED_BYTES', len(inner) + len(SRT) - 1)
    assert _extract_download(outer, subtitle()) == (None, None)
    assert reads == ['show.S02E05.zip']


def test_7z_reads_only_a_selected_target_and_never_readall(monkeypatch):
    import py7zr
    archive = Mock()
    archive.list.return_value = [
        SimpleNamespace(filename='show.S02E04.chs.srt', uncompressed=len(SRT), is_directory=False),
        SimpleNamespace(filename='show.S02E05.chs.srt', uncompressed=len(SRT), is_directory=False),
    ]
    archive.read.return_value = {'show.S02E05.chs.srt': io.BytesIO(SRT)}
    monkeypatch.setattr(py7zr, 'SevenZipFile', Mock(return_value=archive))
    assert _extract_download(b"7z\xbc\xaf'\x1c" + b'mocked archive header', subtitle()) == (SRT, 'srt')
    archive.read.assert_called_once_with(targets=['show.S02E05.chs.srt'])
    archive.readall.assert_not_called()
    archive.close.assert_called_once()
