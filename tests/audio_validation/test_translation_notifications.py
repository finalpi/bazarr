import ast
import logging
import os
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]


def load_notification_helper(namespace):
    source = ROOT / 'bazarr/subtitles/tools/translate/main.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == '_send_llm_translation_notification')
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['_send_llm_translation_notification']


def test_llm_translation_notifications_route_and_redact_details():
    episode_notifications = []
    movie_notifications = []
    settings = SimpleNamespace(translator=SimpleNamespace(
        openai_model='gpt-test', gemini_model='gemini-test', openai_api_key='secret-key'))
    notify = load_notification_helper({
        'logging': logging,
        'os': os,
        'settings': settings,
        'send_notifications': lambda *args: episode_notifications.append(args),
        'send_notifications_movie': lambda *args: movie_notifications.append(args),
    })

    notify(True, 'openai_compatible', 'zh', 'episode', 12, 2389, None,
           output_path='/tv/episode.llm.zh.ass')
    notify(False, 'gemini', 'zh', 'movie', None, None, 55,
           error=RuntimeError('request failed with secret-key'))
    notify(True, 'google_translate', 'zh', 'episode', 12, 2389, None,
           output_path='/tv/episode.zh.srt')

    assert episode_notifications == [(
        12, 2389, 'LLM translation completed to ZH using gpt-test: episode.llm.zh.ass')]
    assert len(movie_notifications) == 1
    assert movie_notifications[0][0] == 55
    assert 'LLM translation failed to ZH using gemini-test' in movie_notifications[0][1]
    assert '[redacted]' in movie_notifications[0][1]
    assert 'secret-key' not in movie_notifications[0][1]
