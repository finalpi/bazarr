import ast
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'libs'), str(ROOT / 'custom_libs')]

from subzero.language import Language


def load_guess_external_subtitles():
    source = ROOT / 'bazarr/subtitles/indexer/utils.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == 'guess_external_subtitles')
    namespace = {
        'os': os,
        'logging': logging,
        'core': SimpleNamespace(SUBTITLE_EXTENSIONS={'.srt'}),
        'Language': Language,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['guess_external_subtitles']


def test_untagged_external_subtitle_is_unknown_even_if_previously_guessed(tmp_path):
    path = tmp_path / 'episode.srt'
    path.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello\n', encoding='utf-8')
    subtitles = {'episode.srt': None}
    previous = [{
        'path': str(path),
        'file_size': path.stat().st_size,
        'code2': 'en',
        'forced': False,
        'hi': False,
    }]

    result = load_guess_external_subtitles()(str(tmp_path), subtitles, previous)

    assert result['episode.srt'].basename == 'und'
