"""Verify subtitle timing from unique English dialogue anchors, without media I/O."""

from collections import Counter, defaultdict
from difflib import SequenceMatcher
import html
import math
import re
from statistics import median
import unicodedata

import pysubs2
from subliminal_patch.subtitle_coverage import _subtitle_text_for_parse


RATES = (1.0, 1000 / 1001, 1001 / 1000, 24 / 25, 25 / 24, 24000 / 25025, 25025 / 24000)
MIN_SIMILARITY = 0.90
MIN_WORDS = 4
MIN_CHARACTERS = 12
MIN_ANCHORS = 12
MIN_WINDOW_ANCHORS = 2
MIN_WINDOWS = 3
MIN_SPAN_RATIO = 0.5
MIN_INLIER_RATIO = 0.85
MIN_MATCH_COVERAGE = 0.50
MAX_RESIDUAL = 2.0
MAX_RESIDUAL_P90 = 1.5
MIN_OFFSET_CORRECTION = 0.75
_HAN = re.compile(r'[\u3400-\u9fff]')
_WORDS = re.compile(r"[a-z]+(?:'[a-z]+)*|\d+", re.IGNORECASE)


def _english(text):
    if not isinstance(text, str):
        return '', 0
    text = html.unescape(unicodedata.normalize('NFKC', text)).replace('’', "'").replace('‘', "'")
    text = re.sub(r'\{[^}]*\}|<[^>]*>|\[[^\]]*\]', ' ', text)
    text = text.replace(r'\N', '\n').replace(r'\n', '\n').replace(r'\h', ' ')
    lines = text.splitlines()
    english_lines = [line for line in lines if not _HAN.search(line) and re.search(r'[A-Za-z]', line)]
    # Latin names in a Chinese translation line must not pollute its paired English line.
    text = ' '.join(english_lines or lines)
    text = _HAN.sub(' ', text).casefold()
    tokens = [token.replace("'", '') for token in _WORDS.findall(text)]
    return ' '.join(tokens), sum(bool(re.search(r'[a-z]', token)) for token in tokens)


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _units(entries):
    """Include bounded adjacent pairs to tolerate ordinary cue split/merge differences."""
    entries = [entry for entry in entries if entry['end'] - entry['start'] <= 20]
    units = list(entries)
    ordered = sorted(entries, key=lambda entry: (entry['start'], entry['end']))
    for first, second in zip(ordered, ordered[1:]):
        if (first.get('window') != second.get('window') or
                not -2 <= second['start'] - first['end'] <= 2 or
                max(first['end'], second['end']) - first['start'] > 20):
            continue
        units.append(dict(first, end=max(first['end'], second['end']),
                          normalized=first['normalized'] + ' ' + second['normalized'],
                          words=first['words'] + second['words'], ids=first['ids'] + second['ids']))
    groups = defaultdict(list)
    for unit in units:
        if unit['words'] >= MIN_WORDS and len(unit['normalized']) >= MIN_CHARACTERS:
            groups[unit['normalized']].append(unit)
    unique, ambiguous = [], 0
    for group in groups.values():
        first = group[0]
        if any(abs(entry['start'] - first['start']) > 0.25 or
               abs(entry['end'] - first['end']) > 0.25 for entry in group[1:]):
            ambiguous += 1
            continue
        unique.append(first)
    return unique, ambiguous


def _deduplicated_entries(entries):
    seen, result = set(), []
    for entry in entries:
        key = (entry['start'], entry['end'], entry['normalized'])
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def _usable_ids(entries):
    """Count source cues once, including ambiguous dialogue that cannot match."""
    ids = set()
    ordered = sorted((entry for entry in entries if entry['end'] - entry['start'] <= 20),
                     key=lambda entry: (entry['start'], entry['end']))
    for entry in ordered:
        if entry['words'] >= MIN_WORDS and len(entry['normalized']) >= MIN_CHARACTERS:
            ids.update(entry['ids'])
    for first, second in zip(ordered, ordered[1:]):
        if (first.get('window') == second.get('window') and
                -2 <= second['start'] - first['end'] <= 2 and
                max(first['end'], second['end']) - first['start'] <= 20 and
                first['words'] + second['words'] >= MIN_WORDS and
                len(first['normalized'] + ' ' + second['normalized']) >= MIN_CHARACTERS):
            ids.update(first['ids'] + second['ids'])
    return ids


def _matches(candidates, references):
    exact = {entry['normalized']: entry for entry in candidates}
    words_to_candidates = defaultdict(set)
    for index, candidate in enumerate(candidates):
        for word in set(candidate['normalized'].split()):
            words_to_candidates[word].add(index)
    proposed, ambiguous = [], 0
    for reference in references:
        target = reference['normalized']
        if target in exact:
            proposed.append((1.0, reference, exact[target]))
            continue
        hits = []
        target_words = set(target.split())
        overlaps = Counter(index for word in target_words for index in words_to_candidates.get(word, ()))
        minimum_overlap = max(2, math.ceil(len(target_words) * 0.5))
        for index, overlap in overlaps.items():
            if overlap < minimum_overlap:
                continue
            candidate = candidates[index]
            value = candidate['normalized']
            if 2 * min(len(value), len(target)) / (len(value) + len(target)) < MIN_SIMILARITY:
                continue
            similarity = SequenceMatcher(None, value, target, autojunk=False).ratio()
            if similarity >= MIN_SIMILARITY:
                hits.append((similarity, candidate))
        if not hits:
            continue
        hits.sort(key=lambda hit: hit[0], reverse=True)
        best = hits[0]
        if any(not set(best[1]['ids']).intersection(other['ids']) for _, other in hits[1:]):
            ambiguous += 1
            continue
        proposed.append((best[0], reference, best[1]))
    # A cue used in a merged phrase cannot also inflate the count as an individual anchor.
    proposed.sort(key=lambda item: (-item[0], len(item[1]['ids']) + len(item[2]['ids']),
                                    -min(item[1]['words'], item[2]['words'])))
    used_candidates, used_references, anchors = set(), set(), []
    for similarity, reference, candidate in proposed:
        if used_candidates.intersection(candidate['ids']) or used_references.intersection(reference['ids']):
            continue
        used_candidates.update(candidate['ids'])
        used_references.update(reference['ids'])
        anchors.append({'candidate_start': candidate['start'], 'candidate_end': candidate['end'],
                        'reference_start': reference['start'], 'reference_end': reference['end'],
                        'window': reference['window'], 'similarity': similarity,
                        '_candidate_ids': candidate['ids'], '_reference_ids': reference['ids']})
    return sorted(anchors, key=lambda anchor: anchor['reference_start']), ambiguous


def _percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    left, right = int(math.floor(position)), int(math.ceil(position))
    return values[left] + (values[right] - values[left]) * (position - left)


def _evaluate(anchors, duration, rate, offset, candidate_entries, reference_entries,
              usable_candidate_ids, usable_reference_ids):
    result_anchors = []
    errors, end_errors, inliers = [], [], []
    for anchor in anchors:
        error = anchor['reference_start'] - (anchor['candidate_start'] * rate + offset)
        end_error = anchor['reference_end'] - (anchor['candidate_end'] * rate + offset)
        errors.append(abs(error))
        end_errors.append(abs(end_error))
        row = dict(anchor, residual_seconds=error, end_residual_seconds=end_error)
        result_anchors.append({key: value for key, value in row.items() if not key.startswith('_')})
        if abs(error) <= MAX_RESIDUAL:
            inliers.append(row)
    counts = Counter(anchor['window'] for anchor in inliers)
    counts = {window: count for window, count in counts.items() if count >= MIN_WINDOW_ANCHORS}
    eligible = [anchor for anchor in inliers if anchor['window'] in counts]
    span = (max(anchor['reference_start'] for anchor in eligible) -
            min(anchor['reference_start'] for anchor in eligible)) / duration if eligible else 0.0
    ratio = len(inliers) / len(anchors) if anchors else 0.0
    p90 = _percentile(errors, 0.9)
    end_p90 = _percentile(end_errors, 0.9)
    bounds = {}
    for entry in reference_entries:
        window = entry['window']
        previous = bounds.get(window, (entry['start'], entry['end']))
        bounds[window] = (min(previous[0], entry['start']), max(previous[1], entry['end']))
    # The proposed fit determines which candidate cues actually fall in the
    # observed reference windows; unrelated unsampled film dialogue is excluded.
    available_candidates = {
        identifier for entry in candidate_entries
        if any(entry['end'] * rate + offset > left and entry['start'] * rate + offset < right
               for left, right in bounds.values())
        for identifier in entry['ids'] if identifier in usable_candidate_ids
    }
    matched_candidates = {identifier for anchor in eligible for identifier in anchor['_candidate_ids']}
    matched_references = {identifier for anchor in eligible for identifier in anchor['_reference_ids']}
    candidate_matched = len(matched_candidates.intersection(available_candidates))
    reference_matched = len(matched_references.intersection(usable_reference_ids))
    candidate_coverage = candidate_matched / len(available_candidates) if available_candidates else 0.0
    reference_coverage = reference_matched / len(usable_reference_ids) if usable_reference_ids else 0.0
    qualified = (len(eligible) >= MIN_ANCHORS and len(counts) >= MIN_WINDOWS and span >= MIN_SPAN_RATIO and
                 ratio >= MIN_INLIER_RATIO and p90 is not None and p90 <= MAX_RESIDUAL_P90 and
                 end_p90 is not None and end_p90 <= 3.0 and
                 candidate_coverage >= MIN_MATCH_COVERAGE and reference_coverage >= MIN_MATCH_COVERAGE)
    return {'accepted': qualified, 'rate': rate, 'offset_seconds': offset,
            'anchor_count': len(anchors), 'window_counts': counts, 'window_count': len(counts),
            'span_ratio': span, 'inlier_ratio': ratio, 'residual_median_seconds': median(errors) if errors else None,
            'residual_p90_seconds': p90, 'end_residual_p90_seconds': end_p90,
            'candidate_available_cues': len(available_candidates), 'reference_available_cues': len(usable_reference_ids),
            'candidate_matched_cues': candidate_matched, 'reference_matched_cues': reference_matched,
            'candidate_coverage_ratio': candidate_coverage, 'reference_coverage_ratio': reference_coverage,
            'anchors': result_anchors}


def verify_dialogue_alignment(candidate_text, reference_cues, duration, rates=RATES, max_offset=120):
    """Prove current timing or a bounded correction using independent reference dialogue.

    The returned transform is ``reference_time = candidate_time * rate + offset``.
    Inconclusive evidence never establishes a mismatch. Diagnostics contain only
    numeric anchor timing/similarity, not dialogue or subtitle content.
    """
    result = {'accepted': False, 'reason': 'reference_inconclusive', 'rate': 1.0, 'offset_seconds': 0.0,
              'correction_required': False, 'anchor_count': 0, 'window_counts': {}, 'window_count': 0,
              'span_ratio': 0.0, 'inlier_ratio': 0.0, 'residual_median_seconds': None,
              'residual_p90_seconds': None, 'end_residual_p90_seconds': None, 'anchors': [],
              'candidate_available_cues': 0, 'reference_available_cues': 0,
              'candidate_matched_cues': 0, 'reference_matched_cues': 0,
              'candidate_coverage_ratio': 0.0, 'reference_coverage_ratio': 0.0}
    duration, max_offset = _finite(duration), _finite(max_offset)
    if (duration is None or duration <= 0 or max_offset is None or max_offset < 0 or
            not isinstance(candidate_text, str) or not candidate_text or len(candidate_text) > 2_000_000 or
            not isinstance(reference_cues, (list, tuple))):
        return result
    try:
        cues = pysubs2.SSAFile.from_string(_subtitle_text_for_parse(candidate_text))
    except Exception:
        return result
    candidate_entries = []
    for index, cue in enumerate(cues):
        if cue.is_comment:
            continue
        start, end = _finite(cue.start / 1000), _finite(cue.end / 1000)
        normalized, words = _english(cue.plaintext)
        if start is not None and end is not None and end > start and normalized:
            candidate_entries.append({'start': start, 'end': end, 'normalized': normalized,
                                      'words': words, 'ids': (index,)})
    reference_entries = []
    for index, cue in enumerate(reference_cues):
        if not isinstance(cue, dict):
            continue
        start, end, window = _finite(cue.get('start')), _finite(cue.get('end')), cue.get('window')
        normalized, words = _english(cue.get('text'))
        if (start is not None and end is not None and end > start and normalized and
                isinstance(window, int) and not isinstance(window, bool) and window >= 0):
            reference_entries.append({'start': start, 'end': end, 'normalized': normalized,
                                      'words': words, 'window': window, 'ids': (index,)})
    candidate_entries = _deduplicated_entries(candidate_entries)
    reference_entries = _deduplicated_entries(reference_entries)
    candidates, duplicate_candidates = _units(candidate_entries)
    references, duplicate_references = _units(reference_entries)
    usable_candidate_ids, usable_reference_ids = _usable_ids(candidate_entries), _usable_ids(reference_entries)
    anchors, ambiguous_matches = _matches(candidates, references)
    result.update(duplicate_candidate_sentences=duplicate_candidates,
                  duplicate_reference_sentences=duplicate_references, ambiguous_matches=ambiguous_matches)
    baseline = _evaluate(anchors, duration, 1.0, 0.0, candidate_entries, reference_entries,
                         usable_candidate_ids, usable_reference_ids)
    result.update(baseline)
    if baseline['accepted']:
        result['reason'] = 'reference_match'
        return result
    if len(anchors) < MIN_ANCHORS:
        return result
    fitted = []
    for value in rates or ():
        rate = _finite(value)
        if rate is None or rate <= 0:
            continue
        offset = median(anchor['reference_start'] - anchor['candidate_start'] * rate for anchor in anchors)
        if abs(offset) > max_offset or (rate == 1.0 and abs(offset) < MIN_OFFSET_CORRECTION):
            continue
        fit = _evaluate(anchors, duration, rate, offset, candidate_entries, reference_entries,
                        usable_candidate_ids, usable_reference_ids)
        if fit['accepted']:
            fitted.append(fit)
    if fitted:
        best = min(fitted, key=lambda fit: (fit['residual_p90_seconds'], fit['residual_median_seconds'],
                                           abs(fit['rate'] - 1.0), abs(fit['offset_seconds'])))
        result.update(best, reason='reference_correction', correction_required=True)
    return result
