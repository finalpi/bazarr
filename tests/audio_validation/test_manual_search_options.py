import ast
import importlib.util
import logging
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse
from unittest.mock import Mock

import pytest
from flask import Flask
from flask_restx import reqparse
from subliminal import Episode
from subzero.language import Language

from subliminal_patch.chinese import search_title_variants, title_variants
from subliminal_patch.core import SZProviderPool
from subliminal_patch.providers.assrt import AssrtProvider, AssrtSubtitle
from subliminal_patch.providers.embeddedsubtitles import EmbeddedSubtitle, EmbeddedSubtitlesProvider
from subliminal_patch.providers.localsibling import _same_work
from subliminal_patch.providers.r3sub import R3subProvider, R3subSubtitle
from subliminal_patch.providers.subhd import SubhdProvider, SubhdSubtitle
from subliminal_patch.providers.zimuku import ZimukuProvider, ZimukuSubtitle


ROOT = Path(__file__).resolve().parents[2]
AVAILABLE = ['subhd', 'r3sub', 'assrt', 'zimuku']
CHINESE = Language('zho', 'CN')


def load_functions(relative_path, names, namespace):
    source = ROOT / relative_path
    tree = ast.parse(source.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


def options_validator():
    return load_functions('bazarr/subtitles/manual.py', {'validate_manual_search_options'}, {})[
        'validate_manual_search_options']


def episode():
    return Episode('/tmp/Canonical.Show.S03E02.mkv', 'Canonical Show', 3, 2, year=2020,
                   alternative_series=['Known Alias'])


def test_options_keep_defaults_and_normalize_only_this_request():
    validate = options_validator()
    assert validate(None, None, AVAILABLE) == (None, AVAILABLE)
    assert validate('   ', None, AVAILABLE) == (None, AVAILABLE)
    assert validate('  中文别名  ', [' subhd ', 'r3sub', 'subhd'], AVAILABLE) == (
        '中文别名', ['subhd', 'r3sub'])
    assert AVAILABLE == ['subhd', 'r3sub', 'assrt', 'zimuku']
    assert validate('中' * 200, ['subhd'], AVAILABLE)[0] == '中' * 200


@pytest.mark.parametrize('providers', [[], [''], ['  '], ['subhd', ''], ['disabled'], ['SUBHD']])
def test_options_reject_empty_disabled_and_unavailable_selections(providers):
    with pytest.raises(ValueError):
        options_validator()('中文别名', providers, AVAILABLE)


def test_options_reject_overlong_keyword_and_a_throttled_provider():
    with pytest.raises(ValueError, match='200'):
        options_validator()('中' * 201, ['subhd'], AVAILABLE)
    with pytest.raises(ValueError, match='currently available'):
        options_validator()(None, ['subhd'], ['r3sub'])
    assert options_validator()(None, None, None) == (None, None)


class Query:
    def __getattr__(self, _name):
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __eq__(self, other):
        return self


def load_search_api(media_type, search):
    filename = 'providers_episodes.py' if media_type == 'series' else 'providers_movies.py'
    source = ROOT / 'bazarr/api/providers' / filename
    tree = ast.parse(source.read_text(encoding='utf-8'))
    api_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    parser_nodes = []
    for node in api_class.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == 'get_response_model':
            break
        parser_nodes.append(node)
    get_method = next(node for node in api_class.body if isinstance(node, ast.FunctionDef) and node.name == 'get')
    get_method.decorator_list = []
    api_class.bases = []
    api_class.decorator_list = []
    api_class.body = parser_nodes + [get_method]
    metadata = SimpleNamespace(title='Canonical Show', path='/tmp/media.mkv', sceneName=None,
                               profileId=1, missing_subtitles='[]')
    namespace = {
        'reqparse': reqparse,
        'validate_manual_search_options': options_validator(),
        'get_providers': lambda: AVAILABLE,
        'select': Query(), 'TableEpisodes': Query(), 'TableShows': Query(), 'TableMovies': Query(),
        'database': SimpleNamespace(execute=lambda query: SimpleNamespace(first=lambda: metadata)),
        'get_subtitles': lambda **kwargs: [{'path': '/tmp/existing.srt', 'embedded_track_id': None}],
        'path_mappings': SimpleNamespace(path_replace=str, path_replace_movie=str),
        'os': SimpleNamespace(path=SimpleNamespace(exists=lambda path: True)),
        'manual_search': search,
        'marshal': lambda data, model, envelope: {envelope: data},
    }
    exec(compile(ast.Module(body=[api_class], type_ignores=[]), str(source), 'exec'), namespace)
    resource = namespace[api_class.name]()
    resource.get_response_model = None
    return resource


@pytest.mark.parametrize('media_type,id_name', [('series', 'episodeid'), ('movie', 'radarrid')])
@pytest.mark.parametrize('selection,expected', [
    ('providers=subhd', ['subhd']), ('providers=subhd&providers=r3sub', ['subhd', 'r3sub'])])
def test_search_api_accepts_provider_query_parameters_and_forwards_keyword(media_type, id_name, selection, expected):
    search = Mock(return_value=[{'provider': 'subhd', 'subtitle': 'cached-id'}])
    resource = load_search_api(media_type, search)
    with Flask(__name__).test_request_context(
            '/?' + id_name + '=1&' + selection + '&keyword=%20%E4%B8%AD%E6%96%87%20'):
        assert resource.get() == {'data': [{'provider': 'subhd', 'subtitle': 'cached-id'}]}
    assert search.call_args.args == ('/tmp/media.mkv', 1, expected, 'None', 'Canonical Show', media_type)
    assert search.call_args.kwargs == {'keyword': '中文'}


@pytest.mark.parametrize('media_type,id_name', [('series', 'episodeid'), ('movie', 'radarrid')])
def test_search_api_keeps_automatic_source_and_keyword_behavior_when_options_are_absent(media_type, id_name):
    search = Mock(return_value=[])
    resource = load_search_api(media_type, search)
    with Flask(__name__).test_request_context('/?' + id_name + '=1'):
        assert resource.get() == {'data': []}
    assert search.call_args.args[2] == AVAILABLE
    assert search.call_args.kwargs == {'keyword': None}


@pytest.mark.parametrize('media_type,id_name', [('series', 'episodeid'), ('movie', 'radarrid')])
@pytest.mark.parametrize('options', ['providers=', 'providers=disabled', 'keyword=' + 'x' * 201])
def test_search_api_rejects_invalid_options_before_search(media_type, id_name, options):
    search = Mock()
    resource = load_search_api(media_type, search)
    with Flask(__name__).test_request_context('/?' + id_name + '=1&' + options):
        assert resource.get()[1] == 400
    search.assert_not_called()


def test_request_pool_keeps_auth_limits_and_blacklists_without_falling_back_to_all_sources():
    factory = Mock(return_value=object())
    namespace = load_functions('bazarr/subtitles/pool.py', {'_init_pool'}, {
        'provider_pool': lambda: factory,
        'get_providers': lambda: AVAILABLE,
        'get_providers_auth': lambda: {'r3sub': {'email': 'configured'}},
        'get_blacklist': lambda: [('series', 'blocked')],
        'get_blacklist_movie': lambda: [('movie', 'blocked')],
        'provider_throttle': 'existing-throttle',
        'get_ban_list': lambda profile_id: {'must_not_contain': ['sample']},
        'get_language_equals': lambda: ['existing-language-rule'],
    })
    namespace['_init_pool']('series', 1, providers=['r3sub'])
    assert factory.call_args.kwargs == {
        'providers': ['r3sub'], 'provider_configs': {'r3sub': {'email': 'configured'}},
        'blacklist': [('series', 'blocked')], 'throttle_callback': 'existing-throttle',
        'ban_list': {'must_not_contain': ['sample']}, 'language_hook': None,
        'language_equals': ['existing-language-rule'],
    }
    namespace['_init_pool']('movie', 1, providers=[])
    assert factory.call_args.kwargs['providers'] == []
    namespace['_init_pool']('movie', 1)
    assert factory.call_args.kwargs['providers'] == AVAILABLE


def manual_namespace(pool, video, subtitles, cached):
    def list_subtitles(videos, languages, request_pool):
        assert request_pool is pool
        assert videos == [video]
        assert languages == {CHINESE}
        return {video: subtitles}

    return load_functions('bazarr/subtitles/manual.py', {'manual_search', '_manual_search_with_pool'}, {
        'logging': logging,
        '_manual_search_pool': Mock(return_value=pool),
        '_get_pool': Mock(side_effect=AssertionError('must not use the shared pool')),
        '_get_language_obj': lambda **kwargs: ({CHINESE}, True),
        '_set_forced_providers': lambda **kwargs: None,
        'get_video': lambda *args, **kwargs: video,
        'force_unicode': str,
        'list_all_subtitles': list_subtitles,
        'settings': SimpleNamespace(general=SimpleNamespace(minimum_score=0, minimum_score_movie=0)),
        'DEFAULT_SCORES': {'episode': {'series': 1, 'season': 1, 'episode': 1, 'year': 1}, 'movie': {}},
        '_get_scores': lambda *args: (0, 100, {'series', 'season', 'episode', 'year'}),
        'compute_score': lambda *args, **kwargs: (80, 80),
        'subtitle_cache': SimpleNamespace(store=lambda sub: cached.append(sub) or 'cached-id'),
        'subliminal': SimpleNamespace(region=SimpleNamespace(backend=SimpleNamespace(sync=lambda: None))),
    })


def test_manual_search_isolates_sources_preserves_identity_and_rejects_other_episodes():
    video = episode()
    wanted = SubhdSubtitle(CHINESE, 'wanted', '/wanted', '中文别名 S03E02 ASS', video)
    wrong = SubhdSubtitle(CHINESE, 'wrong', '/wrong', '中文别名 S03E01 ASS', video)
    pool = Mock()
    pool.__enter__ = Mock(return_value=pool)
    pool.__exit__ = Mock(return_value=False)
    cached = []
    namespace = manual_namespace(pool, video, [wrong, wanted], cached)
    result = namespace['manual_search']('/tmp/media.mkv', 1, ['subhd'], 'None', 'Canonical Show',
                                        'series', keyword='中文别名')
    namespace['_manual_search_pool'].assert_called_once_with('series', 1, providers=['subhd'])
    namespace['_get_pool'].assert_not_called()
    pool.__exit__.assert_called_once()
    assert video.search_keyword == '中文别名'
    assert (video.series, video.season, video.episode) == ('Canonical Show', 3, 2)
    assert cached == [wanted]
    assert result[0]['provider'] == 'subhd'
    assert result[0]['subtitle'] == 'cached-id'


def test_manual_search_closes_its_pool_after_parse_failure():
    pool = Mock()
    pool.__enter__ = Mock(return_value=pool)
    pool.__exit__ = Mock(return_value=False)
    namespace = manual_namespace(pool, episode(), [], [])
    namespace['_get_language_obj'] = Mock(side_effect=RuntimeError('invalid profile'))
    with pytest.raises(RuntimeError, match='invalid profile'):
        namespace['manual_search']('/tmp/media.mkv', 1, ['subhd'], 'None', 'Canonical Show', 'series')
    pool.__exit__.assert_called_once()


def request_pool_factory(config):
    namespace = load_functions('bazarr/subtitles/pool.py', {'_init_pool', '_manual_search_pool'}, {
        'TemporaryDirectory': TemporaryDirectory,
        'provider_pool': lambda: SZProviderPool,
        'get_providers': lambda: ['embeddedsubtitles'],
        'get_providers_auth': lambda: config,
        'get_blacklist': lambda: [],
        'get_blacklist_movie': lambda: [],
        'provider_throttle': lambda *args, **kwargs: None,
        'get_ban_list': lambda profile_id: {},
        'get_language_equals': lambda: [],
    })
    return contextmanager(namespace['_manual_search_pool'])


def subtitle_cache():
    spec = importlib.util.spec_from_file_location('manual_search_subtitle_cache', ROOT / 'bazarr/subtitles/cache.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._SubtitleCache()


def test_concurrent_request_pool_cleanup_preserves_persistent_embedded_paths(tmp_path):
    persistent = EmbeddedSubtitlesProvider(cache_dir=str(tmp_path / 'persistent'), timeout=75)
    persistent.initialize()
    cached_file = Path(persistent._cache_dir) / 'protected.srt'
    cached_file.write_bytes(b'persistent subtitle')
    cached_paths = {'protected.mkv': {2: str(cached_file)}}
    persistent._cached_paths.update(cached_paths)
    config = {'embeddedsubtitles': {'cache_dir': str(tmp_path / 'persistent'), 'timeout': 75}}
    request_pool = request_pool_factory(config)

    with request_pool('series', 1, ['embeddedsubtitles']) as first:
        first_provider = first['embeddedsubtitles']
        first_file = Path(first_provider._cache_dir) / 'first.srt'
        first_file.write_bytes(b'first request')
        with request_pool('series', 1, ['embeddedsubtitles']) as second:
            second_provider = second['embeddedsubtitles']
            second_file = Path(second_provider._cache_dir) / 'second.srt'
            second_file.write_bytes(b'second request')
            assert first_provider._cache_dir != second_provider._cache_dir != persistent._cache_dir
            assert first_provider._timeout == second_provider._timeout == 75
        assert not second_file.exists()
        assert first_file.read_bytes() == b'first request'
        assert cached_file.read_bytes() == b'persistent subtitle'
        assert persistent._cached_paths == cached_paths
    assert not first_file.exists()
    assert cached_file.read_bytes() == b'persistent subtitle'
    assert persistent._cached_paths == cached_paths
    assert config == {'embeddedsubtitles': {'cache_dir': str(tmp_path / 'persistent'), 'timeout': 75}}


def test_cached_embedded_candidate_downloads_after_its_request_cache_is_cleaned(tmp_path, monkeypatch):
    disposition = SimpleNamespace(hearing_impaired=False, forced=False, generic=True,
                                  language_kwargs=lambda: {'hi': False, 'forced': False})
    stream = SimpleNamespace(index=2, language=CHINESE, disposition=disposition, codec_name='subrip',
                             suffix='zh.srt', tags=SimpleNamespace(language_fallback=False, frames=100, _data={}))
    extraction_dirs = []

    def extract(streams, directory, **kwargs):
        extraction_dirs.append(directory)
        extracted_path = Path(directory) / 'extracted.srt'
        extracted_path.write_bytes(b'1\n00:00:01,000 --> 00:00:02,000\nembedded dialogue\n')
        return {2: str(extracted_path)}

    container = SimpleNamespace(path='/tmp/media.mkv', get_subtitles=lambda: [stream], copy_subtitles=extract)
    monkeypatch.setattr('subliminal_patch.providers.embeddedsubtitles._get_memoized_video_container',
                        Mock(return_value=container))
    monkeypatch.setattr(EmbeddedSubtitlesProvider, '_is_path_valid', lambda provider, path: True)
    persistent_root = str(tmp_path / 'persistent')
    request_pool = request_pool_factory({'embeddedsubtitles': {'cache_dir': persistent_root}})
    cache = subtitle_cache()
    with request_pool('series', 1, ['embeddedsubtitles']) as pool:
        provider = pool['embeddedsubtitles']
        selected = provider.list_subtitles(episode(), {CHINESE})[0]
        assert isinstance(selected, EmbeddedSubtitle)
        candidate_id = cache.store(selected)
        temporary_extracted = Path(provider._get_subtitle_path(selected))
        assert temporary_extracted.exists()
    assert not temporary_extracted.exists()

    # Download uses the persistent provider and the cached container/stream
    # identity, not the deleted request extraction path.
    persistent = EmbeddedSubtitlesProvider(cache_dir=persistent_root)
    persistent.initialize()
    candidate = cache.get(candidate_id)
    persistent.download_subtitle(candidate)
    assert b'embedded dialogue' in candidate.content
    assert extraction_dirs == [str(temporary_extracted.parent), persistent._cache_dir]
    assert Path(persistent._cached_paths[container.path][2]).exists()


def test_keyword_is_an_alias_only_for_providers_that_explicitly_opt_in():
    video = episode()
    video.search_keyword = '  中文别名  '
    assert search_title_variants(video) == ['中文别名']
    assert title_variants(video) == ['Canonical Show', 'Known Alias']
    assert title_variants(video, include_search_keyword=True) == ['中文别名', 'Canonical Show', 'Known Alias']
    assert not _same_work(video, {'title': '中文别名'})


def test_subhd_and_r3sub_search_the_exact_keyword_without_replacing_metadata():
    video = episode()
    video.search_keyword = '中文别名 S03E02'
    subhd = SubhdProvider()
    subhd._request = Mock(return_value='<div><a href="/a/Ab12">中文别名 S03E02</a></div>'.encode('utf-8'))
    assert len(subhd.list_subtitles(video, {CHINESE})) == 1
    assert unquote(urlparse(subhd._request.call_args.args[0]).path) == '/search/中文别名 S03E02'
    r3sub = R3subProvider(email='configured', password='configured')
    r3sub._curl = Mock(return_value=b'<div><a href="show.php?id=abc">Chinese result</a></div>')
    assert len(r3sub.list_subtitles(video, {CHINESE})) == 1
    assert parse_qs(urlparse(r3sub._curl.call_args.args[0]).query)['s'] == ['中文别名 S03E02']
    assert (video.series, video.season, video.episode) == ('Canonical Show', 3, 2)


def test_assrt_searches_exact_keyword_and_zimuku_retains_real_episode_filters(monkeypatch):
    video = episode()
    video.search_keyword = '中文别名'
    assrt = AssrtProvider(token='configured')
    assrt.max_request_per_minute = 60
    assrt.session.get = Mock(return_value=SimpleNamespace(
        json=lambda: {'sub': {'subs': []}}, raise_for_status=lambda: None))
    monkeypatch.setattr('subliminal_patch.providers.assrt.sleep', lambda delay: None)
    assert assrt.list_subtitles(video, {CHINESE}) == []
    assert assrt.session.get.call_args.kwargs['params']['q'] == '中文别名'
    zimuku = ZimukuProvider()
    zimuku.query = Mock(return_value=[])
    assert zimuku.list_subtitles(video, {CHINESE}) == []
    zimuku.query.assert_called_once_with('中文别名', season=3, episode=2, year=2020, exact_query=True)


def test_zimuku_custom_keyword_does_not_get_an_extra_season_suffix():
    zimuku = ZimukuProvider()
    zimuku.yunsuo_bypass = Mock(return_value=SimpleNamespace(content=b'', raise_for_status=lambda: None))
    assert zimuku.query('中文别名 S03E02', season=3, episode=2, year=2020, exact_query=True) == []
    assert unquote(parse_qs(urlparse(zimuku.yunsuo_bypass.call_args.args[0]).query)['q'][0]) == '中文别名 S03E02'


@pytest.mark.parametrize('provider', ['subhd', 'r3sub', 'assrt', 'zimuku'])
def test_custom_alias_accepts_the_same_episode_but_does_not_claim_a_wrong_one(provider):
    video = episode()
    video.search_keyword = '中文别名'

    def subtitle(release):
        if provider == 'subhd':
            return SubhdSubtitle(CHINESE, 'id', '/id', release, video)
        if provider == 'r3sub':
            return R3subSubtitle(CHINESE, 'id', release, video, [])
        if provider == 'assrt':
            return AssrtSubtitle(CHINESE, 'id', release, None, 'configured', 60)
        return ZimukuSubtitle(CHINESE, '/id', release, None, 2020)

    assert {'series', 'season', 'episode'} <= subtitle('中文别名.S03E02.1080p').get_matches(video)
    assert not {'series', 'season', 'episode'} <= subtitle('中文别名.S03E01.1080p').get_matches(video)
    assert not {'series', 'season', 'episode'} <= subtitle('中文别名.S02E02.1080p').get_matches(video)
