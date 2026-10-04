"""Download one SubHD season archive and safely replace verified episode subtitles."""

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from threading import RLock

from subzero.language import Language
from subliminal.subtitle import fix_line_ending
from subliminal_patch.core import get_subtitle_path
from subliminal_patch.providers.subhd import SubhdProvider, extract_archive_subtitle
from subliminal_patch.score import compute_score, DEFAULT_SCORES

from app.config import settings
from app.get_args import args
from app.database import database, select, TableEpisodes, TableShows, get_subtitles, get_audio_profile_languages
from app.jobs_queue import jobs_queue, JobExecutionError
from app.notifier import send_notifications
from sonarr.history import history_log
from utilities.path_mappings import path_mappings
from bazarr.subtitles.cache import subtitle_cache
from .indexer.series import store_subtitles
from .manual import manual_download_subtitle, _require_manual_download_result, _safe_manual_text
from .rejections import record_rejection
from .utils import get_video


_ENQUEUE_LOCK = RLock()
_TEXT_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt'}
_MAX_BACKUP_BYTES = 32 * 1024 * 1024
_PROGRESS_KEYS = {'index', 'total', 'start_seconds', 'cache_hit', 'duration', 'cue_count', 'parsed_cues',
                  'first_start_seconds', 'last_end_seconds', 'score', 'peak_z', 'rate', 'offset_seconds',
                  'windows', 'window_scores', 'audio_index', 'stream_index', 'language', 'reason',
                  'failure_reason', 'failure_detail', 'failure_stage', 'exception_type', 'coverage_windows',
                  'remaining_seconds', 'elapsed_seconds'}


class SeasonRequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _flag(value):
    return value is True or value in ('True', 'true', 1, '1')


def _path(value):
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def _symlink(path):
    current = os.path.abspath(path)
    while True:
        if os.path.islink(current):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _language(value):
    value = str(getattr(value, 'basename', value) or '').split(':')[0].replace('_', '-').lower()
    if value in {'zh', 'zh-cn', 'zh-hans', 'zho', 'chs'}:
        return 'zh'
    if value in {'zt', 'zht', 'cht', 'zh-tw', 'zh-hant', 'zh-hk', 'zh-mo'}:
        return 'zh-tw'
    return value


def _destination(video_path):
    mode = settings.general.subfolder
    folder = str(settings.general.subfolder_custom or '').strip()
    if mode == 'absolute' and folder:
        return _path(folder)
    if mode == 'relative' and folder:
        return _path(os.path.join(os.path.dirname(video_path), folder))
    return _path(os.path.dirname(video_path))


def _matching_rows(rows, subtitle):
    language, forced = _language(subtitle.language), bool(subtitle.language.forced)
    return [row for row in rows if row.get('path') and row.get('embedded_track_id') is None and
            _language(row.get('code2') or row.get('language')) == language and
            _flag(row.get('forced', False)) == forced]


def _safe_external(path, video_path, directory):
    path = _path(path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    name, extension = os.path.splitext(os.path.basename(path))
    if name == stem:
        suffix_allowed = True
    elif name.startswith(stem + '.'):
        suffix = name[len(stem) + 1:]
        suffix_allowed = bool(re.fullmatch(
            r'(?:llm\.)?[a-z]{2,3}(?:[-_](?:[a-z]{2}|[a-z]{4}|\d{3}))?'
            r'(?:\.(?:hi|sdh|cc|forced|llm)(?:-(?:hi|sdh|cc|forced))?)*', suffix, re.I))
    else:
        suffix_allowed = False
    return (os.path.dirname(path) == _path(directory) and not _symlink(path) and
            suffix_allowed and extension.lower() in _TEXT_EXTENSIONS)


def _rows(episode_id=None, series_id=None, season=None, episode_ids=None):
    query = select(TableEpisodes.sonarrEpisodeId, TableEpisodes.sonarrSeriesId,
                   TableEpisodes.season, TableEpisodes.episode, TableEpisodes.path,
                   TableEpisodes.sceneName, TableEpisodes.audio_language,
                   TableEpisodes.title.label('episode_title'), TableShows.title.label('series_title'),
                   TableShows.profileId).select_from(TableEpisodes).join(TableShows)
    if episode_id is not None:
        query = query.where(TableEpisodes.sonarrEpisodeId == episode_id)
    if series_id is not None:
        query = query.where(TableEpisodes.sonarrSeriesId == series_id)
    if season is not None:
        query = query.where(TableEpisodes.season == season)
    if episode_ids is not None:
        query = query.where(TableEpisodes.sonarrEpisodeId.in_(episode_ids))
    return [dict(row._mapping) for row in database.execute(
        query.order_by(TableEpisodes.episode, TableEpisodes.sonarrEpisodeId)).all()]


def _selection(episode_id, candidate_key):
    if isinstance(episode_id, bool) or not isinstance(episode_id, int) or episode_id <= 0:
        raise SeasonRequestError('A valid source episode is required')
    candidate = subtitle_cache.get(candidate_key)
    if candidate is None:
        raise SeasonRequestError('Subtitle cache expired. Please search again.', 404)
    source = _rows(episode_id=episode_id)
    if not source:
        raise SeasonRequestError('Episode not found', 404)
    row, video = source[0], getattr(candidate, 'video', None)
    if getattr(candidate, 'provider_name', None) != 'subhd':
        raise SeasonRequestError('Season downloads are available only for SubHD')
    if (video is None or getattr(video, 'sonarrEpisodeId', None) != episode_id or
            getattr(video, 'sonarrSeriesId', None) != row['sonarrSeriesId'] or
            getattr(video, 'season', None) != row['season'] or
            getattr(video, 'episode', None) != row['episode']):
        raise SeasonRequestError('Subtitle candidate does not belong to this episode and season')
    return candidate, row


def preview_season(episode_id, candidate_key):
    candidate, source = _selection(episode_id, candidate_key)
    episodes = []
    for row in _rows(series_id=source['sonarrSeriesId'], season=source['season']):
        video_path = path_mappings.path_replace(row['path'])
        if not video_path or not os.path.isfile(video_path):
            continue
        existing = _matching_rows(get_subtitles(sonarr_episode_id=row['sonarrEpisodeId']), candidate)
        count = sum(os.path.isfile(item['path']) and _safe_external(
            item['path'], video_path, _destination(video_path)) for item in existing)
        episodes.append({'episode_id': row['sonarrEpisodeId'], 'episode': row['episode'],
                         'title': row['episode_title'], 'existing_subtitles': count})
    if not episodes:
        raise SeasonRequestError('No local episode files were found in this season', 404)
    return {'series_id': source['sonarrSeriesId'], 'season': source['season'], 'title': source['series_title'],
            'total': len(episodes), 'language': str(candidate.language.basename), 'provider': 'subhd',
            'episodes': episodes}


def enqueue_season(episode_id, candidate_key, hi='False', forced='False', use_original_format=False):
    preview = preview_season(episode_id, candidate_key)
    scope = {'series_id': preview['series_id'], 'season': preview['season']}
    with _ENQUEUE_LOCK, getattr(jobs_queue, '_queue_lock', nullcontext()):
        for job in jobs_queue.list_jobs_from_queue():
            if (job.get('status') in {'pending', 'running'} and job.get('module') == 'subtitles.season' and
                    job.get('func') == 'download_season' and (job.get('kwargs') or {}).get('scope') == scope):
                raise SeasonRequestError('A subtitle download for this season is already pending or running', 409)
        job_id = jobs_queue.feed_jobs_pending_queue(
            job_name='Downloading season subtitles for %s - S%02d' % (preview['title'], preview['season']),
            module='subtitles.season', func='download_season', is_progress=True, progress_max=100,
            kwargs={'episode_id': episode_id, 'candidate_key': candidate_key, 'scope': scope,
                    'episode_ids': [row['episode_id'] for row in preview['episodes']],
                    'hi': 'True' if _flag(hi) else 'False', 'forced': 'True' if _flag(forced) else 'False',
                    'use_original_format': _flag(use_original_format)})
        if not job_id:
            raise SeasonRequestError('A subtitle download for this season is already queued', 409)
        return job_id


def _digest(path):
    if _symlink(path) or not os.path.isfile(path):
        raise RuntimeError('Subtitle file is unavailable or is a symlink')
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()


class _EpisodeBackup:
    def __init__(self, job_dir, episode_id, video_path, subtitle, indexed_rows, target_folder=None):
        self.directory = _path(target_folder or _destination(video_path))
        self.video_path = _path(video_path)
        self.subtitle = subtitle
        self.rows = indexed_rows
        self.backup_dir = os.path.join(job_dir, str(episode_id))
        self.backups = {}
        self.written = {}
        self.removed = set()
        self.planned = set()
        self.ready = False

    def _allowed(self, path):
        return _safe_external(path, self.video_path, self.directory)

    def before_save(self, video, subtitle):
        if _path(video.original_path) != self.video_path or self.ready:
            raise RuntimeError('Subtitle backup does not belong to this video or was already started')
        if _symlink(self.directory) or _symlink(self.backup_dir):
            raise RuntimeError('Subtitle or backup directory is a symlink')
        os.makedirs(self.directory, exist_ok=True)
        os.makedirs(self.backup_dir, mode=0o700, exist_ok=False)
        for row in _matching_rows(self.rows, subtitle):
            path = _path(row['path'])
            if not self._allowed(path) or not os.path.isfile(path):
                continue
            if os.path.getsize(path) > _MAX_BACKUP_BYTES:
                raise RuntimeError('An existing subtitle exceeds the backup size limit')
            digest = _digest(path)
            backup = os.path.join(self.backup_dir, '%03d%s' % (len(self.backups), os.path.splitext(path)[1]))
            shutil.copy2(path, backup, follow_symlinks=False)
            if _digest(backup) != digest or _digest(path) != digest:
                raise RuntimeError('An existing subtitle changed during backup')
            self.backups[path] = {'path': backup, 'sha256': digest}
        # save_subtitles may detect or remove HI after before_save. Protect both
        # possible names without ever overwriting a file outside the indexed set.
        for hi in (False, True):
            language = Language.rebuild(subtitle.language, hi=hi)
            path = get_subtitle_path(video.original_path, None if settings.general.single_language else language,
                                     extension='.' + subtitle.format, forced_tag=language.forced, hi_tag=language.hi)
            path = _path(os.path.join(self.directory, os.path.basename(path)))
            if not self._allowed(path):
                raise RuntimeError('The planned subtitle path is unsafe')
            self.planned.add(path)
            if os.path.lexists(path) and path not in self.backups:
                raise RuntimeError('The subtitle save target exists but was not safely indexed and backed up')
        self.ready = True

    def after_save(self, saved_subtitle):
        path = _path(saved_subtitle.storage_path)
        if not self.ready or path not in self.planned or not self._allowed(path) or not os.path.isfile(path):
            raise RuntimeError('The saved subtitle path is outside the prepared backup scope')
        self.written[path] = _digest(path)
        for previous, backup in self.backups.items():
            if previous == path:
                continue
            if not self._allowed(previous) or _digest(previous) != backup['sha256']:
                raise RuntimeError('An existing subtitle changed before replacement cleanup')
            os.unlink(previous)
            self.removed.add(previous)

    def rollback(self):
        restored, removed, conflicts = 0, 0, []
        for path, digest in self.written.items():
            if path in self.backups:
                continue
            if not os.path.lexists(path):
                continue
            try:
                if not self._allowed(path) or _digest(path) != digest:
                    raise RuntimeError('New subtitle changed independently after save')
                os.unlink(path)
                removed += 1
            except Exception as error:
                conflicts.append(os.path.basename(path))
                logging.warning('BAZARR Season subtitle rollback conflict: %s (%s)',
                                _safe_manual_text(os.path.basename(path)), type(error).__name__)
        for path, backup in self.backups.items():
            try:
                if not self._allowed(path) or _digest(backup['path']) != backup['sha256']:
                    raise RuntimeError('Unsafe or changed backup')
                if os.path.lexists(path):
                    digest = _digest(path)
                    if digest == backup['sha256']:
                        continue
                    if self.written.get(path) != digest:
                        raise RuntimeError('Subtitle changed independently after save')
                elif path not in self.removed and path not in self.written:
                    raise RuntimeError('Subtitle disappeared independently after backup')
                descriptor, temporary = tempfile.mkstemp(prefix='.bazarr-restore-', dir=os.path.dirname(path))
                os.close(descriptor)
                try:
                    shutil.copy2(backup['path'], temporary)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                restored += 1
            except Exception as error:
                conflicts.append(os.path.basename(path))
                logging.warning('BAZARR Season subtitle rollback conflict: %s (%s)',
                                _safe_manual_text(os.path.basename(path)), type(error).__name__)
        return {'restored': restored, 'removed': removed, 'conflicts': conflicts}


def _write_report(job_dir, report):
    descriptor, temporary = tempfile.mkstemp(prefix='.report-', dir=job_dir)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
        os.replace(temporary, os.path.join(job_dir, 'report.json'))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _job_progress(job_id, value, message):
    if job_id is None:
        return
    try:
        jobs_queue.update_job_progress_status(job_id=job_id, is_progress=True)
        jobs_queue.update_job_progress(job_id=job_id, progress_value=value, progress_max=100,
                                       progress_message=_safe_manual_text(message))
    except Exception as error:
        logging.warning('BAZARR Season progress reporting failed (%s)', type(error).__name__)


def _episode_progress(job_id, index, total, episode_id):
    last = 0
    stages = {'download_unpack': 0, 'downloaded': 5, 'validation': 10, 'subtitle_coverage': 10,
              'original_audio': 15, 'audio_cache': 20, 'audio_sample': 30, 'timing_check': 60,
              'timing_search': 65, 'timing_verify': 70, 'reference_check': 75,
              'reference_cache': 76, 'reference_sample': 80, 'timing_accepted': 85,
              'saving': 90, 'postprocessing': 95, 'completed': 100}

    def progress(stage, details=None):
        nonlocal last
        last = max(last, stages.get(stage, last))
        value = min(99, round(10 + 89 * ((index - 1) + last / 100) / total))
        message = 'Episode %s/%s: %s' % (index, total, _safe_manual_text(stage))
        safe_details = {}
        for key, data in (details.items() if isinstance(details, dict) else []):
            if key not in _PROGRESS_KEYS:
                continue
            if isinstance(data, (bool, int, float)) or data is None:
                safe_details[key] = data
            elif isinstance(data, (tuple, list)):
                safe_details[key] = [entry for entry in data[:10] if isinstance(entry, (bool, int, float))]
            else:
                safe_details[key] = _safe_manual_text(data)
        if stage in {'audio_sample', 'reference_sample'} and safe_details.get('total'):
            message += ' %s/%s' % (safe_details.get('index', '?'), safe_details['total'])
        _job_progress(job_id, value, message)
        logging.info('BAZARR Season subtitle stage: %s', json.dumps({
            'job_id': job_id, 'episode_id': episode_id, 'episode_index': index, 'episode_total': total,
            'stage': _safe_manual_text(stage), **safe_details}, ensure_ascii=False))
    return progress


def download_season(episode_id, candidate_key, scope, episode_ids, hi='False', forced='False',
                    use_original_format=False, job_id=None):
    started = time.monotonic()
    try:
        candidate, source = _selection(episode_id, candidate_key)
    except SeasonRequestError as error:
        jobs_queue.update_job_name(job_id=job_id, new_job_name='Failed downloading season subtitles')
        raise JobExecutionError(_safe_manual_text(error)) from None
    expected = {'series_id': source['sonarrSeriesId'], 'season': source['season']}
    if scope != expected or not isinstance(episode_ids, (list, tuple)) or not episode_ids:
        raise JobExecutionError('Season download scope is invalid or changed')
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in episode_ids):
        raise JobExecutionError('Season episode snapshot contains invalid identifiers')
    episode_ids = list(dict.fromkeys(episode_ids))
    root = os.path.join(args.config_dir, 'season-downloads')
    if _symlink(root):
        raise JobExecutionError('Season backup directory is a symlink')
    os.makedirs(root, mode=0o700, exist_ok=True)
    job_dir = tempfile.mkdtemp(prefix='job-%s-' % (job_id or 'manual'), dir=root)
    report = {'job_id': job_id, 'series_id': scope['series_id'], 'season': scope['season'],
              'provider': 'subhd', 'language': str(candidate.language.basename), 'total': len(episode_ids),
              'saved': 0, 'skipped': 0, 'failed': 0, 'episodes': [],
              'started_at': datetime.now(timezone.utc).isoformat()}
    seed = copy.copy(candidate)
    seed.language = Language.rebuild(candidate.language, hi=_flag(hi), forced=_flag(forced))
    _job_progress(job_id, 1, 'Downloading one subtitle archive for the season')
    try:
        with SubhdProvider() as provider:
            content = provider.download_archive(seed)
    except Exception as error:
        reason = 'Archive download failed (%s)' % type(error).__name__
        for identifier in episode_ids:
            report['episodes'].append({'episode_id': identifier, 'status': 'failed', 'reason': reason})
        report['failed'] = len(episode_ids)
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        _write_report(job_dir, report)
        jobs_queue.update_job_name(job_id=job_id, new_job_name='Failed downloading season subtitles: saved 0/%s' %
                                   len(episode_ids))
        raise JobExecutionError(reason) from None
    _job_progress(job_id, 10, 'Archive downloaded; selecting verified episode subtitles')
    for index, identifier in enumerate(episode_ids, 1):
        item = {'episode_id': identifier, 'status': 'failed', 'reason': None, 'member': None}
        transaction = None
        phase = 'episode_metadata'
        progress = _episode_progress(job_id, index, len(episode_ids), identifier)
        try:
            rows = _rows(episode_id=identifier, series_id=scope['series_id'], season=scope['season'])
            if not rows:
                item.update(status='skipped', reason='Episode is no longer in the selected season')
                continue
            row = rows[0]
            item['episode'] = row['episode']
            video_path = path_mappings.path_replace(row['path'])
            if not video_path or not os.path.isfile(video_path):
                item.update(status='skipped', reason='The local episode file is unavailable')
                continue
            video = get_video(video_path, row['series_title'], row['sceneName'] or 'None',
                              providers={'subhd'}, media_type='series')
            if (video is None or getattr(video, 'sonarrEpisodeId', None) != identifier or
                    getattr(video, 'sonarrSeriesId', None) != scope['series_id'] or
                    getattr(video, 'season', None) != scope['season'] or
                    getattr(video, 'episode', None) != row['episode']):
                raise JobExecutionError('Fresh video metadata does not match this episode')
            current = copy.copy(candidate)
            current.language = Language.rebuild(seed.language)
            current.video = video
            current.content = None
            current.audio_timing_validated = False
            current.audio_timing_failure_reason = None
            current.audio_timing_failure_detail = None
            current.use_original_format = _flag(use_original_format)
            current.matches = {match for match in current.get_matches(video) if match in DEFAULT_SCORES['episode']}
            score, score_without_hash = compute_score(current.matches, current, video, hearing_impaired=_flag(hi))
            current.score = score if 'hash' in current.matches else score_without_hash
            phase = 'archive_selection'
            progress('download_unpack')
            extracted, subtitle_format = extract_archive_subtitle(content, current, strict_episode=True)
            item['member'] = _safe_manual_text(getattr(current, 'selected_archive_member', None)) or None
            if not extracted:
                reason = getattr(current, 'download_failure_reason', None) or 'no_eligible_subtitle'
                record_rejection(video, current, reason)
                item.update(status='skipped', reason=_safe_manual_text(reason))
                continue
            current.content = fix_line_ending(extracted)
            current.format = subtitle_format
            if not current.is_valid():
                raise JobExecutionError('The selected episode subtitle is not valid')
            if settings.general.utf8_encode:
                current.normalize()
            transaction = _EpisodeBackup(job_dir, identifier, video_path, current,
                                         get_subtitles(sonarr_episode_id=identifier))
            audio = get_audio_profile_languages(row['audio_language'])
            phase = 'validation_and_save'
            result = manual_download_subtitle(
                video_path, audio[0]['name'] if audio else 'None',
                'True' if _flag(hi) else 'False', 'True' if _flag(forced) else 'False',
                candidate_key, 'subhd', row['sceneName'] or 'None', row['series_title'], 'series',
                use_original_format, profile_id=row['profileId'], job_id=job_id, progress=progress,
                prepared_subtitle=current, prepared_video=video,
                before_save=transaction.before_save, after_save=transaction.after_save)
            result = _require_manual_download_result(result)
            if not transaction.written:
                raise JobExecutionError('Subtitle saving did not confirm a file in the prepared scope')
            phase = 'index_and_history'
            store_subtitles(identifier)
            history_log(2, scope['series_id'], identifier, result)
            if not settings.general.dont_notify_manual_actions:
                try:
                    send_notifications(scope['series_id'], identifier, result.message)
                except Exception as error:
                    logging.warning('BAZARR Season subtitle notification failed for episode %s (%s)',
                                    identifier, type(error).__name__)
            item.update(status='saved', reason='Verified subtitle saved')
            progress('completed')
        except Exception as error:
            item.update(status='failed', reason=(_safe_manual_text(error) if isinstance(error, JobExecutionError)
                                                else 'Episode subtitle failed (%s)' % type(error).__name__), stage=phase)
            if transaction is not None:
                try:
                    item['rollback'] = transaction.rollback()
                except Exception as rollback_error:
                    item['rollback'] = {'error': type(rollback_error).__name__}
                try:
                    store_subtitles(identifier)
                except Exception as index_error:
                    item['reindex_error'] = type(index_error).__name__
        finally:
            report[item['status']] += 1
            report['episodes'].append(item)
            report['elapsed_seconds'] = round(time.monotonic() - started, 3)
            _write_report(job_dir, report)
            logging.info('BAZARR Season subtitle episode result: %s', json.dumps(item, ensure_ascii=False))
            _job_progress(job_id, min(99, round(10 + 89 * index / len(episode_ids))),
                          'Episode %s/%s: %s' % (index, len(episode_ids), item['status']))
    name = 'Season subtitles: saved %s/%s skipped %s failed %s' % (
        report['saved'], report['total'], report['skipped'], report['failed'])
    jobs_queue.update_job_name(job_id=job_id, new_job_name=name if report['saved'] else 'Failed ' + name)
    _job_progress(job_id, 100, name)
    if not report['saved']:
        raise JobExecutionError(name)
    return report
