"""Content coverage is relative to media duration, before audio decoding."""

import math
import subprocess

import pysubs2
import pytest

from subliminal_patch.subtitle_coverage import subtitle_coverage


METRIC_KEYS = {
    'total_cues', 'dialogue_cues', 'declared_cues', 'parse_ratio',
    'first_seconds', 'last_seconds', 'span_seconds', 'span_ratio',
    'occupied_bins', 'bin_count', 'accepted', 'reason', 'coverage_class',
}


def timestamp(seconds, separator=','):
    milliseconds = round(abs(seconds) * 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return ('-' if seconds < 0 else '') + (
        '%02d:%02d:%02d%s%03d' % (hours, minutes, whole_seconds, separator, milliseconds))


def srt(count=900, span=7700, offset=0, text='A complete dialogue line.'):
    return '\n\n'.join(
        '%d\n%s --> %s\n%s' % (
            index + 1, timestamp(offset + span * index / (count - 1)),
            timestamp(offset + span * index / (count - 1) + 2), text)
        for index in range(count)) + '\n'


def ass(events):
    """Preserve negative timestamps; serializer normally clamps these to zero."""
    header = ('[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\n[Events]\n'
              'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
    return header + '\n'.join(
        '%s: 0,%s,%s,Default,,0,0,0,,%s' % (
            kind, timestamp(start, '.'), timestamp(end, '.'), text)
        for kind, start, end, text in events) + '\n'


def lossy_srt(parsed_count=7, declared_count=10, span=900, text='Retained dialogue'):
    # These are visibly declared cue rows in MM:SS.mmm form. The SRT parser
    # requires HH:MM:SS.mmm and silently absorbs them into the preceding text.
    result = srt(count=parsed_count, span=span, text=text)
    for index in range(parsed_count, declared_count):
        result += '\n\n%d\n01:05.000 --> 01:06.000\nDropped dialogue.\n' % (index + 1)
    return result


def test_observed_63_cue_four_minute_input_cannot_cover_a_129_minute_movie():
    result = subtitle_coverage(srt(count=63, span=272), 129 * 60)
    assert METRIC_KEYS <= result.keys()
    assert result['total_cues'] == result['dialogue_cues'] == result['declared_cues'] == 63
    assert result['parse_ratio'] == pytest.approx(1)
    assert result['first_seconds'] == pytest.approx(0)
    assert result['last_seconds'] == result['span_seconds'] == pytest.approx(274)
    assert result['span_ratio'] == pytest.approx(274 / (129 * 60))
    assert not result['accepted']
    assert result['reason'] == 'obvious_fragment'
    assert result['coverage_class'] == 'fragment'


def test_complete_900_cue_movie_is_accepted_and_reports_actual_full_span():
    result = subtitle_coverage(srt(), 129 * 60)
    assert result['accepted']
    assert result['total_cues'] == result['dialogue_cues'] == result['declared_cues'] == 900
    assert result['last_seconds'] == result['span_seconds'] == pytest.approx(7702)
    assert result['span_ratio'] == pytest.approx(7702 / (129 * 60))
    assert len(result['occupied_bins']) >= 4
    assert all(0 <= bucket < 10 for bucket in result['occupied_bins'])
    assert result['bin_count'] == 10
    assert result['coverage_class'] == 'full'
    assert result['reason'] == 'full_coverage'


@pytest.mark.parametrize('duration,count,span', [(180, 20, 160), (30, 12, 24), (100, 4, 92)])
def test_short_drama_clip_and_low_cue_count_use_proportions_not_a_fixed_minimum(duration, count, span):
    result = subtitle_coverage(srt(count=count, span=span), duration)
    assert result['accepted']
    assert result['dialogue_cues'] == count
    assert result['span_ratio'] >= 0.5
    assert len(result['occupied_bins']) >= 4


@pytest.mark.parametrize('duration,span', [(240, 110), (60, 25)])
def test_intermediate_coverage_is_retained_for_final_audio_validation(duration, span):
    result = subtitle_coverage(srt(count=63, span=span), duration)
    assert result['accepted']
    assert result['span_ratio'] < 0.5
    assert result['coverage_class'] == 'limited'
    assert result['reason'] == 'limited_coverage'


def test_exactly_half_duration_with_distributed_dialogue_is_accepted():
    result = subtitle_coverage(srt(count=50, span=498), 1000)
    assert result['span_ratio'] == pytest.approx(0.5)
    assert result['accepted']


@pytest.mark.parametrize('offset', [-8000, -300, 0, 300, 8000])
def test_uniform_positive_or_negative_offset_does_not_change_coverage(offset):
    events = [('Dialogue', offset + index * 85, offset + index * 85 + 2, 'Dialogue')
              for index in range(10)]
    result = subtitle_coverage(ass(events), 1000)
    baseline = subtitle_coverage(ass([('Dialogue', index * 85, index * 85 + 2, 'Dialogue')
                                      for index in range(10)]), 1000)
    assert result['accepted'] == baseline['accepted'] is True
    assert result['span_seconds'] == baseline['span_seconds']
    assert result['span_ratio'] == baseline['span_ratio']
    assert result['occupied_bins'] == baseline['occupied_bins']
    assert result['first_seconds'] == pytest.approx(offset)
    assert result['last_seconds'] == pytest.approx(offset + 767)


def test_two_edge_cues_cannot_make_a_nearly_empty_movie_count_as_full_coverage():
    result = subtitle_coverage(srt(count=2, span=950), 1000)
    assert result['span_ratio'] > 0.9
    assert len(result['occupied_bins']) == 2
    assert result['accepted']
    assert result['coverage_class'] == 'limited'


def test_one_long_cue_does_not_fill_ten_dialogue_bins():
    result = subtitle_coverage('1\n00:00:00,000 --> 00:16:30,000\nOne line.\n', 1000)
    assert result['span_ratio'] == pytest.approx(0.99)
    assert len(result['occupied_bins']) == 1
    assert result['accepted']
    assert result['coverage_class'] == 'limited'


def test_small_span_spread_across_three_bins_is_limited_rather_than_definitely_incomplete():
    result = subtitle_coverage(srt(count=30, span=210), 1000)
    assert result['span_ratio'] < 0.25
    assert len(result['occupied_bins']) == 3
    assert result['accepted']
    assert result['coverage_class'] == 'limited'


def test_exactly_one_quarter_span_is_limited_even_with_only_two_bins():
    result = subtitle_coverage(srt(count=2, span=248), 1000)
    assert result['span_ratio'] == pytest.approx(0.25)
    assert len(result['occupied_bins']) == 2
    assert result['accepted']
    assert result['coverage_class'] == 'limited'


def test_comments_empty_and_nonpositive_events_do_not_expand_dialogue_coverage():
    events = [('Dialogue', start, start + 2, 'Real dialogue') for start in (2, 31, 62, 92)]
    events += [
        ('Comment', -5000, 10000, 'Do not count this comment'),
        ('Dialogue', -2000, 8000, r'{\i1}'),
        ('Dialogue', -1000, -1002, 'Backwards interval'),
        ('Dialogue', 5000, 5000, 'Zero interval'),
    ]
    result = subtitle_coverage(ass(events), 100)
    assert result['total_cues'] == 8
    assert result['declared_cues'] == 7
    assert result['dialogue_cues'] == 4
    assert result['parse_ratio'] == pytest.approx(1)
    assert result['first_seconds'] == pytest.approx(2)
    assert result['last_seconds'] == pytest.approx(94)
    assert result['span_seconds'] == pytest.approx(92)
    assert result['accepted']


def test_nonfinite_parser_events_do_not_expand_actual_dialogue_span(monkeypatch):
    text = ass([('Dialogue', start, start + 2, 'Real dialogue') for start in (2, 31, 62, 92, 5000, 8000)])
    parsed = pysubs2.SSAFile.from_string(text)
    parsed[-2].start = float('nan')
    parsed[-1].end = float('inf')
    monkeypatch.setattr(pysubs2.SSAFile, 'from_string', lambda *_args, **_kwargs: parsed)
    result = subtitle_coverage(text, 100)
    assert result['total_cues'] == result['declared_cues'] == 6
    assert result['dialogue_cues'] == 4
    assert result['first_seconds'] == pytest.approx(2)
    assert result['last_seconds'] == pytest.approx(94)
    assert result['accepted']


@pytest.mark.parametrize('format_name', ['srt', 'ass', 'ssa'])
def test_genuinely_declared_rows_dropped_by_the_parser_are_reported_as_parse_loss(format_name):
    if format_name == 'srt':
        text = lossy_srt()
    else:
        events = [('Dialogue', index * 150, index * 150 + 2, 'Retained dialogue')
                  for index in range(7)]
        events += [('dialogue', 920 + index, 921 + index, 'Dropped dialogue') for index in range(3)]
        text = ass(events)
        if format_name == 'ssa':
            text = text.replace('v4.00+', 'v4.00').replace('[V4+ Styles]', '[V4 Styles]')
            text = text.replace('Format: Layer,', 'Format: Marked,')
    result = subtitle_coverage(text, 1000)
    assert result['declared_cues'] == 10
    assert result['total_cues'] == 7
    assert result['parse_ratio'] == pytest.approx(0.7)
    assert not result['accepted']
    assert result['reason'] == 'parse_loss'


def test_exactly_eighty_percent_parser_retention_does_not_trigger_parse_loss():
    text = lossy_srt(parsed_count=8)
    result = subtitle_coverage(text, 1000)
    assert result['declared_cues'] == 10
    assert result['parse_ratio'] == pytest.approx(0.8)
    assert result['accepted']


def test_dialogue_text_arrows_are_not_mistaken_for_declared_timestamp_rows():
    result = subtitle_coverage(srt(count=10, span=900, text='The arrow --> is dialogue text.'), 1000)
    assert result['declared_cues'] == result['total_cues'] == 10
    assert result['parse_ratio'] == pytest.approx(1)
    assert result['accepted']


def test_webvtt_note_and_style_blocks_do_not_expand_dialogue_coverage():
    metadata = ('WEBVTT\n\nNOTE Example, not spoken dialogue:\n'
                '00:00:00.000 --> 04:00:00.000\nMetadata example\n\n'
                'STYLE\n::cue { color: red; }\n\n')
    text = metadata + srt(count=10, span=900).replace(',', '.')
    result = subtitle_coverage(text, 1000)
    assert result['declared_cues'] == result['total_cues'] == result['dialogue_cues'] == 10
    assert result['span_seconds'] == pytest.approx(902)
    assert result['parse_ratio'] == pytest.approx(1)
    assert result['accepted']


@pytest.mark.parametrize('duration', [None, 0, -1, float('nan'), float('inf')])
def test_unknown_or_invalid_media_duration_does_not_guess_a_runtime(duration):
    result = subtitle_coverage(srt(count=2, span=10), duration)
    assert result['accepted']
    assert result['reason'] == 'unknown_duration'
    assert result['coverage_class'] == 'unknown'
    assert result['span_ratio'] is None or math.isfinite(result['span_ratio'])


def test_forced_subtitle_is_allowed_to_cover_only_selected_scenes():
    result = subtitle_coverage(srt(count=2, span=10), 7740, forced=True)
    assert result['accepted']
    assert result['dialogue_cues'] == 2
    assert result['span_seconds'] == pytest.approx(12)
    assert result['coverage_class'] == 'partial'
    assert result['reason'] == 'forced_exception'


def test_explicit_partial_candidate_is_allowed_only_after_valid_dialogue_parse():
    result = subtitle_coverage(srt(count=2, span=10), 7740, partial=True)
    assert result['accepted']
    assert result['coverage_class'] == 'partial'
    assert result['reason'] == 'partial_exception'
    assert not subtitle_coverage('Not a subtitle.', 7740, partial=True)['accepted']
    lossy = lossy_srt(parsed_count=2, declared_count=3, span=10)
    assert subtitle_coverage(lossy, 7740, partial=True)['reason'] == 'parse_loss'


@pytest.mark.parametrize('flag_name,flag_value', [('forced', 'true'), ('partial', 'yes'), ('partial', 1)])
def test_only_explicit_boolean_flags_can_bypass_full_dialogue_coverage(flag_name, flag_value):
    result = subtitle_coverage(srt(count=2, span=10), 7740, **{flag_name: flag_value})
    assert not result['accepted']
    assert result['coverage_class'] == 'fragment'


@pytest.mark.parametrize('text', ['', 'This is not a subtitle.', '<html>Download failed</html>',
                                  '[Script Info]\n[V4+ Styles]\n[Events]\n'])
@pytest.mark.parametrize('duration,forced', [(7740, False), (None, False), (7740, True)])
def test_bad_format_or_no_dialogue_is_rejected_before_coverage_exemptions(text, duration, forced):
    result = subtitle_coverage(text, duration, forced=forced)
    assert not result['accepted']
    assert result['reason'] != 'unknown_duration'


def test_forced_subtitles_must_still_retain_their_declared_rows():
    text = lossy_srt(parsed_count=2, declared_count=3, span=10)
    result = subtitle_coverage(text, 7740, forced=True)
    assert result['parse_ratio'] == pytest.approx(2 / 3)
    assert not result['accepted']
    assert result['reason'] == 'parse_loss'


def test_coverage_does_not_launch_media_or_audio_commands(monkeypatch):
    def unexpected_command(*args, **kwargs):
        pytest.fail('Coverage must be a pure subtitle parse')

    monkeypatch.setattr(subprocess, 'run', unexpected_command)
    monkeypatch.setattr(subprocess, 'Popen', unexpected_command)
    assert subtitle_coverage(srt(), 7740)['accepted']
