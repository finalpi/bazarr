import ast
import json
import logging
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'libs'))

import pysubs2


def load_translation_namespace(settings):
    source = ROOT / 'bazarr/subtitles/tools/translate/services/openai_translator.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    wanted = {'_plain', 'wrap_translation', '_extract_json', 'OpenAICompatibleTranslatorService'}
    nodes = [node for node in tree.body if getattr(node, 'name', None) in wanted]
    namespace = {
        'json': json, 'logging': logging, 'os': os, 're': re, 'time': __import__('time'),
        'pysubs2': pysubs2, 'requests': SimpleNamespace(RequestException=Exception),
        'settings': settings, 'jobs_queue': SimpleNamespace(update_job_progress=lambda **kwargs: None),
        'get_description': lambda *args: 'Criminal Minds season 1',
        'add_translator_info': lambda *args: None,
        'create_process_result': lambda *args: object(),
        'language_from_alpha2': str, 'language_from_alpha3': str,
        'history_log': lambda **kwargs: None, 'history_log_movie': lambda **kwargs: None,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


def translator_settings(**overrides):
    values = dict(openai_model='qwen-test', openai_batch_size=2, openai_context_lines=1,
                  openai_bilingual=True, openai_api_key='',
                  openai_base_url='http://host.docker.internal:11434/v1', openai_timeout=300,
                  translator_info=False)
    values.update(overrides)
    return SimpleNamespace(translator=SimpleNamespace(**values))


def make_service(namespace, source, destination):
    return namespace['OpenAICompatibleTranslatorService'](
        source_srt_file=str(source), dest_srt_file=str(destination), to_lang='zho',
        media_type='episode', sonarr_series_id=1, sonarr_episode_id=22, radarr_id=None,
        forced=False, hi=False, video_path='video.mp4', from_lang='en', orig_to_lang='zh')


def test_wraps_chinese_at_punctuation_and_parses_fenced_json():
    namespace = load_translation_namespace(translator_settings())
    wrapped = namespace['wrap_translation']('这是一句比较长的中文字幕，需要在合适的位置自动断句。', width=12)
    assert r'\N' in wrapped
    assert wrapped.replace(r'\N', '') == '这是一句比较长的中文字幕，需要在合适的位置自动断句。'
    assert namespace['_extract_json']('```json\n{"translations": []}\n```') == {'translations': []}


def test_context_batches_and_bilingual_output_preserve_cue_timing(tmp_path):
    settings = translator_settings()
    namespace = load_translation_namespace(settings)
    source, destination = tmp_path / 'source.srt', tmp_path / 'translated.srt'
    subtitles = pysubs2.SSAFile()
    for index, text in enumerate(['Previously', 'What did he say?', 'I do not know.', 'Let us leave.', 'All right.']):
        subtitles.append(pysubs2.SSAEvent(start=index * 2000, end=index * 2000 + 1500, text=text))
    subtitles.save(source, format_='srt', encoding='utf-8')
    service = make_service(namespace, source, destination)
    batches = []
    def translate_batch(targets, context, description):
        batches.append((targets, context, description))
        return {item['index']: '这是结合前后文翻译后的自然中文句子。' for item in targets}
    service._translate_batch = translate_batch
    assert service.translate(job_id=1) == str(destination)
    result = pysubs2.load(destination, encoding='utf-8')
    assert [(cue.start, cue.end) for cue in result] == [(cue.start, cue.end) for cue in subtitles]
    assert result[0].text.startswith(r'Previously\N这是')
    assert r'\N' in result[0].text
    assert [len(batch[0]) for batch in batches] == [2, 2, 1]
    assert [item['index'] for item in batches[1][1]] == [1, 2, 3, 4]
    assert [item['translate'] for item in batches[1][1]] == [False, True, True, False]


def test_request_bounds_model_output_and_validates_indices():
    settings = translator_settings()
    namespace = load_translation_namespace(settings)
    captured = {}
    class Response:
        @staticmethod
        def raise_for_status():
            return None
        @staticmethod
        def json():
            return {'choices': [{'message': {'content':
                    '{"translations":[{"index":0,"translation":"你好"}]}'}}]}
    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()
    namespace['requests'] = SimpleNamespace(post=post, RequestException=Exception)
    service = make_service(namespace, 'source.srt', 'translated.srt')
    target = [{'index': 0, 'content': 'Hello'}]
    assert service._request(target, [dict(target[0], translate=True)], '') == {0: '你好'}
    assert captured['json']['max_tokens'] == 512
    assert captured['timeout'] == 300


def processing_function(name, namespace):
    source = ROOT / 'bazarr/subtitles/processing.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace[name]


def test_auto_translation_queues_only_when_chinese_is_missing(tmp_path, monkeypatch):
    queued = []
    settings = SimpleNamespace(translator=SimpleNamespace(auto_translate_missing_chinese=True))
    metadata = SimpleNamespace(sonarrEpisodeId=22, sonarrSeriesId=1)
    chinese = tmp_path / 'episode.zh.srt'
    modules = {
        'app.database': SimpleNamespace(get_subtitles=lambda **kwargs: []),
        'subzero.language': SimpleNamespace(Language=lambda code: code),
        'subliminal_patch.core': SimpleNamespace(get_subtitle_path=lambda *args, **kwargs: str(chinese)),
        'utilities.helper': SimpleNamespace(get_target_folder=lambda path: None),
        'subtitles.tools.translate.main': SimpleNamespace(
            translate_subtitles_file=lambda **kwargs: queued.append(kwargs)),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    function = processing_function('_queue_missing_chinese_translation',
                                   {'settings': settings, 'logging': logging, 'os': os})
    assert function('video.mp4', 'episode.en.srt', 'en', False, False, 'series', metadata)
    assert queued[0]['to_lang'] == 'zh'
    queued.clear()
    modules['app.database'].get_subtitles = lambda **kwargs: [
        {'code2': 'zh', 'embedded_track_id': 0, 'path': None}]
    assert not function('video.mp4', 'episode.en.srt', 'en', False, False, 'series', metadata)
    assert not queued
    assert not function('video.mp4', 'episode.en.srt', 'en', True, False, 'series', metadata)
