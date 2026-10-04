"""Season jobs stay scoped and use transactional replacement of local subtitles."""

import ast
import copy
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from threading import Lock, RLock
import time
from types import ModuleType, SimpleNamespace
from typing import Callable, Optional
from unittest.mock import Mock

import pytest
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, LargeBinary, Text, UniqueConstraint
from sqlalchemy import create_engine, delete, insert, select
from sqlalchemy.orm import Session, declarative_base, mapped_column
from sqlalchemy.pool import StaticPool
from subzero.language import Language
from subliminal import Episode
from subliminal.subtitle import fix_line_ending
from subliminal_patch.providers.subhd import SubhdSubtitle
from subliminal_patch.score import compute_score, DEFAULT_SCORES
from subliminal_patch.core import get_subtitle_path

from test_manual_download_jobs import queue_namespace
from test_subhd_script_guard import SIMPLIFIED, srt


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_KEY = 'season-candidate-uuid'


@pytest.fixture
def season_namespace(tmp_path, monkeypatch, queue_namespace):
    base = declarative_base()
    namespace = dict(Base=base, mapped_column=mapped_column, Integer=Integer, Text=Text, BigInteger=BigInteger,
                     Boolean=Boolean, DateTime=DateTime, LargeBinary=LargeBinary, ForeignKey=ForeignKey,
                     UniqueConstraint=UniqueConstraint)
    model_names = {'TableLanguagesProfiles', 'TableShows', 'TableEpisodes', 'TableEpisodesSubtitles'}
    model_path = ROOT / 'bazarr/app/database.py'
    model_nodes = [node for node in ast.parse(model_path.read_text(encoding='utf-8')).body
                   if isinstance(node, ast.ClassDef) and node.name in model_names]
    exec(compile(ast.Module(body=model_nodes, type_ignores=[]), str(model_path), 'exec'), namespace)
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    base.metadata.create_all(engine)
    database = Session(engine)
    show, episodes, indexed = (namespace[name] for name in ('TableShows', 'TableEpisodes', 'TableEpisodesSubtitles'))
    media = tmp_path / 'media'
    media.mkdir()
    files = {}
    for identifier, series, season, episode, exists in (
            (101, 7, 1, 1, True), (102, 7, 1, 2, True), (103, 7, 1, 3, False),
            (201, 7, 2, 1, True), (301, 8, 1, 1, True)):
        path = media / f'Show{series}.S{season:02d}E{episode:02d}.mkv'
        if exists:
            path.write_bytes(b'media fixture')
        files[identifier] = path
        database.add(episodes(sonarrEpisodeId=identifier, sonarrSeriesId=series, season=season,
                              episode=episode, title=f'Episode {episode}', path=str(path), audio_language='English'))
    database.add_all([show(sonarrSeriesId=7, title='Show', path=str(media / 'Show'), originalLanguage='English'),
                      show(sonarrSeriesId=8, title='Other show', path=str(media / 'Other'), originalLanguage='English')])
    database.commit()
    source_video = Episode(str(files[101]), 'Show', 1, 1)
    source_video.sonarrEpisodeId, source_video.sonarrSeriesId = 101, 7
    source = SubhdSubtitle(Language('zho', 'CN'), 'SeasonPack', 'https://subhd.me/a/SeasonPack', 'Show S01', source_video)
    source.subtitle_tags = ['简体']
    cache = {CANDIDATE_KEY: source}
    queue = queue_namespace['JobsQueue']()
    provider = Mock()
    provider.download_archive.return_value = b'one mocked archive'
    provider.__enter__ = Mock(return_value=provider)
    provider.__exit__ = Mock(return_value=False)

    def get_video(path, *args, **kwargs):
        row = database.execute(select(episodes).where(episodes.path == str(path))).scalar_one_or_none()
        if row is None or not Path(path).is_file():
            return None
        result = Episode(str(path), 'Show', row.season, row.episode)
        result.sonarrEpisodeId, result.sonarrSeriesId = row.sonarrEpisodeId, row.sonarrSeriesId
        result.original_path, result.original_language, result.duration = str(path), 'English', 3600
        return result

    def extract_archive(content, target, strict_episode=True):
        assert content == provider.download_archive.return_value
        assert strict_episode is True
        target.selected_archive_member = f'S01E{target.video.episode:02d}.chs.srt'
        target.download_failure_reason = None
        return srt(SIMPLIFIED + f' episode {target.video.episode}'), 'srt'

    def indexed_rows(*args, **kwargs):
        identifier = kwargs.get('sonarr_episode_id', args[0] if args else None)
        rows = database.execute(select(indexed).where(indexed.sonarrEpisodeId == identifier)).scalars()
        return [dict((column.name, getattr(row, column.name)) for column in indexed.__table__.columns) for row in rows]

    def manual_success(*args, **kwargs):
        current, video = kwargs['prepared_subtitle'], kwargs['prepared_video']
        kwargs['before_save'](video, current)
        saved = copy.copy(current)
        path = get_subtitle_path(video.original_path, current.language, extension='.' + current.format,
                                 forced_tag=current.language.forced, hi_tag=current.language.hi)
        saved.storage_path = path
        Path(path).write_bytes(saved.content)
        kwargs['after_save'](saved)
        return SimpleNamespace(message='Saved safely', subtitle=saved)

    module = ModuleType('isolated_season_service')
    monkeypatch.setitem(__import__('sys').modules, module.__name__, module)
    service = module.__dict__
    service.update(namespace)
    service.update(logging=logging, json=json, math=math, os=os, re=re, shutil=shutil, tempfile=tempfile,
                   copy=copy, hashlib=hashlib, Path=Path, Lock=Lock, RLock=RLock, time=time,
                   compute_score=compute_score, DEFAULT_SCORES=DEFAULT_SCORES,
                   nullcontext=nullcontext, stat=stat, get_subtitle_path=get_subtitle_path,
                   datetime=datetime, timezone=timezone, dataclass=dataclass, field=field, Optional=Optional,
                   Callable=Callable, SimpleNamespace=SimpleNamespace, Language=Language,
                   args=SimpleNamespace(config_dir=str(tmp_path / 'config')), database=database,
                   select=select, insert=insert, delete=delete, fix_line_ending=fix_line_ending,
                   subtitle_cache=SimpleNamespace(get=lambda key: cache.get(key)), jobs_queue=queue,
                   JobExecutionError=queue_namespace['JobExecutionError'],
                   get_video=Mock(side_effect=get_video), get_subtitles=indexed_rows,
                   get_target_folder=lambda path: str(Path(path).parent),
                   get_profile_id=lambda **kwargs: 1, get_audio_profile_languages=lambda _: [{'name': 'English'}],
                   path_mappings=SimpleNamespace(path_replace=lambda path: path,
                                                 path_replace_reverse=lambda path: path),
                   settings=SimpleNamespace(general=SimpleNamespace(dont_notify_manual_actions=False,
                       single_language=False, subzero_mods=[], utf8_encode=True, chmod_enabled=False, chmod='0644',
                       subfolder='current', subfolder_custom='')),
                   SubhdProvider=Mock(return_value=provider), extract_archive_subtitle=Mock(side_effect=extract_archive),
                   manual_download_subtitle=Mock(side_effect=manual_success),
                   store_subtitles=Mock(), history_log=Mock(), send_notifications=Mock(), record_rejection=Mock())
    manual_path = ROOT / 'bazarr/subtitles/manual.py'
    manual_nodes = [node for node in ast.parse(manual_path.read_text(encoding='utf-8')).body
                    if isinstance(node, ast.FunctionDef) and node.name in
                    {'_safe_manual_text', '_require_manual_download_result'}]
    exec(compile(ast.Module(body=manual_nodes, type_ignores=[]), str(manual_path), 'exec'), service)
    path = ROOT / 'bazarr/subtitles/season.py'
    nodes = [node for node in ast.parse(path.read_text(encoding='utf-8')).body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.Assign, ast.AnnAssign))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), service)
    service.update(fixture_source=source, fixture_cache=cache, fixture_files=files, fixture_database=database,
                   fixture_provider=provider, fixture_queue=queue, fixture_tmp=tmp_path,
                   fixture_manual_success=manual_success)
    yield service
    database.close()
    engine.dispose()


def preview(namespace):
    return namespace['preview_season'](101, CANDIDATE_KEY)


def enqueue(namespace, **options):
    flags = dict(hi='False', forced='False', use_original_format=False)
    flags.update(options)
    return namespace['enqueue_season'](101, CANDIDATE_KEY, **flags)


def error_status(namespace, action, status):
    with pytest.raises(namespace['SeasonRequestError']) as error:
        action()
    assert error.value.status == status


def test_preview_lists_only_existing_files_of_the_cached_media_current_series_and_season(season_namespace):
    namespace = season_namespace
    result = preview(namespace)
    assert result['series_id'] == 7 and result['season'] == 1
    assert result['title'] == 'Show' and result['provider'] == 'subhd'
    assert result['total'] == 2
    assert [row['episode_id'] for row in result['episodes']] == [101, 102]
    assert [row['episode'] for row in result['episodes']] == [1, 2]
    assert all({'episode_id', 'episode', 'title', 'existing_subtitles'} <= row.keys() for row in result['episodes'])
    namespace['fixture_provider'].download_archive.assert_not_called()
    namespace['manual_download_subtitle'].assert_not_called()


def test_expired_candidate_returns_not_found_without_any_provider_request(season_namespace):
    namespace = season_namespace
    error_status(namespace, lambda: namespace['preview_season'](101, 'expired-key'), 404)
    namespace['fixture_provider'].download_archive.assert_not_called()


@pytest.mark.parametrize('field,value', [('sonarrEpisodeId', 102), ('sonarrSeriesId', 8), ('season', 2)])
def test_cached_candidate_cannot_be_used_for_another_episode_series_or_season(season_namespace, field, value):
    namespace = season_namespace
    setattr(namespace['fixture_source'].video, field, value)
    error_status(namespace, lambda: preview(namespace), 400)


def test_cached_candidates_from_other_providers_are_rejected(season_namespace):
    namespace = season_namespace
    namespace['fixture_source'].provider_name = 'assrt'
    error_status(namespace, lambda: preview(namespace), 400)


def test_missing_current_database_source_episode_is_not_found(season_namespace):
    namespace = season_namespace
    database, table = namespace['fixture_database'], namespace['TableEpisodes']
    database.execute(delete(table).where(table.sonarrEpisodeId == 101))
    database.commit()
    error_status(namespace, lambda: preview(namespace), 404)


def test_preview_refuses_an_empty_season_after_media_files_disappear(season_namespace):
    namespace = season_namespace
    for identifier in (101, 102):
        namespace['fixture_files'][identifier].unlink()
    error_status(namespace, lambda: preview(namespace), 404)


def test_enqueue_stores_explicit_scope_and_existing_episode_ids_snapshot(season_namespace):
    namespace = season_namespace
    job_id = enqueue(namespace)
    job, = namespace['fixture_queue'].jobs_pending_queue
    assert isinstance(job_id, int) and job_id == job.job_id
    assert job.module == 'subtitles.season' and job.func == 'download_season'
    assert job.kwargs['scope'] == {'series_id': 7, 'season': 1}
    assert job.kwargs['episode_ids'] == [101, 102]
    assert job.kwargs['hi'] == 'False' and job.kwargs['forced'] == 'False'
    namespace['fixture_provider'].download_archive.assert_not_called()


@pytest.mark.parametrize('running', [False, True])
def test_same_series_and_season_cannot_be_enqueued_twice_pending_or_running(season_namespace, running):
    namespace = season_namespace
    enqueue(namespace)
    queue = namespace['fixture_queue']
    if running:
        job = queue.jobs_pending_queue.popleft()
        job.status = 'running'
        queue.jobs_running_queue.append(job)
    error_status(namespace, lambda: enqueue(namespace), 409)
    assert len(queue.jobs_pending_queue) + len(queue.jobs_running_queue) == 1


def test_an_existing_job_for_another_season_does_not_block_this_scope(season_namespace):
    namespace = season_namespace
    queue = namespace['fixture_queue']
    queue.feed_jobs_pending_queue('Other season', 'subtitles.season', 'download_season',
                                 kwargs={'scope': {'series_id': 7, 'season': 2}, 'episode_ids': [201]})
    assert enqueue(namespace) > 0
    assert len(queue.jobs_pending_queue) == 2


def test_enqueue_episode_snapshot_does_not_expand_after_new_files_appear(season_namespace):
    namespace = season_namespace
    enqueue(namespace)
    job, = namespace['fixture_queue'].jobs_pending_queue
    namespace['fixture_files'][103].write_bytes(b'new media after job enqueue')
    assert job.kwargs['episode_ids'] == [101, 102]


def run_enqueued(namespace):
    job_id = enqueue(namespace)
    job, = namespace['fixture_queue'].jobs_pending_queue
    return namespace['download_season'](**dict(job.kwargs, job_id=job_id))


def test_worker_fetches_archive_once_and_binds_independent_fresh_media_and_language_copies(season_namespace):
    namespace = season_namespace
    source = namespace['fixture_source']
    result = run_enqueued(namespace)
    namespace['fixture_provider'].download_archive.assert_called_once()
    assert namespace['get_video'].call_count == 2
    calls = namespace['manual_download_subtitle'].call_args_list
    assert len(calls) == 2
    candidates = [call.kwargs['prepared_subtitle'] for call in calls]
    videos = [call.kwargs['prepared_video'] for call in calls]
    assert candidates[0] is not candidates[1] and all(item is not source for item in candidates)
    assert candidates[0].language is not candidates[1].language and candidates[0].language is not source.language
    assert [item.sonarrEpisodeId for item in videos] == [101, 102]
    assert all(candidate.video is video for candidate, video in zip(candidates, videos))
    assert [item.video.episode for item in candidates] == [1, 2]
    assert all(call.kwargs['before_save'] and call.kwargs['after_save'] for call in calls)
    assert source.content is None and source.video.episode == 1
    assert namespace['store_subtitles'].call_count == namespace['history_log'].call_count == 2
    assert all(call.args[0] == 2 for call in namespace['history_log'].call_args_list)
    assert result is not None


def test_one_episode_manual_failure_does_not_stop_later_episode_or_log_failed_history(season_namespace):
    namespace = season_namespace
    def manual(*args, **kwargs):
        if kwargs['prepared_video'].sonarrEpisodeId == 101:
            return 'Validation could not confirm timing'
        return namespace['fixture_manual_success'](*args, **kwargs)
    namespace['manual_download_subtitle'].side_effect = manual
    result = run_enqueued(namespace)
    assert namespace['manual_download_subtitle'].call_count == 2
    namespace['fixture_provider'].download_archive.assert_called_once()
    assert 102 in [call.args[0] for call in namespace['store_subtitles'].call_args_list]
    assert namespace['history_log'].call_count == 1
    assert namespace['history_log'].call_args.args[:3] == (2, 7, 102)
    assert result is not None


def test_zero_saved_episodes_is_a_failed_job_not_a_success_report(season_namespace):
    namespace = season_namespace
    namespace['manual_download_subtitle'].side_effect = None
    namespace['manual_download_subtitle'].return_value = 'Validation failed for this episode'
    with pytest.raises(namespace['JobExecutionError']):
        run_enqueued(namespace)
    namespace['fixture_provider'].download_archive.assert_called_once()
    assert namespace['manual_download_subtitle'].call_count == 2
    namespace['history_log'].assert_not_called()


def test_worker_rechecks_episode_scope_and_file_existence_without_expanding_snapshot(season_namespace):
    namespace = season_namespace
    job_id = enqueue(namespace)
    job, = namespace['fixture_queue'].jobs_pending_queue
    namespace['fixture_files'][102].unlink()
    namespace['fixture_files'][103].write_bytes(b'late media that was not in snapshot')
    namespace['download_season'](**dict(job.kwargs, job_id=job_id))
    assert namespace['manual_download_subtitle'].call_count == 1
    namespace['store_subtitles'].assert_called_once_with(101)


def test_failed_archive_extraction_is_skipped_and_later_episode_still_saves(season_namespace):
    namespace = season_namespace
    namespace['extract_archive_subtitle'].side_effect = [(None, None), (srt(SIMPLIFIED), 'srt')]
    run_enqueued(namespace)
    assert namespace['manual_download_subtitle'].call_count == 1
    namespace['store_subtitles'].assert_called_once_with(102)
    assert namespace['history_log'].call_count == 1


def test_notification_preference_is_obeyed_after_success_only(season_namespace):
    namespace = season_namespace
    namespace['settings'].general.dont_notify_manual_actions = True
    run_enqueued(namespace)
    namespace['send_notifications'].assert_not_called()
    assert namespace['history_log'].call_count == 2


def test_queued_candidate_expiration_reports_a_clear_failed_job_before_download(season_namespace):
    namespace = season_namespace
    job_id = enqueue(namespace)
    job, = namespace['fixture_queue'].jobs_pending_queue
    namespace['fixture_cache'].clear()
    with pytest.raises(namespace['JobExecutionError'], match='cache expired'):
        namespace['download_season'](**dict(job.kwargs, job_id=job_id))
    namespace['fixture_provider'].download_archive.assert_not_called()


def test_episode_scores_are_recomputed_instead_of_copying_the_search_episode(season_namespace):
    namespace = season_namespace
    namespace['fixture_source'].matches = {'hash'}
    namespace['fixture_source'].score = 99999
    run_enqueued(namespace)
    for call in namespace['manual_download_subtitle'].call_args_list:
        candidate = call.kwargs['prepared_subtitle']
        assert 'hash' not in candidate.matches
        expected, expected_without_hash = compute_score(candidate.matches, candidate, candidate.video,
                                                        hearing_impaired=False)
        assert candidate.score == expected_without_hash
        assert candidate.score != 99999


def test_progress_preserves_safe_timing_details_and_redacts_credentials(season_namespace, caplog):
    callback = season_namespace['_episode_progress'](None, 1, 2, 101)
    with caplog.at_level(logging.INFO):
        callback('reference_sample', {'index': 2, 'total': 5, 'remaining_seconds': 36,
                                     'reason': 'https://example.test/?token=SECRET',
                                     'token': 'SECRET', 'raw_subtitle': 'PRIVATE-TEXT'})
    payload = json.loads(caplog.records[-1].getMessage().split(': ', 1)[1])
    assert payload['index'] == 2 and payload['total'] == 5 and payload['remaining_seconds'] == 36
    assert payload['episode_index'] == 1 and payload['episode_total'] == 2
    assert 'SECRET' not in caplog.text and 'PRIVATE-TEXT' not in caplog.text


def test_worker_failure_reports_never_expose_external_credentials_or_subtitle_body(season_namespace, caplog):
    namespace = season_namespace
    namespace['manual_download_subtitle'].side_effect = [
        ValueError('https://unsafe.example/?token=PRIVATE-TOKEN Cookie: session=PRIVATE-COOKIE'),
        ValueError('1\n00:00:01,000 --> 00:00:02,000\nPRIVATE SUBTITLE CONTENT'),
    ]
    with caplog.at_level(logging.INFO), pytest.raises(namespace['JobExecutionError']) as error:
        run_enqueued(namespace)
    output = str(error.value) + caplog.text
    assert 'PRIVATE-TOKEN' not in output
    assert 'PRIVATE-COOKIE' not in output
    assert 'PRIVATE SUBTITLE CONTENT' not in output


def index_old_file(namespace, episode_id, extension, data):
    video_path = namespace['fixture_files'][episode_id]
    path = video_path.with_suffix('.zh-CN.' + extension)
    path.write_bytes(data)
    database = namespace['fixture_database']
    database.add(namespace['TableEpisodesSubtitles'](
        sonarrEpisodeId=episode_id, sonarrSeriesId=7, path=str(path), language='zh-CN',
        forced=False, hi=False, embedded_track_id=None, size=len(data)))
    database.commit()
    return path


def test_failure_after_save_restores_previous_formats_and_later_episode_still_commits(season_namespace):
    namespace = season_namespace
    old_srt = index_old_file(namespace, 101, 'srt', b'old indexed SRT bytes')
    old_ass = index_old_file(namespace, 101, 'ass', b'old indexed ASS bytes')

    def history(action, series_id, episode_id, result):
        if episode_id == 101:
            raise RuntimeError('history failure after subtitle write')

    namespace['history_log'].side_effect = history
    report = run_enqueued(namespace)
    assert old_srt.read_bytes() == b'old indexed SRT bytes'
    assert old_ass.read_bytes() == b'old indexed ASS bytes'
    assert namespace['fixture_files'][102].with_suffix('.zh-CN.srt').is_file()
    assert report['saved'] == report['failed'] == 1 and report['skipped'] == 0
    assert report['episodes'][0]['rollback']['restored'] == 2
    assert report['episodes'][0]['rollback']['conflicts'] == []


def test_validation_failure_before_save_leaves_previous_files_untouched(season_namespace):
    namespace = season_namespace
    old_srt = index_old_file(namespace, 101, 'srt', b'old indexed SRT bytes')
    old_ass = index_old_file(namespace, 101, 'ass', b'old indexed ASS bytes')

    def manual(*args, **kwargs):
        if kwargs['prepared_video'].sonarrEpisodeId == 101:
            return 'Timing evidence is inconclusive'
        return namespace['fixture_manual_success'](*args, **kwargs)

    namespace['manual_download_subtitle'].side_effect = manual
    report = run_enqueued(namespace)
    assert old_srt.read_bytes() == b'old indexed SRT bytes'
    assert old_ass.read_bytes() == b'old indexed ASS bytes'
    assert report['saved'] == report['failed'] == 1
    assert report['episodes'][0]['rollback']['restored'] == 0


def test_worker_skips_an_episode_id_outside_explicit_scope_even_if_its_file_exists(season_namespace):
    namespace = season_namespace
    report = namespace['download_season'](101, CANDIDATE_KEY, {'series_id': 7, 'season': 1}, [101, 301])
    assert report['saved'] == 1 and report['skipped'] == 1 and report['failed'] == 0
    assert namespace['manual_download_subtitle'].call_count == 1
    assert namespace['manual_download_subtitle'].call_args.kwargs['prepared_video'].sonarrEpisodeId == 101
    namespace['fixture_provider'].download_archive.assert_called_once()


def backup_fixture(namespace):
    video_path = namespace['fixture_files'][101]
    folder, stem = video_path.parent, video_path.stem
    matching, protected, rows = {}, {}, []
    for extension, hi in [('srt', False), ('ass', True), ('ssa', False), ('vtt', False)]:
        path = folder / (stem + '.zh' + ('.hi' if hi else '') + '.' + extension)
        data = ('old ' + extension).encode('utf-8')
        path.write_bytes(data)
        matching[extension] = (path, data)
        rows.append({'path': str(path), 'language': 'zh', 'forced': False, 'hi': hi,
                     'embedded_track_id': None, 'sonarrEpisodeId': 101, 'sonarrSeriesId': 7})
    for name, language, forced, track, indexed in [
        (stem + '.zh.hi.ssa', 'zh', False, None, False),
        ('DifferentEpisode.zh.srt', 'zh', False, None, True),
        (stem + '.OtherVersion.zh.srt', 'zh', False, None, True),
        (stem + '.en.srt', 'en', False, None, True),
        (stem + '.zh.forced.srt', 'zh', True, None, True),
        (stem + '.zh.hi.vtt', 'zh', False, 2, True),
    ]:
        path = folder / name
        data = ('untouched ' + name).encode('utf-8')
        path.write_bytes(data)
        protected[path] = data
        if indexed:
            rows.append({'path': str(path), 'language': language, 'forced': forced, 'hi': True,
                         'embedded_track_id': track, 'sonarrEpisodeId': 101, 'sonarrSeriesId': 7})
    target = copy.copy(namespace['fixture_source'])
    target.language = Language('zho')
    target.format, target.content = 'srt', srt(SIMPLIFIED)
    video = SimpleNamespace(original_path=str(video_path), sonarrEpisodeId=101, sonarrSeriesId=7)
    job_dir = namespace['fixture_tmp'] / 'job-backups'
    backup = namespace['_EpisodeBackup'](job_dir, 101, str(video_path), target, rows)
    return SimpleNamespace(backup=backup, matching=matching, protected=protected, subtitle=target, video=video, rows=rows)


def test_backup_before_save_only_copies_and_never_removes_existing_subtitles(season_namespace):
    case = backup_fixture(season_namespace)
    case.backup.before_save(case.video, case.subtitle)
    for path, data in list(case.matching.values()) + list(case.protected.items()):
        assert path.read_bytes() == data


def test_successful_save_removes_only_backed_up_same_language_formats_and_preserves_selected_file(season_namespace):
    case = backup_fixture(season_namespace)
    case.backup.before_save(case.video, case.subtitle)
    saved = copy.copy(case.subtitle)
    path, _ = case.matching['srt']
    saved.storage_path = str(path)
    path.write_bytes(saved.content)
    case.backup.after_save(saved)
    assert path.read_bytes() == saved.content
    assert all(not path.exists() for extension, (path, _) in case.matching.items() if extension != 'srt')
    for path, data in case.protected.items():
        assert path.read_bytes() == data


def test_rollback_restores_overwritten_and_removed_original_subtitles_after_own_save(season_namespace):
    case = backup_fixture(season_namespace)
    case.backup.before_save(case.video, case.subtitle)
    saved = copy.copy(case.subtitle)
    saved.storage_path = str(case.matching['srt'][0])
    Path(saved.storage_path).write_bytes(saved.content)
    case.backup.after_save(saved)
    case.backup.rollback()
    for path, data in list(case.matching.values()) + list(case.protected.items()):
        assert path.read_bytes() == data


def test_rollback_does_not_overwrite_an_external_change_after_this_job_saved(season_namespace):
    case = backup_fixture(season_namespace)
    case.backup.before_save(case.video, case.subtitle)
    saved = copy.copy(case.subtitle)
    path, _ = case.matching['srt']
    saved.storage_path = str(path)
    path.write_bytes(saved.content)
    case.backup.after_save(saved)
    external = b'external writer changed this file after job save'
    path.write_bytes(external)
    case.backup.rollback()
    assert path.read_bytes() == external


def test_rollback_removes_only_the_new_file_written_by_this_job(season_namespace):
    namespace = season_namespace
    video_path = namespace['fixture_files'][101]
    target = copy.copy(namespace['fixture_source'])
    target.language, target.format, target.content = Language('zho'), 'srt', srt(SIMPLIFIED)
    saved_path = video_path.with_suffix('.zh.srt')
    backup = namespace['_EpisodeBackup'](namespace['fixture_tmp'] / 'new-file-backup', 101,
                                         str(video_path), target, [])
    backup.before_save(SimpleNamespace(original_path=str(video_path)), target)
    saved = copy.copy(target)
    saved.storage_path = str(saved_path)
    saved_path.write_bytes(saved.content)
    backup.after_save(saved)
    report = backup.rollback()
    assert not saved_path.exists() and video_path.is_file()
    assert report['removed'] == 1 and report['conflicts'] == []


def test_rollback_preserves_original_subtitle_file_mode_and_mtime(season_namespace):
    case = backup_fixture(season_namespace)
    snapshots = {}
    for path, _ in case.matching.values():
        os.chmod(path, 0o640)
        os.utime(path, ns=(1_770_000_000_000_000_000, 1_770_000_000_000_000_000))
        snapshots[path] = (stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
    case.backup.before_save(case.video, case.subtitle)
    saved = copy.copy(case.subtitle)
    saved.storage_path = str(case.matching['srt'][0])
    Path(saved.storage_path).write_bytes(saved.content)
    case.backup.after_save(saved)
    case.backup.rollback()
    for path, original in snapshots.items():
        assert (stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns) == original


def test_nonindexed_colliding_target_file_is_protected_before_save(season_namespace):
    namespace = season_namespace
    video_path = namespace['fixture_files'][101]
    path = video_path.with_suffix('.zh.srt')
    existing = b'nonindexed external subtitle must remain'
    path.write_bytes(existing)
    target = copy.copy(namespace['fixture_source'])
    target.language, target.format = Language('zho'), 'srt'
    target.content = srt(SIMPLIFIED)
    backup = namespace['_EpisodeBackup'](namespace['fixture_tmp'] / 'empty-index', 101, str(video_path), target, [])
    with pytest.raises((ValueError, RuntimeError)):
        backup.before_save(SimpleNamespace(original_path=str(video_path)), target)
    assert path.read_bytes() == existing


def test_symlink_indexed_subtitle_is_never_backed_up_deleted_or_followed(season_namespace):
    namespace = season_namespace
    case = backup_fixture(namespace)
    outside = namespace['fixture_tmp'] / 'outside-subtitle.srt'
    outside.write_bytes(b'outside target must remain')
    path = namespace['fixture_files'][101].with_suffix('.zh-CN.srt')
    try:
        path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('The host cannot create symbolic links')
    indexed = case.rows + [{'path': str(path), 'language': 'zh', 'forced': False, 'hi': False,
                'embedded_track_id': None, 'sonarrEpisodeId': 101, 'sonarrSeriesId': 7}]
    backup = namespace['_EpisodeBackup'](namespace['fixture_tmp'] / 'symlink-index', 101,
                                         str(namespace['fixture_files'][101]), case.subtitle, indexed)
    backup.before_save(case.video, case.subtitle)
    saved = copy.copy(case.subtitle)
    saved.storage_path = str(case.matching['srt'][0])
    Path(saved.storage_path).write_bytes(saved.content)
    backup.after_save(saved)
    assert path.is_symlink() and outside.read_bytes() == b'outside target must remain'
