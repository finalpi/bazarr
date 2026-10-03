"""Measure full-dialogue coverage without assuming a fixed subtitle duration."""

import math
import re

import pysubs2


BIN_COUNT = 10
MIN_PARSE_RATIO = 0.8
MAX_TEXT_LENGTH = 2_000_000
_TIME_ROW = re.compile(
    r'^\s*(?:-?\d{1,3}:)?\d{1,2}:\d{2}[,.]\d{1,3}\s*-->\s*'
    r'(?:-?\d{1,3}:)?\d{1,2}:\d{2}[,.]\d{1,3}(?:\s|$)')


def _duration_seconds(duration):
    try:
        if hasattr(duration, 'total_seconds'):
            duration = duration.total_seconds()
        value = float(duration)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _declared_cues(text, subtitle_format):
    if subtitle_format in ('ass', 'ssa'):
        return len(re.findall(r'^\s*Dialogue\s*:', text, flags=re.MULTILINE | re.IGNORECASE))
    if subtitle_format not in ('srt', 'vtt'):
        return None
    count, ignored_block = 0, False
    for line in text.splitlines():
        stripped = line.strip()
        if subtitle_format == 'vtt':
            if not stripped:
                ignored_block = False
                continue
            if re.match(r'^(?:NOTE(?:\s|$)|STYLE$|REGION$)', stripped):
                ignored_block = True
            if ignored_block:
                continue
        if _TIME_ROW.match(line):
            count += 1
    return count


def _subtitle_text_for_parse(text):
    if not re.match(r'^\ufeff?\s*WEBVTT(?:\s|$)', text):
        return text
    # PySubs2's VTT parser can treat example timestamps inside NOTE as cues.
    blocks = re.split(r'\r?\n\s*\r?\n', text)
    return '\n\n'.join(block for block in blocks if not re.match(
        r'^\s*(?:NOTE(?:\s|$)|STYLE(?:\s|$)|REGION(?:\s|$))', block))


def subtitle_coverage(text, duration, forced=False, partial=False):
    """Classify ordinary coverage as full, limited, or an obvious fragment.

    Limited coverage remains eligible for the independent audio validator.
    Forced and explicitly partial subtitles bypass completeness checks, but
    invalid formats and substantial parser losses never bypass validation.
    Bin positions are relative to the first cue, so a uniform offset does not
    alter the coverage classification.
    """
    duration = _duration_seconds(duration)
    result = {
        'accepted': False, 'reason': 'invalid_format', 'coverage_class': 'unknown',
        'format': None, 'duration_seconds': duration, 'duration_known': duration is not None,
        'forced': forced is True, 'partial': partial is True,
        'total_cues': 0, 'dialogue_cues': 0, 'declared_cues': None, 'parse_ratio': None,
        'first_seconds': None, 'last_seconds': None, 'span_seconds': None, 'span_ratio': None,
        'occupied_bins': [], 'bin_count': BIN_COUNT,
    }
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT_LENGTH or '\x00' in text:
        return result
    try:
        cues = pysubs2.SSAFile.from_string(_subtitle_text_for_parse(text))
    except Exception:
        return result

    subtitle_format = cues.format
    result['format'] = subtitle_format
    result['total_cues'] = len(cues)
    parsed_rows = [cue for cue in cues if not cue.is_comment]
    declared = _declared_cues(text, subtitle_format)
    result['declared_cues'] = declared
    if declared:
        result['parse_ratio'] = min(1.0, len(parsed_rows) / declared)

    intervals = []
    for cue in parsed_rows:
        try:
            start, end = float(cue.start) / 1000, float(cue.end) / 1000
            if cue.plaintext.strip() and math.isfinite(start) and math.isfinite(end) and end > start:
                intervals.append((start, end))
        except (TypeError, ValueError, OverflowError):
            continue
    result['dialogue_cues'] = len(intervals)
    if intervals:
        first = min(start for start, _ in intervals)
        last = max(end for _, end in intervals)
        span = last - first
        result.update(first_seconds=first, last_seconds=last, span_seconds=span)
        if duration is not None:
            occupied = {min(BIN_COUNT - 1, max(0, int(((start + end) / 2 - first) / duration * BIN_COUNT)))
                        for start, end in intervals}
            result.update(span_ratio=span / duration, occupied_bins=sorted(occupied))

    if result['parse_ratio'] is not None and result['parse_ratio'] < MIN_PARSE_RATIO:
        result['reason'] = 'parse_loss'
        return result
    if not intervals:
        result['reason'] = 'no_dialogue'
        return result
    if forced is True or partial is True:
        result.update(accepted=True, reason='forced_exception' if forced is True else 'partial_exception',
                      coverage_class='partial')
    elif duration is None:
        result.update(accepted=True, reason='unknown_duration', coverage_class='unknown')
    elif result['span_ratio'] < 0.25 and len(result['occupied_bins']) < 3:
        result.update(reason='obvious_fragment', coverage_class='fragment')
    elif result['span_ratio'] >= 0.5 and len(result['occupied_bins']) >= 4:
        result.update(accepted=True, reason='full_coverage', coverage_class='full')
    else:
        result.update(accepted=True, reason='limited_coverage', coverage_class='limited')
    return result
