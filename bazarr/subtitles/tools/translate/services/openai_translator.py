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
    """Balance Chinese lines without leaving punctuation at a line boundary."""
    text = _plain(text)
    if len(text) <= width:
        return text
    line_count = int((len(text) + width - 1) / width)
    no_start = '，。！？；：、”’》）】'
    no_end = '“‘《（【'
    lines = []
    start = 0
    for remaining_lines in range(line_count, 1, -1):
        remaining = len(text) - start
        target = start + int(round(remaining / remaining_lines))
        lower = max(start + 1, target - 4)
        upper = min(len(text) - (remaining_lines - 1), target + 4)
        punctuation_breaks = [index for index in range(lower, upper + 1)
                              if text[index - 1] in '，。！？；：、 ' and text[index] not in no_start]
        end = min(punctuation_breaks, key=lambda index: abs(index - target)) if punctuation_breaks else target
        if (end < len(text) and text[end - 1].isascii() and text[end].isascii()
                and text[end - 1].isalnum() and text[end].isalnum()):
            word_start, word_end = end, end
            while word_start > start and text[word_start - 1].isascii() and text[word_start - 1].isalnum():
                word_start -= 1
            while word_end < len(text) and text[word_end].isascii() and text[word_end].isalnum():
                word_end += 1
            end = word_start if word_start > start else word_end
        while end < len(text) and text[end] in no_start:
            end += 1
        while end > start + 1 and text[end - 1] in no_end:
            end -= 1
        lines.append(text[start:end].strip())
        start = end
    lines.append(text[start:].strip())
    return r'\N'.join(lines)


def _ass_color(value, fallback):
    """Convert a web color to the RGBA value expected by pysubs2."""
    match = re.fullmatch(r'#?([0-9a-fA-F]{6})', str(value).strip())
    color = match.group(1) if match else fallback.lstrip('#')
    return pysubs2.Color(*(int(color[index:index + 2], 16) for index in (0, 2, 4)))


def _ass_style(prefix, default_font, default_color, margin):
    outline_color = getattr(settings.translator, prefix + '_outline_color')
    return pysubs2.SSAStyle(
        fontname=str(getattr(settings.translator, prefix + '_font_name')).strip() or default_font,
        fontsize=int(getattr(settings.translator, prefix + '_font_size')),
        primarycolor=_ass_color(getattr(settings.translator, prefix + '_primary_color'), default_color),
        outlinecolor=_ass_color(outline_color, '#000000'),
        backcolor=_ass_color(outline_color, '#000000'),
        bold=bool(getattr(settings.translator, prefix + '_bold')),
        outline=int(getattr(settings.translator, prefix + '_outline')),
        shadow=int(getattr(settings.translator, prefix + '_shadow')),
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginl=40,
        marginr=40,
        marginv=margin,
    )


def apply_ass_style(subtitles, bilingual=False):
    subtitles.info['PlayResX'] = '1920'
    subtitles.info['PlayResY'] = '1080'
    subtitles.info['ScaledBorderAndShadow'] = 'yes'
    subtitles.info['Collisions'] = 'Reverse'
    margin = int(settings.translator.openai_ass_bilingual_margin_v)
    subtitles.styles['Chinese'] = _ass_style(
        'openai_ass_chinese', 'Noto Sans CJK SC', '#FFE66D', margin)
    subtitles.styles['Original'] = _ass_style(
        'openai_ass_original', 'Arial', '#FFFFFF', margin)

    if not bilingual:
        for cue in subtitles:
            cue.style = 'Chinese'
        return subtitles

    events = []
    for cue in subtitles:
        original, separator, chinese = cue.text.partition(r'\N')
        if not separator:
            cue.style = 'Chinese'
            events.append(cue)
            continue
        original_cue = cue.copy()
        original_cue.text = original
        original_cue.style = 'Original'
        cue.text = chinese
        cue.style = 'Chinese'
        events.extend((original_cue, cue))
    subtitles.events = events
    return subtitles


def _extract_numbered(content, target_ids):
    content = content.strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:text)?\s*|\s*```$', '', content, flags=re.I | re.S)
    requested = set(target_ids)
    items = []
    seen = set()
    for line in content.splitlines():
        match = re.match(r'^\s*(?:\[(\d+)]|(\d+)[.)、])\s*(.+?)\s*$', line)
        if not match:
            continue
        index = int(match.group(1) or match.group(2))
        translation = match.group(3).strip()
        if index in seen or not translation:
            raise ValueError('Model returned invalid or duplicate subtitle indices')
        seen.add(index)
        items.append((index, translation))
    indices = [item[0] for item in items]
    if set(indices) == requested and len(indices) == len(target_ids):
        return dict(items)
    relative = list(range(len(target_ids)))
    one_based = list(range(1, len(target_ids) + 1))
    shifted = [index + 1 for index in target_ids]
    if len(items) == len(target_ids) and indices in (relative, one_based, shifted):
        return {target: item[1] for target, item in zip(target_ids, items)}
    raise ValueError('Model did not return every requested subtitle index')


def _numbered(items, include_translation=False):
    lines = []
    for item in items:
        line = '[%d] %s' % (item['index'], item['content'])
        if include_translation and item.get('translation'):
            line += ' => ' + item['translation']
        lines.append(line)
    return '\n'.join(lines) or '(none)'


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
        target_set = set(target_ids)
        surrounding = [item for item in context if item['index'] not in target_set]
        prompt = (
            'Translate the following English audiovisual subtitles into polished Simplified Chinese.\n\n'
            'Read all cues as one continuous scene and use adjacent cues only to understand pronouns, '
            'fragments, tone, jokes, terminology and implied intent.\n\n'
            'Requirements:\n'
            '1. Produce concise, idiomatic spoken Chinese suitable for streaming subtitles.\n'
            '2. Preserve the precise meaning, emotional intensity, speaker changes, names and technical terms.\n'
            '3. Use established Simplified Chinese transliterations for personal names and keep them consistent.\n'
            '4. Correct malformed source wording only when the intended meaning is strongly supported by adjacent dialogue; otherwise preserve the ambiguity.\n'
            '5. Never add unstated specifications, directions, relationships, actions or plot facts.\n'
            '6. Never move information between cues or complete a sentence early. Each output must contain only information expressed in its matching source cue.\n'
            '7. Preserve interruptions, hesitation and unfinished sentences with Chinese ellipses.\n'
            '8. Retain separate leading dashes when a cue contains multiple speakers.\n'
            '9. Preserve wordplay, catchphrases, cultural references and invented words with a concise Chinese adaptation; never flatten them into a generic meaning.\n'
            '10. Return every requested [number] exactly once and on one line.\n'
            '11. Output numbered translations only, without Markdown, explanations or source text.\n\n'
            'Media context:\n%s\n\n'
            'Surrounding context (understand only; do not output these numbers):\n%s\n\n'
            'Subtitles to translate:\n%s'
        ) % (description or '(none)', _numbered(surrounding, include_translation=True), _numbered(targets))
        payload = {
            'model': settings.translator.openai_model,
            'temperature': 0,
            # Bound verbose/reasoning-capable local models while leaving enough room per cue.
            'max_tokens': max(512, min(8192, len(targets) * 128)),
            'messages': [{'role': 'user', 'content': prompt}],
        }
        headers = {'Content-Type': 'application/json'}
        api_key = str(settings.translator.openai_api_key).strip()
        if api_key:
            headers['Authorization'] = 'Bearer ' + api_key
        response = requests.post(self._endpoint(settings.translator.openai_base_url), json=payload,
                                 headers=headers, timeout=int(settings.translator.openai_timeout))
        response.raise_for_status()
        body = response.json()
        return _extract_numbered(body['choices'][0]['message']['content'], target_ids)

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
            context = [{'index': index, 'content': originals[index],
                        'translation': translated.get(index)}
                       for index in range(context_start, context_end)]
            translated.update(self._translate_batch(targets, context, description))
            jobs_queue.update_job_progress(job_id=job_id, progress_value=end,
                                           progress_message=self.source_srt_file)
        bilingual = bool(settings.translator.openai_bilingual)
        for index, cue in enumerate(subtitles):
            chinese = wrap_translation(translated[index]) if self.to_lang == 'zho' else translated[index]
            cue.text = originals[index] + r'\N' + chinese if bilingual else chinese
        styled_ass = bool(settings.translator.openai_styled_ass)
        if styled_ass:
            apply_ass_style(subtitles, bilingual=bilingual)
            if settings.translator.translator_info:
                first_start = subtitles[0].start
                info_end = min(first_start, 5000)
                info_start = 1000 if info_end == 5000 else 0
                subtitles.insert(0, pysubs2.SSAEvent(
                    start=info_start,
                    end=info_end,
                    text='# Subtitles translated with %s # ' % settings.translator.openai_model,
                    style='Original',
                ))
        temporary = self.dest_srt_file + '.tmp'
        try:
            subtitles.save(temporary, format_='ass' if styled_ass else 'srt', encoding='utf-8')
            os.replace(temporary, self.dest_srt_file)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
        if settings.translator.translator_info and not styled_ass:
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
