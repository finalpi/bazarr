# coding=utf-8

"""Reuse subtitles from another local encode of the same movie or episode."""

import json
import logging
import os
import subprocess

from guessit import guessit
from subzero.language import Language
from subliminal import Episode, Movie
from subliminal.subtitle import fix_line_ending

from subliminal_patch.chinese import normalize_name, subtitle_language_hint, title_variants
from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle


logger = logging.getLogger(__name__)
_VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.webm')
_SUBTITLE_EXTENSIONS = ('.ass', '.ssa', '.srt', '.vtt', '.smi')


def _duration(path):
    try:
        output = subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'json', path], timeout=20)
        return float(json.loads(output)['format']['duration'])
    except (OSError, KeyError, ValueError, subprocess.SubprocessError):
        return None


def _language_matches(filename, language):
    hint = subtitle_language_hint(filename)
    traditional = language.country == 'TW' or language.script == 'Hant'
    if traditional:
        return hint == 'zt'
    if language.alpha3 == 'zho':
        return hint in ('zh', 'bilingual', 'zh-unknown')
    suffix = '.' + (language.alpha2 or '').lower() + '.'
    return suffix in filename.lower()


def _same_work(video, sibling_guess):
    guessed = sibling_guess.get('title')
    if not isinstance(guessed, str):
        return False
    guessed = normalize_name(guessed)
    return any(guessed == normalize_name(title) for title in title_variants(video))


class LocalSiblingSubtitle(Subtitle):
    provider_name = 'localsibling'

    def __init__(self, language, path, video):
        super().__init__(language, page_link=path)
        self.path = path
        self.video = video
        self.release_info = os.path.basename(path)

    @property
    def id(self):
        return self.path

    def get_matches(self, video):
        if isinstance(video, Episode):
            return {'series', 'year', 'season', 'episode', 'source', 'release_group',
                    'audio_codec', 'resolution', 'video_codec', 'streaming_service',
                    'hearing_impaired'}
        return {'title', 'year', 'source', 'edition', 'release_group', 'audio_codec',
                'resolution', 'video_codec', 'streaming_service', 'hearing_impaired'}


class LocalSiblingProvider(Provider):
    languages = {Language('zho', 'CN'), Language('zho', 'TW'), Language('eng'), Language('jpn')}
    video_types = (Episode, Movie)
    subtitle_class = LocalSiblingSubtitle

    def initialize(self):
        pass

    def terminate(self):
        pass

    def list_subtitles(self, video, languages):
        if not os.path.isfile(video.name):
            return []
        target_duration = _duration(video.name)
        if target_duration is None:
            return []
        directory = os.path.dirname(video.name)
        target_guess = guessit(os.path.basename(video.name), {
            'type': 'episode' if isinstance(video, Episode) else 'movie'})
        results = []
        for filename in os.listdir(directory):
            sibling_path = os.path.join(directory, filename)
            if sibling_path == video.name or os.path.splitext(filename)[1].lower() not in _VIDEO_EXTENSIONS:
                continue
            sibling_guess = guessit(filename, {
                'type': 'episode' if isinstance(video, Episode) else 'movie'})
            if not _same_work(video, sibling_guess):
                continue
            if isinstance(video, Episode):
                if (sibling_guess.get('season'), sibling_guess.get('episode')) != \
                        (target_guess.get('season'), target_guess.get('episode')):
                    continue
            sibling_duration = _duration(sibling_path)
            if sibling_duration is None or abs(sibling_duration - target_duration) > 2.0:
                continue
            stem = os.path.splitext(sibling_path)[0]
            for subtitle_name in os.listdir(directory):
                subtitle_path = os.path.join(directory, subtitle_name)
                if not subtitle_path.startswith(stem + '.') or \
                        os.path.splitext(subtitle_name)[1].lower() not in _SUBTITLE_EXTENSIONS:
                    continue
                for language in languages:
                    if _language_matches(subtitle_name, language):
                        results.append(self.subtitle_class(language, subtitle_path, video))
        return results

    def download_subtitle(self, subtitle):
        with open(subtitle.path, 'rb') as source:
            subtitle.content = fix_line_ending(source.read())
        subtitle.format = os.path.splitext(subtitle.path)[1].lstrip('.').lower()
