"""Exercise the production DB refiner without starting Bazarr's application."""

import ast
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from subliminal import Episode, Movie


ROOT = Path(__file__).resolve().parents[2]


class Column:
    def __init__(self, name):
        self.name = name

    def label(self, name):
        return Column(name)

    def __eq__(self, value):
        return self.name, value


class Table:
    def __init__(self, name):
        self.name = name

    def __getattr__(self, name):
        return Column(self.name + '.' + name)


class Query:
    def __init__(self, columns):
        self.columns = columns

    def select_from(self, table):
        return self

    def join(self, table):
        return self

    def where(self, condition):
        self.condition = condition
        return self


def production_refiner(row):
    queries = []

    def select(*columns):
        query = Query(columns)
        queries.append(query)
        return query

    namespace = {
        'ast': ast,
        'Episode': Episode,
        'Movie': Movie,
        '_TITLE_RE': re.compile(r'\s(\(\d{4}\))'),
        'TableShows': Table('shows'),
        'TableEpisodes': Table('episodes'),
        'TableMovies': Table('movies'),
        'select': select,
        'database': SimpleNamespace(execute=lambda query: SimpleNamespace(first=lambda: row)),
        'path_mappings': SimpleNamespace(path_replace_reverse=str, path_replace_reverse_movie=str),
        'convert_to_guessit': lambda kind, value: value,
    }
    source = ROOT / 'bazarr/subtitles/refiners/database.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'refine_from_db')
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['refine_from_db'], queries


def metadata(original_language):
    return SimpleNamespace(
        title='The Game', seriesTitle='Canonical Show', episodeTitle='Episode Title',
        year=1997, tvdbId=123, alternativeTitles='[]', format='BluRay', resolution='1080p',
        video_codec='H.264', audio_codec='DTS', imdbId='tt0119174', tmdbId=2649, radarrId=49,
        season=2, episode=5, absoluteEpisode=12, sonarrSeriesId=10, sonarrEpisodeId=50,
        originalLanguage=original_language,
        audio_language="[{'name': 'Russian'}, {'name': 'English'}]",
    )


@pytest.mark.parametrize('original_language', ['English', 'Japanese', None, ''])
def test_movie_refiner_uses_original_language_metadata_not_first_audio_language(original_language):
    refine, queries = production_refiner(metadata(original_language))
    video = Movie('/library/game.mkv', 'Guessed Title')
    assert refine(video.name, video) is video
    assert video.original_language == original_language
    assert 'movies.originalLanguage' in [column.name for column in queries[0].columns]
    assert 'movies.audio_language' not in [column.name for column in queries[0].columns]
    assert video.radarrId == 49
    assert video.title == 'The Game'


@pytest.mark.parametrize('original_language', ['English', 'Japanese', None, ''])
def test_episode_refiner_uses_show_original_language_metadata_not_first_audio_language(original_language):
    refine, queries = production_refiner(metadata(original_language))
    video = Episode('/library/show.S02E05.mkv', 'Guessed Show', 2, 5)
    assert refine(video.name, video) is video
    assert video.original_language == original_language
    assert 'shows.originalLanguage' in [column.name for column in queries[0].columns]
    assert 'shows.audio_language' not in [column.name for column in queries[0].columns]
    assert (video.series, video.season, video.episode, video.absolute_episode) == (
        'Canonical Show', 2, 5, 12)


@pytest.mark.parametrize('video', [Movie('/missing.mkv', 'Movie'),
                                   Episode('/missing.S01E01.mkv', 'Series', 1, 1)])
def test_missing_database_row_does_not_invent_original_language(video):
    refine, _ = production_refiner(None)
    assert refine(video.name, video) is video
    assert not hasattr(video, 'original_language')
