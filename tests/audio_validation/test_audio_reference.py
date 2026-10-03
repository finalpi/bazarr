import copy
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'libs'))
spec = importlib.util.spec_from_file_location('audio_reference', ROOT / 'bazarr/subtitles/audio_reference.py')
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
select_original_audio_stream = reference.select_original_audio_stream


def audio(index, language=None, title='', **disposition):
    tags = {'title': title}
    if language is not None:
        tags['language'] = language
    return {'index': index, 'codec_type': 'audio', 'tags': tags, 'disposition': disposition}


def test_the_game_remux_selects_global_11_relative_audio_10_and_avoids_commentaries():
    streams = [{'index': 0, 'codec_type': 'video'}]
    streams += [audio(index, 'rus', 'Russian MVO', default=index == 1) for index in range(1, 11)]
    streams += [
        audio(11, 'eng', 'English Original DTS-HD MA'),
        audio(12, 'eng', 'English AC3', default=True),
        audio(13, 'eng', 'Original Director Commentary', default=True),
        audio(14, 'eng', 'Original English Audio Description', default=True),
        {'index': 17, 'codec_type': 'subtitle', 'tags': {'language': 'eng', 'title': 'Full'}},
    ]
    before = copy.deepcopy(streams)
    result = select_original_audio_stream(streams, 'English')
    assert result == {
        'audio_index': 10, 'stream_index': 11, 'language': 'eng',
        'title': 'English Original DTS-HD MA', 'reason': 'original-language-original-title',
    }
    assert streams == before


@pytest.mark.parametrize('original_language,expected', [
    ('Japanese', (1, 2, 'jpn')), ('English', (2, 5, 'eng'))])
def test_original_language_overrides_default_foreign_audio_and_indices_follow_actual_stream_order(
        original_language, expected):
    streams = [
        {'index': 0, 'codec_type': 'video'}, audio(1, 'rus', 'Russian', default=True),
        audio(2, 'jpn', 'Japanese Original'), {'index': 3, 'codec_type': 'subtitle'},
        audio(5, 'eng', 'English Original'),
    ]
    for ordered in (streams, list(reversed(streams))):
        result = select_original_audio_stream(ordered, original_language)
        assert (result['audio_index'], result['stream_index'], result['language']) == expected


@pytest.mark.parametrize('original_language,label,expected', [
    ('en', 'eng', 'eng'), ('eng', 'en', 'eng'), ('en-US', 'ENG', 'eng'),
    ('英语', 'eng', 'eng'), ('英語', 'English', 'eng'),
    ('ja', 'jpn', 'jpn'), ('日语', 'ja', 'jpn'), ('日語', 'jpn', 'jpn'),
    ('Chinese', 'chi', 'zho'), ('中文', 'zh-CN', 'zho'), ('普通话', 'cmn', 'zho'),
    ('French', 'fre', 'fra'), ('德语', 'ger', 'deu'), ('俄语', 'ru', 'rus'),
    ('粤语', 'yue', 'yue'), ('Cantonese', 'yue', 'yue'),
    ({'name': 'English'}, 'eng', 'eng'),
])
def test_language_names_iso_codes_regions_and_chinese_names_identify_the_same_original_language(
        original_language, label, expected):
    result = select_original_audio_stream([audio(4, label, 'Original')], original_language)
    assert result['language'] == expected
    assert result['stream_index'] == 4 and result['audio_index'] == 0


def test_matching_original_title_precedes_default_and_default_breaks_otherwise_equal_matches():
    streams = [audio(2, 'eng', 'English AC3', default=True), audio(3, 'eng', '原音 DTS')]
    assert select_original_audio_stream(streams, 'English')['stream_index'] == 3
    streams[1]['tags']['title'] = 'English DTS'
    assert select_original_audio_stream(streams, 'English')['stream_index'] == 2
    streams[0]['disposition']['default'] = False
    assert select_original_audio_stream(streams, 'English')['stream_index'] == 2


@pytest.mark.parametrize('original_language', ['Chinese', 'zh', 'zho', 'chi', '中文', '汉语', '漢語'])
def test_generic_chinese_prefers_original_cantonese_over_default_mandarin(original_language):
    streams = [audio(1, 'zho', 'Mandarin Full', default=True), audio(2, 'yue', '粤语 原声')]
    result = select_original_audio_stream(streams, original_language)
    assert result['stream_index'] == 2 and result['audio_index'] == 1 and result['language'] == 'yue'


@pytest.mark.parametrize('original_language', ['Cantonese', 'yue', '粤语', '粵語'])
def test_explicit_cantonese_does_not_use_an_original_default_mandarin_track(original_language):
    streams = [audio(1, 'zho', 'Original Mandarin', default=True), audio(2, 'yue', 'Cantonese Full')]
    assert select_original_audio_stream(streams, original_language)['stream_index'] == 2
    with pytest.raises(ValueError):
        select_original_audio_stream(streams[:1], original_language)


@pytest.mark.parametrize('original_language', ['Mandarin', 'Mandarin Chinese', 'cmn', 'cmn-CN', '普通话', '国语'])
def test_explicit_mandarin_does_not_use_an_original_default_cantonese_track(original_language):
    streams = [audio(1, 'yue', 'Original Cantonese', default=True), audio(2, 'cmn', 'Mandarin Full')]
    result = select_original_audio_stream(streams, original_language)
    assert result['stream_index'] == 2 and result['language'] == 'zho'
    with pytest.raises(ValueError):
        select_original_audio_stream(streams[:1], original_language)


def test_generic_chinese_family_never_falls_back_to_english_or_russian():
    with pytest.raises(ValueError):
        select_original_audio_stream([audio(1, 'eng', 'Original'), audio(2, 'rus', default=True)], 'Chinese')


def test_two_explicit_original_encodes_prefer_the_matching_default_encode():
    streams = [audio(11, 'eng', 'Original DTS'), audio(12, 'eng', '原声 AC3', default=True)]
    result = select_original_audio_stream(streams, 'English')
    assert result['audio_index'] == 1 and result['stream_index'] == 12


@pytest.mark.parametrize('title', [
    'Original Director Commentary', 'English Director_Commentary', 'English 解说', 'English 評論',
    'Original Audio Description', 'English audio-description', 'Descriptive Audio English', 'English Dubbed',
])
def test_commentary_described_and_dubbed_titles_cannot_take_original_dialogue_priority(title):
    streams = [audio(1, 'eng', title, default=True), audio(2, 'eng', 'English Full')]
    result = select_original_audio_stream(streams, 'English')
    assert result['stream_index'] == 2 and result['audio_index'] == 1


@pytest.mark.parametrize('disposition', ['comment', 'visual_impaired', 'dub'])
def test_excluded_dispositions_cannot_take_an_original_title_or_default_priority(disposition):
    streams = [audio(1, 'eng', 'Original', **{disposition: 1, 'default': 1}), audio(2, 'eng', 'Full')]
    result = select_original_audio_stream(streams, 'English')
    assert result['stream_index'] == 2 and result['audio_index'] == 1


def test_string_zero_dispositions_are_not_mistaken_for_true_flags():
    result = select_original_audio_stream(
        [audio(1, 'eng', 'Original', comment='0', visual_impaired='0', dub='0', default='0')], 'English')
    assert result['stream_index'] == 1


def test_uppercase_ffprobe_tags_do_not_let_commentary_take_original_priority():
    streams = [audio(1, default=True), audio(2)]
    streams[0]['tags'] = {'LANGUAGE': 'eng', 'TITLE': 'Original Director Commentary'}
    streams[1]['tags'] = {'LANGUAGE': 'eng', 'TITLE': 'English Original'}
    result = select_original_audio_stream(streams, 'English')
    assert result['audio_index'] == 1 and result['stream_index'] == 2
    assert result['language'] == 'eng' and result['title'] == 'English Original'


@pytest.mark.parametrize('streams', [
    [audio(1, 'rus', 'Russian', default=True)],
    [audio(1, 'rus', 'Original Russian'), audio(2, 'jpn', 'Original Japanese')],
    [audio(1, 'eng', 'English Commentary'), audio(2, 'rus', 'Russian')],
    [audio(1, 'eng', 'English Dubbed')],
])
def test_known_original_language_without_an_eligible_matching_track_is_an_error(streams):
    with pytest.raises(ValueError):
        select_original_audio_stream(streams, 'English')


def test_single_unlabelled_avi_audio_is_allowed_without_claiming_a_known_track_language():
    streams = [{'index': 0, 'codec_type': 'video'}, audio(1)]
    result = select_original_audio_stream(streams, 'English')
    assert result == {
        'audio_index': 0, 'stream_index': 1, 'language': 'und', 'title': '', 'reason': 'single-unlabelled',
    }


def test_single_eligible_unlabelled_track_keeps_its_index_after_excluded_commentary():
    streams = [audio(1, 'eng', 'Commentary'), audio(2)]
    result = select_original_audio_stream(streams, 'English')
    assert result['audio_index'] == 1 and result['stream_index'] == 2
    assert result['reason'] == 'single-unlabelled'


@pytest.mark.parametrize('streams', [
    [audio(1), audio(2)],
    [audio(1, None, 'Original'), audio(2)],
    [audio(1, None, 'Russian MVO')],
    [audio(1, 'unrecognized-label')],
])
def test_unlabelled_ambiguous_or_contradictory_tracks_do_not_silently_become_original(streams):
    with pytest.raises(ValueError):
        select_original_audio_stream(streams, 'English')


def test_missing_original_language_uses_an_explicit_original_title_before_other_defaults():
    streams = [audio(1, 'rus', 'Russian', default=True), audio(2, 'eng', '原声')]
    result = select_original_audio_stream(streams, None)
    assert result['stream_index'] == 2 and result['language'] == 'eng'
    assert result['reason'] == 'original-title'


@pytest.mark.parametrize('original_language', [None, '', 'Unknown', 'und'])
def test_missing_original_language_can_use_a_unique_eligible_dialogue_track(original_language):
    result = select_original_audio_stream([audio(3, 'jpn', 'Full')], original_language)
    assert result['audio_index'] == 0 and result['language'] == 'jpn'
    assert result['reason'] == 'single-eligible'


def test_missing_original_language_does_not_turn_a_default_flag_into_language_evidence():
    with pytest.raises(ValueError, match='multiple tracks'):
        select_original_audio_stream([audio(1, 'rus', default=True), audio(2, 'eng')], None)


def test_conflicting_explicit_original_languages_require_real_original_language_metadata():
    streams = [audio(1, 'rus', 'Original'), audio(2, 'eng', 'Original')]
    with pytest.raises(ValueError, match='conflicting languages'):
        select_original_audio_stream(streams, None)
    assert select_original_audio_stream(streams, 'English')['stream_index'] == 2


def test_negative_original_wording_is_not_original_track_evidence():
    streams = [audio(1, 'rus', 'Non-original', default=True), audio(2, 'eng', 'Original')]
    assert select_original_audio_stream(streams, None)['stream_index'] == 2


@pytest.mark.parametrize('streams', [[], [{'index': 0, 'codec_type': 'video'}],
                                    [{'codec_type': 'audio'}], [audio(1), audio(1)]])
def test_missing_audio_or_invalid_indices_fail_explicitly(streams):
    with pytest.raises(ValueError):
        select_original_audio_stream(streams, 'English')


def test_unrecognized_original_language_metadata_is_not_treated_as_missing():
    with pytest.raises(ValueError, match='could not be recognized'):
        select_original_audio_stream([audio(1, 'rus')], 'Mystery Language')
