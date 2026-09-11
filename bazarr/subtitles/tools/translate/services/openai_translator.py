# coding=utf-8

"""Context-aware subtitle translation for OpenAI-compatible chat APIs."""

import json
import logging
import os
import re
import time

import pysubs2
import requests
from charset_normalizer import detect

from app.config import settings
from app.jobs_queue import jobs_queue
from languages.get_languages import language_from_alpha2, language_from_alpha3
from sonarr.history import history_log
from radarr.history import history_log_movie
from ..openai_profiles import get_active_openai_profile
from ..core.translator_utils import add_translator_info, create_process_result, get_description


logger = logging.getLogger(__name__)

_ENGLISH_NAME_STOPWORDS = {
    'A', 'All', 'And', 'Are', 'But', 'Can', 'Come', 'Did', 'Do', 'For', 'Good', 'Great',
    'Have', 'He', 'Hello', 'Hey', 'How', 'I', 'If', 'In', 'Is', 'It', 'Just', 'Let',
    'Look', 'Maybe', 'My', 'No', 'Now', 'Oh', 'Okay', 'Please', 'Right', 'She', 'So',
    'Thank', 'That', 'The', 'Then', 'There', 'They', 'This', 'To', 'Wait', 'We', 'Well',
    'What', 'When', 'Where', 'Who', 'Why', 'Yes', 'You', 'Your',
}

_ENGLISH_SOUND_WORDS = {
    'applause', 'breathes', 'breathing', 'cheering', 'chuckles', 'closes', 'coughs',
    'cries', 'crying', 'door', 'exhales', 'gasps', 'groans', 'grunts', 'inhales',
    'laughing', 'laughs', 'music', 'mumbles', 'mumbling', 'phone', 'rings', 'screams',
    'shouts', 'sighs', 'singing', 'sniffs', 'sobbing', 'speaks', 'whispers', 'yells',
}


class TranslationConstraintError(ValueError):
    def __init__(self, message, partial_result, invalid_ids):
        super().__init__(message)
        self.partial_result = partial_result
        self.invalid_ids = set(invalid_ids)


def _plain(text):
    return re.sub(r'\s+', ' ', text.replace(r'\N', ' ').replace('\n', ' ')).strip()


def normalize_chinese_translation(text):
    """Apply the punctuation and speaker-marker rules used by Simplified Chinese subtitles."""
    text = re.sub(r'(^|\s)[—–-]\s*(?=\S)', r'\1-', text)
    text = re.sub(r'\.{3,}', '…', text)
    text = re.sub(r'[，。]', ' ', text)
    text = re.sub(r'(?<!\d),(?!\d)', ' ', text)
    text = re.sub(r'(?<![A-Za-z0-9])\.(?![A-Za-z0-9])', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s+([？！!?：:；;、”’》）】])', r'\1', text)
    text = re.sub(r'([“‘《（【])\s+', r'\1', text)
    return text


def _speaker_marker_count(text):
    return len(re.findall(r'(^|\s)-(?=\S)', _plain(text)))


def _is_dual_speaker(text):
    return _speaker_marker_count(text) >= 2


def _looks_like_sound_description(value):
    words = {word.lower() for word in re.findall(r"[A-Za-z]+", value)}
    return bool(words & _ENGLISH_SOUND_WORDS)


def _extract_english_name_candidates(lines):
    counts = {}
    speaker_names = set()
    for line in lines:
        for match in re.finditer(r'\[\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\s*]', line):
            name = match.group(1)
            if not _looks_like_sound_description(name):
                speaker_names.add(name)
        for match in re.finditer(r'\b[A-Z][A-Za-z]{1,}(?:\s+[A-Z][A-Za-z]{1,})*\b', line):
            name = match.group(0)
            if name not in _ENGLISH_NAME_STOPWORDS and not _looks_like_sound_description(name):
                counts[name] = counts.get(name, 0) + 1
    recurring = {name for name, count in counts.items() if count >= 2}
    return sorted(speaker_names | recurring, key=str.casefold)


def load_subtitles_with_encoding(path):
    try:
        return pysubs2.load(path, encoding='utf-8')
    except UnicodeDecodeError:
        with open(path, 'rb') as source:
            raw = source.read()
        encoding = (detect(raw) or {}).get('encoding')
        if not encoding:
            raise
        logger.info('BAZARR detected %s encoding for translation source %s', encoding, path)
        return pysubs2.load(path, encoding=encoding)


def wrap_translation(text, width=24):
    """Use a valid model-supplied break, or find a local clause boundary as fallback."""
    marked_parts = re.split(r'\s*<br\s*/?>\s*', text, flags=re.I)
    text = _plain(''.join(marked_parts))
    if len(text) <= width:
        return text
    if len(marked_parts) == 2:
        marked_lines = [_plain(part) for part in marked_parts]
        if all(marked_lines) and all(len(line) <= width + 6 for line in marked_lines):
            return r'\N'.join(marked_lines)
    line_count = int((len(text) + width - 1) / width)
    no_start = '，。！？；：、”’》）】'
    no_end = '“‘《（【'
    clause_starts = ('但是', '不过', '然而', '所以', '因此', '而且', '如果', '虽然',
                     '因为', '同时', '然后', '而', '但', '却')
    lines = []
    start = 0
    for remaining_lines in range(line_count, 1, -1):
        remaining = len(text) - start
        target = start + int(round(remaining / remaining_lines))
        lower = max(start + 1, target - 4)
        upper = min(len(text) - (remaining_lines - 1), target + 4)
        punctuation_breaks = [index for index in range(lower, upper + 1)
                              if text[index - 1] in '，。！？；：、 ' and text[index] not in no_start]
        clause_breaks = [index for index in range(lower, upper + 1)
                         if any(text.startswith(word, index) for word in clause_starts)]
        preferred_breaks = punctuation_breaks or clause_breaks
        end = min(preferred_breaks, key=lambda index: abs(index - target)) if preferred_breaks else target
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
        'openai_ass_chinese', 'Noto Sans CJK SC', '#FFFF80', margin)
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
        profile = get_active_openai_profile()
        preserve_english_names = str(self.from_lang).lower() in {'en', 'eng'}
        batch_text = ' '.join(item['content'] for item in context + targets)
        english_names = ([name for name in getattr(self, 'english_names', [])
                          if re.search(r'(?<![A-Za-z])%s(?![A-Za-z])' % re.escape(name), batch_text)]
                         if preserve_english_names else [])
        if preserve_english_names:
            name_instruction = (
                '3. Keep personal names, character names, nicknames and speaker labels in their original '
                'Latin spelling. Never translate or transliterate them into Chinese.'
            )
            if english_names:
                name_instruction += ' Names that must remain unchanged: %s.' % ', '.join(english_names)
        else:
            name_instruction = (
                '3. Use established Simplified Chinese translations or transliterations for personal names '
                'and keep them consistent.'
            )
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
            '%s\n'
            '4. Correct malformed source wording only when the intended meaning is strongly supported by adjacent dialogue; otherwise preserve the ambiguity.\n'
            '5. Never add unstated specifications, directions, relationships, actions or plot facts.\n'
            '6. Never move information between cues or complete a sentence early. Each output must contain only information expressed in its matching source cue.\n'
            '7. Preserve interruptions, hesitation and unfinished sentences with Chinese ellipses.\n'
            '8. When two speakers share one cue, prefix both utterances with an ASCII hyphen and keep them on one physical line, exactly like: -你好吗？ -我很好\n'
            '9. Preserve wordplay, catchphrases, cultural references and invented words with a concise Chinese adaptation; never flatten them into a generic meaning.\n'
            '10. Do not use commas or periods in Chinese; replace each with one space. Keep question marks and necessary exclamation marks, colons, quotation marks and ellipses.\n'
            '11. Prefer concise Chinese lines of 18 full-width characters or fewer. Never exceed 24 characters per display line; for a longer single-speaker translation, insert exactly one literal <br> at a natural clause boundary. Never split a word, name or fixed phrase.\n'
            '12. Return every requested [number] exactly once and on one physical line; <br> is the only allowed line-break marker.\n'
            '13. Output numbered translations only, without Markdown, explanations or source text.\n\n'
            'Media context:\n%s\n\n'
            'Surrounding context (understand only; do not output these numbers):\n%s\n\n'
            'Subtitles to translate:\n%s'
        ) % (name_instruction, description or '(none)',
             _numbered(surrounding, include_translation=True), _numbered(targets))
        payload = {
            'model': profile['model'],
            'temperature': 0,
            # Bound verbose/reasoning-capable local models while leaving enough room per cue.
            'max_tokens': max(512, min(8192, len(targets) * 128)),
            'messages': [{'role': 'user', 'content': prompt}],
        }
        headers = {'Content-Type': 'application/json'}
        api_key = profile['api_key']
        if api_key:
            headers['Authorization'] = 'Bearer ' + api_key
        response = requests.post(self._endpoint(profile['base_url']), json=payload,
                                 headers=headers, timeout=int(settings.translator.openai_timeout))
        response.raise_for_status()
        body = response.json()
        result = _extract_numbered(body['choices'][0]['message']['content'], target_ids)
        invalid_ids = set()
        for target in targets:
            index = target['index']
            result[index] = normalize_chinese_translation(result[index])
            if (_is_dual_speaker(target['content']) and
                    (_speaker_marker_count(result[index]) < 2 or '<br' in result[index].lower())):
                invalid_ids.add(index)
            if preserve_english_names:
                for name in english_names:
                    if re.search(r'(?<![A-Za-z])%s(?![A-Za-z])' % re.escape(name), target['content']) and \
                            not re.search(r'(?<![A-Za-z])%s(?![A-Za-z])' % re.escape(name), result[index]):
                        invalid_ids.add(index)
        if invalid_ids:
            partial_result = {index: text for index, text in result.items() if index not in invalid_ids}
            raise TranslationConstraintError(
                'Model output violated name or speaker constraints for subtitles %s' %
                ', '.join(str(index) for index in sorted(invalid_ids)),
                partial_result, invalid_ids)
        return result

    def _translate_batch(self, targets, context, description):
        error = None
        translated = {}
        pending = list(targets)
        working_context = [dict(item) for item in context]
        for attempt in range(3):
            try:
                translated.update(self._request(pending, working_context, description))
                return translated
            except TranslationConstraintError as exc:
                error = exc
                translated.update(exc.partial_result)
                pending = [item for item in pending if item['index'] in exc.invalid_ids]
                for item in working_context:
                    if item['index'] in translated:
                        item['translation'] = translated[item['index']]
                logger.warning('Retrying only constrained subtitle cues %s (attempt %d/3)',
                               sorted(exc.invalid_ids), attempt + 1)
            except (requests.RequestException, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise RuntimeError(f'OpenAI-compatible translation failed after 3 attempts: {error}') from error

    def translate(self, job_id):
        profile = get_active_openai_profile()
        subtitles = load_subtitles_with_encoding(self.source_srt_file)
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
        self.english_names = (_extract_english_name_candidates(originals)
                              if str(self.from_lang).lower() in {'en', 'eng'} else [])
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
            if self.to_lang == 'zho':
                chinese = normalize_chinese_translation(translated[index])
                if not _is_dual_speaker(originals[index]):
                    chinese = wrap_translation(chinese)
            else:
                chinese = translated[index]
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
                    text='# Subtitles translated with %s # ' % profile['model'],
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
                                '# Subtitles translated with %s # ' % profile['model'])
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
