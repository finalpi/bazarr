import ast
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask_restx import fields, marshal
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, insert, select


ROOT = Path(__file__).resolve().parents[2]


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), namespace)
    return namespace


@pytest.fixture
def indexed_database(monkeypatch):
    metadata = MetaData()
    tables = {}
    for name, key, is_history in [('movie_subtitles', 'radarrId', False),
                                  ('episode_subtitles', 'sonarrEpisodeId', False),
                                  ('movie_history', 'radarrId', True),
                                  ('episode_history', 'sonarrEpisodeId', True)]:
        columns = [Column('id', Integer, primary_key=True), Column(key, Integer)]
        if is_history:
            columns += [Column('action', Integer), Column('provider', String),
                        Column('subtitles_path', String), Column('video_path', String)]
        else:
            columns += [Column('path', String), Column('language', String), Column('forced', Boolean),
                        Column('hi', Boolean), Column('size', Integer), Column('embedded_track_id', Integer)]
        tables[name] = Table(name, metadata, *columns)
    engine = create_engine('sqlite:///:memory:')
    metadata.create_all(engine)
    connection = engine.connect()
    mapper = lambda path: path.replace('/media/', '/media/mount/', 1) if isinstance(path, str) else path
    package = types.ModuleType('subtitles')
    package.__path__ = [str(ROOT / 'bazarr/subtitles')]
    monkeypatch.setitem(sys.modules, 'subtitles', package)
    provenance_spec = importlib.util.spec_from_file_location('subtitles.provenance',
                                                          ROOT / 'bazarr/subtitles/provenance.py')
    provenance = importlib.util.module_from_spec(provenance_spec)
    provenance_spec.loader.exec_module(provenance)
    monkeypatch.setitem(sys.modules, 'subtitles.provenance', provenance)
    monkeypatch.setitem(sys.modules, 'languages.get_languages', SimpleNamespace(
        alpha3_from_alpha2=lambda code: {'zh': 'zho', 'zt': 'zht', 'en': 'eng'}[code],
        language_from_alpha2=lambda code: {'zh': 'Chinese Simplified', 'zt': 'Chinese Traditional', 'en': 'English'}[code]))
    namespace = {'List': list, 'database': connection, 'select': select,
                 'path_mappings': SimpleNamespace(path_replace=mapper, path_replace_movie=mapper)}
    for symbol, name in [('TableMoviesSubtitles', 'movie_subtitles'),
                         ('TableEpisodesSubtitles', 'episode_subtitles'),
                         ('TableHistoryMovie', 'movie_history'), ('TableHistory', 'episode_history')]:
        namespace[symbol] = SimpleNamespace(**{column.name: column for column in tables[name].c})
    functions('bazarr/app/database.py', {'get_subtitles', 'get_subtitle_history'}, namespace)
    yield namespace, connection, tables, mapper
    connection.close()
    engine.dispose()


@pytest.mark.parametrize('media_type,identifier', [('movie', 'radarrId'), ('episode', 'sonarrEpisodeId')])
def test_indexed_subtitles_keep_sources_through_mapping_and_real_marshal(indexed_database, media_type, identifier):
    namespace, connection, tables, mapper = indexed_database
    video = '/media/Game.mkv'
    connection.execute(insert(tables[media_type + '_subtitles']), [
        {identifier: 49, 'path': '/media/Game.zh.srt', 'language': 'zh', 'forced': False, 'hi': False,
         'size': 20, 'embedded_track_id': None},
        {identifier: 49, 'path': '/media/Game.zt.hi.srt', 'language': 'zt', 'forced': False, 'hi': False,
         'size': 30, 'embedded_track_id': None},
        {identifier: 49, 'path': None, 'language': 'en', 'forced': False, 'hi': False,
         'size': None, 'embedded_track_id': 0},
    ])
    connection.execute(insert(tables[media_type + '_history']), [
        {identifier: 49, 'action': 1, 'provider': 'opensubtitlescom', 'subtitles_path': '/media/Game.zh.srt',
         'video_path': video},
        {identifier: 49, 'action': 2, 'provider': 'subhd', 'subtitles_path': '/media/Game.zt.hi.srt',
         'video_path': video},
        {identifier: 50, 'action': 3, 'provider': 'wrong-media', 'subtitles_path': '/media/Game.zh.srt',
         'video_path': video},
    ])
    args = {'radarr_id' if media_type == 'movie' else 'sonarr_episode_id': 49, 'video_path': video}
    output = namespace['get_subtitles'](**args)
    by_language = {subtitle['code2']: subtitle for subtitle in output}
    assert by_language['zh']['source'] == 'opensubtitlescom'
    assert by_language['zt']['source'] == 'subhd'
    assert by_language['zt']['hi'] is False
    assert by_language['en']['source_type'] == 'embedded'
    assert by_language['en']['embedded_track_id'] == 0
    assert by_language['zh']['path'] == mapper('/media/Game.zh.srt')
    model_tree = ast.parse((ROOT / 'bazarr/api/swaggerui.py').read_text(encoding='utf-8'))
    model = next(node.value for node in model_tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == 'subtitles_model' for target in node.targets))
    schema = eval(compile(ast.Expression(model), '<subtitle-model>', 'eval'), {'fields': fields})
    wire = marshal(output, schema)
    assert {item['source'] for item in wire} == {'opensubtitlescom', 'subhd', 'embedded'}
    assert all('source_type' in item for item in wire)


def test_explicit_empty_batch_does_not_refetch_history(indexed_database):
    namespace, connection, tables, _ = indexed_database
    connection.execute(insert(tables['movie_subtitles']), {
        'radarrId': 49, 'path': '/media/Game.zh.srt', 'language': 'zh',
        'forced': False, 'hi': False, 'embedded_track_id': None})
    namespace['get_subtitle_history'] = Mock(side_effect=AssertionError('Batch history must not be queried again'))
    output = namespace['get_subtitles'](radarr_id=49, subtitle_history=[])
    assert output[0]['source_type'] == 'unknown'
    namespace['get_subtitle_history'].assert_not_called()


def test_history_batches_deduplicate_ids_and_preserve_media_table_boundaries(indexed_database):
    namespace, connection, tables, _ = indexed_database
    connection.execute(insert(tables['movie_history']), {'radarrId': 49, 'action': 1, 'provider': 'subhd',
                                                       'subtitles_path': '/media/Game.zh.srt'})
    connection.execute(insert(tables['episode_history']), {'sonarrEpisodeId': 49, 'action': 1,
                                                         'provider': 'r3sub', 'subtitles_path': '/media/Game.zh.srt'})
    movie = namespace['get_subtitle_history'](radarr_ids=[49, 49, *range(500, 1100)])
    episode = namespace['get_subtitle_history'](sonarr_episode_ids=[49])
    assert movie[49][0]['provider'] == 'subhd'
    assert len(movie[49]) == 1
    assert episode[49][0]['provider'] == 'r3sub'
    assert namespace['get_subtitle_history'](radarr_ids=[]) == {}
    with pytest.raises(ValueError):
        namespace['get_subtitle_history'](radarr_ids=[49], sonarr_episode_ids=[49])


def test_postprocess_passes_prefetched_history_and_original_video_path():
    subtitle_lookup = Mock(return_value=[])
    mapper = lambda path: '/mounted' + path if path else path
    namespace = functions('bazarr/api/utils.py', {'postprocess'}, {
        'None_Keys': [None, '', 'null', 'undefined'], 'ast': ast,
        'path_mappings': SimpleNamespace(path_replace=mapper, path_replace_movie=mapper),
        'get_subtitles': subtitle_lookup,
        'settings': SimpleNamespace(general=SimpleNamespace(embedded_subs_show_desired=False)),
    })
    result = namespace['postprocess']({'radarrId': 49, 'path': '/media/Game.mkv'}, subtitle_history=[])
    assert result['path'] == '/mounted/media/Game.mkv'
    subtitle_lookup.assert_called_once_with(sonarr_episode_id=None, radarr_id=49,
                                          subtitle_history=[], video_path='/media/Game.mkv')
