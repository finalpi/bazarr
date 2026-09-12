# coding=utf-8

"""Shared, dependency-free helpers for Chinese subtitle providers."""

import os
import re


SUBTITLE_EXTENSIONS = ('.ass', '.ssa', '.srt', '.vtt', '.smi', '.sami', '.sup', '.idx', '.sub')

_SIMPLIFIED = ('zh', 'zho', 'chs', 'sc', 'gb', 'zh-cn', 'zh_hans', 'zh-hans', '简体', '简中', '简')
_TRADITIONAL = ('zt', 'zht', 'cht', 'tc', 'big5', 'zh-tw', 'zh_hant', 'zh-hant',
                '繁体', '繁體', '繁中', '繁')
_BILINGUAL = ('bilingual', 'dual', 'chs&eng', 'chi&eng', '中英', '双语', '雙語')
_FOREIGN = ('english', ' eng ', '.eng.', '.en.', 'japanese', '.jpn.', '.ja.', '日语', '日語', '韩语', '韓語')
_INCOMPLETE = ('sample', 'preview', 'trailer', '片段', '预览', '預覽', '仅特效', '僅特效',
               'signs.only', 'songs.only', 'op only', 'ed only')
_FORMATS = {'.ass': 90, '.ssa': 85, '.srt': 80, '.vtt': 65, '.smi': 60, '.sami': 60,
            '.sup': 45, '.idx': 40, '.sub': 35}


def normalize_name(value):
    return re.sub(r'[^0-9a-z\u3400-\u9fff]+', '', (value or '').lower())


def subtitle_language_hint(filename):
    value = filename.lower()
    tokens = set(filter(None, re.split(r'[^0-9a-z\u3400-\u9fff]+', value)))
    if any(token in value for token in _BILINGUAL):
        return 'bilingual'
    if (re.search(r'(?i)(?:^|[._ -])zh[-_ ]?(?:tw|hant)(?:$|[._ -])', value) or
            any((token in tokens if token.isascii() else token in value) for token in _TRADITIONAL)):
        return 'zt'
    if any((token in tokens if token.isascii() else token in value) for token in _SIMPLIFIED):
        return 'zh'
    if any(token in value for token in _FOREIGN):
        return 'foreign'
    if re.search(r'[\u3400-\u9fff]', filename):
        return 'zh-unknown'
    return None


def _episode_markers(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    markers = []
    for match in re.finditer(r'(?i)(?:s(\d{1,2}))?e(?:p)?(\d{1,4})', stem):
        markers.append((int(match.group(1)) if match.group(1) else None, int(match.group(2))))
    for match in re.finditer(r'(?<!\d)\[(\d{1,3})\](?!\d)', stem):
        markers.append((None, int(match.group(1))))
    for match in re.finditer(r'(?i)(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)', stem):
        markers.append((int(match.group(1)), int(match.group(2))))
    # Anime packs often use "Title - 01" without an E marker. Keep this
    # deliberately narrow so years, resolutions and codec names are ignored.
    for match in re.finditer(r'(?:^|\s-\s)(\d{1,3})(?:v\d+)?(?=$|[\s._-])', stem):
        markers.append((None, int(match.group(1))))
    return markers


def archive_entry_score(filename, season=None, episode=None, absolute_episode=None,
                        desired_language=None, hearing_impaired=False, forced=False):
    """Score one archive member; ``None`` means it is unsafe for this target."""
    basename = os.path.basename(filename)
    lower = basename.lower()
    extension = os.path.splitext(lower)[1]
    if extension not in SUBTITLE_EXTENSIONS:
        return None
    if any(token in lower for token in _INCOMPLETE):
        return None

    score = _FORMATS.get(extension, 0)
    markers = _episode_markers(basename)
    if markers and episode is not None:
        exact = any(ep == episode and (
            s == season or season is None or (s is None and absolute_episode is None)
        ) for s, ep in markers)
        absolute = absolute_episode is not None and any(ep == absolute_episode for _, ep in markers)
        if not (exact or absolute):
            return None
        score += 500 if exact else 460
        if any(s == season and ep == episode for s, ep in markers):
            score += 40
    elif episode is not None:
        score -= 40

    language = subtitle_language_hint(basename)
    desired = str(desired_language or '')
    wants_traditional = desired in ('zt', 'zh-TW', 'zht') or 'Hant' in desired
    if wants_traditional:
        score += {'zt': 180, 'bilingual': 130, 'zh-unknown': 80, None: 20, 'zh': -30,
                  'foreign': -500}.get(language, 0)
    else:
        score += {'zh': 180, 'bilingual': 150, 'zh-unknown': 90, None: 20, 'zt': 20,
                  'foreign': -500}.get(language, 0)

    is_forced = any(token in lower for token in ('forced', 'foreign.only', '强制', '強制'))
    is_hi = any(token in lower for token in ('sdh', '.hi.', '[hi]', 'hearing'))
    score += 30 if forced == is_forced else (-120 if is_forced else 0)
    score += 20 if hearing_impaired == is_hi else (-20 if is_hi else 0)
    return score


def select_archive_entry(names, **criteria):
    ranked = rank_archive_entries(names, **criteria)
    return ranked[0] if ranked else None


def rank_archive_entries(names, **criteria):
    scored = []
    for index, name in enumerate(names):
        score = archive_entry_score(name, **criteria)
        if score is not None:
            scored.append((score, -index, name))
    return [item[2] for item in sorted(scored, reverse=True)]


def title_variants(video):
    """Return stable, deduplicated title variants already known by Sonarr/Radarr."""
    values = []
    is_episode = bool(getattr(video, 'series', None))
    primary_attrs = ('series',) if is_episode else ('title',)
    alternative_attrs = ('alternative_series',) if is_episode else ('alternative_titles',)
    for attr in primary_attrs:
        value = getattr(video, attr, None)
        if isinstance(value, str):
            values.append(value)
    for attr in alternative_attrs:
        value = getattr(video, attr, None) or []
        if isinstance(value, str):
            value = [value]
        values.extend(item for item in value if isinstance(item, str))
    result = []
    seen = set()
    for value in values:
        value = value.strip()
        key = normalize_name(value)
        if value and key and key not in seen:
            seen.add(key)
            result.append(value)
    return result
