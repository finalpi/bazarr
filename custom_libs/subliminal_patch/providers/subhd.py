# coding=utf-8

"""SubHD provider using the current temporary-download protocol."""

import html
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
from urllib.parse import quote, unquote, urljoin, urlparse
from zipfile import BadZipFile, ZipFile, is_zipfile

import rarfile
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
            text = probe.decode(encoding)
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


def _extract_download(content, subtitle, _depth=0, _budget=None):
    extracted, subtitle_format, _ = _extract_download_best(content, subtitle, _depth, _budget)
    return extracted, subtitle_format


def _extract_download_best(content, subtitle, _depth=0, _budget=None, _episode_context=None, _fallback_name=None):
    if _depth >= _MAX_ARCHIVE_DEPTH:
        return None, None, None
    if _budget is None:
        _budget = [_MAX_EXTRACTED_BYTES]
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
                [name for name in names if _matches_archive_episode(name, criteria)], criteria, _episode_context)
            for selected in ranked[:_MAX_ARCHIVE_CANDIDATES]:
                if _budget[0] <= 0:
                    break
                try:
                    extracted = reader(selected, min(_MAX_ARCHIVE_MEMBER_BYTES, _budget[0]))
                except (OSError, ValueError, RuntimeError, BadZipFile, rarfile.Error):
                    logger.debug('Skipping unreadable SubHD archive member: %s', selected, exc_info=True)
                    continue
                if len(extracted) > _budget[0]:
                    return best
                _budget[0] -= len(extracted)
                extension = os.path.splitext(selected)[1].lstrip('.').lower()
                subtitle_format = _detect_subtitle_format(extracted, extension)
                candidate = copy.copy(subtitle)
                candidate.content = fix_line_ending(extracted)
                candidate.use_original_format = True
                candidate.format = subtitle_format
                candidate._is_valid = False
                candidate._guessed_encoding = None
                if candidate.is_valid():
                    best = (extracted, subtitle_format, _archive_member_score(selected, criteria, _episode_context))
                    break
                logger.debug('Skipping invalid SubHD archive member: %s', selected)
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
                except (OSError, ValueError, RuntimeError, BadZipFile, rarfile.Error):
                    logger.debug('Skipping unreadable nested SubHD archive: %s', selected, exc_info=True)
                    continue
                if len(inner) > _budget[0]:
                    return best
                _budget[0] -= len(inner)
                try:
                    candidate = _extract_download_best(
                        inner, subtitle, _depth + 1, _budget,
                        _archive_episode_context(selected, criteria, _episode_context), virtual_name)
                except (OSError, ValueError, RuntimeError, BadZipFile, rarfile.Error):
                    logger.debug('Skipping invalid nested SubHD archive: %s', selected, exc_info=True)
                    continue
                if candidate[0] and candidate[1] and candidate[2] is not None:
                    if best[2] is None or candidate[2] > best[2]:
                        best = candidate
            return best
        finally:
            closer()

    subtitle_format = _detect_subtitle_format(content)
    if _fallback_name is None:
        # Preserve the direct, non-archive download behavior.
        return content, subtitle_format, None
    if not subtitle_format:
        return None, None, None
    candidate = copy.copy(subtitle)
    candidate.content = fix_line_ending(content)
    candidate.use_original_format = True
    candidate.format = subtitle_format
    candidate._is_valid = False
    candidate._guessed_encoding = None
    if not candidate.is_valid():
        return None, None, None
    return content, subtitle_format, _archive_member_score(_fallback_name, criteria, _episode_context)


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
    languages = {Language('zho', 'CN'), Language('zho', 'TW')}
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
                        for language in languages:
                            key = (subtitle_id, str(language))
                            if key in seen:
                                continue
                            subtitle = self.subtitle_class(
                                language, subtitle_id, host + '/a/' + subtitle_id, release, video)
                            subtitle.subtitle_tags = list(tags_by_id.get(subtitle_id, []))
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
        parsed_page = urlparse(subtitle.page_link)
        host = '%s://%s' % (parsed_page.scheme, parsed_page.netloc)
        subtitle_id = subtitle.subtitle_id
        last_error = None
        for _ in range(3):
            try:
                with tempfile.TemporaryDirectory(prefix='bazarr-subhd-') as directory:
                    jar = os.path.join(directory, 'cookies.txt')
                    headers = os.path.join(directory, 'headers.txt')
                    detail = host + '/a/' + subtitle_id
                    prepare = json.loads(self._request(
                        host + '/api/sub/prepare-download', method='POST', body={'sid': subtitle_id},
                        cookie_jar=jar, referer=detail, headers_file=headers).decode('utf-8'))
                    if not prepare.get('success'):
                        raise ValueError('SubHD prepare-download failed')
                    self._request(host + '/down/' + subtitle_id, cookie_jar=jar, referer=detail)
                    result = json.loads(self._request(
                        host + '/api/sub/down', method='POST', body={'sid': subtitle_id},
                        cookie_jar=jar, referer=host + '/down/' + subtitle_id).decode('utf-8'))
                    url = result.get('url') if result.get('success') else None
                    parsed = urlparse(url or '')
                    if parsed.scheme != 'https' or not parsed.hostname or not any(
                            parsed.hostname == suffix[1:] or parsed.hostname.endswith(suffix)
                            for suffix in _TRUSTED_DOWNLOAD_SUFFIXES):
                        raise ValueError('SubHD returned an untrusted download URL')
                    content = self._request(url, timeout=60)
                    extracted, subtitle_format = _extract_download(content, subtitle)
                    if not extracted:
                        raise ValueError('SubHD archive has no matching subtitle')
                    subtitle.content = fix_line_ending(extracted)
                    if subtitle_format:
                        subtitle.format = subtitle_format
                    return
            except (BadZipFile, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
                last_error = error
                logger.debug('SubHD download attempt failed', exc_info=True)
        logger.warning('SubHD download failed after retries: %s', last_error)
        subtitle.content = None
