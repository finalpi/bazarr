"""Use the existing media metadata for duration; never assume an episode runtime."""

import ast
from datetime import timedelta
import logging
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from subliminal import Movie
from guessit.jsonutils import GuessitEncoder


SOURCE = Path(__file__).resolve().parents[2] / 'bazarr/subtitles/refiners/ffprobe.py'


def load_refiner(data):
    class Query:
        def where(self, _):
            return self

    class Column:
        def __eq__(self, _):
            return True

    table = SimpleNamespace(movie_file_id=None, file_size=None, path=Column())
    namespace = {
        'math': math, 'logging': logging, 'Movie': Movie, 'json': __import__('json'),
        'GuessitEncoder': GuessitEncoder,
        'database': SimpleNamespace(execute=lambda _: SimpleNamespace(
            first=lambda: SimpleNamespace(file_size=100, movie_file_id=49))),
        'select': lambda *args: Query(), 'TableMovies': table,
        'path_mappings': SimpleNamespace(path_replace_reverse_movie=str),
        'parse_video_metadata': lambda **kwargs: data,
    }
    nodes = [node for node in ast.parse(SOURCE.read_text(encoding='utf-8')).body
             if isinstance(node, ast.FunctionDef)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('value,expected', [(timedelta(hours=2, minutes=8, seconds=53.12), 7733.12),
                                         ('1442.5', 1442.5), (1200, 1200),
                                         (None, None), ('bad', None), (-1, None),
                                         (float('nan'), None), (float('inf'), None)])
def test_duration_is_positive_finite_seconds(value, expected):
    convert = load_refiner({})['duration_seconds']
    if expected is None:
        assert convert(value) is None
    else:
        assert convert(value) == pytest.approx(expected)


@pytest.mark.parametrize('parser', ['ffprobe', 'mediainfo'])
@pytest.mark.parametrize('location', ['container', 'video_track'])
def test_refiner_copies_actual_duration_from_cached_parser(parser, location):
    duration = timedelta(seconds=7733.12)
    parsed = {'video': [{'duration': duration}] if location == 'video_track' else [{}], 'audio': [{}]}
    if location == 'container':
        parsed['duration'] = duration
    data = {'ffprobe': None, 'mediainfo': None, parser: parsed}
    namespace = load_refiner(data)
    video = Movie('/movies/game.mkv', 'The Game')
    assert namespace['refine_from_ffprobe'](video.name, video) is video
    assert video.duration == pytest.approx(7733.12)


def test_missing_duration_stays_unknown_instead_of_inventing_a_runtime():
    namespace = load_refiner({'ffprobe': {'video': [{}], 'audio': [{}]}, 'mediainfo': None})
    video = Movie('/movies/game.mkv', 'The Game')
    namespace['refine_from_ffprobe'](video.name, video)
    assert not hasattr(video, 'duration')
