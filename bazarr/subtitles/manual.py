# coding=utf-8
# fmt: off

import os
import sys
import logging
import json
import re
import time
import subliminal

from subzero.language import Language
from subliminal_patch.core import save_subtitles
from .audio_validation import validate_download
from subliminal_patch.core_persistent import list_all_subtitles, download_subtitles
from subliminal_patch.score import compute_score, DEFAULT_SCORES

from languages.get_languages import alpha3_from_alpha2
from app.config import settings, get_array_from
from utilities.helper import get_target_folder, force_unicode
from utilities.path_mappings import path_mappings
from app.database import (database, get_profiles_list, select, TableEpisodes, TableShows, get_audio_profile_languages,
                          get_profile_id, TableMovies)
from app.jobs_queue import JobExecutionError, jobs_queue
from app.notifier import send_notifications, send_notifications_movie
from sonarr.history import history_log
from radarr.history import history_log_movie
from subtitles.indexer.series import store_subtitles
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.processing import ProcessSubtitlesResult

from bazarr.subtitles.cache import subtitle_cache
from .pool import update_pools, _get_pool, _manual_search_pool
from .utils import get_video, _get_lang_obj, _get_scores, _set_forced_providers
from .processing import process_subtitle
from .rejections import load_rejections, get_rejection, record_rejection, clear_rejection


def validate_manual_search_options(keyword, providers, available_providers):
    """Validate request-local options without changing configured providers or media metadata."""
    keyword = (keyword or '').strip()
    if len(keyword) > 200:
        raise ValueError('Search keyword must be 200 characters or less')
    if providers is None:
        return keyword or None, available_providers
    if not providers or any(not provider.strip() for provider in providers):
        raise ValueError('Select at least one subtitle provider')
    providers = list(dict.fromkeys(provider.strip() for provider in providers))
    available = set(available_providers or [])
    if any(provider not in available for provider in providers):
        raise ValueError('Selected subtitle providers must be enabled and currently available')
    return keyword or None, providers


def manual_search(path, profile_id, providers, sceneName, title, media_type, keyword=None):
    logging.debug(f'BAZARR Manually searching subtitles for this file: {path}')

    if not providers:
        logging.info("BAZARR All providers are throttled")
        return 'All providers are throttled'

    # Manual source selections must never alter the persistent pools used by
    # concurrent automatic searches and downloads.
    with _manual_search_pool(media_type, profile_id, providers=providers) as pool:
        return _manual_search_with_pool(path, profile_id, providers, sceneName, title, media_type, pool, keyword)


def _manual_search_with_pool(path, profile_id, providers, sceneName, title, media_type, pool, keyword=None):
    final_subtitles = []

    language_set, original_format = _get_language_obj(profile_id=profile_id)
    also_forced = any([x.forced for x in language_set])
    forced_required = all([x.forced for x in language_set])
    normal = not also_forced and not forced_required and all([not x.hi for x in language_set])
    _set_forced_providers(pool=pool, also_forced=also_forced, forced_required=forced_required)

    if providers:
        video = get_video(force_unicode(path), title, sceneName, providers=providers, media_type=media_type)
        if video and keyword:
            # Refiners have already filled the real media identity; only text
            # providers opt into this request-local search term.
            video.search_keyword = keyword
    else:
        logging.info("BAZARR All providers are throttled")
        return 'All providers are throttled'
    if video:
        rejection_records = load_rejections(video)
        try:
            if providers:
                subtitles = list_all_subtitles([video], language_set, pool)
            else:
                logging.info("BAZARR All providers are throttled")
                return 'All providers are throttled'
        except Exception as e:
            logging.exception(f"BAZARR Error trying to get Subtitle list from provider for this file {path}: {repr(e)}")
        else:
            subtitles_list = []
            minimum_score = settings.general.minimum_score
            minimum_score_movie = settings.general.minimum_score_movie
            score_handler = DEFAULT_SCORES['episode'] if media_type == "series" else DEFAULT_SCORES['movie']

            for s in subtitles[video]:
                if not normal and s.language not in language_set:
                    logging.debug(f"Skipping subtitle {s.language} because it's not requested")
                    continue

                try:
                    matches = s.matches if hasattr(s, 'matches') and isinstance(s.matches, set) and len(s.matches) \
                        else s.get_matches(video)
                    matches = {match for match in matches if match in score_handler.keys()}  # cleanup unwanted criterion
                except AttributeError:
                    continue

                # skip wrong season/episodes
                if media_type == "series":
                    can_verify_series = True
                    if not s.hash_verifiable and "hash" in matches:
                        can_verify_series = False

                    if can_verify_series and not {"series", "season", "episode"}.issubset(matches):
                        try:
                            logging.debug(f"BAZARR Skipping {s}, because it doesn't match our series/episode")
                        except TypeError:
                            logging.debug("BAZARR Ignoring invalid subtitles")
                        continue

                if s.hearing_impaired or normal:
                    matches.add('hearing_impaired')

                _, max_score, scores = _get_scores(media_type, minimum_score_movie, minimum_score)
                score, score_without_hash = compute_score(matches, s, video, hearing_impaired=s.hearing_impaired,)

                if 'hash' not in matches:
                    not_matched = scores - matches
                    not_matched = {match for match in not_matched if match in score_handler.keys()}
                    s.score = score_without_hash
                else:
                    matches = s.matches = {match for match in matches if match in ("hash", "hearing_impaired")}
                    s.score = score
                    not_matched = set()

                releases = []
                if hasattr(s, 'release_info'):
                    if s.release_info is not None:
                        for s_item in s.release_info.split(','):
                            if s_item.strip():
                                releases.append(s_item)

                if s.uploader and s.uploader.strip():
                    s_uploader = s.uploader.strip()
                else:
                    s_uploader = None

                tag_values = getattr(s, 'subtitle_tags', []) or []
                tags = list(dict.fromkeys(tag.strip() for tag in tag_values
                                          if isinstance(tag, str) and tag.strip())) \
                    if isinstance(tag_values, list) else []

                if original_format in (1, "1", "True", True):
                    s.use_original_format = True

                rejection = get_rejection(video, s, records=rejection_records)
                subtitles_list.append(
                    dict(score=round((score / max_score * 100), 2),
                         orig_score=score,
                         score_without_hash=score_without_hash,
                         forced=str(s.language.forced),
                         language=str(s.language.basename),
                         hearing_impaired=str(s.hearing_impaired),
                         provider=s.provider_name,
                         subtitle=subtitle_cache.store(s),
                         url=s.page_link,
                         original_format=s.use_original_format,
                         matches=list(matches),
                         dont_matches=list(not_matched),
                         release_info=releases,
                         tags=tags,
                         rejected=rejection is not None,
                         rejection=rejection,
                         uploader=s_uploader))

            final_subtitles = sorted(subtitles_list, key=lambda x: (x['orig_score'], x['score_without_hash']),
                                     reverse=True)
            logging.debug(f'BAZARR {len(final_subtitles)} Subtitles have been found for this file: {path}')
            logging.debug(f'BAZARR Ended searching Subtitles for this file: {path}')

    subliminal.region.backend.sync()

    return final_subtitles


def clear_manual_rejection(media_type, media_id, cache_key):
    """Only restore a candidate belonging to this movie or episode."""
    subtitle = subtitle_cache.get(cache_key)
    if subtitle is None:
        return 'Subtitle cache expired. Please search again.', 404
    video = getattr(subtitle, 'video', None)
    identity = 'radarrId' if media_type == 'movie' else 'sonarrEpisodeId'
    if video is None or getattr(video, identity, None) != media_id:
        return 'Subtitle candidate does not belong to this media.', 400
    clear_rejection(video, subtitle)
    return '', 204


def _safe_manual_text(value, limit=500):
    text = str(value or '')
    text = re.sub(r'https?://[^\s<>\]\)\"\']+', '[redacted-url]', text, flags=re.I)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', text)
    text = re.sub(r'(?i)\b(token|api[_-]?key|password|passwd|cookie|authorization|secret|skey)\s*[=:]\s*'
                  r'(?:"[^"]*"|\'[^\']*\'|[^\s,;]+)', r'\1=[redacted]', text)
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def _manual_subtitle_stats(subtitle):
    if subtitle is None:
        return {}
    content = getattr(subtitle, 'content', None)
    stats = {'bytes': len(content) if isinstance(content, (bytes, str)) else 0,
             'selected_archive_member': _safe_manual_text(getattr(subtitle, 'selected_archive_member', None)) or None}
    if content:
        try:
            import pysubs2
            cues = [cue for cue in pysubs2.SSAFile.from_string(subtitle.text)
                    if not cue.is_comment and cue.plaintext.strip()]
            stats.update(parsed_cues=len(cues),
                         first_start_seconds=min((cue.start / 1000 for cue in cues), default=None),
                         last_end_seconds=max((cue.end / 1000 for cue in cues), default=None))
        except Exception as error:
            stats['parsed_stats_exception_type'] = type(error).__name__
    return stats


def _manual_download_progress(job_id, provider, subtitle):
    started = time.monotonic()
    progress_values = {
        'download_unpack': 10, 'downloaded': 30, 'validation': 40, 'subtitle_coverage': 40,
        'original_audio': 45, 'audio_cache': 50, 'audio_sample': 55, 'timing_check': 75,
        'timing_search': 76, 'timing_verify': 78, 'reference_check': 79,
        'reference_cache': 80, 'reference_sample': 81, 'timing_accepted': 85,
        'saving': 85, 'postprocessing': 95, 'completed': 100,
    }
    labels = {
        'download_unpack': 'Downloading and unpacking subtitle', 'downloaded': 'Subtitle downloaded and unpacked',
        'validation': 'Validating subtitle timing', 'subtitle_coverage': 'Checking subtitle coverage',
        'original_audio': 'Selecting original dialogue audio', 'audio_cache': 'Checking cached original audio activity',
        'audio_sample': 'Sampling original audio', 'timing_check': 'Checking subtitle timing',
        'timing_search': 'Searching subtitle timing offset and frame rate', 'timing_verify': 'Verifying corrected timing',
        'reference_check': 'Checking dialogue against original-language embedded subtitles',
        'reference_cache': 'Checking cached dialogue reference', 'reference_sample': 'Sampling embedded dialogue',
        'timing_accepted': 'Subtitle timing confirmed', 'timing_rejected': 'Subtitle timing rejected',
        'saving': 'Saving verified subtitle', 'postprocessing': 'Processing saved subtitle',
        'completed': 'Subtitle saved successfully', 'failed': 'Subtitle download failed',
    }
    safe_keys = {
        'index', 'total', 'start_seconds', 'cache_hit', 'duration', 'cue_count', 'parsed_cues',
        'first_start_seconds', 'last_end_seconds', 'score', 'peak_z', 'rate', 'offset_seconds',
        'windows', 'window_scores', 'audio_index', 'stream_index', 'language', 'reason',
        'failure_reason', 'failure_detail', 'failure_stage', 'exception_type', 'save_path', 'coverage_windows',
        'remaining_seconds', 'elapsed_seconds',
    }
    progress_value = 0

    def progress(stage, details=None):
        nonlocal progress_value
        stage = _safe_manual_text(stage, 100)
        detail_values = {}
        if isinstance(details, dict):
            for key, value in details.items():
                if key not in safe_keys:
                    continue
                if isinstance(value, (bool, int, float)) or value is None:
                    detail_values[key] = value
                elif isinstance(value, (list, tuple)):
                    detail_values[key] = [item for item in value[:10] if isinstance(item, (bool, int, float))]
                else:
                    detail_values[key] = _safe_manual_text(value)
        desired = progress_values.get(stage, progress_value)
        if stage == 'audio_sample':
            index, total = detail_values.get('index'), detail_values.get('total')
            if isinstance(index, (int, float)) and isinstance(total, (int, float)) and total > 0:
                desired = 50 + round(24 * max(0, min(index, total)) / total)
        progress_value = max(progress_value, desired)
        message = labels.get(stage, 'Validating subtitle timing')
        if stage in ('audio_sample', 'reference_sample') and detail_values.get('total'):
            message += ' %s/%s' % (detail_values.get('index', '?'), detail_values['total'])
        if stage in {'failed', 'timing_rejected'}:
            reason = detail_values.get('failure_detail') or detail_values.get('failure_reason') or detail_values.get('reason')
            if reason:
                message += ': ' + _safe_manual_text(reason)
        try:
            jobs_queue.update_job_progress_status(job_id=job_id, is_progress=True)
            jobs_queue.update_job_progress(job_id=job_id, progress_value=progress_value, progress_max=100,
                                           progress_message=message)
        except Exception as error:
            logging.warning('BAZARR Unable to report manual subtitle job %s progress (%s)', job_id, type(error).__name__)
        payload = {'job_id': job_id, 'provider': _safe_manual_text(provider),
                   'subid': _safe_manual_text(getattr(subtitle, 'subtitle_id', None) or
                                             getattr(subtitle, 'id', None)) if subtitle else None,
                   'stage': stage, 'elapsed_seconds': round(time.monotonic() - started, 3)}
        payload.update(_manual_subtitle_stats(subtitle))
        payload.update(detail_values)
        logging.info('BAZARR Manual subtitle job stage: %s', json.dumps(payload, ensure_ascii=False))
        progress.last_stage = stage

    progress.last_stage = 'initializing'
    return progress


def _audio_validation_failure(subtitle):
    reason = getattr(subtitle, 'audio_timing_failure_reason', None)
    detail = getattr(subtitle, 'audio_timing_failure_detail', None)
    if reason or detail:
        explanations = {
            'obvious_fragment': 'The subtitle contains only a short fragment of the video',
            'parse_loss': 'The subtitle parser could not read a substantial part of the file',
            'invalid_format': 'The downloaded file is not a supported text subtitle',
            'no_dialogue': 'The subtitle contains no usable dialogue cues',
            'insufficient_speech': 'The original-audio samples contain too little dialogue to confirm timing',
            'timing_not_confirmed': 'Available evidence could not confirm subtitle timing',
            'corrected_timing_not_confirmed': 'Available evidence could not confirm the corrected subtitle timing',
            'timing_mismatch': 'Independent reference evidence confirms a subtitle timing mismatch',
            'validation_timeout': 'Original-audio timing validation exceeded its time limit',
            'validation_unavailable': 'Original-audio timing validation is unavailable',
        }
        explanation = explanations.get(reason, reason)
        if detail and detail != reason:
            explanation = ((explanation + ' (' + _safe_manual_text(detail) + ')')
                           if reason == 'validation_unavailable' else detail)
        return 'Subtitle timing validation failed: ' + _safe_manual_text(explanation)
    return 'Audio validation did not confirm this subtitle. Check log and try another candidate.'


def _require_manual_download_result(result):
    if isinstance(result, tuple):
        if len(result) > 1 and isinstance(result[1], int) and result[1] >= 400:
            raise JobExecutionError(_safe_manual_text(result[0]) or 'Manual subtitle download failed')
        result = result[0] if result else None
    if isinstance(result, str):
        raise JobExecutionError(_safe_manual_text(result))
    if not result:
        raise JobExecutionError('No subtitle was saved by the manual download')
    return result


def _manual_job_failure(job_id, description, progress, error):
    message = (_safe_manual_text(error) if isinstance(error, JobExecutionError)
               else 'Manual subtitle download failed (%s)' % type(error).__name__)
    progress('failed', {'failure_reason': message, 'exception_type': type(error).__name__,
                        'failure_stage': progress.last_stage})
    jobs_queue.update_job_name(job_id=job_id, new_job_name='Failed downloading subtitles for ' + description)
    return JobExecutionError(message)


@update_pools
def manual_download_subtitle(path, audio_language, hi, forced, subtitle, provider, sceneName, title, media_type,
                             use_original_format, profile_id, job_id=None, progress=None):
    logging.debug(f'BAZARR Manually downloading Subtitles for this file: {path}')

    if settings.general.utf8_encode:
        os.environ["SZ_KEEP_ENCODING"] = ""
    else:
        os.environ["SZ_KEEP_ENCODING"] = "True"

    subtitle = subtitle_cache.get(subtitle)
    if subtitle is None:
        logging.error("BAZARR Subtitle not found in cache (expired or invalid ID)")
        return 'Subtitle not found in cache. Please search again.'
    if hi == 'True':
        subtitle.language.hi = True
    else:
        subtitle.language.hi = False
    if forced == 'True':
        subtitle.language.forced = True
    else:
        subtitle.language.forced = False
    if use_original_format in (1, "1", "True", True):
        subtitle.use_original_format = True

    subtitle.mods = get_array_from(settings.general.subzero_mods)
    if progress:
        progress('download_unpack')
    video = get_video(force_unicode(path), title, sceneName, providers={provider}, media_type=media_type)
    if video:
        # Search results can outlive a metadata refresh. Archive completeness
        # must use the current probe duration while retaining the candidate's
        # season/episode identity and provider search metadata.
        candidate_video = getattr(subtitle, 'video', None)
        if candidate_video is None:
            subtitle.video = video
        elif getattr(video, 'duration', None) is not None:
            candidate_video.duration = video.duration
        rejection = get_rejection(video, subtitle)
        if rejection:
            return 'This subtitle was excluded after validation: ' + _safe_manual_text(
                rejection.get('detail') or rejection['reason']) + '. Use Allow retry before downloading it again.'
        try:
            if provider:
                download_subtitles([subtitle], _get_pool(media_type, profile_id))
                if progress:
                    progress('downloaded')
                logging.debug(f'BAZARR Subtitles file downloaded for this file: {path}')
            else:
                logging.info("BAZARR All providers are throttled")
                return 'All providers are throttled'
        except Exception as error:
            logging.warning('BAZARR Error downloading subtitles for %s (%s)', path, type(error).__name__)
            if progress:
                progress('failed', {'failure_reason': 'provider_download_failed', 'failure_stage': 'download_unpack',
                                    'exception_type': type(error).__name__})
            return 'Error downloading subtitles (%s)' % type(error).__name__
        else:
            if not subtitle.is_valid():
                failure_reason = getattr(subtitle, 'download_failure_reason', None)
                record_rejection(video, subtitle, failure_reason)
                if failure_reason == 'script_inconclusive':
                    return 'Could not confirm the subtitle Chinese script. This result has not been permanently excluded.'
                logging.error(f"BAZARR Downloaded subtitles isn't valid for this file: {path}")
                return "Downloaded subtitles isn't valid. Check log."
            if progress:
                progress('validation')
            validated = validate_download(video, subtitle, progress=progress) if progress else validate_download(video, subtitle)
            if not validated:
                return _audio_validation_failure(subtitle)
            if progress:
                progress('saving')
            try:
                chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
                    'win') and settings.general.chmod_enabled else None
                saved_subtitles = save_subtitles(video.original_path, [subtitle],
                                                 single=settings.general.single_language,
                                                 tags=None,  # fixme
                                                 directory=get_target_folder(path),
                                                 chmod=chmod,
                                                 formats=(subtitle.format,),
                                                 path_decoder=force_unicode)
            except Exception as error:
                logging.warning('BAZARR Error saving subtitles for %s (%s)', path, type(error).__name__)
                if progress:
                    progress('failed', {'failure_reason': 'subtitle_save_failed', 'failure_stage': 'saving',
                                        'exception_type': type(error).__name__})
                return 'Error saving subtitles file to disk (%s)' % type(error).__name__
            else:
                if saved_subtitles:
                    _, max_score, _ = _get_scores(media_type)
                    for saved_subtitle in saved_subtitles:
                        save_path = getattr(saved_subtitle, 'storage_path', None)
                        if not save_path or not os.path.isfile(save_path):
                            return 'Subtitle saving did not produce a file on disk'
                        if progress:
                            progress('postprocessing', {'save_path': save_path})
                        processed_subtitle = process_subtitle(subtitle=saved_subtitle, media_type=media_type,
                                                              audio_language=audio_language, is_upgrade=False,
                                                              is_manual=True, path=path, max_score=max_score,
                                                              job_id=job_id)
                        if processed_subtitle:
                            return processed_subtitle
                        else:
                            logging.debug(f"BAZARR unable to process this subtitles: {subtitle}")
                            continue
                else:
                    logging.error(
                        f"BAZARR Tried to manually download a Subtitles for file: {path} but we weren't able to do "
                        f"(probably throttled by {subtitle.provider_name}. Please retry later or select a Subtitles "
                        f"from another provider.")
                    return 'Something went wrong, check the logs for error'

    return 'Subtitle video information or processing result is unavailable'


def episode_manually_download_specific_subtitle(sonarr_series_id, sonarr_episode_id, hi, forced, use_original_format,
                                                selected_provider, subtitle, job_id=None):
    if not job_id:
        return jobs_queue.add_job_from_function("Manually downloading Subtitles", is_progress=True, progress_max=100)

    description = 'episode %s' % sonarr_episode_id
    progress = _manual_download_progress(job_id, selected_provider, subtitle_cache.get(subtitle))
    try:
        episodeInfo = database.execute(
            select(TableEpisodes.audio_language, TableEpisodes.path, TableEpisodes.sceneName,
                   TableEpisodes.season, TableEpisodes.episode, TableEpisodes.title.label('episodeTitle'),
                   TableShows.title)
            .select_from(TableEpisodes).join(TableShows)
            .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id)).first()
        if not episodeInfo:
            raise JobExecutionError('Episode not found')
        title = episodeInfo.title
        description = f'{title} - S{episodeInfo.season:02d}E{episodeInfo.episode:02d} - {episodeInfo.episodeTitle}'
        jobs_queue.update_job_name(job_id=job_id, new_job_name='Manually downloading Subtitles for ' + description)
        episodePath = path_mappings.path_replace(episodeInfo.path)
        sceneName = episodeInfo.sceneName or "None"
        audio_language_list = get_audio_profile_languages(episodeInfo.audio_language)
        audio_language = audio_language_list[0]['name'] if audio_language_list else 'None'
        result = manual_download_subtitle(episodePath, audio_language, hi, forced, subtitle, selected_provider,
                                          sceneName, title, 'series', use_original_format,
                                          profile_id=get_profile_id(episode_id=sonarr_episode_id), job_id=job_id,
                                          progress=progress)
        result = _require_manual_download_result(result)
        store_subtitles(sonarr_episode_id)
        history_log(2, sonarr_series_id, sonarr_episode_id, result)
        if not settings.general.dont_notify_manual_actions:
            send_notifications(sonarr_series_id, sonarr_episode_id, result.message)
        progress('completed')
        jobs_queue.update_job_name(job_id=job_id, new_job_name='Manually downloaded Subtitles for ' + description)
        return '', 204
    except Exception as error:
        raise _manual_job_failure(job_id, description, progress, error) from None


def movie_manually_download_specific_subtitle(radarr_id, hi, forced, use_original_format, selected_provider, subtitle,
                                              job_id=None):
    if not job_id:
        return jobs_queue.add_job_from_function("Manually downloading Subtitles", is_progress=True, progress_max=100)

    description = 'movie %s' % radarr_id
    progress = _manual_download_progress(job_id, selected_provider, subtitle_cache.get(subtitle))
    try:
        movieInfo = database.execute(
            select(TableMovies.title, TableMovies.year, TableMovies.path, TableMovies.sceneName, TableMovies.audio_language)
            .where(TableMovies.radarrId == radarr_id)).first()
        if not movieInfo:
            raise JobExecutionError('Movie not found')
        title = movieInfo.title
        description = f'{title} ({movieInfo.year})'
        jobs_queue.update_job_name(job_id=job_id, new_job_name='Manually downloading Subtitles for ' + description)
        moviePath = path_mappings.path_replace_movie(movieInfo.path)
        sceneName = movieInfo.sceneName or "None"
        audio_language_list = get_audio_profile_languages(movieInfo.audio_language)
        audio_language = audio_language_list[0]['name'] if audio_language_list else 'None'
        result = manual_download_subtitle(moviePath, audio_language, hi, forced, subtitle, selected_provider,
                                          sceneName, title, 'movie', use_original_format,
                                          profile_id=get_profile_id(movie_id=radarr_id), job_id=job_id,
                                          progress=progress)
        result = _require_manual_download_result(result)
        store_subtitles_movie(radarr_id)
        history_log_movie(2, radarr_id, result)
        if not settings.general.dont_notify_manual_actions:
            send_notifications_movie(radarr_id, result.message)
        progress('completed')
        jobs_queue.update_job_name(job_id=job_id, new_job_name='Manually downloaded Subtitles for ' + description)
        return '', 204
    except Exception as error:
        raise _manual_job_failure(job_id, description, progress, error) from None


def _get_language_obj(profile_id):
    language_set = set()

    profile = get_profiles_list(profile_id=int(profile_id))
    language_items = profile['items']
    original_format = profile['originalFormat']

    for language in language_items:
        forced = language['forced']
        hi = language['hi']
        language = language['language']

        lang = alpha3_from_alpha2(language)

        lang_obj = _get_lang_obj(lang)

        if forced == "True":
            lang_obj = Language.rebuild(lang_obj, forced=True)

        if hi == "True":
            lang_obj = Language.rebuild(lang_obj, hi=True)

        language_set.add(lang_obj)

    return language_set, original_format
