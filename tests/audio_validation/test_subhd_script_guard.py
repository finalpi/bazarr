import io
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from subliminal import Episode
from subzero.language import Language

from subliminal_patch.providers import subhd as subhd_module
from subliminal_patch.providers.subhd import SubhdProvider, SubhdSubtitle, _extract_download


CN = Language('zho', 'CN')
TW = Language('zho', 'TW')
ZH = Language('zho')
SIMPLIFIED = '这次我们说的话会让他们觉得很好 请给我听听'
TRADITIONAL = '這次我們說的話會讓他們覺得很好 請給我聽聽'


def video():
    return Episode('/tmp/Show.S02E05.mkv', 'Show', 2, 5)


def subtitle(language=CN, tags=()):
    result = SubhdSubtitle(language, 'SiteId', '/a/SiteId', 'Show S02', video())
    result.subtitle_tags = list(tags)
    return result


def srt(text):
    return ('1\n00:00:01,000 --> 00:00:02,000\n' + text + '\n').encode('utf-8')


def archive(files):
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w') as output:
        for name, content in files.items():
            output.writestr(name, content)
    return buffer.getvalue()


def row(tags):
    badges = ''.join('<span class="p-1 fw-bold">%s</span>' % tag for tag in tags)
    return ('<div class="row"><a href="/a/SiteId">Show S02</a>'
            '<div class="text-truncate py-2 f11">%s</div></div>' % badges).encode('utf-8')


@pytest.mark.parametrize('tags,expected', [
    (['简体', '英语', 'ASS'], ['zh-CN']),
    (['繁體', '英語', 'SRT'], ['zh-TW']),
    (['简体', '繁体', '英语', '双语'], ['zh-CN', 'zh-TW']),
    (['英语', 'SRT'], []), (['日语', 'ASS'], []), (['俄语', 'SRT'], []),
    (['双语', '英语', 'SRT'], ['zh-CN', 'zh-TW']), ([], ['zh-CN', 'zh-TW']),
])
def test_site_language_tags_filter_requested_languages_and_keep_all_tags(tags, expected):
    provider = SubhdProvider()
    provider._request = Mock(return_value=row(tags))
    candidates = provider.list_subtitles(video(), {TW, CN})
    assert [item.language.basename for item in candidates] == expected
    assert all(item.id == item.subtitle_id == 'SiteId' for item in candidates)
    assert all(item.subtitle_tags == tags for item in candidates)


def test_real_generic_chinese_profile_can_request_simplified_without_a_country_suffix():
    assert ZH in SubhdProvider.languages
    provider = SubhdProvider()
    provider._request = Mock(return_value=row(['简体', '英语']))
    result = provider.list_subtitles(video(), {ZH, TW})
    assert [item.language.basename for item in result] == ['zh']
    assert result[0].language.country is None


def test_candidate_language_and_flags_are_isolated_from_other_candidates_and_callers():
    cn = Language.rebuild(CN, forced=True, hi=True)
    tw = Language.rebuild(TW, forced=True, hi=True)
    provider = SubhdProvider()
    provider._request = Mock(return_value=row(['简体', '繁体', '双语']))
    candidates = provider.list_subtitles(video(), {tw, cn})
    assert [item.language.basename for item in candidates] == ['zh-CN', 'zh-TW']
    assert all(item.language.forced and item.language.hi for item in candidates)
    candidates[0].language.hi = False
    candidates[0].language.forced = False
    assert candidates[1].language.hi and candidates[1].language.forced
    assert cn.hi and cn.forced and tw.hi and tw.forced


@pytest.mark.parametrize('language,filename,text', [
    (TW, 'show.S02E05.CHS.srt', SIMPLIFIED),
    (TW, 'show.S02E05.CHS&ENG.srt', SIMPLIFIED + '\nEnglish'),
    (CN, 'show.S02E05.CHT.srt', TRADITIONAL),
    (CN, 'show.S02E05.CHT&ENG.srt', TRADITIONAL + '\nEnglish'),
])
def test_wrong_script_members_are_rejected_in_flat_and_nested_archives_without_changing_target(
        language, filename, text):
    for payload in (archive({filename: srt(text)}),
                    archive({'show.S02E05.zip': archive({filename: srt(text)})})):
        target = subtitle(language)
        original = target.language.basename
        assert _extract_download(payload, target) == (None, None)
        assert target.language.basename == original


@pytest.mark.parametrize('language,matching,opposite,matching_text,opposite_text', [
    (CN, 'CHS', 'CHT', SIMPLIFIED, TRADITIONAL),
    (TW, 'CHT', 'CHS', TRADITIONAL, SIMPLIFIED),
])
def test_bilingual_priority_is_applied_only_within_the_requested_script(
        language, matching, opposite, matching_text, opposite_text):
    files = {
        'show.S02E05.%s.srt' % matching: srt(matching_text),
        'show.S02E05.%s&ENG.srt' % opposite: srt(opposite_text + '\nEnglish'),
    }
    assert _extract_download(archive(files), subtitle(language)) == (srt(matching_text), 'srt')
    files['show.S02E05.%s&ENG.srt' % matching] = srt(matching_text + '\nEnglish')
    assert _extract_download(archive(files), subtitle(language)) == (srt(matching_text + '\nEnglish'), 'srt')


@pytest.mark.parametrize('filename', ['show.S02E05.ENG.srt', 'show.S02E05.CHS.srt', 'show.S02E05.srt'])
def test_english_only_dialogue_is_never_saved_as_chinese_even_with_wrong_tags_or_filename(filename):
    for language in (CN, TW):
        assert _extract_download(archive({filename: srt('This is an English-only subtitle.')}),
                                 subtitle(language, ['简体', '繁体', '英语'])) == (None, None)


def test_strong_content_script_contradiction_is_rejected_and_matching_content_is_unchanged():
    assert _extract_download(archive({'show.S02E05.CHS.srt': srt(TRADITIONAL)}), subtitle(CN)) == (None, None)
    assert _extract_download(archive({'show.S02E05.CHT.srt': srt(SIMPLIFIED)}), subtitle(TW)) == (None, None)
    original = srt(SIMPLIFIED + '\nEnglish dialogue')
    target = subtitle(CN)
    assert _extract_download(archive({'show.S02E05.CHS&ENG.srt': original}), target) == (original, 'srt')
    assert target.language.basename == 'zh-CN'


def test_small_opposite_script_logo_does_not_override_strong_dialogue_script():
    original = srt((SIMPLIFIED + '\n') * 20 + '【這】')
    assert _extract_download(archive({'show.S02E05.CHS.srt': original}), subtitle(CN)) == (original, 'srt')
    assert _extract_download(archive({'show.S02E05.CHT.srt': original}), subtitle(TW)) == (None, None)


@pytest.mark.parametrize('language,tags,expected', [
    (CN, [], False), (TW, [], False),
    (CN, ['简体'], True), (TW, ['简体'], False),
    (TW, ['繁体'], True), (CN, ['繁体'], False),
    (CN, ['简体', '繁体'], False), (TW, ['双语'], False),
])
def test_shared_han_characters_need_unambiguous_single_script_page_evidence(language, tags, expected):
    payload = archive({'show.S02E05.srt': srt('你好 人生和平')})
    result = _extract_download(payload, subtitle(language, tags))
    assert bool(result[0]) is expected


@pytest.mark.parametrize('language,text', [(CN, SIMPLIFIED), (TW, TRADITIONAL)])
def test_unknown_page_and_unmarked_filename_can_use_strong_actual_script_evidence(language, text):
    payload = archive({'show.S02E05.srt': srt(text)})
    assert _extract_download(payload, subtitle(language)) == (srt(text), 'srt')


def test_mixed_script_and_japanese_only_unknown_members_are_not_assumed_to_match():
    mixed = srt(SIMPLIFIED + '\n' + TRADITIONAL)
    japanese = srt('これは日本語の字幕です みんなここに来てください')
    for language in (CN, TW):
        assert _extract_download(archive({'show.S02E05.srt': mixed}), subtitle(language)) == (None, None)
        assert _extract_download(archive({'show.S02E05.srt': japanese}), subtitle(language)) == (None, None)


def test_direct_download_also_requires_real_script_evidence_and_does_not_mutate_text_or_language():
    target = subtitle(TW)
    assert _extract_download(srt(SIMPLIFIED), target) == (None, None)
    original = srt(TRADITIONAL)
    assert _extract_download(original, target) == (original, 'srt')
    assert target.language.basename == 'zh-TW'


def test_downloaded_wrong_script_package_is_rejected_without_refetching_it():
    target = subtitle(TW)
    target.page_link = 'https://subhd.me/a/SiteId'
    payload = archive({'show.S02E05.CHS.srt': srt(SIMPLIFIED)})
    provider = SubhdProvider()
    provider._request = Mock(side_effect=[
        b'{"success":true}', b'download page',
        b'{"success":true,"url":"https://static.subhd.me/test-package.zip"}', payload,
    ])
    provider.download_subtitle(target)
    assert target.content is None
    assert target.language.basename == 'zh-TW'
    assert provider._request.call_count == 4


def test_direct_chinese_utf8_format_probe_can_end_inside_a_multibyte_character():
    text = SIMPLIFIED + '文' * 3100
    payload = srt(text)
    while True:
        try:
            payload[:8192].decode('utf-8')
        except UnicodeDecodeError:
            break
        text = ' ' + text
        payload = srt(text)
    assert _extract_download(payload, subtitle(ZH)) == (payload, 'srt')


@pytest.mark.parametrize('language,text,encoding', [(ZH, SIMPLIFIED, 'cp936'), (TW, TRADITIONAL, 'cp950')])
def test_direct_legacy_chinese_encoding_still_detects_srt_before_script_validation(language, text, encoding):
    payload = srt(text).decode('utf-8').encode(encoding)
    assert _extract_download(payload, subtitle(language)) == (payload, 'srt')


@pytest.mark.parametrize('language,tags,text,filename,expected', [
    (CN, [], '你好 人生和平', 'show.S02E05.srt', 'script_inconclusive'),
    (TW, [], '你好 人生和平', 'show.S02E05.srt', 'script_inconclusive'),
    (CN, ['简体', '繁体'], '你好 人生和平', 'show.S02E05.srt', 'script_inconclusive'),
    (TW, ['简体', '繁体'], '你好 人生和平', 'show.S02E05.srt', 'script_inconclusive'),
    (CN, ['双语', '英语'], '你好 人生和平', 'show.S02E05.srt', 'script_inconclusive'),
    (CN, ['英语'], '你好 人生和平', 'show.S02E05.srt', 'script_inconclusive'),
    (CN, ['简体'], SIMPLIFIED + '\n' + TRADITIONAL, 'show.S02E05.CHS.srt', 'script_inconclusive'),
    (TW, ['繁体'], SIMPLIFIED + '\n' + TRADITIONAL, 'show.S02E05.CHT.srt', 'script_inconclusive'),
    (CN, [], TRADITIONAL, 'show.S02E05.srt', 'script_mismatch'),
    (TW, [], SIMPLIFIED, 'show.S02E05.srt', 'script_mismatch'),
    (CN, ['简体'], TRADITIONAL, 'show.S02E05.CHS.srt', 'script_mismatch'),
    (TW, ['繁体'], SIMPLIFIED, 'show.S02E05.CHT.srt', 'script_mismatch'),
    (CN, ['简体', '繁体'], 'A clearly English subtitle without Chinese dialogue.',
     'show.S02E05.srt', 'script_mismatch'),
    (TW, [], 'これは日本語の字幕です みんなここに来てください',
     'show.S02E05.srt', 'script_mismatch'),
    (CN, [], SIMPLIFIED, 'show.S02E05.srt', 'script_match'),
    (TW, [], TRADITIONAL, 'show.S02E05.srt', 'script_match'),
    (CN, ['简体'], '你好 人生和平', 'show.S02E05.srt', 'script_match'),
    (TW, ['繁体'], '你好 人生和平', 'show.S02E05.srt', 'script_match'),
])
def test_actual_member_script_verdict_distinguishes_uncertainty_from_proven_conflict(
        language, tags, text, filename, expected):
    target = subtitle(language, tags)
    candidate = subtitle(language, tags)
    candidate.content = srt(text)
    candidate.use_original_format = True
    candidate.format = 'srt'
    result = subhd_module._member_script_verdict(filename, candidate, target)
    assert result == {'accepted': expected == 'script_match', 'reason': expected}
    assert subhd_module._member_script_allowed(filename, candidate, target) is (expected == 'script_match')


@pytest.mark.parametrize('language,tags,text', [
    (CN, [], '你好 人生和平'),
    (TW, ['简体', '繁体'], '你好 人生和平'),
    (CN, [], SIMPLIFIED + '\n' + TRADITIONAL),
    (TW, [], SIMPLIFIED + '\n' + TRADITIONAL),
])
@pytest.mark.parametrize('packaged', [False, True])
def test_an_unknown_valid_member_sets_nonpersistent_inconclusive_failure_reason(language, tags, text, packaged):
    target = subtitle(language, tags)
    payload = srt(text)
    if packaged:
        payload = archive({'show.S02E05.srt': payload})
    assert _extract_download(payload, target) == (None, None)
    assert target.download_failure_reason == 'script_inconclusive'
    assert target.selected_archive_member is None


@pytest.mark.parametrize('language,opposite', [(CN, TRADITIONAL), (TW, SIMPLIFIED)])
@pytest.mark.parametrize('unknown_first', [True, False])
@pytest.mark.parametrize('layout', ['flat', 'inner', 'two_inner', 'unknown_inner', 'opposite_inner'])
def test_any_valid_unknown_member_prevents_permanent_script_mismatch_for_the_package(
        language, opposite, unknown_first, layout):
    unknown_name, opposite_name = 'show.S02E05.A.srt', 'show.S02E05.B.srt'
    entries = [(unknown_name, srt('你好 人生和平')), (opposite_name, srt(opposite))]
    if not unknown_first:
        entries.reverse()
    if layout == 'flat':
        payload = archive(dict(entries))
    elif layout == 'inner':
        payload = archive({'show.S02E05.zip': archive(dict(entries))})
    elif layout == 'two_inner':
        payload = archive({name.replace('.srt', '.zip'): archive({name: content}) for name, content in entries})
    else:
        nested_name = unknown_name if layout == 'unknown_inner' else opposite_name
        payload = archive({name.replace('.srt', '.zip') if name == nested_name else name:
                           archive({name: content}) if name == nested_name else content
                           for name, content in entries})
    target = subtitle(language, ['简体', '繁体'])
    assert _extract_download(payload, target) == (None, None)
    assert target.download_failure_reason == 'script_inconclusive'
    assert target.selected_archive_member is None


@pytest.mark.parametrize('language,opposite', [(CN, TRADITIONAL), (TW, SIMPLIFIED)])
@pytest.mark.parametrize('nested', [False, True])
def test_only_proven_opposite_script_members_still_report_mismatch(language, opposite, nested):
    payload = archive({'show.S02E05.A.srt': srt(opposite), 'show.S02E05.B.srt': srt(opposite)})
    if nested:
        payload = archive({'show.S02E05.zip': payload})
    target = subtitle(language)
    assert _extract_download(payload, target) == (None, None)
    assert target.download_failure_reason == 'script_mismatch'


@pytest.mark.parametrize('language,matching,opposite', [(CN, SIMPLIFIED, TRADITIONAL),
                                                     (TW, TRADITIONAL, SIMPLIFIED)])
def test_successful_matching_member_clears_previous_failure_even_after_uncertain_or_opposite_members(
        language, matching, opposite):
    target = subtitle(language, ['简体', '繁体'])
    target.download_failure_reason = 'script_mismatch'
    payload = archive({'show.S02E05.A.srt': srt('你好 人生和平'),
                       'show.S02E05.B.srt': srt(opposite),
                       'show.S02E05.C.srt': srt(matching)})
    assert _extract_download(payload, target) == (srt(matching), 'srt')
    assert target.download_failure_reason is None
    assert target.selected_archive_member == 'show.S02E05.C.srt'


@pytest.mark.parametrize('failure', ['obvious_fragment', 'parse_loss'])
@pytest.mark.parametrize('nested', [False, True])
def test_valid_unknown_script_member_prevents_other_member_coverage_failures_from_sealing_package(
        failure, nested):
    from test_subtitle_coverage import lossy_srt, srt as complete_srt

    unknown = complete_srt(count=100, span=3500, text='你好 人生和平').encode('utf-8')
    rejected = (complete_srt(count=63, span=272, text=SIMPLIFIED) if failure == 'obvious_fragment'
                else lossy_srt(parsed_count=7, declared_count=10, span=3500, text=SIMPLIFIED)).encode('utf-8')
    payload = archive({'show.S02E05.A.srt': rejected, 'show.S02E05.B.srt': unknown})
    if nested:
        payload = archive({'show.S02E05.zip': payload})
    target = subtitle(CN, ['简体', '繁体'])
    target.video.duration = 3600
    assert _extract_download(payload, target) == (None, None)
    assert target.download_failure_reason == 'script_inconclusive'
    assert target.selected_archive_member is None
