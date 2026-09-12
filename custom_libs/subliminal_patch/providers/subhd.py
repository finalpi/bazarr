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
from urllib.parse import quote, urlparse
from zipfile import BadZipFile, ZipFile, is_zipfile

import rarfile
from bs4 import BeautifulSoup
from guessit import guessit
from subzero.language import Language
from subliminal import Episode, Movie
from subliminal.exceptions import ConfigurationError
from subliminal.subtitle import fix_line_ending

from subliminal_patch.chinese import normalize_name, rank_archive_entries, title_variants
from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle, guess_matches


logger = logging.getLogger(__name__)

_BASE_URL = 'https://subhd.me'
_SEARCH_MIRRORS = (_BASE_URL, 'https://subhd.one', 'https://subhd.top', 'https://subhd.cc')
_TRUSTED_DOWNLOAD_SUFFIXES = ('.subhd.me', '.subhd.tv', '.subhd.com')
_USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
_SUBTITLE_EXTENSIONS = ('.ass', '.ssa', '.srt', '.vtt', '.smi', '.sami', '.sup', '.idx', '.sub')


def _archive_names_and_reader(content):
    stream = io.BytesIO(content)
    if is_zipfile(stream):
        archive = ZipFile(stream)
        return archive.namelist(), archive.read, archive.close
    stream.seek(0)
    if rarfile.is_rarfile(stream):
        archive = rarfile.RarFile(stream)
        return archive.namelist(), archive.read, archive.close
    if content.startswith(b"7z\xbc\xaf'\x1c"):
        from py7zr import SevenZipFile
        archive = SevenZipFile(io.BytesIO(content), mode='r')
        files = archive.readall() or {}
        return list(files), lambda name: files[name].read(), archive.close
    return None, None, None


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


def _extract_download(content, subtitle):
    names, reader, closer = _archive_names_and_reader(content)
    if names is not None:
        try:
            ranked = rank_archive_entries(
                names,
                season=getattr(subtitle.video, 'season', None),
                episode=getattr(subtitle.video, 'episode', None),
                absolute_episode=getattr(subtitle.video, 'absolute_episode', None),
                desired_language=str(subtitle.language),
                hearing_impaired=subtitle.hearing_impaired,
                forced=subtitle.language.forced,
            )
            for selected in ranked:
                extracted = reader(selected)
                extension = os.path.splitext(selected)[1].lstrip('.').lower()
                subtitle_format = _detect_subtitle_format(extracted, extension)
                candidate = copy.copy(subtitle)
                candidate.content = fix_line_ending(extracted)
                candidate.use_original_format = True
                candidate.format = subtitle_format
                candidate._is_valid = False
                candidate._guessed_encoding = None
                if candidate.is_valid():
                    return extracted, subtitle_format
                logger.debug('Skipping invalid SubHD archive member: %s', selected)
            return None, None
        finally:
            closer()

    return content, _detect_subtitle_format(content)


class SubhdSubtitle(Subtitle):
    provider_name = 'subhd'

    def __init__(self, language, subtitle_id, page_link, release_info, video):
        super().__init__(language, page_link=page_link)
        self.subtitle_id = subtitle_id
        self.release_info = release_info
        self.video = video

    @property
    def id(self):
        return self.subtitle_id

    def get_matches(self, video):
        kind = 'episode' if isinstance(video, Episode) else 'movie'
        matches = guess_matches(video, guessit(self.release_info, {'type': kind}))
        normalized = normalize_name(self.release_info)
        aliases = title_variants(video)
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

    def list_subtitles(self, video, languages):
        subtitles = []
        seen = set()
        titles = title_variants(video)[:6]
        queries = []
        if isinstance(video, Episode):
            queries.extend('%s S%02dE%02d' % (title, video.season, video.episode) for title in titles)
        queries.extend(titles)
        for query in queries:
            found_for_title = False
            for host in _SEARCH_MIRRORS:
                try:
                    body = self._request(
                        host + '/search/' + quote(query, safe=''), timeout=15).decode('utf-8', 'replace')
                    parsed = self._parse_results(body)
                    if not parsed:
                        continue
                    for subtitle_id, release in parsed[:30]:
                        for language in languages:
                            key = (subtitle_id, str(language))
                            if key in seen:
                                continue
                            seen.add(key)
                            subtitles.append(self.subtitle_class(
                                language, subtitle_id, host + '/a/' + subtitle_id, release, video))
                    found_for_title = True
                    break
                except (OSError, subprocess.SubprocessError):
                    logger.debug('SubHD search failed on %s', host, exc_info=True)
            if found_for_title:
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
