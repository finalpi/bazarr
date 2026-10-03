"""Known mismatches stay visible and are skipped before any network download."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from flask_restx import marshal, reqparse

from test_manual_download_jobs import manual_namespace, queue_namespace, lowlevel as call_lowlevel
from test_manual_search_options import manual_namespace as search_namespace, episode, CHINESE
from test_subhd_search_tags import response_model
from test_timing import pool_method, candidate, Episode, production_function
from subliminal_patch.providers.subhd import SubhdSubtitle


ROOT = Path(__file__).resolve().parents[2]
REJECTION = {'id': 7, 'reason': 'timing_mismatch', 'detail': 'Timing does not match original audio',
             'member': 'CHS&ENG.srt', 'timestamp': '2026-10-04T00:00:00Z'}


def test_rejected_candidate_remains_in_search_with_reason_and_one_scoped_load():
    video = episode()
    subtitle = SubhdSubtitle(CHINESE, 'raw-site-id', '/a/raw-site-id', 'Canonical Show S03E02', video)
    pool = Mock()
    pool.__enter__ = Mock(return_value=pool)
    pool.__exit__ = Mock(return_value=False)
    namespace = search_namespace(pool, video, [subtitle], [])
    load, get = Mock(return_value={'scoped': REJECTION}), Mock(return_value=REJECTION)
    namespace.update(load_rejections=load, get_rejection=get)
    rows = namespace['manual_search']('/tmp/media.mkv', 1, ['subhd'], 'None', 'Canonical Show', 'series')
    assert len(rows) == 1 and rows[0]['rejected'] is True
    assert rows[0]['rejection'] == REJECTION and rows[0]['subtitle'] == 'cached-id'
    load.assert_called_once_with(video)
    get.assert_called_once_with(video, subtitle, records={'scoped': REJECTION})


@pytest.mark.parametrize('media_type', ['movie', 'series'])
def test_response_marshalling_preserves_rejection_and_legacy_rows(media_type):
    model = response_model(media_type)
    row = marshal({'provider': 'subhd', 'subtitle': 'uuid', 'rejected': True, 'rejection': REJECTION}, model)
    assert row['rejected'] and row['rejection'] == REJECTION
    legacy = marshal({'provider': 'legacy', 'subtitle': 'uuid'}, model)
    assert not legacy['rejected'] and legacy['rejection'] is None


def test_manual_stale_result_is_blocked_before_provider_download(manual_namespace):
    namespace = manual_namespace
    namespace['get_rejection'].return_value = REJECTION
    result = call_lowlevel(namespace)
    assert 'excluded' in result and 'Allow retry' in result
    namespace['download_subtitles'].assert_not_called()
    namespace['validate_download'].assert_not_called()
    namespace['save_subtitles'].assert_not_called()


def test_explicit_provider_fragment_failure_is_recorded_before_return(manual_namespace):
    namespace = manual_namespace
    namespace['subtitle_fixture'].is_valid.return_value = False
    namespace['subtitle_fixture'].download_failure_reason = 'obvious_fragment'
    call_lowlevel(namespace)
    namespace['record_rejection'].assert_called_once_with(
        namespace['video_fixture'], namespace['subtitle_fixture'], 'obvious_fragment')
    namespace['save_subtitles'].assert_not_called()


def test_automatic_download_skips_rejected_candidate_before_http_and_tries_next():
    bad, good = candidate(1), candidate(2)
    download = Mock(return_value=True)
    check = Mock(side_effect=lambda video, subtitle: REJECTION if subtitle.id == 1 else None)
    pool = SimpleNamespace(download_subtitle=download, providers=['test'], subtitle_rejection_check=check)
    assert pool_method()(pool, [bad, good], Episode(), [bad.language], only_one=True) == [good]
    download.assert_called_once_with(good)


def test_whisper_fallback_cannot_download_a_previously_rejected_candidate():
    bad = candidate(1, 'whisperai')
    download = Mock(return_value=True)
    pool = SimpleNamespace(download_subtitle=download, providers=['whisperai'],
                           subtitle_rejection_check=lambda video, subtitle: REJECTION)
    assert pool_method()(pool, [bad], Episode(), [bad.language], min_score=400, fallback_allowed=True) == []
    download.assert_not_called()


@pytest.mark.parametrize('media_type,identity', [('movie', 'radarrId'), ('series', 'sonarrEpisodeId')])
def test_clear_is_scoped_to_media_and_does_not_download(media_type, identity):
    video = SimpleNamespace(**{identity: 49})
    subtitle = SimpleNamespace(video=video)
    clear = Mock(return_value=True)
    function = production_function('bazarr/subtitles/manual.py', 'clear_manual_rejection', {
        'subtitle_cache': SimpleNamespace(get=lambda key: subtitle if key == 'uuid' else None),
        'clear_rejection': clear,
    })
    assert function(media_type, 50, 'uuid')[1] == 400
    assert function(media_type, 49, 'expired')[1] == 404
    clear.assert_not_called()
    assert function(media_type, 49, 'uuid') == ('', 204)
    clear.assert_called_once_with(video, subtitle)


@pytest.mark.parametrize('media_type,id_name', [('movie', 'radarrid'), ('series', 'episodeid')])
def test_delete_api_forwards_only_media_id_and_cache_uuid(media_type, id_name):
    filename = 'providers_movies.py' if media_type == 'movie' else 'providers_episodes.py'
    path = ROOT / 'bazarr/api/providers' / filename
    resource = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n, ast.ClassDef))
    assignments = [n for n in resource.body if isinstance(n, (ast.Assign, ast.Expr)) and
                   'delete_request_parser' in ast.unparse(n)]
    method = next(n for n in resource.body if isinstance(n, ast.FunctionDef) and n.name == 'delete')
    method.decorator_list = []
    resource.bases, resource.decorator_list, resource.body = [], [], assignments + [method]
    clear = Mock(return_value=('', 204))
    namespace = {'reqparse': reqparse, 'clear_manual_rejection': clear}
    exec(compile(ast.Module(body=[resource], type_ignores=[]), str(path), 'exec'), namespace)
    with Flask(__name__).test_request_context('/', method='DELETE', data={id_name: 49, 'subtitle': 'uuid'}):
        assert namespace[resource.name]().delete() == ('', 204)
    clear.assert_called_once_with(media_type, 49, 'uuid')
