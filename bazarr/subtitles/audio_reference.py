"""Choose a movie or episode's original dialogue track from ffprobe metadata."""

import re
import unicodedata

from babelfish import Language, LanguageConvertError, LanguageReverseError


_MISSING_LANGUAGES = {'', 'und', 'unknown', 'undefined', 'n/a', 'none', 'null', '未知', '未指定'}
_MANDARIN_LANGUAGES = {'mandarin', 'mandarin chinese', 'cmn', '国语', '國語', '普通话', '普通話'}
_LANGUAGE_ALIASES = {
    '英语': 'eng', '英語': 'eng', '英文': 'eng',
    '日语': 'jpn', '日語': 'jpn', '日文': 'jpn',
    '中文': 'zho', '汉语': 'zho', '漢語': 'zho', '国语': 'zho', '國語': 'zho',
    '普通话': 'zho', '普通話': 'zho', '华语': 'zho', '華語': 'zho',
    '简体中文': 'zho', '簡體中文': 'zho', '繁体中文': 'zho', '繁體中文': 'zho',
    'mandarin': 'zho', 'mandarin chinese': 'zho', 'cmn': 'zho',
    '粤语': 'yue', '粵語': 'yue', '广东话': 'yue', '廣東話': 'yue', 'cantonese': 'yue',
    '俄语': 'rus', '俄語': 'rus', '俄文': 'rus',
    '法语': 'fra', '法語': 'fra', '法文': 'fra',
    '德语': 'deu', '德語': 'deu', '德文': 'deu',
    '韩语': 'kor', '韓語': 'kor', '韩文': 'kor', '韓文': 'kor',
    '西班牙语': 'spa', '西班牙語': 'spa',
    '葡萄牙语': 'por', '葡萄牙語': 'por',
    '意大利语': 'ita', '義大利語': 'ita', '阿拉伯语': 'ara', '阿拉伯語': 'ara',
}
_EXCLUDED_TITLE = re.compile(
    r'\bcomment(?:ary|aries)?\b|\baudio[ -]*description\b|\bdescriptive[ -]*audio\b|'
    r'\bdub(?:bed|bing)?\b|解说|解說|评论|評論|口述影像|視障|视障|描述音轨|描述音軌|配音|译制|譯製',
    re.IGNORECASE)
_ORIGINAL_TITLE = re.compile(r'\boriginal\b|\bnative\b|原声|原聲|原音', re.IGNORECASE)
_NOT_ORIGINAL_TITLE = re.compile(r'\b(?:not|non)[ -]+original\b|非原声|非原聲|非原音', re.IGNORECASE)


def _language_value(value):
    if isinstance(value, dict):
        value = value.get('name') or value.get('alpha3') or value.get('language') or ''
    elif not isinstance(value, str) and value is not None:
        value = getattr(value, 'alpha3', value)
    return unicodedata.normalize('NFKC', str(value or '')).strip().casefold()


def normalize_audio_language(value):
    """Normalize language names, ISO codes and common Chinese names to ISO alpha3."""
    value = _language_value(value)
    if value in _MISSING_LANGUAGES:
        return None
    if value in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[value]
    code = value.replace('_', '-').split('-')[0]
    # ISO codes take precedence over names: "En" is also the name of a
    # different language in the ISO registry, but ffprobe's "en" is English.
    parsers = ([(Language.fromalpha2, code), (Language.fromalpha3b, code), (Language, code)]
               if re.fullmatch(r'[a-z]{2,3}', code) else [])
    for parse, candidate in parsers + [(Language.fromname, value)]:
        try:
            language = parse(candidate).alpha3
            return _LANGUAGE_ALIASES.get(language, language)
        except (ValueError, LanguageConvertError, LanguageReverseError):
            continue
    return None


def _flag(value):
    if isinstance(value, str):
        return value.strip().casefold() in {'1', 'true', 'yes'}
    return bool(value)


def _title_languages(title):
    """Conservative language evidence used to reject contradictory unlabelled tracks."""
    values = dict(_LANGUAGE_ALIASES)
    values.update({
        'english': 'eng', 'japanese': 'jpn', 'russian': 'rus', 'french': 'fra',
        'german': 'deu', 'korean': 'kor', 'spanish': 'spa', 'portuguese': 'por',
        'italian': 'ita', 'arabic': 'ara', 'chinese': 'zho',
    })
    found = set()
    for name, language in values.items():
        if (re.search(r'(?<![a-z0-9])' + re.escape(name) + r'(?![a-z0-9])', title)
                if name.isascii() else name in title):
            found.add(language)
    return found


def select_original_audio_stream(streams, original_language):
    """Select original dialogue without silently falling back to the first audio track.

    Relative audio indices count every audio stream, including excluded tracks.
    Returned language is the selected track's normalized label, or ``und`` when
    the permitted single-track fallback has no language label.
    """
    wanted = normalize_audio_language(original_language)
    original_value = _language_value(original_language)
    if wanted is None and original_value not in _MISSING_LANGUAGES:
        raise ValueError('Original audio language could not be recognized')
    explicit_mandarin = (original_value in _MANDARIN_LANGUAGES or
                         original_value.replace('_', '-').split('-')[0] == 'cmn')
    matching_languages = {'zho', 'yue'} if wanted == 'zho' and not explicit_mandarin else {wanted}
    audio = [stream for stream in streams or [] if stream.get('codec_type') == 'audio']
    if not audio:
        raise ValueError('No audio streams are available')
    try:
        audio = sorted(audio, key=lambda stream: int(stream['index']))
        indices = [int(stream['index']) for stream in audio]
        if len(set(indices)) != len(indices) or any(index < 0 for index in indices):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Audio stream indices are invalid') from error

    eligible = []
    for audio_index, stream in enumerate(audio):
        tags = {str(key).casefold(): value for key, value in (stream.get('tags') or {}).items()}
        disposition = stream.get('disposition') or {}
        title = str(tags.get('title') or '')
        normalized_title = unicodedata.normalize('NFKC', title).casefold().replace('_', ' ')
        if (_EXCLUDED_TITLE.search(normalized_title) or
                any(_flag(disposition.get(flag)) for flag in ('comment', 'visual_impaired', 'dub'))):
            continue
        label = _language_value(tags.get('language'))
        language = normalize_audio_language(label)
        eligible.append({
            'audio_index': audio_index, 'stream_index': int(stream['index']),
            'language': language or 'und', 'title': title,
            '_unlabelled': label in _MISSING_LANGUAGES,
            '_title_languages': _title_languages(normalized_title),
            '_original': bool(_ORIGINAL_TITLE.search(normalized_title) and
                              not _NOT_ORIGINAL_TITLE.search(normalized_title)),
            '_default': _flag(disposition.get('default')),
        })
    if not eligible:
        raise ValueError('No original dialogue audio streams are available')

    def choose(candidates):
        return min(candidates, key=lambda track: (not track['_original'], not track['_default'], track['audio_index']))

    if wanted:
        matching = [track for track in eligible if track['language'] in matching_languages]
        if matching:
            selected = choose(matching)
            reason = ('original-language-original-title' if selected['_original'] else
                      'original-language-default' if selected['_default'] else 'original-language-match')
        elif (len(eligible) == 1 and eligible[0]['_unlabelled'] and
              not eligible[0]['_title_languages'] - matching_languages):
            selected, reason = eligible[0], 'single-unlabelled'
        else:
            raise ValueError('No eligible audio track matches the original language: ' + wanted)
    else:
        original = [track for track in eligible if track['_original']]
        if original:
            labelled_languages = {track['language'] for track in original if track['language'] != 'und'}
            if len(labelled_languages) > 1:
                raise ValueError('Original audio titles identify conflicting languages')
            selected, reason = choose(original), 'original-title'
        elif len(eligible) == 1:
            selected = eligible[0]
            reason = 'single-unlabelled' if selected['_unlabelled'] else 'single-eligible'
        else:
            raise ValueError('Original audio language is missing and multiple tracks are ambiguous')
    return {key: selected[key] for key in ('audio_index', 'stream_index', 'language', 'title')} | {'reason': reason}
