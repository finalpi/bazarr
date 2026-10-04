"""One downloaded season archive serves strict, independently selected episodes."""

import builtins
import copy
import json
import logging
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from subliminal import Episode
from subzero.language import Language

from subliminal_patch.providers import subhd
from subliminal_patch.providers.subhd import SubhdProvider, SubhdSubtitle
from test_subhd_script_guard import SIMPLIFIED, TRADITIONAL, archive, srt


CN, TW = Language('zho', 'CN'), Language('zho', 'TW')
CDN_URL = 'https://static.subhd.me/season-package.zip'


def subtitle(season=1, episode=1, language=CN):
    video = Episode(f'/tmp/Show.S{season:02d}E{episode:02d}.mkv', 'Show', season, episode)
    return SubhdSubtitle(language, 'SeasonPack', 'https://subhd.me/a/SeasonPack', 'Show S01', video)


def strict_extract(payload, target):
    return subhd.extract_archive_subtitle(payload, target, strict_episode=True)


def download_responses(payload, url=CDN_URL):
    return [b'{"success":true}', b'download page',
            json.dumps({'success': True, 'url': url}).encode('utf-8'), payload]


def zip_reads(monkeypatch):
    original_open, reads = ZipFile.open, []

    def tracked_open(package, name, *args, **kwargs):
        if kwargs.get('mode', args[0] if args else 'r') == 'r':
            reads.append(name)
        return original_open(package, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, 'open', tracked_open)
    return reads


@pytest.mark.parametrize('filename', ['Season1/01.srt', 'S01/01.srt', 'Season1/E01.srt',
                                     'S01E01.srt', '1x01.srt', 'Season1/Show.S01E01.CHS.srt'])
def test_strict_selection_accepts_explicit_supported_season_and_episode_names(filename):
    content = srt(SIMPLIFIED)
    target = subtitle()
    assert strict_extract(archive({filename: content}), target) == (content, 'srt')
    assert target.selected_archive_member == filename
    assert target.download_failure_reason is None


@pytest.mark.parametrize('collection', [
    'Veep (2012) Season 1-7 S01-S07 (1080p BluRay x265 HEVC 10bit AAC 5.1 Silence)',
    'Season1-7', 'S01-S07', 'Season1 Season2',
])
def test_multi_season_collection_parent_does_not_override_explicit_single_episode(collection):
    content = srt(SIMPLIFIED)
    name = collection + '/Veep (2012) - S01E01 - Fundraiser (1080p BluRay x265 Silence).srt'
    assert strict_extract(archive({name: content}), subtitle()) == (content, 'srt')


def test_multi_season_collection_still_cannot_supply_a_missing_season_or_override_single_season_parent():
    for name in ('Season1-7/01.srt', 'Season1-7/S02/S01E01.srt', 'Season1-7/S01E01-E02.srt'):
        assert strict_extract(archive({name: srt(SIMPLIFIED)}), subtitle()) == (None, None)


def test_nested_multi_season_collection_container_needs_complete_inner_episode_identity():
    content = srt(SIMPLIFIED)
    assert strict_extract(archive({'Season 1-7.zip': archive({'S01E01.chs.srt': content})}),
                          subtitle()) == (content, 'srt')
    assert strict_extract(archive({'Season 1-7.zip': archive({'01.chs.srt': content})}),
                          subtitle()) == (None, None)


@pytest.mark.parametrize('outer_name', ['Season1/S01E01.zip', 'Show.S01E01.zip', 'Season1/E01.zip'])
def test_explicit_episode_archive_passes_identity_to_generic_inner_member(outer_name, monkeypatch):
    content = srt(SIMPLIFIED)
    reads = zip_reads(monkeypatch)
    target = subtitle()
    payload = archive({outer_name: archive({'generic.chs.srt': content})})
    assert strict_extract(payload, target) == (content, 'srt')
    assert target.selected_archive_member == outer_name + '/generic.chs.srt'
    assert reads == [outer_name, 'generic.chs.srt']


@pytest.mark.parametrize('filename', [
    'Season1/generic.chs.srt', 'E01.srt', '01.srt', 'generic.chs.srt',
    'Season2/01.srt', 'S02E01.srt', 'S01E02.srt',
    'Season2/S01E01.srt', 'S01/Season2/E01.srt',
    'S01E01E02.srt', 'S01E01-E02.srt', 'S01E01.S01E02.srt',
    '1x01-1x02.srt', 'Season1/E01-E02.srt', 'Season1/01-02.srt',
])
def test_missing_wrong_or_ambiguous_identity_is_skipped_without_opening_members(filename, monkeypatch):
    payload = archive({filename: srt(SIMPLIFIED)})
    reads = zip_reads(monkeypatch)
    target = subtitle()
    assert strict_extract(payload, target) == (None, None)
    assert target.selected_archive_member is None
    assert reads == []


@pytest.mark.parametrize('outer_name', ['Season2/S01E01.zip', 'S01E01E02.zip', 'S01E01-E02.zip'])
def test_wrong_or_ambiguous_inner_archive_is_not_opened(outer_name, monkeypatch):
    payload = archive({outer_name: archive({'generic.chs.srt': srt(SIMPLIFIED)})})
    reads = zip_reads(monkeypatch)
    assert strict_extract(payload, subtitle()) == (None, None)
    assert reads == []


@pytest.mark.parametrize('outer_name', ['Season1/generic.zip', 'E01.zip', 'generic.zip'])
def test_generic_container_can_be_probed_but_does_not_supply_missing_identity(outer_name, monkeypatch):
    payload = archive({outer_name: archive({'generic.chs.srt': srt(SIMPLIFIED)})})
    reads = zip_reads(monkeypatch)
    assert strict_extract(payload, subtitle()) == (None, None)
    assert reads == [outer_name]


@pytest.mark.parametrize('outer_name,inner_name', [('Season1/generic.zip', 'E01.chs.srt'),
                                                ('E01.zip', 'Season1/generic.chs.srt'),
                                                ('generic.zip', 'S01E01.chs.srt')])
def test_partial_identity_container_can_find_complete_explicit_identity_inside(outer_name, inner_name):
    content = srt(SIMPLIFIED)
    target = subtitle()
    assert strict_extract(archive({outer_name: archive({inner_name: content})}), target) == (content, 'srt')
    assert target.selected_archive_member == outer_name + '/' + inner_name


@pytest.mark.parametrize('season,episode', [(None, 1), (1, None), (-1, 1), (1, -1),
                                         (True, 1), (1, False), (1.0, 1), (1, '1')])
def test_strict_request_requires_explicit_integer_target_season_and_episode(season, episode, monkeypatch):
    target = subtitle()
    target.video.season, target.video.episode = season, episode
    payload = archive({'S01E01.chs.srt': srt(SIMPLIFIED)})
    reads = zip_reads(monkeypatch)
    assert strict_extract(payload, target) == (None, None)
    assert reads == []


@pytest.mark.parametrize('season,episode,filename', [(0, 1, 'S00E01.chs.srt'), (1, 0, 'S01E00.chs.srt')])
def test_zero_season_or_episode_is_usable_only_when_explicitly_named(season, episode, filename):
    content = srt(SIMPLIFIED)
    assert strict_extract(archive({filename: content}), subtitle(season, episode)) == (content, 'srt')


def test_strict_mode_does_not_guess_anime_absolute_episode_without_a_season(monkeypatch):
    target = subtitle(season=2, episode=5)
    target.video.absolute_episode = 12
    payload = archive({'E12.chs.srt': srt(SIMPLIFIED)})
    reads = zip_reads(monkeypatch)
    assert strict_extract(payload, target) == (None, None)
    assert reads == []


def test_raw_direct_download_without_member_identity_is_skipped_only_in_strict_mode():
    content = srt(SIMPLIFIED)
    assert strict_extract(content, subtitle()) == (None, None)
    assert subhd._extract_download(content, subtitle()) == (content, 'srt')


def test_same_episode_number_in_another_season_never_wins_or_gets_read(monkeypatch):
    season_one, season_two = srt(SIMPLIFIED + ' first season'), srt(SIMPLIFIED + ' second season')
    payload = archive({'Season2/01.chs.srt': season_two, 'Season1/01.chs.srt': season_one})
    reads = zip_reads(monkeypatch)
    assert strict_extract(payload, subtitle()) == (season_one, 'srt')
    assert reads == ['Season1/01.chs.srt']


def test_only_requested_episode_inner_archive_is_read(monkeypatch):
    requested = srt(SIMPLIFIED)
    payload = archive({
        'S01E02.zip': b'not an archive and must not be opened',
        'S02E01.zip': b'wrong season must not be opened',
        'S01E01.zip': archive({'generic.chs.srt': requested}),
    })
    reads = zip_reads(monkeypatch)
    assert strict_extract(payload, subtitle()) == (requested, 'srt')
    assert reads == ['S01E01.zip', 'generic.chs.srt']


def test_conflicting_inner_season_does_not_override_explicit_parent_identity(monkeypatch):
    payload = archive({'S01E01.zip': archive({'S02E01.chs.srt': srt(SIMPLIFIED)})})
    reads = zip_reads(monkeypatch)
    assert strict_extract(payload, subtitle()) == (None, None)
    assert reads == ['S01E01.zip']


def test_single_network_download_can_be_reused_by_two_independent_episode_copies(monkeypatch):
    first, second = srt(SIMPLIFIED + ' first episode'), srt(SIMPLIFIED + ' second episode')
    payload = archive({'Season1/01.chs.srt': first, 'Season1/02.chs.srt': second})
    provider, source = SubhdProvider(), subtitle()
    provider._request = Mock(side_effect=download_responses(payload))
    package = provider.download_archive(source)
    assert package == payload and provider._request.call_count == 4
    result = []
    for episode in (1, 2):
        target = copy.copy(source)
        target.video = copy.copy(source.video)
        target.video.episode = episode
        result.append(strict_extract(package, target))
        assert target.selected_archive_member == f'Season1/{episode:02d}.chs.srt'
        assert target.download_failure_reason is None
    assert result == [(first, 'srt'), (second, 'srt')]
    assert source.video.episode == 1 and source.content is None
    assert provider._request.call_count == 4
    assert provider._request.call_args_list[-1].kwargs['max_bytes'] == subhd._MAX_DOWNLOAD_BYTES


def test_archive_downloader_performs_no_subtitle_selection(monkeypatch):
    payload = archive({'Season1/01.chs.srt': srt(SIMPLIFIED)})
    provider = SubhdProvider()
    provider._request = Mock(side_effect=download_responses(payload))
    selection = Mock(side_effect=AssertionError('Archive downloader must not select an episode'))
    monkeypatch.setattr(subhd, '_extract_download', selection)
    assert provider.download_archive(subtitle()) == payload
    selection.assert_not_called()


def test_existing_single_download_reuses_the_archive_downloader(monkeypatch):
    content = srt(SIMPLIFIED)
    provider, target = SubhdProvider(), subtitle()
    provider.download_archive = Mock(return_value=archive({'S01E01.chs.srt': content}))
    provider._request = Mock(side_effect=AssertionError('Single download must reuse downloaded archive bytes'))
    provider.download_subtitle(target)
    provider.download_archive.assert_called_once_with(target)
    assert target.content == content and target.format == 'srt'
    assert target.download_failure_reason is None
    provider._request.assert_not_called()


@pytest.mark.parametrize('url', ['http://static.subhd.me/a.zip', 'https://evil.example/a.zip?token=PRIVATE-TOKEN',
                               'https://subhd.me.evil.example/a.zip', 'file:///tmp/private.zip'])
def test_archive_downloader_retries_and_rejects_untrusted_urls_without_fetching_them(url, caplog):
    provider, target = SubhdProvider(), subtitle()
    response = download_responses(b'never fetched', url=url)[:3]
    provider._request = Mock(side_effect=response * 3)
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match='^SubHD archive download failed$'):
        provider.download_archive(target)
    assert target.download_failure_reason == 'download_failed'
    assert provider._request.call_count == 9
    assert all(call.args[0] != url for call in provider._request.call_args_list)
    assert 'PRIVATE-TOKEN' not in caplog.text


def test_archive_downloader_network_failures_are_bounded_and_do_not_expose_error_credentials(caplog):
    provider, target = SubhdProvider(), subtitle()
    provider._request = Mock(side_effect=subprocess.CalledProcessError(
        22, ['curl', 'https://unsafe.example/?token=PRIVATE-TOKEN'], stderr=b'Cookie: session=PRIVATE-COOKIE'))
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match='^SubHD archive download failed$') as error:
        provider.download_archive(target)
    assert provider._request.call_count == 3
    assert target.download_failure_reason == 'download_failed'
    assert 'PRIVATE-TOKEN' not in str(error.value) + caplog.text
    assert 'PRIVATE-COOKIE' not in str(error.value) + caplog.text


def test_archive_download_limit_is_sixty_four_mib_and_rechecks_mocked_oversized_responses(monkeypatch):
    assert subhd._MAX_DOWNLOAD_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr(subhd, '_MAX_DOWNLOAD_BYTES', 100)
    provider, target = SubhdProvider(), subtitle()
    provider._request = Mock(side_effect=download_responses(b'x' * 101) * 3)
    with pytest.raises(ValueError, match='^SubHD archive download failed$'):
        provider.download_archive(target)
    assert target.download_failure_reason == 'download_failed'
    assert provider._request.call_count == 12
    assert all(call.kwargs['max_bytes'] == 100 for call in provider._request.call_args_list if call.args[0] == CDN_URL)


def test_bounded_curl_uses_output_file_and_max_filesize_for_success_without_content_length(monkeypatch):
    content, commands = b'mocked chunked response bytes', []

    def fake_run(command, **kwargs):
        commands.append(command)
        assert command[command.index('--max-filesize') + 1] == '100'
        assert '--output' in command
        Path(command[command.index('--output') + 1]).write_bytes(content)
        return SimpleNamespace(stdout=b'not the archive bytes', stderr=b'', returncode=0)

    monkeypatch.setattr(subhd.subprocess, 'run', fake_run)
    assert SubhdProvider._curl(CDN_URL, max_bytes=100) == content
    assert len(commands) == 1


def test_bounded_curl_stats_oversized_chunked_output_before_any_file_read(monkeypatch):
    original_open, original_path_open = builtins.open, Path.open
    output_path, reads = [], []

    def fake_run(command, **kwargs):
        path = command[command.index('--output') + 1]
        output_path.append(path)
        with original_open(path, 'wb') as output:
            output.write(b'x' * 101)
        return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)

    def guarded_open(path, mode='r', *args, **kwargs):
        if str(path) in output_path and ('r' in mode or '+' in mode):
            reads.append(str(path))
            raise AssertionError('An oversized archive must not be read')
        return original_open(path, mode, *args, **kwargs)

    def guarded_path_open(path, mode='r', *args, **kwargs):
        if str(path) in output_path and ('r' in mode or '+' in mode):
            reads.append(str(path))
            raise AssertionError('An oversized archive must not be read')
        return original_path_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(subhd.subprocess, 'run', fake_run)
    monkeypatch.setattr(builtins, 'open', guarded_open)
    monkeypatch.setattr(Path, 'open', guarded_path_open)
    with pytest.raises(ValueError):
        SubhdProvider._curl(CDN_URL, max_bytes=100)
    assert reads == []


def test_strict_selection_keeps_bilingual_priority_within_actual_requested_script():
    monolingual, bilingual = srt(SIMPLIFIED), srt(SIMPLIFIED + '\nEnglish dialogue here.')
    payload = archive({'Season1/01.CHS.srt': monolingual, 'Season1/01.CHS&ENG.srt': bilingual,
                       'Season1/01.CHT&ENG.srt': srt(TRADITIONAL + '\nEnglish dialogue here.')})
    target = subtitle()
    assert strict_extract(payload, target) == (bilingual, 'srt')
    assert target.selected_archive_member == 'Season1/01.CHS&ENG.srt'


def test_strict_episode_selection_preserves_coverage_filter_before_bilingual_filename_score():
    from test_subtitle_coverage import srt as whole_srt

    fragment = whole_srt(count=63, span=272, text=SIMPLIFIED + '\nEnglish dialogue.').encode('utf-8')
    complete = whole_srt(count=900, span=3500, text=SIMPLIFIED).encode('utf-8')
    payload = archive({'Season1/01.CHS&ENG.srt': fragment, 'Season1/01.CHS.srt': complete})
    target = subtitle()
    target.video.duration = 3600
    assert strict_extract(payload, target) == (complete, 'srt')
    assert target.selected_archive_member == 'Season1/01.CHS.srt'


def test_strict_episode_selection_preserves_script_inconclusive_instead_of_permanent_rejection():
    target = subtitle()
    payload = archive({'Season1/E01.A.srt': srt('你好 人生和平'),
                       'Season1/E01.B.srt': srt(TRADITIONAL)})
    assert strict_extract(payload, target) == (None, None)
    assert target.download_failure_reason == 'script_inconclusive'


def test_strict_member_size_is_checked_before_decompression(monkeypatch):
    content = srt(SIMPLIFIED)
    payload = archive({'S01E01.CHS.srt': content})
    reads = zip_reads(monkeypatch)
    monkeypatch.setattr(subhd, '_MAX_ARCHIVE_MEMBER_BYTES', len(content) - 1)
    assert strict_extract(payload, subtitle()) == (None, None)
    assert reads == []


def test_strict_nested_total_budget_is_checked_before_inner_member_decompression(monkeypatch):
    content = srt(SIMPLIFIED)
    inner = archive({'generic.chs.srt': content})
    payload = archive({'S01E01.zip': inner})
    reads = zip_reads(monkeypatch)
    monkeypatch.setattr(subhd, '_MAX_EXTRACTED_BYTES', len(inner) + len(content) - 1)
    assert strict_extract(payload, subtitle()) == (None, None)
    assert reads == ['S01E01.zip']


def test_strict_fourth_archive_level_is_not_read(monkeypatch):
    inner = archive({'generic.chs.srt': srt(SIMPLIFIED)})
    for _ in range(3):
        inner = archive({'S01E01.zip': inner})
    reads = zip_reads(monkeypatch)
    assert strict_extract(inner, subtitle()) == (None, None)
    assert reads == ['S01E01.zip', 'S01E01.zip']
