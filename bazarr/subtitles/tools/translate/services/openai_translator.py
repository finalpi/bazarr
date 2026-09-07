# coding=utf-8

"""Context-aware subtitle translation for OpenAI-compatible chat APIs."""

import json
import logging
import os
import re
import time

import pysubs2
import requests

from app.config import settings
from app.jobs_queue import jobs_queue
from languages.get_languages import language_from_alpha2, language_from_alpha3
from sonarr.history import history_log
from radarr.history import history_log_movie
from ..core.translator_utils import add_translator_info, create_process_result, get_description


logger = logging.getLogger(__name__)


def _plain(text):
    return re.sub(r'\s+', ' ', text.replace(r'\N', ' ').replace('\n', ' ')).strip()


def wrap_translation(text, width=18):
    """Break translated text at sentence boundaries without changing cue timing."""
    text = _plain(text)
    if len(text) <= width:
        return text
    pieces = [piece for piece in re.split(r'(?<=[。！？；，、：])', text) if piece]
    lines = []
    current = ''
    for piece in pieces:
        if current and len(current) + len(piece) > width:
            lines.append(current)
            current = ''
        while len(piece) > width:
            room = width - len(current)
            current += piece[:room]
            lines.append(current)
            current = ''
            piece = piece[room:]
        current += piece
    if current:
        lines.append(current)
    return r'\N'.join(lines)


def _extract_json(content):
    content = content.strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content, flags=re.I | re.S)
    start, end = content.find('{'), content.rfind('}')
    if start < 0 or end < start:
        raise ValueError('Model response does not contain a JSON object')
    return json.loads(content[start:end + 1])


class OpenAICompatibleTranslatorService:

    def __init__(self, source_srt_file, dest_srt_file, to_lang, media_type, sonarr_series_id,
                 sonarr_episode_id, radarr_id, forced, hi, video_path, from_lang, orig_to_lang, **kwargs):
        self.source_srt_file = source_srt_file
        self.dest_srt_file = dest_srt_file
        self.to_lang = to_lang
        self.media_type = media_type
        self.sonarr_series_id = sonarr_series_id
        self.sonarr_episode_id = sonarr_episode_id
        self.radarr_id = radarr_id
        self.forced = forced
        self.hi = hi
        self.video_path = video_path
        self.from_lang = from_lang
        self.orig_to_lang = orig_to_lang

    @staticmethod
    def _endpoint(base_url):
        base_url = base_url.rstrip('/')
        return base_url if base_url.endswith('/chat/completions') else base_url + '/chat/completions'

    def _request(self, targets, context, description):
        target_ids = [item['index'] for item in targets]
        system = (
            'You are a professional audiovisual subtitle translator. Translate into Simplified Chinese. '
            'Use all supplied neighboring cues to resolve pronouns, names, tone, jokes and terminology. '
            'Translate only entries whose translate field is true. Preserve meaning and natural spoken Chinese. '
            'Do not merge, split, omit or renumber cues. Return JSON only as '
            '{"translations":[{"index":integer,"translation":string}]}. '
            'Return every requested index exactly once. Do not include timestamps or the source text. '
            'Keep each translation concise enough for its original display interval. '
            'Use Chinese punctuation and sentence boundaries; do not insert line breaks.'
        )
        if description:
            system += '\nMedia context: ' + description
        payload = {
            'model': settings.translator.openai_model,
            'temperature': 0.2,
            # Bound verbose/reasoning-capable local models while leaving enough room per cue.
            'max_tokens': max(512, min(8192, len(targets) * 128)),
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps({'cues': context}, ensure_ascii=False)},
            ],
        }
        headers = {'Content-Type': 'application/json'}
        api_key = str(settings.translator.openai_api_key).strip()
        if api_key:
            headers['Authorization'] = 'Bearer ' + api_key
        response = requests.post(self._endpoint(settings.translator.openai_base_url), json=payload,
                                 headers=headers, timeout=int(settings.translator.openai_timeout))
        response.raise_for_status()
        body = response.json()
        parsed = _extract_json(body['choices'][0]['message']['content'])
        translations = parsed.get('translations')
        if not isinstance(translations, list):
            raise ValueError('Model response is missing translations')
        result = {}
        for item in translations:
            index = item.get('index')
            translation = item.get('translation')
            if index in result or index not in target_ids or not isinstance(translation, str) or not translation.strip():
                raise ValueError('Model returned invalid or duplicate subtitle indices')
            result[index] = translation.strip()
        if set(result) != set(target_ids):
            raise ValueError('Model did not return every requested subtitle index')
        return result

    def _translate_batch(self, targets, context, description):
        error = None
        for attempt in range(3):
            try:
                return self._request(targets, context, description)
            except (requests.RequestException, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError('OpenAI-compatible translation failed after 3 attempts') from error

    def translate(self, job_id):
        subtitles = pysubs2.load(self.source_srt_file, encoding='utf-8')
        subtitles.remove_miscellaneous_events()
        if not subtitles:
            raise ValueError('Subtitle file has no dialogue cues')
        batch_size = int(settings.translator.openai_batch_size)
        overlap = int(settings.translator.openai_context_lines)
        description = get_description('movies' if self.media_type == 'movie' else self.media_type,
                                      self.radarr_id, self.sonarr_series_id)
        jobs_queue.update_job_progress(job_id=job_id, progress_max=len(subtitles),
                                       progress_message=self.source_srt_file)
        originals = [_plain(cue.text) for cue in subtitles]
        translated = {}
        for start in range(0, len(subtitles), batch_size):
            end = min(start + batch_size, len(subtitles))
            context_start, context_end = max(0, start - overlap), min(len(subtitles), end + overlap)
            targets = [{'index': index, 'content': originals[index]} for index in range(start, end)]
            context = [{'index': index, 'content': originals[index], 'translate': start <= index < end}
                       for index in range(context_start, context_end)]
            translated.update(self._translate_batch(targets, context, description))
            jobs_queue.update_job_progress(job_id=job_id, progress_value=end,
                                           progress_message=self.source_srt_file)
        bilingual = bool(settings.translator.openai_bilingual)
        for index, cue in enumerate(subtitles):
            chinese = wrap_translation(translated[index]) if self.to_lang == 'zho' else translated[index]
            cue.text = originals[index] + r'\N' + chinese if bilingual else chinese
        temporary = self.dest_srt_file + '.tmp'
        try:
            subtitles.save(temporary, format_='srt', encoding='utf-8')
            os.replace(temporary, self.dest_srt_file)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
        if settings.translator.translator_info:
            add_translator_info(self.dest_srt_file,
                                '# Subtitles translated with %s # ' % settings.translator.openai_model)
        message = '%s subtitles translated to %s.' % (
            language_from_alpha2(self.from_lang), language_from_alpha3(self.to_lang))
        result = create_process_result(message, self.video_path, self.orig_to_lang, self.forced, self.hi,
                                       self.dest_srt_file, self.media_type)
        if self.media_type == 'episode':
            history_log(action=6, sonarr_series_id=self.sonarr_series_id,
                        sonarr_episode_id=self.sonarr_episode_id, result=result)
        else:
            history_log_movie(action=6, radarr_id=self.radarr_id, result=result)
        return self.dest_srt_file
