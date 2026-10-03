"""Manual failures reach the failed queue and only saved subtitles reach completion."""

import ast
from collections import deque
from datetime import datetime, timezone
import inspect
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Lock, RLock, Thread
import time
from types import SimpleNamespace
from typing import Union
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_nodes(relative_path, names, namespace):
    source = ROOT / relative_path
    tree = ast.parse(source.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


@pytest.fixture
def queue_namespace():
    events = []
    namespace = dict(logging=logging, inspect=inspect, os=os, time=time, sleep=lambda _: None,
                     datetime=datetime, timezone=timezone, deque=deque, Union=Union,
                     Thread=Thread, Lock=Lock, RLock=RLock,
                     event_stream=lambda **event: events.append(event),
                     settings=SimpleNamespace(general=SimpleNamespace(concurrent_jobs=1)))
    load_nodes('bazarr/app/jobs_queue.py', {'JobExecutionError', 'Job', 'JobsQueue'}, namespace)
    namespace['events'] = events
    return namespace


class Query:
    def __getattr__(self, _name):
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __eq__(self, other):
        return self


@pytest.fixture
def manual_namespace(queue_namespace, tmp_path):
    subtitle = SimpleNamespace(id='sePyJ8', subtitle_id='sePyJ8', provider_name='subhd',
                               content=b'', text='', format='srt', language=SimpleNamespace(),
                               selected_archive_member=None, is_valid=Mock(return_value=True))
    video = SimpleNamespace(original_path=str(tmp_path / 'movie.mkv'))
    metadata = SimpleNamespace(title='The Game', year=1997, path=video.original_path, sceneName=None,
                               audio_language='English', season=2, episode=5, episodeTitle='Hundred Dollar Baby')
    queue = queue_namespace['JobsQueue']()
    namespace = dict(logging=logging, json=json, re=re, time=time, os=os, sys=sys,
                     JobExecutionError=queue_namespace['JobExecutionError'], jobs_queue=queue,
                     subtitle_cache=SimpleNamespace(get=lambda key: subtitle if key == 'candidate-uuid' else None),
                     settings=SimpleNamespace(general=SimpleNamespace(
                         utf8_encode=True, subzero_mods=[], chmod='0644', chmod_enabled=False,
                         single_language=False, dont_notify_manual_actions=False)),
                     get_array_from=lambda value: value, force_unicode=lambda value: value,
                     get_video=Mock(return_value=video), _get_pool=lambda *args: object(),
                     download_subtitles=Mock(), validate_download=Mock(return_value=True),
                     get_target_folder=lambda _: str(tmp_path), save_subtitles=Mock(),
                     _get_scores=lambda _: (0, 100, None), process_subtitle=Mock(),
                     database=SimpleNamespace(execute=Mock(return_value=SimpleNamespace(first=lambda: metadata))),
                     select=Query(), TableEpisodes=Query(), TableShows=Query(), TableMovies=Query(),
                     path_mappings=SimpleNamespace(path_replace=lambda value: value,
                                                   path_replace_movie=lambda value: value),
                     get_audio_profile_languages=lambda _: [{'name': 'English'}], get_profile_id=lambda **_: 1,
                     store_subtitles=Mock(), store_subtitles_movie=Mock(),
                     history_log=Mock(), history_log_movie=Mock(),
                     send_notifications=Mock(), send_notifications_movie=Mock())
    names = {'_safe_manual_text', '_manual_subtitle_stats', '_manual_download_progress',
             '_audio_validation_failure', '_require_manual_download_result', '_manual_job_failure',
             'manual_download_subtitle', 'movie_manually_download_specific_subtitle',
             'episode_manually_download_specific_subtitle'}
    load_nodes('bazarr/subtitles/manual.py', names, namespace)
    namespace.update(subtitle_fixture=subtitle, video_fixture=video, metadata_fixture=metadata,
                     events=queue_namespace['events'], queue_namespace=queue_namespace)
    return namespace


def invoke_wrapper(namespace, media_type, job_id):
    common = dict(hi='False', forced='False', use_original_format=False, selected_provider='subhd',
                  subtitle='candidate-uuid', job_id=job_id)
    if media_type == 'movie':
        return namespace['movie_manually_download_specific_subtitle'](radarr_id=47, **common)
    return namespace['episode_manually_download_specific_subtitle'](
        sonarr_series_id=9, sonarr_episode_id=47, **common)


def run_wrapper_as_job(namespace, media_type):
    queue = namespace['jobs_queue']
    execute = lambda job_id: invoke_wrapper(namespace, media_type, job_id)
    namespace['queue_namespace']['importlib'] = SimpleNamespace(import_module=lambda _: SimpleNamespace(execute=execute))
    job_id = queue.feed_jobs_pending_queue('Manually downloading Subtitles', 'manual_test', 'execute',
                                         is_progress=True, progress_max=100)
    succeeded = queue._run_job()
    return succeeded, queue.list_jobs_from_queue(job_id=job_id)[0]


def lowlevel(namespace, progress=None):
    return namespace['manual_download_subtitle'](
        path=namespace['video_fixture'].original_path, audio_language='English', hi='False', forced='False',
        subtitle='candidate-uuid', provider='subhd', sceneName='The.Game.1997', title='The Game', media_type='movie',
        use_original_format=False, profile_id=1, job_id=3, progress=progress)


def stage_logs(caplog):
    prefix = 'BAZARR Manual subtitle job stage: '
    return [json.loads(record.getMessage()[len(prefix):]) for record in caplog.records
            if record.getMessage().startswith(prefix)]


@pytest.mark.parametrize('result', [('legacy error', 500), False, None])
def test_queue_preserves_unrelated_return_value_semantics(queue_namespace, result):
    queue = queue_namespace['JobsQueue']()
    queue_namespace['importlib'] = SimpleNamespace(
        import_module=lambda _: SimpleNamespace(execute=lambda **_: result))
    queue.feed_jobs_pending_queue('Legacy task', 'legacy', 'execute')
    assert queue._run_job() is True
    assert len(queue.jobs_completed_queue) == 1
    assert queue.jobs_completed_queue[0].job_returned_value == result
    assert not queue.jobs_failed_queue


def test_queue_dedicated_failure_marks_failed_and_refreshes_frontend(queue_namespace):
    queue = queue_namespace['JobsQueue']()

    def reject(**_):
        raise queue_namespace['JobExecutionError']('No original audio track is available')

    queue_namespace['importlib'] = SimpleNamespace(import_module=lambda _: SimpleNamespace(execute=reject))
    job_id = queue.feed_jobs_pending_queue('Manual download', 'manual', 'execute', is_progress=True, progress_max=100)
    assert queue._run_job() is False
    assert not queue.jobs_completed_queue
    assert not queue.jobs_running_queue
    failed = queue.list_jobs_from_queue(job_id=job_id)[0]
    assert failed['status'] == 'failed'
    assert failed['progress_message'] == 'No original audio track is available'
    assert queue_namespace['events'][-1]['payload'] == {
        'job_id': job_id, 'status': 'failed', 'progress_value': None}


@pytest.mark.parametrize('media_type', ['movie', 'series'])
@pytest.mark.parametrize('result', ['Audio validation failed', ('Provider download failed', 500), None])
def test_manual_error_results_reach_failed_queue_without_history(manual_namespace, media_type, result):
    namespace = manual_namespace
    namespace['manual_download_subtitle'] = Mock(return_value=result)
    succeeded, job = run_wrapper_as_job(namespace, media_type)
    assert succeeded is False
    assert job['status'] == 'failed'
    assert job['job_name'].startswith('Failed downloading subtitles')
    assert 'downloaded' not in job['job_name'].lower()
    assert job['progress_value'] < 100
    assert not namespace['history_log'].called
    assert not namespace['history_log_movie'].called
    assert not namespace['store_subtitles'].called
    assert not namespace['store_subtitles_movie'].called
    assert not namespace['send_notifications'].called
    assert not namespace['send_notifications_movie'].called


@pytest.mark.parametrize('media_type', ['movie', 'series'])
def test_missing_media_reaches_failed_queue(manual_namespace, media_type):
    namespace = manual_namespace
    namespace['database'].execute.return_value.first = lambda: None
    succeeded, job = run_wrapper_as_job(namespace, media_type)
    assert succeeded is False
    assert job['status'] == 'failed'
    assert 'not found' in job['progress_message'].lower()
    assert not namespace['save_subtitles'].called


def test_timeout_failure_log_does_not_expose_command_or_signed_url(manual_namespace, caplog):
    namespace = manual_namespace
    namespace['manual_download_subtitle'] = Mock(side_effect=subprocess.TimeoutExpired(
        ['ffmpeg', 'https://private.example/download?token=secret', 'Authorization=Bearer-secret'], 35))
    with caplog.at_level(logging.INFO):
        succeeded, job = run_wrapper_as_job(namespace, 'movie')
    assert succeeded is False
    assert 'TimeoutExpired' in job['progress_message']
    assert 'private.example' not in caplog.text
    assert 'Bearer-secret' not in caplog.text
    assert 'secret' not in job['progress_message']
    assert stage_logs(caplog)[-1]['exception_type'] == 'TimeoutExpired'


def test_rejected_timing_returns_readable_reason_and_never_saves(manual_namespace):
    namespace = manual_namespace
    subtitle = namespace['subtitle_fixture']
    subtitle.audio_timing_failure_reason = 'insufficient_coverage'
    subtitle.audio_timing_failure_detail = 'Subtitle covers only 63 cues and 4 minutes of this movie'
    namespace['validate_download'].return_value = False
    callback = Mock()
    result = lowlevel(namespace, callback)
    assert '63 cues' in result
    assert '4 minutes' in result
    namespace['validate_download'].assert_called_once_with(namespace['video_fixture'], subtitle, progress=callback)
    assert not namespace['save_subtitles'].called
    assert not namespace['process_subtitle'].called
    assert [call.args[0] for call in callback.call_args_list] == ['download_unpack', 'downloaded', 'validation']


def test_validation_machine_reason_is_explained_to_user(manual_namespace):
    subtitle = manual_namespace['subtitle_fixture']
    subtitle.audio_timing_failure_reason = subtitle.audio_timing_failure_detail = 'obvious_fragment'
    message = manual_namespace['_audio_validation_failure'](subtitle)
    assert 'short fragment' in message
    assert 'obvious_fragment' not in message


def test_direct_legacy_caller_keeps_two_argument_validation(manual_namespace):
    namespace = manual_namespace
    namespace['validate_download'].return_value = False
    lowlevel(namespace)
    namespace['validate_download'].assert_called_once_with(namespace['video_fixture'], namespace['subtitle_fixture'])


def test_download_provider_receives_current_duration_without_losing_episode_identity(manual_namespace):
    namespace = manual_namespace
    candidate_video = SimpleNamespace(duration=270, season=2, episode=5, search_keyword='Known alias')
    subtitle = namespace['subtitle_fixture']
    subtitle.video = candidate_video
    namespace['video_fixture'].duration = 7733.12
    namespace['validate_download'].return_value = False

    def download(candidates, pool):
        assert candidates[0].video is candidate_video
        assert candidates[0].video.duration == 7733.12
        assert candidates[0].video.season == 2
        assert candidates[0].video.episode == 5
        assert candidates[0].video.search_keyword == 'Known alias'

    namespace['download_subtitles'].side_effect = download
    lowlevel(namespace)
    namespace['download_subtitles'].assert_called_once()


def test_expired_candidate_is_not_a_completed_download(manual_namespace):
    namespace = manual_namespace
    namespace['subtitle_cache'].get = lambda _: None
    succeeded, job = run_wrapper_as_job(namespace, 'movie')
    assert succeeded is False
    assert 'search again' in job['progress_message'].lower()
    assert not namespace['download_subtitles'].called
    assert not namespace['save_subtitles'].called


def test_missing_file_after_save_is_not_success(manual_namespace, tmp_path):
    namespace = manual_namespace
    subtitle = namespace['subtitle_fixture']
    subtitle.storage_path = str(tmp_path / 'does-not-exist.srt')
    namespace['save_subtitles'].return_value = [subtitle]
    succeeded, job = run_wrapper_as_job(namespace, 'movie')
    assert succeeded is False
    assert 'file on disk' in job['progress_message']
    assert not namespace['process_subtitle'].called
    assert not namespace['history_log_movie'].called


@pytest.mark.parametrize('media_type', ['movie', 'series'])
@pytest.mark.parametrize('validation_enabled', [True, False])
def test_saved_download_is_completed_after_all_stages(manual_namespace, tmp_path, caplog,
                                                     media_type, validation_enabled):
    namespace = manual_namespace
    subtitle = namespace['subtitle_fixture']
    subtitle.text = '1\n00:00:01,000 --> 00:00:02,500\n你好，晚上11点30分。\n'
    subtitle.content = subtitle.text.encode('utf-8')
    subtitle.selected_archive_member = 'The.Game.1997.CHS&ENG.srt'
    saved_path = tmp_path / 'The.Game.1997.zh.srt'

    def save(*args, **kwargs):
        saved_path.write_bytes(subtitle.content)
        subtitle.storage_path = str(saved_path)
        return [subtitle]

    def validate(video, candidate, progress=None):
        if validation_enabled:
            progress('subtitle_coverage', {'cue_count': 1})
            progress('original_audio', {'audio_index': 10, 'stream_index': 11, 'language': 'eng'})
            progress('audio_cache', {'cache_hit': False})
            for index in range(1, 6):
                progress('audio_sample', {'index': index, 'total': 5, 'cache_hit': False})
            progress('timing_check', {'score': .45})
            progress('timing_search', {'rate': 1, 'offset_seconds': 12})
            progress('timing_verify', {'score': .55})
            progress('timing_accepted')
        return True

    namespace['save_subtitles'].side_effect = save
    namespace['validate_download'].side_effect = validate
    result = SimpleNamespace(message='Saved verified subtitle')
    namespace['process_subtitle'].return_value = result
    with caplog.at_level(logging.INFO):
        succeeded, job = run_wrapper_as_job(namespace, media_type)
    assert succeeded is True
    assert job['status'] == 'completed'
    assert job['job_name'].startswith('Manually downloaded Subtitles')
    assert job['progress_value'] == job['progress_max'] == 100
    assert saved_path.read_bytes() == subtitle.content
    history = namespace['history_log_movie'] if media_type == 'movie' else namespace['history_log']
    assert history.call_args.args[-1] is result
    logs = stage_logs(caplog)
    stages = [entry['stage'] for entry in logs]
    assert stages[:3] == ['download_unpack', 'downloaded', 'validation']
    assert stages[-3:] == ['saving', 'postprocessing', 'completed']
    assert ('timing_accepted' in stages) is validation_enabled
    progress_values = [event['payload']['progress_value'] for event in namespace['events']
                       if isinstance(event['payload'].get('progress_value'), int)]
    assert progress_values == sorted(progress_values)
    assert logs[-1]['selected_archive_member'] == subtitle.selected_archive_member
    assert logs[-1]['bytes'] == len(subtitle.content)
    assert logs[-1]['parsed_cues'] == 1
    assert logs[-1]['last_end_seconds'] == 2.5
    assert logs[-2]['save_path'] == str(saved_path)
    assert all(entry['job_id'] == job['job_id'] and entry['provider'] == 'subhd' and
               entry['subid'] == 'sePyJ8' and entry['elapsed_seconds'] >= 0 for entry in logs)
    assert subtitle.text not in caplog.text


def test_progress_drops_credential_fields_and_redacts_failure_details(manual_namespace, caplog):
    namespace = manual_namespace
    progress = namespace['_manual_download_progress'](3, 'subhd', namespace['subtitle_fixture'])
    with caplog.at_level(logging.INFO):
        progress('timing_rejected', {
            'failure_detail': 'Request failed https://private.test/?signature=abc token=private-token',
            'url': 'https://private.test/', 'token': 'other-secret', 'subtitle_text': 'do not log this text'})
    log = stage_logs(caplog)[0]
    assert 'url' not in log and 'token' not in log and 'subtitle_text' not in log
    assert '[redacted-url]' in log['failure_detail']
    assert 'token=[redacted]' in log['failure_detail']
    assert 'private.test' not in caplog.text
    assert 'private-token' not in caplog.text
    assert 'other-secret' not in caplog.text


@pytest.mark.parametrize('media_type', ['movie', 'series'])
def test_enqueue_requests_progress_without_executing_download(manual_namespace, media_type):
    namespace = manual_namespace
    queue = namespace['jobs_queue']
    queue.add_job_from_function = Mock(return_value=12)
    assert invoke_wrapper(namespace, media_type, None) == 12
    queue.add_job_from_function.assert_called_once_with(
        'Manually downloading Subtitles', is_progress=True, progress_max=100)
    assert not namespace['database'].execute.called
    assert not namespace['download_subtitles'].called
