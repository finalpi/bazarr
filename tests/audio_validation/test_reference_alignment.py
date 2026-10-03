"""Dialogue anchors must prove a correction without media I/O or real dialogue fixtures."""

import copy
import importlib.util
import math
from pathlib import Path
import subprocess

import pysubs2
import pytest


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_KEYS = {
    'accepted', 'reason', 'rate', 'offset_seconds', 'correction_required', 'anchor_count',
    'window_counts', 'window_count', 'span_ratio', 'inlier_ratio', 'residual_median_seconds',
    'residual_p90_seconds', 'end_residual_p90_seconds', 'anchors',
}
DURATION = 3600
SENTENCES = (
    'Before daylight our neighbor carried the broken bicycle across the frozen bridge.',
    'The young librarian discovered a handwritten recipe hidden behind the astronomy books.',
    'Please leave the yellow umbrella beside the kitchen door when you return tomorrow.',
    'Nobody remembered why the station clock stopped during the afternoon thunderstorm.',
    'Our teacher placed twelve smooth stones inside the cardboard box after lunch.',
    'Three travelers followed the narrow mountain trail toward the abandoned observatory.',
    'A silver envelope arrived yesterday containing photographs of the old fishing harbor.',
    'We should repair the garden fence before those curious goats escape once again.',
    'Your brother borrowed my favorite saucepan while preparing dinner for the musicians.',
    'The mechanic explained how a tiny spring could silence the rattling ceiling fan.',
    'Several children built a paper castle beneath the enormous oak tree this morning.',
    'Her grandmother always counted the passing boats from the upstairs bedroom window.',
    'The visiting scientist carefully measured every crack along the ancient marble staircase.',
    'Bring another clean towel because the friendly puppy has fallen into the fountain.',
    'A determined gardener planted orange flowers around the entrance of the village bakery.',
    'The weather report predicts clear skies throughout our planned expedition next weekend.',
    'Someone left a wooden flute on the bench behind the crowded ticket office.',
    'Our quiet captain promised to explain the mysterious signal after the evening meal.',
    'Do not forget the small package waiting near the entrance to the art museum.',
    'The shopkeeper wrapped six painted bowls in newspaper before opening the heavy door.',
    'My cousin discovered that the forgotten key belonged to an empty railway carriage.',
    'These colorful lanterns will guide the festival guests through the winding orchard paths.',
    "Don't trust the old compass until we compare it with the northern stars tonight.",
    'The last apprentice quietly repaired the damaged violin under the patient instructors gaze.',
    'A cheerful photographer asked every visitor to stand beside the newly restored waterwheel.',
    'The curious astronomer recorded another unusual pattern above the distant desert horizon.',
    'Our neighbors shared fresh peaches with the workers rebuilding the collapsed stone wall.',
    'The wooden chest contained several maps showing a forgotten route across the wetlands.',
    'Every guest received a blue ribbon before entering the decorated community meeting hall.',
    'A patient craftsman polished the brass telescope while discussing plans for the voyage.',
    'Your favorite detective followed muddy footprints toward the empty warehouse beside the river.',
    'The tiny bookstore displayed illustrated guides describing the islands beyond the northern coast.',
)


@pytest.fixture(scope='module')
def verify():
    path = ROOT / 'bazarr/subtitles/reference_alignment.py'
    spec = importlib.util.spec_from_file_location('isolated_reference_alignment', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_dialogue_alignment


def reference(windows=(360, 1080, 1800, 2520, 3240), counts=5):
    result = []
    if isinstance(counts, int):
        counts = [counts] * len(windows)
    for window, (start, count) in enumerate(zip(windows, counts)):
        for cue in range(count):
            index = len(result)
            result.append({'start': start + cue * 7, 'end': start + cue * 7 + 2.2,
                           'text': SENTENCES[index], 'window': window})
    return result


def timestamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'


def candidate(reference_cues, rate=1.0, offset=0.0, jitter=None, overrides=None):
    result = []
    for index, cue in enumerate(reference_cues):
        shift = jitter[index] if jitter is not None else 0.0
        start, end = (cue['start'] - offset) / rate + shift, (cue['end'] - offset) / rate + shift
        text = cue['text'] if overrides is None else overrides[index]
        result.append(f'{index + 1}\n{timestamp(start)} --> {timestamp(end)}\n{text}')
    return '\n\n'.join(result) + '\n'


def assert_accepted(result, reason='reference_match', rate=1.0, offset=0.0, correction=False):
    assert OUTPUT_KEYS <= result.keys()
    assert result['accepted'] is True and result['reason'] == reason
    assert result['rate'] == pytest.approx(rate, abs=0.00001)
    assert result['offset_seconds'] == pytest.approx(offset, abs=0.03)
    assert result['correction_required'] is correction
    assert result['anchor_count'] >= 12 and result['window_count'] >= 3
    assert result['span_ratio'] >= 0.5
    assert result['inlier_ratio'] >= 0.85
    assert result['residual_p90_seconds'] <= 1.5


def assert_inconclusive(result):
    assert OUTPUT_KEYS <= result.keys()
    assert result['accepted'] is False
    assert result['reason'] == 'reference_inconclusive'
    assert result['correction_required'] is False


def test_complete_current_dialogue_proves_existing_timing_without_correction(verify):
    refs = reference()
    result = verify(candidate(refs), refs, DURATION)
    assert_accepted(result)
    assert result['anchor_count'] == 25
    assert result['window_counts'] == {window: 5 for window in range(5)}
    assert result['window_count'] == 5
    assert result['residual_median_seconds'] == pytest.approx(0, abs=0.01)
    assert result['end_residual_p90_seconds'] == pytest.approx(0, abs=0.01)


@pytest.mark.parametrize('jitter', [
    [0.0, 0.2, -0.25, 0.45, -0.45] * 5,
    [0.5] * 25,
    [-0.5] * 25,
])
def test_small_jitter_does_not_trigger_unnecessary_offset_or_rate_adjustment(verify, jitter):
    refs = reference()
    result = verify(candidate(refs, jitter=jitter), refs, DURATION)
    assert_accepted(result)
    assert result['offset_seconds'] == 0 and result['rate'] == 1.0


@pytest.mark.parametrize('offset', [35.0, -41.0])
def test_consistent_offset_is_proven_in_both_directions(verify, offset):
    refs = reference()
    result = verify(candidate(refs, offset=offset), refs, DURATION)
    assert_accepted(result, reason='reference_correction', offset=offset, correction=True)
    assert result['residual_median_seconds'] < 0.01


@pytest.mark.parametrize('rate', [25 / 24, 24 / 25, 1001 / 1000, 1000 / 1001])
def test_film_and_fractional_frame_rate_drift_are_proven_over_the_full_span(verify, rate):
    refs = reference()
    result = verify(candidate(refs, rate=rate), refs, DURATION)
    assert_accepted(result, reason='reference_correction', rate=rate, correction=True)
    assert result['residual_p90_seconds'] < 0.02


def test_real_frame_rate_and_offset_can_be_corrected_together(verify):
    refs = reference()
    result = verify(candidate(refs, rate=25 / 24, offset=35), refs, DURATION)
    assert_accepted(result, reason='reference_correction', rate=25 / 24, offset=35, correction=True)


def test_ass_bilingual_override_tags_html_case_and_curly_apostrophes_preserve_english_anchors(verify):
    refs = reference()
    file = pysubs2.SSAFile()
    for cue in refs:
        text = cue['text'].upper().replace("'", '\u2019')
        text = r'{\an8}<i>中文翻译内容不应参与英文匹配</i>\N{\fnArial}<b>' + text + '</b>'
        file.append(pysubs2.SSAEvent(start=round(cue['start'] * 1000), end=round(cue['end'] * 1000), text=text))
    result = verify(file.to_string('ass'), refs, DURATION)
    assert_accepted(result)
    assert result['anchor_count'] == 25


def test_srt_html_and_multiple_lines_match_normalized_reference_dialogue(verify):
    refs = reference()
    decorated = ['<font color="#ff00aa">中文翻译</font>\n<i>' + cue['text'].upper() + '</i>' for cue in refs]
    result = verify(candidate(refs, overrides=decorated), refs, DURATION)
    assert_accepted(result)


def test_unique_high_similarity_wording_can_supply_fuzzy_anchors(verify):
    refs = reference()
    texts = [cue['text'].rstrip('.') + ' now.' for cue in refs]
    result = verify(candidate(refs, overrides=texts), refs, DURATION)
    assert_accepted(result)
    assert result['anchor_count'] == 25
    assert all(0.9 <= anchor['similarity'] < 1.0 for anchor in result['anchors'])


def test_same_sentence_at_same_time_is_deduplicated_without_losing_real_anchors(verify):
    refs = reference(windows=(360, 1800, 3240), counts=4)
    duplicate_refs = refs + copy.deepcopy(refs)
    result = verify(candidate(duplicate_refs), duplicate_refs, DURATION)
    assert_accepted(result)
    assert result['anchor_count'] == 12
    assert len(result['anchors']) == 12


def test_repeated_sentence_at_different_times_is_not_counted_as_many_anchors(verify):
    refs = reference()
    for cue in refs:
        cue['text'] = 'Please carry this particular umbrella back to the village before the storm begins.'
    result = verify(candidate(refs), refs, DURATION)
    assert_inconclusive(result)
    assert result['anchor_count'] == 0


def test_identical_candidate_dialogue_at_two_times_is_ambiguous_even_with_unique_reference(verify):
    refs = reference()
    duplicated_candidates = refs + [dict(cue, start=cue['start'] + 3, end=cue['end'] + 3) for cue in refs]
    result = verify(candidate(duplicated_candidates), refs, DURATION)
    assert_inconclusive(result)
    assert result['anchor_count'] == 0


def test_multiple_high_similarity_candidates_cannot_fabricate_unique_fuzzy_anchors(verify):
    refs = reference()
    ambiguous = [dict(cue, text=cue['text'].rstrip('.') + ' today.') for cue in refs]
    ambiguous += [dict(cue, start=cue['start'] + 3, end=cue['end'] + 3,
                       text=cue['text'].rstrip('.') + ' again.') for cue in refs]
    result = verify(candidate(ambiguous), refs, DURATION)
    assert_inconclusive(result)
    assert result['anchor_count'] < 12


def split_cues(cues):
    split = []
    for cue in cues:
        words = cue['text'].split()
        midpoint = len(words) // 2
        split.append(dict(cue, end=cue['start'] + 1.0, text=' '.join(words[:midpoint])))
        split.append(dict(cue, start=cue['start'] + 1.1, text=' '.join(words[midpoint:])))
    return split


@pytest.mark.parametrize('split_candidate', [True, False])
def test_adjacent_split_or_merged_dialogue_uses_each_source_cue_only_once(verify, split_candidate):
    refs = reference()
    candidate_cues, reference_cues = (split_cues(refs), refs) if split_candidate else (refs, split_cues(refs))
    result = verify(candidate(candidate_cues), reference_cues, DURATION)
    assert_accepted(result)
    assert result['anchor_count'] == 25
    assert len({anchor['candidate_start'] for anchor in result['anchors']}) == 25
    assert len({anchor['reference_start'] for anchor in result['anchors']}) == 25


def test_split_reference_cues_cannot_reuse_nine_candidate_cues_to_meet_twelve_anchor_threshold(verify):
    refs = reference(windows=(360, 1800, 3240), counts=3)
    result = verify(candidate(refs), split_cues(refs), DURATION)
    assert_inconclusive(result)
    assert result['anchor_count'] <= 9


@pytest.mark.parametrize('windows,counts', [
    ((360, 3240), 8),
    ((360, 1800, 3240), 3),
    ((360, 1080, 2520, 3240), (1, 1, 5, 5)),
    ((960, 1260, 1460), 4),
])
def test_window_anchor_count_and_film_span_are_independent_required_evidence(verify, windows, counts):
    refs = reference(windows, counts)
    assert_inconclusive(verify(candidate(refs), refs, DURATION))


@pytest.mark.parametrize('offset,max_offset', [(150, 120), (-150, 120), (35, 30)])
def test_offset_outside_allowed_range_is_inconclusive(verify, offset, max_offset):
    refs = reference()
    assert_inconclusive(verify(candidate(refs, offset=offset), refs, DURATION, max_offset=max_offset))


@pytest.mark.parametrize('rate', [0.8, 1.2])
def test_unsupported_extreme_rate_cannot_prove_a_correction(verify, rate):
    refs = reference()
    assert_inconclusive(verify(candidate(refs, rate=rate), refs, DURATION))


def test_user_rates_limit_the_search_to_the_requested_allowed_candidates(verify):
    refs = reference()
    assert_inconclusive(verify(candidate(refs, rate=25 / 24), refs, DURATION, rates=(1.0,)))


def test_a_few_outliers_do_not_erase_consistent_current_timing(verify):
    refs = reference()
    jitter = [0.0] * len(refs)
    jitter[3], jitter[18] = 9.0, -9.0
    result = verify(candidate(refs, jitter=jitter), refs, DURATION)
    assert_accepted(result)
    assert result['inlier_ratio'] == pytest.approx(23 / 25)


def test_p90_threshold_rejects_even_when_eighty_five_percent_are_inliers(verify):
    refs = reference()
    jitter = [0.0] * len(refs)
    for index in (3, 8, 18):
        jitter[index] = 9.0
    result = verify(candidate(refs, jitter=jitter), refs, DURATION)
    assert_inconclusive(result)


def test_too_many_timing_outliers_cannot_be_hidden_by_median_offset(verify):
    refs = reference()
    jitter = [0.0] * len(refs)
    for index in (3, 8, 13, 18, 23):
        jitter[index] = 9.0
    assert_inconclusive(verify(candidate(refs, jitter=jitter), refs, DURATION))


@pytest.mark.parametrize('extra_end_seconds', [10, 40])
def test_correct_starts_do_not_accept_severely_wrong_display_end_times(verify, extra_end_seconds):
    refs = reference()
    stretched = [dict(cue, end=cue['end'] + extra_end_seconds) for cue in refs]
    assert_inconclusive(verify(candidate(stretched), refs, DURATION))


def test_small_reading_end_extension_keeps_current_timing_without_a_correction(verify):
    refs = reference()
    stretched = [dict(cue, end=cue['end'] + 2.5) for cue in refs]
    result = verify(candidate(stretched), refs, DURATION)
    assert_accepted(result)
    assert result['end_residual_p90_seconds'] == pytest.approx(2.5)


def test_dialogue_that_does_not_match_reference_is_only_inconclusive(verify):
    refs = reference()
    texts = ['Mechanical orbit amber violet canyon silver kettle peach domino compass.' for _ in refs]
    result = verify(candidate(refs, overrides=texts), refs, DURATION)
    assert_inconclusive(result)
    assert result['anchor_count'] == 0


@pytest.mark.parametrize('text', ['Wait!', '这是一条中文对白没有英文内容', 'One two three.'])
def test_short_or_non_english_dialogue_is_not_eligible_anchor_evidence(verify, text):
    refs = reference()
    for cue in refs:
        cue['text'] = text
    result = verify(candidate(refs), refs, DURATION)
    assert_inconclusive(result)
    assert result['anchor_count'] == 0


@pytest.mark.parametrize('text', [None, '', 'not a subtitle', b'bytes are not subtitle text', 42, {}, []])
def test_invalid_candidate_text_returns_safe_inconclusive_result(verify, text):
    assert_inconclusive(verify(text, reference(), DURATION))


@pytest.mark.parametrize('duration', [None, 0, -1, math.nan, math.inf, -math.inf, 'unknown'])
def test_duration_must_be_positive_and_finite(verify, duration):
    refs = reference()
    assert_inconclusive(verify(candidate(refs), refs, duration))


@pytest.mark.parametrize('refs', [
    None, [], {}, [None], [{'text': SENTENCES[0]}],
    [{'start': 1, 'end': 2, 'text': SENTENCES[0]}],
    [{'start': 'bad', 'end': 2, 'text': SENTENCES[0], 'window': 0}],
    [{'start': 1, 'end': math.inf, 'text': SENTENCES[0], 'window': 0}],
    [{'start': 2, 'end': 1, 'text': SENTENCES[0], 'window': 0}],
    [{'start': math.nan, 'end': 2, 'text': SENTENCES[0], 'window': 0}],
])
def test_missing_or_invalid_reference_fields_are_safe_and_inconclusive(verify, refs):
    valid = reference()
    assert_inconclusive(verify(candidate(valid), refs, DURATION))


@pytest.mark.parametrize('field', ['start', 'end', 'text', 'window'])
def test_incomplete_reference_rows_cannot_collect_enough_fake_evidence(verify, field):
    valid = reference()
    broken = copy.deepcopy(valid)
    for cue in broken:
        cue.pop(field)
    assert_inconclusive(verify(candidate(valid), broken, DURATION))


def test_invalid_extra_reference_rows_do_not_destroy_valid_evidence_or_mutate_input(verify):
    refs = reference()
    refs += [None, {'text': 'Incomplete reference row'}, {'start': 'invalid', 'window': 1}]
    before = copy.deepcopy(refs)
    result = verify(candidate(before[:25]), refs, DURATION)
    assert_accepted(result)
    assert refs == before


def test_diagnostics_contain_no_dialogue_or_subtitle_payloads(verify):
    refs = reference()
    result = verify(candidate(refs, offset=35), refs, DURATION)
    assert_accepted(result, reason='reference_correction', offset=35, correction=True)
    representation = repr(result)
    assert not any(sentence in representation for sentence in SENTENCES)
    assert '-->' not in representation
    assert isinstance(result['anchors'], list) and result['anchors']
    for anchor in result['anchors']:
        assert 'similarity' in anchor and 'window' in anchor
        for key, value in anchor.items():
            assert key not in {'text', 'candidate_text', 'reference_text', 'dialogue'}
            assert isinstance(value, (int, float))
            assert math.isfinite(value)


def test_verify_is_a_pure_function_that_never_opens_files_or_spawns_processes(verify, monkeypatch):
    refs = reference()
    text = candidate(refs)

    def forbidden(*args, **kwargs):
        raise AssertionError('Dialogue alignment must not perform filesystem or process I/O')

    monkeypatch.setattr('builtins.open', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    assert_accepted(verify(text, refs, DURATION))
