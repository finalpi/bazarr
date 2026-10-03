# coding=utf-8

"""SubHD provider using the current temporary-download protocol."""

import html
import codecs
import io
import json
import logging
import os
import copy
import random
import re
import subprocess
import tempfile
import time
import unicodedata
from collections import Counter
from urllib.parse import quote, unquote, urljoin, urlparse
from zipfile import BadZipFile, ZipFile, is_zipfile

import rarfile
import pysubs2
from bs4 import BeautifulSoup
from guessit import guessit
from subzero.language import Language
from subliminal import Episode, Movie
from subliminal.exceptions import ConfigurationError
from subliminal.subtitle import fix_line_ending

from subliminal_patch.chinese import (
    _episode_markers, archive_entry_score, normalize_name, search_title_variants, title_variants,
)
from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle, guess_matches
from subliminal_patch.subtitle_coverage import subtitle_coverage, _subtitle_text_for_parse


logger = logging.getLogger(__name__)

_BASE_URL = 'https://subhd.me'
_SEARCH_MIRRORS = (_BASE_URL, 'https://subhd.one', 'https://subhd.top', 'https://subhd.cc')
_TRUSTED_DOWNLOAD_SUFFIXES = ('.subhd.me', '.subhd.tv', '.subhd.com')
_USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
_SUBTITLE_EXTENSIONS = ('.ass', '.ssa', '.srt', '.vtt', '.smi', '.sami', '.sup', '.idx', '.sub')
_MAX_SEARCH_PAGES = 3
_MAX_SEARCH_REQUESTS = 12
_ARCHIVE_EXTENSIONS = ('.zip', '.rar', '.7z')
_MAX_ARCHIVE_DEPTH = 3
_MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_CANDIDATES = 20
_SCRIPT_MARKERS = {
    'zh': re.compile(r'(?<![a-z0-9])(?:chs|sc|zhs|hans|zh[-_ ]?(?:cn|hans)|gb|simplified)(?![a-z0-9])|'
                     r'简体|簡體|简中|簡中|简英|簡英|(?<![\u3400-\u9fff])(?:简|簡)(?![\u3400-\u9fff])', re.I),
    'zt': re.compile(r'(?<![a-z0-9])(?:cht|tc|zht|hant|zh[-_ ]?(?:tw|hant)|big5|traditional)(?![a-z0-9])|'
                     r'繁体|繁體|繁中|繁英|正体|正體|(?<![\u3400-\u9fff])繁(?![\u3400-\u9fff])', re.I),
}
_FOREIGN_TAGS = {
    '英语', '英語', '英文', '日语', '日語', '日文', '韩语', '韓語', '韩文', '韓文',
    '法语', '法語', '德语', '德語', '俄语', '俄語', '西班牙语', '西班牙語',
    '葡萄牙语', '葡萄牙語', '意大利语', '義大利語', '阿拉伯语', '阿拉伯語',
    '泰语', '泰語', '荷兰语', '荷蘭語', '希伯来语', '希伯來語', '印地语', '印地語',
    'english', 'japanese', 'korean', 'french', 'german', 'russian', 'spanish',
    'portuguese', 'italian', 'arabic', 'thai', 'dutch', 'hebrew', 'hindi',
}
_CHINESE_TAGS = {'中文', '汉语', '漢語', 'chinese', 'chi', 'zho', 'zh', '中英', '中日', '中韩', '中韓'}
_BILINGUAL_MARKER = re.compile(r'(?<![a-z0-9])(?:bilingual|dual)(?![a-z0-9])|双语|雙語|中英|中日|中韩|中韓', re.I)
_FOREIGN_FILENAME = re.compile(
    r'(?:^|[._ -])(?:eng|en|english|jpn|ja|japanese|kor|ko|korean|fre|fra|fr|ger|deu|de|rus|ru)'
    r'(?:[._ -](?:hi|sdh|forced))*$', re.I)
# Only differing character forms contribute evidence; shared Han characters do
# not establish a script. This intentionally omits ambiguous forms such as 后.
_SIMPLIFIED_FEATURES = frozenset(
    '这们说让过还听觉语汉爱给经实应无总进远边动场务员证师纸电气车马龙长万东风飞兴亲欢标转际续虽数级'
    '话备达带当读断队尔儿够广记讲紧举离连领买卖难请认声头条铁网选样业义阴阳鱼园运乐办变别宾补冲传'
    '单岛敌顶该个赶归观号获济检将节尽绝军灵刘罗满梦鸟宁齐桥轻区确热萨杀伤设术双岁孙随谈团显写谢'
    '寻严银优战张赵质众专装资组')
_TRADITIONAL_FEATURES = frozenset(
    '這們說讓過還聽覺語漢愛給經實應無總進遠邊動場務員證師紙電氣車馬龍長萬東風飛興親歡標轉際續雖數級'
    '話備達帶當讀斷隊爾兒夠廣記講緊舉離連領買賣難請認聲頭條鐵網選樣業義陰陽魚園運樂辦變別賓補衝傳'
    '單島敵頂該個趕歸觀號獲濟檢將節盡絕軍靈劉羅滿夢鳥寧齊橋輕區確熱薩殺傷設術雙歲孫隨談團顯寫謝'
    '尋嚴銀優戰張趙質眾專裝資組')


def _script_markers(value):
    value = unicodedata.normalize('NFKC', str(value or ''))
    return {script for script, pattern in _SCRIPT_MARKERS.items() if pattern.search(value)}


def _tag_language_evidence(tags):
    values = [unicodedata.normalize('NFKC', tag).strip().casefold()
              for tag in tags or [] if isinstance(tag, str) and tag.strip()]
    scripts = set().union(*(_script_markers(value) for value in values)) if values else set()
    chinese = bool(scripts or any(value in _CHINESE_TAGS for value in values))
    bilingual = any(_BILINGUAL_MARKER.search(value) for value in values)
    foreign_only = any(value in _FOREIGN_TAGS for value in values) and not chinese and not bilingual
    return scripts, foreign_only


def _requested_script(language):
    return 'zt' if str(getattr(language, 'country', '') or '') in {'TW', 'HK', 'MO'} or \
        str(getattr(language, 'script', '') or '') == 'Hant' else 'zh'


def _available_chinese_languages(tags, languages):
    scripts, foreign_only = _tag_language_evidence(tags)
    if foreign_only:
        return []
    return [Language.rebuild(language) for language in sorted(
        (language for language in languages if language.alpha3 == 'zho' and
         (not scripts or _requested_script(language) in scripts)),
        key=lambda language: (_requested_script(language) != 'zh', language.basename,
                              bool(language.forced), bool(language.hi)))]


def _filename_allowed(name, subtitle):
    if os.path.splitext(name)[1].lower() not in _SUBTITLE_EXTENSIONS:
        return True
    scripts = _script_markers(os.path.basename(name))
    if scripts and _requested_script(subtitle.language) not in scripts:
        return False
    stem = os.path.splitext(os.path.basename(name))[0]
    if not scripts and _FOREIGN_FILENAME.search(stem) and not re.search(
            r'(?<![a-z0-9])(?:chs|cht|chi|zho|zh|zht)[&+._ -](?:eng|en)(?![a-z0-9])', stem, re.I):
        return False
    return True


def _content_script_evidence(text):
    counts = Counter(text)
    han_count = sum(count for character, count in counts.items() if '\u3400' <= character <= '\u9fff')
    kana_hangul = sum(count for character, count in counts.items()
                      if '\u3040' <= character <= '\u30ff' or '\uac00' <= character <= '\ud7af')
    amounts = {script: sum(counts[character] for character in features)
               for script, features in (('zh', _SIMPLIFIED_FEATURES), ('zt', _TRADITIONAL_FEATURES))}
    distinct = {script: sum(counts[character] > 0 for character in features)
                for script, features in (('zh', _SIMPLIFIED_FEATURES), ('zt', _TRADITIONAL_FEATURES))}
    total = sum(amounts.values())
    for script in ('zh', 'zt'):
        if amounts[script] >= 3 and distinct[script] >= 2 and amounts[script] >= total * 0.9:
            return script, bool(han_count), kana_hangul > han_count + 3
    mixed = all(amounts[script] >= 3 and distinct[script] >= 2 for script in ('zh', 'zt'))
    return 'mixed' if mixed else None, bool(han_count), kana_hangul > han_count + 3


def _member_script_allowed(name, candidate, subtitle):
    if subtitle.language.alpha3 != 'zho':
        return False
    try:
        cues = pysubs2.SSAFile.from_string(_subtitle_text_for_parse(candidate.text))
        text = '\n'.join(cue.plaintext for cue in cues if not cue.is_comment)
    except (ValueError, TypeError, UnicodeError):
        return False
    script, has_chinese, other_script = _content_script_evidence(text)
    if not has_chinese or other_script or script == 'mixed':
        return False
    wanted = _requested_script(subtitle.language)
    named_scripts = _script_markers(os.path.basename(name)) if name else set()
    if script:
        return script == wanted and (not named_scripts or script in named_scripts)
    if named_scripts:
        return named_scripts == {wanted}
    page_scripts, foreign_only = _tag_language_evidence(getattr(subtitle, 'subtitle_tags', []))
    return not foreign_only and page_scripts == {wanted}


def _archive_names_and_reader(content):
    stream = io.BytesIO(content)
    if is_zipfile(stream):
        archive = ZipFile(stream)
        return archive.namelist(), _bounded_archive_reader(archive), archive.close
    stream.seek(0)
    if rarfile.is_rarfile(stream):
        archive = rarfile.RarFile(stream)
        return archive.namelist(), _bounded_archive_reader(archive), archive.close
    if content.startswith(b"7z\xbc\xaf'\x1c"):
        from py7zr import SevenZipFile
        archive = SevenZipFile(io.BytesIO(content), mode='r')
        files = {info.filename: info for info in archive.list() if not info.is_directory}

        def reader(name, max_bytes=_MAX_ARCHIVE_MEMBER_BYTES):
            if files[name].uncompressed > max_bytes:
                raise ValueError('SubHD archive member exceeds the extraction size limit')
            archive.reset()
            extracted = archive.read(targets=[name]) or {}
            member = extracted.get(name)
            if member is None:
                raise ValueError('SubHD 7z member was not extracted')
            data = member.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError('SubHD archive member exceeds the extraction size limit')
            return data

        return list(files), reader, archive.close
    return None, None, None


def _bounded_archive_reader(archive):
    def reader(name, max_bytes=_MAX_ARCHIVE_MEMBER_BYTES):
        if archive.getinfo(name).file_size > max_bytes:
            raise ValueError('SubHD archive member exceeds the extraction size limit')
        with archive.open(name) as member:
            data = member.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError('SubHD archive member exceeds the extraction size limit')
        return data
    return reader


def _matches_archive_episode(name, criteria):
    episode = criteria['episode']
    if episode is None:
        return True
    markers = _episode_markers(name)
    if not markers:
        return True
    season = criteria['season']
    absolute_episode = criteria['absolute_episode']
    return any(
        (ep == episode and (marked_season == season or season is None or
                            (marked_season is None and absolute_episode is None))) or
        (marked_season is None and absolute_episode is not None and ep == absolute_episode)
        for marked_season, ep in markers)


def _detect_subtitle_format(content, fallback=None):
    probe = content[:8192]
    text = None
    for encoding in ('utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be'):
        try:
            text = codecs.getincrementaldecoder(encoding)(errors='strict').decode(probe, final=False)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if '[Script Info]' in text or '-->' in text or text.lstrip().startswith('WEBVTT'):
            break
        text = None
    if text:
        if '[Script Info]' in text or '[V4+ Styles]' in text or '[Events]' in text:
            return 'ass'
        if text.lstrip().startswith('WEBVTT'):
            return 'vtt'
        if '-->' in text:
            return 'srt'
    # Text in legacy Chinese encodings still has ASCII format headers/timestamps.
    if b'[Script Info]' in probe or b'[V4+ Styles]' in probe or b'[Events]' in probe:
        return 'ass'
    if probe.lstrip().startswith(b'WEBVTT'):
        return 'vtt'
    if re.search(rb'\d{1,2}:\d{2}:\d{2}[,.]\d+\s*-->\s*\d{1,2}:\d{2}:\d{2}', probe):
        return 'srt'
    return fallback


def _archive_member_score(name, criteria, episode_context=None):
    if episode_context and not _episode_markers(name):
        name = episode_context + '.' + os.path.basename(name)
    return archive_entry_score(name, **criteria)


def _rank_archive_members(names, criteria, episode_context=None):
    scored = []
    for index, name in enumerate(names):
        score = _archive_member_score(name, criteria, episode_context)
        if score is not None:
            scored.append((score, -index, name))
    return [entry[2] for entry in sorted(scored, reverse=True)]


def _archive_episode_context(name, criteria, previous=None):
    # An explicitly selected episode archive supplies identity for filenames
    # such as "chs&eng.srt" without supplying its language or format hints.
    for season, episode in sorted(_episode_markers(name), key=lambda marker: marker[0] is None):
        marker = 'S%02dE%02d' % (season, episode) if season is not None else 'E%02d' % episode
        if _matches_archive_episode(marker, criteria):
            return marker
    return previous


def _safe_member_name(name):
    value = str(name or '<direct>')
    value = re.sub(r'https?://\S+', '[URL omitted]', value, flags=re.IGNORECASE)
    value = re.sub(r'(?i)(?:token|signature|api[_-]?key|password|cookie|authorization)\s*[=:]\s*[^\s&;]+',
                   '[credential omitted]', value)
    return re.sub(r'\s+', ' ', value)[:512]


def _safe_subtitle_id(subtitle):
    value = str(getattr(subtitle, 'subtitle_id', '') or '')
    return value if re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value) else '[ID omitted]'


def _log_member(record):
    # Normal Bazarr logging suppresses the subliminal_patch logger below
    # CRITICAL. These bounded diagnostics must remain visible with debug off.
    logging.info('BAZARR SubHD subtitle member: %s', json.dumps(record, ensure_ascii=False))


def _remember_failure(selection_info, reason):
    priorities = {'parse_loss': 4, 'obvious_fragment': 3, 'script_mismatch': 2}
    previous = selection_info.get('failure_reason')
    if previous is None or priorities.get(reason, 1) > priorities.get(previous, 1):
        selection_info['failure_reason'] = reason


def _unreadable_member(name, selection_info, error):
    reason = 'member_read_failed'
    _remember_failure(selection_info, reason)
    _log_member({'member': _safe_member_name(name), 'raw_bytes': None, 'encoding': None,
                 'format': None, 'accepted': False, 'reason': reason,
                 'exception_type': type(error).__name__, 'total_cues': None, 'dialogue_cues': None,
                 'first_seconds': None, 'last_seconds': None, 'span_seconds': None,
                 'span_ratio': None, 'occupied_bins': []})


def _evaluate_member(content, name, subtitle, fallback_format=None, display_name=None):
    subtitle_format = _detect_subtitle_format(content, fallback_format)
    candidate = copy.copy(subtitle)
    candidate.content = fix_line_ending(content)
    candidate.use_original_format = True
    candidate.format = subtitle_format
    candidate.encoding = None
    candidate._is_valid = False
    candidate._guessed_encoding = None
    duration = getattr(subtitle.video, 'duration', None)
    forced = getattr(subtitle.language, 'forced', False) is True
    partial = getattr(subtitle, 'is_partial', False) is True
    encoding, valid, script_allowed, error_type = None, False, False, None
    try:
        text = candidate.text
        encoding = candidate.get_encoding()
        coverage = subtitle_coverage(text, duration, forced=forced, partial=partial)
        valid = bool(subtitle_format and candidate.is_valid())
        script_allowed = valid and _member_script_allowed(name, candidate, subtitle)
    except Exception as error:
        coverage = subtitle_coverage(None, duration, forced=forced, partial=partial)
        error_type = type(error).__name__
    record = dict(coverage, member=_safe_member_name(display_name or name), raw_bytes=len(content),
                  encoding=encoding, detected_format=subtitle_format)
    record['format'] = coverage.get('format') or subtitle_format
    record['coverage_reason'] = coverage['reason']
    if not valid:
        record.update(accepted=False, reason='invalid_format')
    elif not script_allowed:
        record.update(accepted=False, reason='script_mismatch')
    if error_type:
        record['exception_type'] = error_type
    _log_member(record)
    return record, subtitle_format


def _selection_rank(coverage, filename_score):
    return (coverage['coverage_class'] == 'full', filename_score or 0)


def _extract_download(content, subtitle, _depth=0, _budget=None):
    started = time.monotonic()
    selection_info = {}
    subtitle.selected_archive_member = None
    extracted, subtitle_format = None, None
    try:
        extracted, subtitle_format, _ = _extract_download_best(
            content, subtitle, _depth, _budget, _selection_info=selection_info)
        subtitle.download_failure_reason = None if extracted else selection_info.get(
            'failure_reason', 'no_eligible_subtitle')
        if extracted:
            subtitle.selected_archive_member = selection_info.get('member')
        return extracted, subtitle_format
    finally:
        logging.info('BAZARR SubHD subtitle selection: %s', json.dumps({
            'provider': 'subhd', 'subtitle_id': _safe_subtitle_id(subtitle),
            'language': str(subtitle.language), 'selected': bool(extracted),
            'selected_member': selection_info.get('member') if extracted else None,
            'coverage_class': selection_info.get('coverage_class') if extracted else None,
            'reason': selection_info.get('reason') if extracted else selection_info.get(
                'failure_reason', 'no_eligible_subtitle'),
            'selection_elapsed_seconds': round(time.monotonic() - started, 3),
        }, ensure_ascii=False))


def _extract_download_best(content, subtitle, _depth=0, _budget=None, _episode_context=None, _fallback_name=None,
                           _selection_info=None, _archive_path=()):
    if _depth >= _MAX_ARCHIVE_DEPTH:
        return None, None, None
    if _budget is None:
        _budget = [_MAX_EXTRACTED_BYTES]
    if _selection_info is None:
        _selection_info = {}
    criteria = dict(
        season=getattr(subtitle.video, 'season', None),
        episode=getattr(subtitle.video, 'episode', None),
        absolute_episode=getattr(subtitle.video, 'absolute_episode', None),
        desired_language=str(subtitle.language), hearing_impaired=subtitle.hearing_impaired,
        forced=subtitle.language.forced)
    names, reader, closer = _archive_names_and_reader(content)
    if names is not None:
        try:
            best = (None, None, None)
            ranked = _rank_archive_members(
                [name for name in names if _matches_archive_episode(name, criteria) and
                 _filename_allowed(name, subtitle)], criteria, _episode_context)
            for selected in ranked[:_MAX_ARCHIVE_CANDIDATES]:
                if _budget[0] <= 0:
                    break
                try:
                    extracted = reader(selected, min(_MAX_ARCHIVE_MEMBER_BYTES, _budget[0]))
                except (OSError, ValueError, RuntimeError, BadZipFile, rarfile.Error) as error:
                    _unreadable_member('/'.join(_archive_path + (selected,)), _selection_info, error)
                    logger.debug('Skipping unreadable SubHD archive member: %s', selected, exc_info=True)
                    continue
                if len(extracted) > _budget[0]:
                    return best
                _budget[0] -= len(extracted)
                extension = os.path.splitext(selected)[1].lstrip('.').lower()
                record, subtitle_format = _evaluate_member(
                    extracted, selected, subtitle, extension, '/'.join(_archive_path + (selected,)))
                if record['accepted']:
                    rank = _selection_rank(record, _archive_member_score(selected, criteria, _episode_context))
                    if best[2] is None or rank > best[2]:
                        best = (extracted, subtitle_format, rank)
                        _selection_info.update(record)
                    if record['coverage_class'] in ('full', 'partial', 'unknown'):
                        break
                else:
                    _remember_failure(_selection_info, record['reason'])
                    logger.debug('Skipping invalid SubHD archive member: %s', _safe_member_name(selected))
            # Some season releases contain one archive per episode instead of
            # subtitle files. Rank their explicit episode markers through the
            # same criteria, then read only matching inner archives.
            if _depth + 1 >= _MAX_ARCHIVE_DEPTH:
                return best
            nested = {
                name + '.srt': name for name in names
                if os.path.splitext(name)[1].lower() in _ARCHIVE_EXTENSIONS and
                (criteria['episode'] is None or _episode_markers(name)) and
                _matches_archive_episode(name, criteria)
            }
            for virtual_name in _rank_archive_members(nested, criteria, _episode_context)[:_MAX_ARCHIVE_CANDIDATES]:
                if _budget[0] <= 0:
                    break
                selected = nested[virtual_name]
                try:
                    inner = reader(selected, min(_MAX_ARCHIVE_MEMBER_BYTES, _budget[0]))
                except (OSError, ValueError, RuntimeError, BadZipFile, rarfile.Error) as error:
                    _unreadable_member('/'.join(_archive_path + (selected,)), _selection_info, error)
                    logger.debug('Skipping unreadable nested SubHD archive: %s', selected, exc_info=True)
                    continue
                if len(inner) > _budget[0]:
                    return best
                _budget[0] -= len(inner)
                _log_member({'member': _safe_member_name('/'.join(_archive_path + (selected,))),
                             'raw_bytes': len(inner), 'encoding': None, 'format': 'archive',
                             'accepted': True, 'reason': 'nested_archive', 'total_cues': None,
                             'dialogue_cues': None, 'first_seconds': None, 'last_seconds': None,
                             'span_seconds': None, 'span_ratio': None, 'occupied_bins': []})
                inner_selection = {}
                try:
                    candidate = _extract_download_best(
                        inner, subtitle, _depth + 1, _budget,
                        _archive_episode_context(selected, criteria, _episode_context), virtual_name,
                        inner_selection, _archive_path + (selected,))
                except (OSError, ValueError, RuntimeError, BadZipFile, rarfile.Error) as error:
                    _unreadable_member('/'.join(_archive_path + (selected,)), _selection_info, error)
                    logger.debug('Skipping invalid nested SubHD archive: %s', selected, exc_info=True)
                    continue
                if candidate[0] and candidate[1] and candidate[2] is not None:
                    if best[2] is None or candidate[2] > best[2]:
                        best = candidate
                        _selection_info.update(inner_selection)
                elif inner_selection.get('failure_reason'):
                    _remember_failure(_selection_info, inner_selection['failure_reason'])
            return best
        finally:
            closer()

    if _fallback_name is None:
        name = None
    else:
        name = _fallback_name
    if name and not _filename_allowed(name, subtitle):
        _remember_failure(_selection_info, 'script_mismatch')
        return None, None, None
    display_name = '/'.join(_archive_path) if _archive_path else name
    record, subtitle_format = _evaluate_member(content, name, subtitle, display_name=display_name)
    if not record['accepted']:
        _remember_failure(_selection_info, record['reason'])
        return None, None, None
    _selection_info.update(record)
    score = _archive_member_score(name, criteria, _episode_context) if name else None
    return content, subtitle_format, _selection_rank(record, score)


class SubhdSubtitle(Subtitle):
    provider_name = 'subhd'

    def __init__(self, language, subtitle_id, page_link, release_info, video):
        super().__init__(language, page_link=page_link)
        self.subtitle_id = subtitle_id
        self.release_info = release_info
        self.video = video
        self.subtitle_tags = []

    @property
    def id(self):
        return self.subtitle_id

    def get_matches(self, video):
        kind = 'episode' if isinstance(video, Episode) else 'movie'
        matches = guess_matches(video, guessit(self.release_info, {'type': kind}))
        normalized = normalize_name(self.release_info)
        aliases = title_variants(video, include_search_keyword=True)
        if any(normalize_name(alias) in normalized for alias in aliases if len(normalize_name(alias)) >= 3):
            matches.add('series' if isinstance(video, Episode) else 'title')
        if getattr(video, 'year', None) and str(video.year) in self.release_info:
            matches.add('year')
        if isinstance(video, Episode):
            exact = re.search(r'(?i)s0*%d\D*e(?:p)?0*%d(?:\D|$)' % (video.season, video.episode),
                              self.release_info)
            season_pack = re.search(r'(?i)s0*%d(?:\D|$)' % video.season, self.release_info)
            has_episode = re.search(r'(?i)e(?:p)?\d+', self.release_info)
            if exact:
                matches.update(('season', 'episode'))
            elif season_pack and not has_episode:
                matches.update(('season', 'episode'))
            if {'series', 'season', 'episode'} <= matches:
                # TV releases rarely repeat the show's original premiere year.
                # Exact series/season/episode identity is sufficient for this
                # field; source, resolution and release group must still match
                # the actual release metadata through guess_matches.
                matches.add('year')
        return matches


class SubhdProvider(Provider):
    languages = {Language('zho'), Language('zho', 'CN'), Language('zho', 'TW')}
    video_types = (Episode, Movie)
    subtitle_class = SubhdSubtitle

    def __init__(self):
        self._last_request = 0.0
        self._proxy_url = os.environ.get('SUBHD_PROXY_URL')
        self._use_proxy = False

    def initialize(self):
        if self._proxy_url:
            parsed = urlparse(self._proxy_url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ConfigurationError('SUBHD_PROXY_URL must be an HTTP or HTTPS proxy URL')

    def terminate(self):
        self._use_proxy = False

    def _wait(self):
        delay = random.uniform(2.0, 5.0) - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()

    @staticmethod
    def _curl(url, method='GET', body=None, cookie_jar=None, referer=None, headers_file=None,
              timeout=20, proxy_url=None):
        args = ['curl', '-sS', '--fail-with-body', '--max-time', str(timeout), '-A', _USER_AGENT]
        if proxy_url:
            args.extend(['-x', proxy_url])
        if cookie_jar:
            args.extend(['-b', cookie_jar, '-c', cookie_jar])
        if headers_file:
            args.extend(['-D', headers_file])
        if referer:
            args.extend(['-H', 'Referer: ' + referer])
        if method != 'GET':
            parsed_url = urlparse(url)
            origin = '%s://%s' % (parsed_url.scheme, parsed_url.netloc)
            args.extend(['-X', method, '-H', 'X-Requested-With: XMLHttpRequest',
                         '-H', 'Origin: ' + origin, '-H', 'Content-Type: application/json'])
        if body is not None:
            args.extend(['--data', json.dumps(body, separators=(',', ':'))])
        args.append(url)
        return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def _request(self, *args, **kwargs):
        self._wait()
        if self._use_proxy:
            return self._curl(*args, proxy_url=self._proxy_url, **kwargs)
        try:
            return self._curl(*args, **kwargs)
        except subprocess.CalledProcessError as error:
            stderr = error.stderr or b''
            if not self._proxy_url or not any(code in stderr for code in (b'403', b'429')):
                raise
            logger.info('SubHD direct access was rate limited; using its dedicated proxy for this session')
            self._use_proxy = True
            self._wait()
            return self._curl(*args, proxy_url=self._proxy_url, **kwargs)

    @staticmethod
    def _parse_results(body):
        results = {}
        soup = BeautifulSoup(body, 'html.parser')
        for anchor in soup.find_all('a', href=re.compile(r'(?:^|/)a/[A-Za-z0-9]+$')):
            match = re.search(r'/a/([A-Za-z0-9]+)$', anchor.get('href', ''))
            if not match or match.group(1) in results:
                continue
            container = anchor.find_parent('div', class_=re.compile(r'\brow\b')) or anchor.parent
            title = ' '.join(container.stripped_strings) if container else anchor.get_text(' ', strip=True)
            title = re.sub(r'\s+', ' ', html.unescape(title)).strip()
            if title:
                results[match.group(1)] = title
        return list(results.items())

    @staticmethod
    def _parse_result_tags(body):
        """Read the search-card metadata row without treating release text as tags."""
        results = {}
        soup = BeautifulSoup(body, 'html.parser')
        for anchor in soup.find_all('a', href=re.compile(r'(?:^|/)a/[A-Za-z0-9]+$')):
            match = re.search(r'/a/([A-Za-z0-9]+)$', anchor.get('href', ''))
            if not match or match.group(1) in results:
                continue
            container = anchor.find_parent('div', class_=re.compile(r'\brow\b')) or anchor.parent
            metadata = container.select_one('div.text-truncate.py-2.f11') if container else None
            tags = []
            if metadata:
                for span in metadata.find_all('span', recursive=False):
                    if 'p-1' not in (span.get('class') or []) or span.find(['svg', 'i']):
                        continue
                    text = re.sub(r'\s+', ' ', span.get_text(' ', strip=True)).strip()
                    if text and not text.isdecimal() and text not in tags:
                        tags.append(text)
            results[match.group(1)] = tags
        return results

    @staticmethod
    def _search_queries(video):
        titles = search_title_variants(video)[:6]
        queries = []
        keyword = getattr(video, 'search_keyword', None)
        for title in titles:
            if isinstance(video, Episode) and not keyword:
                queries.extend((
                    '%s S%02dE%02d' % (title, video.season, video.episode),
                    '%s S%02d' % (title, video.season),
                    title,
                ))
            else:
                queries.append(title)
        if isinstance(video, Episode) and keyword and not re.search(
                r'(?i)(?<![a-z0-9])s\d{1,2}(?!\d)|\bseason\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b|'
                r'\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+season\b|'
                r'第\s*[0-9一二三四五六七八九十百零〇两]+\s*季', keyword):
            queries.append('%s S%02d' % (keyword, video.season))
        return list(dict.fromkeys(queries))

    @staticmethod
    def _search_page_links(body, search_url):
        origin = urlparse(search_url)
        base_path = unquote(origin.path).rstrip('/')
        page_path = re.compile(re.escape(base_path) + r'/(\d+)$')
        links = []
        for anchor in BeautifulSoup(body, 'html.parser').find_all('a', class_='page-link', href=True):
            target = urlparse(urljoin(search_url, anchor['href']))
            path = unquote(target.path).rstrip('/')
            page_match = page_path.fullmatch(path)
            if (target.scheme != origin.scheme or target.netloc.lower() != origin.netloc.lower() or
                    target.query or target.fragment or not (path == base_path or page_match)):
                continue
            page_number = int(page_match.group(1)) if page_match else 1
            suffix = '/%d' % page_number if page_number != 1 else ''
            url = search_url.rstrip('/') + suffix
            if url not in links:
                links.append(url)
        return links

    def list_subtitles(self, video, languages):
        subtitles = []
        seen = set()
        requests_made = 0
        for query in self._search_queries(video):
            for host in _SEARCH_MIRRORS:
                search_url = host + '/search/' + quote(query, safe='')
                pages = [search_url]
                visited = set()
                request_failed = False
                while pages and len(visited) < _MAX_SEARCH_PAGES and requests_made < _MAX_SEARCH_REQUESTS:
                    page_url = pages.pop(0)
                    if page_url in visited:
                        continue
                    visited.add(page_url)
                    requests_made += 1
                    try:
                        body = self._request(page_url, timeout=15).decode('utf-8', 'replace')
                    except (OSError, subprocess.SubprocessError):
                        logger.debug('SubHD search failed on %s', page_url, exc_info=True)
                        request_failed = True
                        break
                    parsed = self._parse_results(body)
                    tags_by_id = self._parse_result_tags(body)
                    for subtitle_id, release in parsed[:30]:
                        tags = list(tags_by_id.get(subtitle_id, []))
                        for language in _available_chinese_languages(tags, languages):
                            key = (subtitle_id, language.basename, bool(language.hi), bool(language.forced))
                            if key in seen:
                                continue
                            subtitle = self.subtitle_class(
                                language, subtitle_id, host + '/a/' + subtitle_id, release, video)
                            subtitle.subtitle_tags = tags.copy()
                            matches = subtitle.get_matches(video)
                            if isinstance(video, Episode) and not {'series', 'season', 'episode'} <= matches:
                                continue
                            subtitle.matches = matches
                            seen.add(key)
                            subtitles.append(subtitle)
                    if subtitles:
                        return subtitles
                    pages.extend(url for url in self._search_page_links(body, search_url)
                                 if url not in visited and url not in pages)
                if requests_made >= _MAX_SEARCH_REQUESTS:
                    return subtitles
                if not request_failed:
                    # Mirrors share search results. A valid empty or mismatched
                    # page should move to the next query, not every mirror.
                    break
        return subtitles

    def download_subtitle(self, subtitle):
        started = time.monotonic()
        subtitle.download_failure_reason = None
        subtitle.selected_archive_member = None
        subtitle.encoding = None
        subtitle._guessed_encoding = None
        subtitle._is_valid = False
        parsed_page = urlparse(subtitle.page_link)
        host = '%s://%s' % (parsed_page.scheme, parsed_page.netloc)
        subtitle_id = subtitle.subtitle_id
        last_error_type, stage = None, 'prepare_download'
        for attempt in range(1, 4):
            try:
                with tempfile.TemporaryDirectory(prefix='bazarr-subhd-') as directory:
                    jar = os.path.join(directory, 'cookies.txt')
                    headers = os.path.join(directory, 'headers.txt')
                    detail = host + '/a/' + subtitle_id
                    stage = 'prepare_download'
                    prepare = json.loads(self._request(
                        host + '/api/sub/prepare-download', method='POST', body={'sid': subtitle_id},
                        cookie_jar=jar, referer=detail, headers_file=headers).decode('utf-8'))
                    if not prepare.get('success'):
                        raise ValueError('SubHD prepare-download failed')
                    stage = 'download_page'
                    self._request(host + '/down/' + subtitle_id, cookie_jar=jar, referer=detail)
                    stage = 'download_metadata'
                    result = json.loads(self._request(
                        host + '/api/sub/down', method='POST', body={'sid': subtitle_id},
                        cookie_jar=jar, referer=host + '/down/' + subtitle_id).decode('utf-8'))
                    url = result.get('url') if result.get('success') else None
                    parsed = urlparse(url or '')
                    stage = 'validate_download_url'
                    if parsed.scheme != 'https' or not parsed.hostname or not any(
                            parsed.hostname == suffix[1:] or parsed.hostname.endswith(suffix)
                            for suffix in _TRUSTED_DOWNLOAD_SUFFIXES):
                        raise ValueError('SubHD returned an untrusted download URL')
                    stage = 'fetch_package'
                    content = self._request(url, timeout=60)
                    stage = 'select_member'
                    extracted, subtitle_format = _extract_download(content, subtitle)
                    if not extracted:
                        subtitle.download_failure_reason = subtitle.download_failure_reason or 'no_eligible_subtitle'
                        logging.warning('BAZARR SubHD package rejected: %s', json.dumps({
                            'provider': 'subhd', 'subtitle_id': _safe_subtitle_id(subtitle),
                            'language': str(subtitle.language), 'stage': stage,
                            'reason': subtitle.download_failure_reason,
                            'elapsed_seconds': round(time.monotonic() - started, 3),
                        }, ensure_ascii=False))
                        subtitle.content = None
                        return
                    subtitle.content = fix_line_ending(extracted)
                    subtitle.encoding = None
                    subtitle._guessed_encoding = None
                    subtitle._is_valid = False
                    if subtitle_format:
                        subtitle.format = subtitle_format
                    logging.info('BAZARR SubHD package download finished: %s', json.dumps({
                        'provider': 'subhd', 'subtitle_id': _safe_subtitle_id(subtitle),
                        'language': str(subtitle.language),
                        'elapsed_seconds': round(time.monotonic() - started, 3),
                    }, ensure_ascii=False))
                    return
            except (BadZipFile, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
                last_error_type = type(error).__name__
                exit_code = getattr(error, 'returncode', None)
                logging.warning('BAZARR SubHD download attempt failed: %s', json.dumps({
                    'provider': 'subhd', 'subtitle_id': _safe_subtitle_id(subtitle),
                    'stage': stage, 'exception_type': last_error_type,
                    'exit_code': exit_code if isinstance(exit_code, int) else None,
                    'attempt': attempt, 'elapsed_seconds': round(time.monotonic() - started, 3),
                }, ensure_ascii=False))
        subtitle.download_failure_reason = 'download_failed'
        logging.warning('BAZARR SubHD download failed after retries: %s', json.dumps({
            'provider': 'subhd', 'subtitle_id': _safe_subtitle_id(subtitle),
            'stage': stage, 'exception_type': last_error_type,
            'elapsed_seconds': round(time.monotonic() - started, 3),
        }, ensure_ascii=False))
        subtitle.content = None
