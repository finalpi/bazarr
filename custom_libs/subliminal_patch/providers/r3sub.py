# coding=utf-8

"""R3Sub provider for official Taiwan subtitle releases."""

import io
import logging
import os
import re
from urllib.parse import urljoin
from zipfile import ZipFile, is_zipfile

import rarfile
from bs4 import BeautifulSoup
from guessit import guessit
from requests import Session
from subzero.language import Language
from subliminal import Episode, Movie
from subliminal.exceptions import AuthenticationError, ConfigurationError, ProviderError
from subliminal.subtitle import fix_line_ending

from subliminal_patch.chinese import normalize_name, select_archive_entry, title_variants
from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle, guess_matches


logger = logging.getLogger(__name__)
_BASE = 'https://r3sub.com'
_SIGN_IN = 'https://forum.r3sub.com/entry/signin'
_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')


class R3subSubtitle(Subtitle):
    provider_name = 'r3sub'

    def __init__(self, language, subtitle_id, release_info, video, files):
        super().__init__(language, page_link='%s/show.php?id=%s' % (_BASE, subtitle_id))
        self.subtitle_id = subtitle_id
        self.release_info = release_info
        self.video = video
        self.files = files

    @property
    def id(self):
        return self.subtitle_id

    def get_matches(self, video):
        kind = 'episode' if isinstance(video, Episode) else 'movie'
        matches = guess_matches(video, guessit(self.release_info, {'type': kind}))
        normalized = normalize_name(self.release_info)
        if any(normalize_name(title) in normalized for title in title_variants(video)
               if len(normalize_name(title)) >= 3):
            matches.add('series' if isinstance(video, Episode) else 'title')
        if getattr(video, 'year', None) and str(video.year) in self.release_info:
            matches.add('year')
        if isinstance(video, Episode) and self.files:
            selected = select_archive_entry(
                self.files, season=video.season, episode=video.episode,
                absolute_episode=getattr(video, 'absolute_episode', None),
                desired_language=str(self.language))
            if selected:
                matches.update(('season', 'episode'))
        elif isinstance(video, Episode) and 'series' in matches:
            # Search rows represent a show or season. The detail archive is
            # checked for the exact episode before any bytes are accepted.
            matches.update(('season', 'episode'))
        if isinstance(video, Episode) and {'series', 'season', 'episode'} <= matches:
            matches.update(('year', 'source', 'release_group', 'audio_codec', 'resolution',
                            'video_codec', 'streaming_service', 'hearing_impaired'))
        elif isinstance(video, Movie) and {'title', 'year'} <= matches:
            matches.update(('source', 'edition', 'release_group', 'audio_codec', 'resolution',
                            'video_codec', 'streaming_service', 'hearing_impaired'))
        return matches


class R3subProvider(Provider):
    languages = {Language('zho', 'CN'), Language('zho', 'TW')}
    video_types = (Episode, Movie)
    subtitle_class = R3subSubtitle

    def __init__(self, email=None, password=None):
        if not email or not password:
            raise ConfigurationError('R3Sub email and password are required')
        self.email = email
        self.password = password
        self.session = None

    def initialize(self):
        self.session = Session()
        self.session.headers.update({'User-Agent': _UA})
        self._login()

    def terminate(self):
        if self.session:
            self.session.close()

    def _login(self):
        page = self.session.get(_SIGN_IN, timeout=20)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, 'html.parser')
        form = soup.find('form', action=re.compile(r'entry/signin')) or soup.find('form')
        payload = {}
        if form:
            for field in form.find_all('input', attrs={'name': True}):
                payload[field['name']] = field.get('value', '')
        payload.update({'Email': self.email, 'Password': self.password, 'Sign In': 'Sign In'})
        response = self.session.post(_SIGN_IN, data=payload, timeout=20, allow_redirects=False)
        if not any(cookie.name == 'R3_Vid' for cookie in self.session.cookies):
            raise AuthenticationError('R3Sub login failed; verify the account and email confirmation')

    @staticmethod
    def _parse_search(html_text):
        soup = BeautifulSoup(html_text, 'html.parser')
        rows = []
        for link in soup.find_all('a', href=re.compile(r'show\.php\?id=')):
            match = re.search(r'id=([A-Za-z0-9]+)', link.get('href', ''))
            if not match:
                continue
            container = link.find_parent('div', class_=re.compile(r'movie')) or link.parent
            text = ' '.join(container.stripped_strings) if container else link.get_text(' ', strip=True)
            files = [node.get('data-fname') for node in container.find_all(attrs={'data-fname': True})] \
                if container else []
            rows.append((match.group(1), text, [name for name in files if name]))
        deduped = {}
        for item in rows:
            deduped.setdefault(item[0], item)
        return list(deduped.values())

    def list_subtitles(self, video, languages):
        results = []
        seen = set()
        for title in title_variants(video)[:6]:
            for attempt in range(2):
                response = self.session.get(
                    _BASE + '/search.php', params={'s': title, 'type': 'movie'}, timeout=20)
                response.raise_for_status()
                if 'entry/signin' not in response.url and 'Form_User_SignIn' not in response.text:
                    break
                if attempt == 0:
                    self._login()
            else:
                raise AuthenticationError('R3Sub session expired and could not be renewed')
            for subtitle_id, release, files in self._parse_search(response.text):
                for language in languages:
                    key = (subtitle_id, str(language))
                    if key not in seen:
                        seen.add(key)
                        results.append(self.subtitle_class(language, subtitle_id, release, video, files))
        return results

    @staticmethod
    def _form_values(html_text):
        soup = BeautifulSoup(html_text, 'html.parser')
        form = soup.find('form', action=re.compile(r'jpdown1\.php'))
        if not form:
            return None
        values = {field['name']: field.get('value', '')
                  for field in form.find_all('input', attrs={'name': True})}
        return values if values.get('id') else None

    def download_subtitle(self, subtitle):
        show = self.session.get(subtitle.page_link, timeout=20)
        show.raise_for_status()
        soup = BeautifulSoup(show.text, 'html.parser')
        filename_field = soup.find('input', attrs={'name': 'filename'})
        files = [node.get('data-fname') for node in soup.find_all(attrs={'data-fname': True})]
        files = [name for name in files if name]
        selected = select_archive_entry(
            files, season=getattr(subtitle.video, 'season', None),
            episode=getattr(subtitle.video, 'episode', None),
            absolute_episode=getattr(subtitle.video, 'absolute_episode', None),
            desired_language=str(subtitle.language))
        filename = filename_field.get('value') if filename_field else (selected or '')
        for language in ('zh', 'cn'):
            intermediate = self.session.post(
                _BASE + '/download.php', data={'id': subtitle.id, 'lang': language, 'filename': filename},
                headers={'Referer': subtitle.page_link}, timeout=20)
            values = self._form_values(intermediate.text)
            if not values:
                continue
            response = self.session.post(
                _BASE + '/jpdown1.php', data={'id': values['id'], 'lang': values.get('lang', language)},
                headers={'Referer': _BASE + '/download.php'}, timeout=40)
            response.raise_for_status()
            content = response.content
            stream = io.BytesIO(content)
            archive = None
            if is_zipfile(stream):
                archive = ZipFile(stream)
            else:
                stream.seek(0)
                if rarfile.is_rarfile(stream):
                    archive = rarfile.RarFile(stream)
            if archive:
                try:
                    member = select_archive_entry(
                        archive.namelist(), season=getattr(subtitle.video, 'season', None),
                        episode=getattr(subtitle.video, 'episode', None),
                        absolute_episode=getattr(subtitle.video, 'absolute_episode', None),
                        desired_language=str(subtitle.language))
                    if member:
                        subtitle.content = fix_line_ending(archive.read(member))
                        subtitle.format = os.path.splitext(member)[1].lstrip('.')
                        return
                finally:
                    archive.close()
        raise ProviderError('R3Sub did not return a matching subtitle archive')
