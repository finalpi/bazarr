import ast
import os
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]


def load_postprocess(namespace):
    source = ROOT / 'bazarr/api/subtitles/subtitles.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == 'postprocess_subtitles')
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['postprocess_subtitles']


def test_episode_postprocess_refreshes_jellyfin_without_chmod():
    stored = []
    refreshed = []
    events = []
    settings = SimpleNamespace(
        general=SimpleNamespace(chmod='0644', chmod_enabled=False, use_plex=False, use_jellyfin=True),
        plex=SimpleNamespace(update_series_library=False, update_movie_library=False),
        jellyfin=SimpleNamespace(update_series_library=True, update_movie_library=True),
    )
    function = load_postprocess({
        'os': os,
        'sys': SimpleNamespace(platform='linux'),
        'settings': settings,
        'store_subtitles': lambda episode_id: stored.append(episode_id),
        'store_subtitles_movie': lambda movie_id: None,
        'event_stream': lambda **kwargs: events.append(kwargs),
        'plex_refresh_item': lambda *args, **kwargs: None,
        'jellyfin_refresh_item': lambda *args, **kwargs: refreshed.append((args, kwargs)),
    })
    metadata = SimpleNamespace(
        sonarrSeriesId=12, imdbId='tt0096697', season=37, episode=4, tvdbId=71663)

    assert function('/tv/episode.zh.srt', 'episode', metadata, 2389) == ('', 204)
    assert stored == [2389]
    assert events == [
        {'type': 'series', 'payload': 12},
        {'type': 'episode', 'payload': 2389},
    ]
    assert refreshed == [(('tt0096697',), {
        'is_movie': False,
        'season': 37,
        'episode': 4,
        'tvdb_id': 71663,
    })]
