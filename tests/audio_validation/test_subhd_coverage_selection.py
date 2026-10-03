"""A high filename score cannot make a short subtitle win a full movie."""

import io
import logging
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from subliminal import Movie
from subzero.language import Language

from subliminal_patch.providers.subhd import SubhdProvider, SubhdSubtitle, _extract_download
from test_subhd_script_guard import SIMPLIFIED
from test_subtitle_coverage import lossy_srt, srt


CN = Language('zho', 'CN')


def archive(files):
    output = io.BytesIO()
    with ZipFile(output, 'w') as package:
        for name, content in files.items():
            package.writestr(name, content)
    return output.getvalue()


def subtitle(duration=7740, forced=False, partial=False):
    video = Movie('/tmp/Mind.Game.2004.mkv', 'Mind Game', year=2004)
    video.duration = duration
    language = Language.rebuild(CN, forced=forced)
    result = SubhdSubtitle(language, 'CoverageRawId', '/a/CoverageRawId', 'Mind Game 2004', video)
    result.is_partial = partial
    return result


def content(count=900, span=7700, bilingual=False, encoding='utf-8'):
    line = SIMPLIFIED + ('\nThis English line is part of the subtitle.' if bilingual else '')
    return srt(count=count, span=span, text=line).encode(encoding)


def test_short_high_score_bilingual_member_is_skipped_for_complete_monolingual_movie():
    short, complete = content(63, 272, bilingual=True), content()
    package = archive({'Mind.Game.CHS&ENG.srt': short, 'Mind.Game.CHS.srt': complete})
    assert _extract_download(package, subtitle()) == (complete, 'srt')


def test_complete_bilingual_member_still_wins_over_complete_monolingual_member():
    bilingual, monolingual = content(bilingual=True), content()
    package = archive({'Mind.Game.CHS.srt': monolingual, 'Mind.Game.CHS&ENG.srt': bilingual})
    assert _extract_download(package, subtitle()) == (bilingual, 'srt')


def test_full_monolingual_has_priority_over_limited_bilingual_before_filename_score():
    limited, complete = content(100, 3500, bilingual=True), content()
    package = archive({'Mind.Game.CHS&ENG.srt': limited, 'Mind.Game.CHS.srt': complete})
    assert _extract_download(package, subtitle()) == (complete, 'srt')


def test_only_limited_candidate_is_left_available_for_audio_validation():
    limited = content(100, 3500, bilingual=True)
    package = archive({'Mind.Game.CHS&ENG.srt': limited})
    assert _extract_download(package, subtitle()) == (limited, 'srt')


def test_within_limited_coverage_group_bilingual_priority_is_preserved():
    bilingual, monolingual = content(100, 3500, bilingual=True), content(100, 3500)
    package = archive({'Mind.Game.CHS.srt': monolingual, 'Mind.Game.CHS&ENG.srt': bilingual})
    assert _extract_download(package, subtitle()) == (bilingual, 'srt')


def test_nested_full_monolingual_has_priority_over_flat_limited_bilingual():
    limited, complete = content(100, 3500, bilingual=True), content()
    package = archive({'Mind.Game.CHS&ENG.srt': limited,
                       'Mind.Game.zip': archive({'CHS.srt': complete})})
    assert _extract_download(package, subtitle()) == (complete, 'srt')


@pytest.mark.parametrize('short_nested', [False, True])
def test_short_bilingual_member_cannot_win_across_flat_and_nested_packages(short_nested):
    short, complete = content(63, 272, bilingual=True), content()
    files = ({'Mind.Game.CHS&ENG.zip': archive({'CHS&ENG.srt': short}), 'Mind.Game.CHS.srt': complete}
             if short_nested else
             {'Mind.Game.CHS&ENG.srt': short, 'Mind.Game.CHS.zip': archive({'CHS.srt': complete})})
    assert _extract_download(archive(files), subtitle()) == (complete, 'srt')


def test_nested_member_coverage_is_checked_before_bilingual_priority():
    short, complete = content(63, 272, bilingual=True), content()
    package = archive({'Mind.Game.zip': archive({'CHS&ENG.srt': short, 'CHS.srt': complete})})
    assert _extract_download(package, subtitle()) == (complete, 'srt')


def test_complete_nested_bilingual_retains_priority_over_flat_complete_monolingual():
    bilingual, monolingual = content(bilingual=True), content()
    package = archive({'Mind.Game.CHS.srt': monolingual,
                       'Mind.Game.zip': archive({'CHS&ENG.srt': bilingual})})
    assert _extract_download(package, subtitle()) == (bilingual, 'srt')


@pytest.mark.parametrize('nested', [False, True])
def test_package_with_only_short_members_returns_no_subtitle(nested):
    package = archive({'Mind.Game.CHS&ENG.srt': content(63, 272, bilingual=True)})
    if nested:
        package = archive({'Mind.Game.zip': package})
    assert _extract_download(package, subtitle()) == (None, None)


@pytest.mark.parametrize('duration,count,span', [(180, 20, 160), (30, 12, 24)])
def test_complete_short_drama_and_clip_downloads_are_not_rejected_by_a_minute_floor(duration, count, span):
    original = content(count, span)
    assert _extract_download(archive({'Mind.Game.CHS.srt': original}), subtitle(duration)) == (original, 'srt')


@pytest.mark.parametrize('duration', [None, 0])
def test_unknown_duration_keeps_valid_legacy_direct_and_archive_downloads(duration):
    original = content(63, 272)
    target = subtitle(duration)
    assert _extract_download(original, target) == (original, 'srt')
    assert _extract_download(archive({'Mind.Game.CHS.srt': original}), target) == (original, 'srt')


def test_forced_and_explicit_partial_candidates_may_be_short_but_keep_format_and_script_guards():
    original = content(63, 272)
    for target in (subtitle(forced=True), subtitle(partial=True)):
        assert _extract_download(original, target) == (original, 'srt')
        assert _extract_download(archive({'Mind.Game.CHS.srt': original}), target) == (original, 'srt')
        assert _extract_download(b'<html>Failed download</html>', target) == (None, None)
        english = srt(count=63, span=272, text='English without Chinese dialogue.').encode('utf-8')
        assert _extract_download(english, target) == (None, None)


def test_release_text_alone_cannot_enable_a_partial_subtitle_exception():
    target = subtitle()
    target.release_info = 'Mind Game 2004 trailer sample partial'
    assert _extract_download(content(63, 272), target) == (None, None)


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16', 'cp936'])
def test_complete_content_is_decoded_for_coverage_without_truncating_or_rewriting_it(encoding):
    original = content(encoding=encoding)
    result, format_name = _extract_download(original, subtitle())
    assert result == original
    assert format_name == 'srt'
    assert result.decode(encoding).count('-->') == 900


def test_parser_loss_member_is_skipped_even_if_it_spans_the_movie():
    lossy = lossy_srt(span=7700, text=SIMPLIFIED + '\nEnglish dialogue').encode('utf-8')
    complete = content()
    package = archive({'Mind.Game.CHS&ENG.srt': lossy, 'Mind.Game.CHS.srt': complete})
    assert _extract_download(package, subtitle()) == (complete, 'srt')


def test_sparse_edge_only_bilingual_member_is_skipped_for_distributed_dialogue():
    sparse = content(count=2, span=7700, bilingual=True)
    complete = content()
    package = archive({'Mind.Game.CHS&ENG.srt': sparse, 'Mind.Game.CHS.srt': complete})
    assert _extract_download(package, subtitle()) == (complete, 'srt')


def test_candidate_diagnostics_are_info_level_and_never_log_subtitle_body(caplog):
    short, complete = content(63, 272, bilingual=True), content()
    package = archive({'Mind.Game.CHS&ENG.srt': short, 'Mind.Game.CHS.srt': complete})
    with caplog.at_level(logging.INFO):
        assert _extract_download(package, subtitle()) == (complete, 'srt')
    diagnostics = '\n'.join(record.getMessage() for record in caplog.records if record.levelno == logging.INFO)
    assert 'BAZARR SubHD subtitle member' in diagnostics
    assert 'Mind.Game.CHS&ENG.srt' in diagnostics
    assert 'Mind.Game.CHS.srt' in diagnostics
    for field in ('raw_bytes', 'encoding', 'format', 'total_cues', 'dialogue_cues',
                  'declared_cues', 'parse_ratio', 'first_seconds', 'last_seconds',
                  'span_seconds', 'span_ratio', 'occupied_bins', 'accepted', 'reason'):
        assert field in diagnostics, field
    assert SIMPLIFIED not in diagnostics
    assert 'This English line is part of the subtitle.' not in diagnostics


def test_download_logs_final_member_and_identity_without_signed_url_or_session_secrets(caplog):
    target = subtitle()
    target.page_link = 'https://subhd.me/a/CoverageRawId'
    complete = content()
    package = archive({'Mind.Game.CHS&ENG.srt': content(63, 272, bilingual=True),
                       'Mind.Game.CHS.srt': complete})
    signed_url = 'https://static.subhd.me/movie.zip?token=TEST_SECRET_DO_NOT_LOG&signature=SIGNATURE_DO_NOT_LOG'
    provider = SubhdProvider()
    provider._request = Mock(side_effect=[
        b'{"success":true}', b'download page',
        ('{"success":true,"url":"' + signed_url + '"}').encode('utf-8'), package,
    ])
    with caplog.at_level(logging.INFO):
        provider.download_subtitle(target)
    assert target.content == complete
    assert provider._request.call_count == 4
    logs = '\n'.join(record.getMessage() for record in caplog.records)
    selected = [record.getMessage() for record in caplog.records
                if record.levelno == logging.INFO and 'selected' in record.getMessage().lower()]
    assert selected
    final_selection = selected[-1]
    assert 'BAZARR SubHD subtitle selection' in final_selection
    assert 'Mind.Game.CHS.srt' in final_selection
    assert 'CoverageRawId' in final_selection
    assert 'subhd' in final_selection.lower()
    assert 'zh-CN' in final_selection
    assert 'time' in final_selection.lower() or 'elapsed' in final_selection.lower()
    assert SIMPLIFIED not in logs
    assert signed_url not in logs
    assert 'TEST_SECRET_DO_NOT_LOG' not in logs
    assert 'SIGNATURE_DO_NOT_LOG' not in logs


@pytest.mark.parametrize('packaged', [False, True])
def test_stale_cached_utf8_state_does_not_control_new_gbk_member_decoding(packaged, caplog):
    original = content(encoding='cp936')
    target = subtitle()
    target.encoding = target._guessed_encoding = 'utf-8'
    target._is_valid = True
    incoming = archive({'Mind.Game.CHS.srt': original}) if packaged else original
    with caplog.at_level(logging.INFO):
        assert _extract_download(incoming, target) == (original, 'srt')
    records = '\n'.join(record.getMessage() for record in caplog.records)
    assert '"encoding": "cp936"' in records


def test_successful_redownload_resets_target_encoding_and_validation_before_using_new_bytes():
    target = subtitle()
    target.content = b'old cached content'
    target.encoding = target._guessed_encoding = 'utf-8'
    target._is_valid = True
    complete = content(encoding='cp936')
    provider = SubhdProvider()
    provider._request = Mock(side_effect=[
        b'{"success":true}', b'download page',
        b'{"success":true,"url":"https://static.subhd.me/new.zip"}',
        archive({'Mind.Game.CHS.srt': complete}),
    ])
    target.page_link = 'https://subhd.me/a/CoverageRawId'
    provider.download_subtitle(target)
    assert target.content == complete
    assert target.encoding is target._guessed_encoding is None
    assert not target._is_valid
    assert target.selected_archive_member == 'Mind.Game.CHS.srt'
    assert target.download_failure_reason is None
    assert SIMPLIFIED in target.text
    assert target.text.count('-->') == 900


def test_rejected_package_keeps_the_coverage_reason_and_clears_stale_success_state():
    target = subtitle()
    target.content = b'old cached content'
    target._is_valid = True
    target.selected_archive_member = 'previous-member.srt'
    target.page_link = 'https://subhd.me/a/CoverageRawId'
    provider = SubhdProvider()
    provider._request = Mock(side_effect=[
        b'{"success":true}', b'download page',
        b'{"success":true,"url":"https://static.subhd.me/short.zip"}',
        archive({'Mind.Game.CHS&ENG.srt': content(63, 272, bilingual=True)}),
    ])
    provider.download_subtitle(target)
    assert target.content is None
    assert not target._is_valid
    assert target.selected_archive_member is None
    assert target.download_failure_reason == 'obvious_fragment'
    assert provider._request.call_count == 4


def test_retry_failures_log_safe_stage_and_exit_code_without_the_exception_command(caplog):
    import subprocess

    target = subtitle()
    target.page_link = 'https://subhd.me/a/CoverageRawId'
    signed_url = 'https://static.subhd.me/file.zip?token=RETRY_TOKEN_DO_NOT_LOG&signature=RETRY_SIG_DO_NOT_LOG'
    error = subprocess.CalledProcessError(28, ['curl', signed_url], stderr=b'COOKIE_DO_NOT_LOG')
    sequence = [b'{"success":true}', b'download page',
                ('{"success":true,"url":"' + signed_url + '"}').encode(), error]
    provider = SubhdProvider()
    provider._request = Mock(side_effect=sequence * 3)
    with caplog.at_level(logging.DEBUG):
        provider.download_subtitle(target)
    failures = [record for record in caplog.records if 'download attempt failed' in record.getMessage()]
    assert len(failures) == 3
    assert all(record.levelno == logging.WARNING for record in failures)
    assert all('"stage": "fetch_package"' in record.getMessage() and
               '"exit_code": 28' in record.getMessage() for record in failures)
    logs = '\n'.join(record.getMessage() for record in caplog.records)
    assert signed_url not in logs
    assert 'RETRY_TOKEN_DO_NOT_LOG' not in logs
    assert 'RETRY_SIG_DO_NOT_LOG' not in logs
    assert 'COOKIE_DO_NOT_LOG' not in logs
    assert target.download_failure_reason == 'download_failed'
    assert target.content is None
