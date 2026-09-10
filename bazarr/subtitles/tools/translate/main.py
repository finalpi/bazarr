# coding=utf-8

import logging
import os


from subliminal_patch.core import get_subtitle_path
from subzero.language import Language

from .core.translator_utils import validate_translation_params, convert_language_codes
from .services.translator_factory import TranslatorFactory
from .traditional import convert_simplified_file
from subtitles.translation_priority import mark_as_llm_subtitle
from languages.get_languages import alpha3_from_alpha2
from app.config import settings
from app.jobs_queue import jobs_queue
from app.notifier import send_notifications, send_notifications_movie
from utilities.helper import get_target_folder


def _send_llm_translation_notification(success, translator_type, to_lang, media_type,
                                       sonarr_series_id, sonarr_episode_id, radarr_id,
                                       output_path=None, error=None):
    if translator_type not in {'openai_compatible', 'gemini', 'lingarr'}:
        return

    if translator_type == 'openai_compatible':
        model = settings.translator.openai_model
    elif translator_type == 'gemini':
        model = settings.translator.gemini_model
    else:
        model = 'Lingarr'

    if success:
        message = f'LLM translation completed to {to_lang.upper()} using {model}'
        if output_path:
            message += f': {os.path.basename(output_path)}'
    else:
        reason = str(error)[:500] if error else 'Unknown error'
        for secret_name in ('openai_api_key', 'gemini_key', 'lingarr_token'):
            secret = str(getattr(settings.translator, secret_name, '') or '')
            if secret:
                reason = reason.replace(secret, '[redacted]')
        message = f'LLM translation failed to {to_lang.upper()} using {model}: {reason}'

    try:
        if media_type == 'episode' and sonarr_series_id and sonarr_episode_id:
            send_notifications(sonarr_series_id, sonarr_episode_id, message)
        elif media_type == 'movie' and radarr_id:
            send_notifications_movie(radarr_id, message)
    except Exception:
        logging.exception('Failed to send LLM translation notification')


def translate_subtitles_file(video_path, source_srt_file, from_lang, to_lang, forced, hi,
                             media_type, sonarr_series_id, sonarr_episode_id, radarr_id, metadata, job_id=None,
                             low_priority=False):
    if not job_id:
        jobs_queue.add_job_from_function(f'Translating from {from_lang.upper()} to {to_lang.upper()} using '
                                         f'{settings.translator.translator_type.replace("_", " ").title()}',
                                         is_progress=True)
        return

    translator_type = settings.translator.translator_type or 'google'
    dest_srt_file = None
    try:
        logging.debug(f'Translation request: video={video_path}, source={source_srt_file}, from={from_lang}, to={to_lang}')

        validate_translation_params(video_path, source_srt_file, from_lang, to_lang)
        lang_obj, orig_to_lang = convert_language_codes(to_lang, forced, hi)

        logging.debug(f'BAZARR is translating in {lang_obj} this subtitles {source_srt_file}')

        # get the destination path if the subtitles are alongside the video
        styled_ass = translator_type == 'openai_compatible' and settings.translator.openai_styled_ass
        dest_srt_file_if_alongside_video = get_subtitle_path(
            video_path,
            language=lang_obj if isinstance(lang_obj, Language) else lang_obj.subzero_language(),
            extension='.ass' if styled_ass else '.srt',
            forced_tag=forced,
            hi_tag=hi and not low_priority,
        )
        if low_priority:
            dest_srt_file_if_alongside_video = mark_as_llm_subtitle(
                dest_srt_file_if_alongside_video, video_path)

        # get the real destination path taking into account if the user set up Bazarr to store external subtitles in
        #  a custom folder or relative folder
        dest_dir_for_srt = get_target_folder(video_path)
        if dest_dir_for_srt:
            # if the user has set up Bazarr to store external subtitles in a custom folder, we need to add the custom
            # folder to the destination path
            dest_srt_file = os.path.join(dest_dir_for_srt, os.path.basename(dest_srt_file_if_alongside_video))
        else:
            # otherwise, we just use the destination path as it is
            dest_srt_file = dest_srt_file_if_alongside_video

        logging.debug(f'Using translator type: {translator_type}')

        translator = TranslatorFactory.create_translator(
            translator_type,
            source_srt_file=source_srt_file,
            dest_srt_file=dest_srt_file,
            lang_obj=lang_obj,
            from_lang=from_lang,
            to_lang=alpha3_from_alpha2(to_lang),
            media_type=media_type,
            video_path=video_path,
            orig_to_lang=orig_to_lang,
            forced=forced,
            hi=hi,
            sonarr_series_id=sonarr_series_id,
            sonarr_episode_id=sonarr_episode_id,
            radarr_id=radarr_id
        )

        logging.debug(f'Created translator instance: {translator.__class__.__name__}')
        result = translator.translate(job_id=job_id)
        if result is False:
            raise RuntimeError(f'{translator.__class__.__name__} returned a failed translation result')
        logging.debug(f'BAZARR saved translated subtitles to {dest_srt_file}')
        from api.subtitles.subtitles import postprocess_subtitles
        # Call postprocess_subtitles after translation
        postprocess_subtitles(dest_srt_file, media_type, metadata, sonarr_episode_id if media_type == 'episode' else radarr_id)
        if to_lang == 'zh' and settings.translator.auto_translate_missing_chinese:
            traditional_language, _ = convert_language_codes('zt', forced, hi)
            traditional_path = get_subtitle_path(
                video_path,
                language=traditional_language if isinstance(traditional_language, Language)
                else traditional_language.subzero_language(),
                extension='.ass' if styled_ass else '.srt', forced_tag=forced, hi_tag=hi and not low_priority)
            if low_priority:
                traditional_path = mark_as_llm_subtitle(traditional_path, video_path)
            if dest_dir_for_srt:
                traditional_path = os.path.join(dest_dir_for_srt, os.path.basename(traditional_path))
            convert_simplified_file(dest_srt_file, traditional_path)
            postprocess_subtitles(traditional_path, media_type, metadata,
                                  sonarr_episode_id if media_type == 'episode' else radarr_id)
            logging.info('BAZARR generated Traditional Chinese locally from %s', dest_srt_file)
        _send_llm_translation_notification(
            True, translator_type, to_lang, media_type, sonarr_series_id, sonarr_episode_id, radarr_id,
            output_path=dest_srt_file)
        return result

    except Exception as e:
        logging.error(f'Translation failed: {str(e)}', exc_info=True)
        _send_llm_translation_notification(
            False, translator_type, to_lang, media_type, sonarr_series_id, sonarr_episode_id, radarr_id,
            output_path=dest_srt_file, error=e)
        raise

    finally:
        jobs_queue.update_job_name(job_id=job_id,
                                   new_job_name=f'Translated from {from_lang.upper()} to {to_lang.upper()} using '
                                                f'{settings.translator.translator_type.replace("_", " ").title()}')
