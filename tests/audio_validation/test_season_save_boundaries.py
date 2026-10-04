"""A prepared season member must still validate, and failed writes preserve the old file."""

import ast
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Blueprint, Flask
from flask_restx import Api, Namespace, Resource, reqparse
import pytest
from subzero.language import Language
from subliminal_patch.core import _atomic_write_subtitle, save_subtitles
import subliminal_patch.core as core

from test_manual_download_jobs import manual_namespace, queue_namespace  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]


def prepared_download(namespace, before=None, after=None):
    return namespace['manual_download_subtitle'](
        path=namespace['video_fixture'].original_path, audio_language='English', hi='False', forced='False',
        subtitle='deliberately-not-cached', provider='subhd', sceneName='Veep.S01E02', title='Veep', media_type='series',
        use_original_format=False, profile_id=1, prepared_subtitle=namespace['subtitle_fixture'],
        prepared_video=namespace['video_fixture'], before_save=before, after_save=after)


def test_prepared_member_skips_network_but_validates_before_backup_and_save(manual_namespace, tmp_path):
    namespace = manual_namespace
    events = []
    subtitle = namespace['subtitle_fixture']
    subtitle.use_original_format = True
    subtitle.content = b'prepared season member'
    namespace['validate_download'].side_effect = lambda *args, **kwargs: events.append('validate') or True
    saved = tmp_path / 'Veep.S01E02.zh.srt'

    def save(*args, **kwargs):
        events.append('save')
        saved.write_bytes(subtitle.content)
        subtitle.storage_path = str(saved)
        return [subtitle]

    namespace['save_subtitles'].side_effect = save
    namespace['process_subtitle'].side_effect = lambda **kwargs: events.append('process') or object()
    assert prepared_download(namespace, lambda video, sub: events.append('backup'),
                             lambda sub: events.append('cleanup'))
    assert events == ['validate', 'backup', 'save', 'cleanup', 'process']
    assert subtitle.use_original_format is False
    namespace['get_video'].assert_not_called()
    namespace['download_subtitles'].assert_not_called()


@pytest.mark.parametrize('failure', ['rejected', 'invalid', 'timing'])
def test_prepared_failure_never_backs_up_cleans_up_or_saves(manual_namespace, failure):
    namespace = manual_namespace
    if failure == 'rejected':
        namespace['get_rejection'].return_value = {'reason': 'timing_mismatch'}
    elif failure == 'invalid':
        namespace['subtitle_fixture'].is_valid.return_value = False
    else:
        namespace['validate_download'].return_value = False
    before, after = Mock(), Mock()
    assert isinstance(prepared_download(namespace, before, after), str)
    before.assert_not_called()
    after.assert_not_called()
    namespace['save_subtitles'].assert_not_called()
    namespace['process_subtitle'].assert_not_called()


def test_backup_failure_prevents_save(manual_namespace):
    before, after = Mock(side_effect=OSError('backup disk full')), Mock()
    assert 'Error saving subtitles' in prepared_download(manual_namespace, before, after)
    manual_namespace['save_subtitles'].assert_not_called()
    after.assert_not_called()


@pytest.mark.parametrize('stage', ['fsync', 'replace', 'chmod'])
def test_atomic_write_failure_keeps_original_and_removes_temporary(tmp_path, monkeypatch, stage):
    target = tmp_path / 'episode.zh.srt'
    target.write_bytes(b'old subtitle')
    monkeypatch.setattr(core.os, stage, Mock(side_effect=OSError('simulated failure')))
    with pytest.raises(OSError):
        _atomic_write_subtitle(str(target), b'new subtitle', chmod=0o644)
    assert target.read_bytes() == b'old subtitle'
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_replaces_complete_file_and_preserves_mode(tmp_path):
    target = tmp_path / 'episode.zh.srt'
    target.write_bytes(b'old subtitle')
    if os.name != 'nt':
        target.chmod(0o640)
    _atomic_write_subtitle(str(target), b'new subtitle')
    assert target.read_bytes() == b'new subtitle'
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [target]


def test_empty_modified_content_does_not_claim_old_file_was_saved(tmp_path):
    video = tmp_path / 'episode.mkv'
    subtitle = SimpleNamespace(content=b'input', text='input', format='ass', mods=[],
                               language=Language('zho'), get_modified_content=Mock(return_value=None))
    target = tmp_path / 'episode.zh.ass'
    target.write_bytes(b'old subtitle')
    assert save_subtitles(str(video), [subtitle], formats=('ass',)) == []
    assert target.read_bytes() == b'old subtitle'


class SeasonError(ValueError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


@pytest.fixture
def season_api():
    path = ROOT / 'bazarr/api/providers/providers_episodes.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    resource = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ProviderEpisodesSeason')
    app = Flask(__name__)
    app.config['TESTING'] = True
    blueprint = Blueprint('api', __name__, url_prefix='/api')
    api = Api(blueprint)
    namespace = Namespace('providers', path='/')
    preview = Mock(return_value={'series_id': 34, 'season': 1, 'title': 'Veep', 'total': 8, 'episodes': []})
    enqueue = Mock(return_value=37)
    symbols = {'api_ns_providers_episodes': namespace, 'Resource': Resource, 'reqparse': reqparse,
               'authenticate': lambda f: f, 'preview_season': preview, 'enqueue_season': enqueue,
               'SeasonRequestError': SeasonError}
    exec(compile(ast.Module(body=[resource], type_ignores=[]), str(path), 'exec'), symbols)
    api.add_namespace(namespace, '/')
    app.register_blueprint(blueprint)
    return app.test_client(), preview, enqueue


def test_season_preview_is_get_only_and_has_direct_json_contract(season_api):
    client, preview, enqueue = season_api
    response = client.get('/api/providers/episodes/season?episodeid=5525&subtitle=candidate&season=7&seriesid=99')
    assert response.status_code == 200
    assert response.json['season'] == 1
    assert response.json['total'] == 8
    preview.assert_called_once_with(5525, 'candidate')
    enqueue.assert_not_called()


def test_season_post_queues_only_server_resolved_scope(season_api):
    client, preview, enqueue = season_api
    response = client.post('/api/providers/episodes/season', data={
        'episodeid': 5525, 'subtitle': 'candidate', 'hi': 'false', 'forced': 'False', 'original_format': 'True',
        'seriesid': 99, 'season': 7})
    assert response.status_code == 202
    assert response.json == {'job_id': 37}
    enqueue.assert_called_once_with(5525, 'candidate', 'False', 'False', 'True')
    preview.assert_not_called()


@pytest.mark.parametrize('data', [
    {}, {'episodeid': 'abc'},
    {'episodeid': 5525, 'subtitle': 'candidate', 'hi': 'true', 'forced': 'False', 'original_format': 'oops'},
])
def test_season_post_invalid_input_cannot_queue(season_api, data):
    client, _, enqueue = season_api
    assert client.post('/api/providers/episodes/season', data=data).status_code == 400
    enqueue.assert_not_called()


@pytest.mark.parametrize('status', [400, 404, 409])
def test_season_error_status_and_message_are_preserved(season_api, status):
    client, _, enqueue = season_api
    enqueue.side_effect = SeasonError('Candidate unavailable', status)
    response = client.post('/api/providers/episodes/season', data={
        'episodeid': 5525, 'subtitle': 'candidate', 'hi': 'False', 'forced': 'False', 'original_format': 'False'})
    assert response.status_code == status
    assert response.json == {'message': 'Candidate unavailable'}
