import ast
import importlib.util
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
import pytest
from charset_normalizer import detect


def load_translation_namespace(settings):
    source = ROOT / 'bazarr/subtitles/tools/translate/services/openai_translator.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    wanted = {'_plain', 'normalize_chinese_translation', '_speaker_marker_count', '_is_dual_speaker',
              'load_subtitles_with_encoding', 'wrap_translation', '_ass_color', '_ass_style',
              'apply_ass_style', '_extract_numbered', '_numbered',
              'OpenAICompatibleTranslatorService'}
    nodes = [node for node in tree.body if getattr(node, 'name', None) in wanted]
    namespace = {
        'json': json, 'logging': logging, 'logger': logging.getLogger(__name__), 'os': os, 're': re,
        'time': __import__('time'), 'detect': detect,
        'pysubs2': pysubs2, 'requests': SimpleNamespace(RequestException=Exception),
        'settings': settings, 'jobs_queue': SimpleNamespace(update_job_progress=lambda **kwargs: None),
        'get_description': lambda *args: 'Criminal Minds season 1',
        'add_translator_info': lambda *args: None,
        'create_process_result': lambda *args: object(),
        'language_from_alpha2': str, 'language_from_alpha3': str,
        'history_log': lambda **kwargs: None, 'history_log_movie': lambda **kwargs: None,
        'get_active_openai_profile': lambda: {
            'id': 'default', 'name': 'Default',
            'base_url': settings.translator.openai_base_url,
            'api_key': settings.translator.openai_api_key,
            'model': settings.translator.openai_model,
        },
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


def translator_settings(**overrides):
    values = dict(openai_model='qwen-test', openai_batch_size=2, openai_context_lines=1,
                  openai_bilingual=True, openai_api_key='',
                  openai_base_url='http://host.docker.internal:11434/v1', openai_timeout=300,
                  translator_info=False, openai_styled_ass=False,
                  openai_ass_font_name='Noto Sans CJK SC', openai_ass_font_size=52,
                  openai_ass_primary_color='#FFFFFF', openai_ass_outline_color='#000000',
                  openai_ass_bold=True, openai_ass_outline=3, openai_ass_shadow=1,
                  openai_ass_margin_v=54,
                  openai_ass_chinese_font_name='Noto Sans CJK SC',
                  openai_ass_chinese_font_size=52,
                  openai_ass_chinese_primary_color='#FFFF80',
                  openai_ass_chinese_outline_color='#000000',
                  openai_ass_chinese_bold=True, openai_ass_chinese_outline=3,
                  openai_ass_chinese_shadow=0,
                  openai_ass_original_font_name='Arial', openai_ass_original_font_size=32,
                  openai_ass_original_primary_color='#FFFFFF',
                  openai_ass_original_outline_color='#000000',
                  openai_ass_original_bold=False, openai_ass_original_outline=2,
                  openai_ass_original_shadow=0, openai_ass_bilingual_margin_v=60)
    values.update(overrides)
    return SimpleNamespace(translator=SimpleNamespace(**values))


def make_service(namespace, source, destination):
    return namespace['OpenAICompatibleTranslatorService'](
        source_srt_file=str(source), dest_srt_file=str(destination), to_lang='zho',
        media_type='episode', sonarr_series_id=1, sonarr_episode_id=22, radarr_id=None,
        forced=False, hi=False, video_path='video.mp4', from_lang='en', orig_to_lang='zh')


def test_wraps_chinese_at_punctuation_and_parses_numbered_output():
    namespace = load_translation_namespace(translator_settings())
    wrapped = namespace['wrap_translation']('这是一句比较长的中文字幕，需要在合适的位置自动断句。', width=12)
    assert r'\N' in wrapped
    assert wrapped.replace(r'\N', '') == '这是一句比较长的中文字幕，需要在合适的位置自动断句。'
    quoted = namespace['wrap_translation']('看来大学生们已经不再喜欢我的“胡志鳅”角色了。', width=18)
    assert not any(line.startswith(tuple('，。！？；：、”’》）】')) for line in quoted.split(r'\N'))
    assert not any(line.endswith(tuple('“‘《（【')) for line in quoted.split(r'\N'))
    invented = namespace['wrap_translation']('呵，什么都这么“drivelous”。', width=10)
    assert r'dr\Nivelous' not in invented
    sentence = '好，很好。你开始能感知别人而不只是自己的感受了。'
    assert r'\N' not in namespace['wrap_translation'](sentence)
    assert namespace['wrap_translation']('这句很短<br>不应换行') == '这句很短不应换行'
    model_wrapped = namespace['wrap_translation'](
        '你已经开始理解其他人的真实感受<br>而不再只是关注自己的想法和处境。')
    assert model_wrapped == (
        '你已经开始理解其他人的真实感受' + r'\N' + '而不再只是关注自己的想法和处境。')
    clause = namespace['wrap_translation'](sentence, width=18)
    assert r'别\N人' not in clause
    assert r'别人\N而' in clause
    assert namespace['_extract_numbered']('```text\n[2] 你好\n[3] 再见\n```', [2, 3]) == {
        2: '你好', 3: '再见'}
    assert namespace['_extract_numbered']('2. 你好\n3) 再见', [2, 3]) == {
        2: '你好', 3: '再见'}
    assert namespace['_extract_numbered']('1. 二十\n2. 二十一', [20, 21]) == {
        20: '二十', 21: '二十一'}


def test_normalizes_chinese_punctuation_and_dual_speakers():
    namespace = load_translation_namespace(translator_settings())
    normalize = namespace['normalize_chinese_translation']
    assert normalize('你好，朋友。你还好吗？') == '你好 朋友 你还好吗？'
    assert normalize('价格是3.5元，编号F.B.I.。') == '价格是3.5元 编号F.B.I.'
    assert normalize('— 你好吗？ — 我很好。') == '-你好吗？ -我很好'
    assert normalize('我…我的意思是...') == '我…我的意思是…'
    assert namespace['_is_dual_speaker']('-How are you? -I am fine.')
    assert namespace['_speaker_marker_count']('-你好吗？ -我很好') == 2


def test_loads_legacy_encoded_translation_source(tmp_path):
    namespace = load_translation_namespace(translator_settings())
    source = tmp_path / 'legacy.srt'
    chinese = '不，伙计。这是一段用于检测中文字幕编码的测试内容。'
    source.write_bytes(
        f'1\n00:00:01,000 --> 00:00:02,000\n{chinese}\nNo, dude.\n'.encode('gb18030'))

    subtitles = namespace['load_subtitles_with_encoding'](str(source))

    assert len(subtitles) == 1
    assert subtitles[0].text == chinese + r'\N' + 'No, dude.'


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
    assert batches[1][1][0]['translation'] == '这是结合前后文翻译后的自然中文句子。'
    assert batches[1][1][-1]['translation'] is None


def test_styled_ass_output_uses_configured_appearance(tmp_path):
    settings = translator_settings(
        openai_styled_ass=True,
        openai_ass_chinese_font_name='PingFang SC', openai_ass_chinese_font_size=58,
        openai_ass_chinese_primary_color='#FFE66D', openai_ass_chinese_outline_color='#102030',
        openai_ass_chinese_bold=False, openai_ass_chinese_outline=4,
        openai_ass_chinese_shadow=2,
        openai_ass_original_font_name='Helvetica', openai_ass_original_font_size=30,
        openai_ass_original_primary_color='#FFFFFF', openai_ass_original_outline_color='#203040',
        openai_ass_original_bold=True, openai_ass_original_outline=2,
        openai_ass_original_shadow=1, openai_ass_bilingual_margin_v=72)
    namespace = load_translation_namespace(settings)
    source, destination = tmp_path / 'source.srt', tmp_path / 'episode.llm.zh.ass'
    subtitles = pysubs2.SSAFile()
    subtitles.append(pysubs2.SSAEvent(start=1000, end=2500, text='Hello'))
    subtitles.save(source, format_='srt', encoding='utf-8')
    service = make_service(namespace, source, destination)
    service._translate_batch = lambda targets, context, description: {
        item['index']: '你好' for item in targets}

    assert service.translate(job_id=1) == str(destination)
    result = pysubs2.load(destination, encoding='utf-8')
    chinese_style = result.styles['Chinese']
    original_style = result.styles['Original']
    assert result.info['PlayResX'] == '1920'
    assert result.info['PlayResY'] == '1080'
    assert result.info['Collisions'] == 'Reverse'
    assert chinese_style.fontname == 'PingFang SC'
    assert chinese_style.fontsize == 58
    assert chinese_style.primarycolor == pysubs2.Color(255, 230, 109)
    assert chinese_style.outlinecolor == pysubs2.Color(16, 32, 48)
    assert chinese_style.bold is False
    assert chinese_style.outline == 4
    assert chinese_style.shadow == 2
    assert original_style.fontname == 'Helvetica'
    assert original_style.fontsize == 30
    assert original_style.primarycolor == pysubs2.Color(255, 255, 255)
    assert original_style.outlinecolor == pysubs2.Color(32, 48, 64)
    assert original_style.bold is True
    assert original_style.outline == 2
    assert original_style.shadow == 1
    assert chinese_style.marginv == original_style.marginv == 72
    assert [(cue.style, cue.text) for cue in result] == [
        ('Original', 'Hello'), ('Chinese', '你好')]
    assert result[0].start == result[1].start == 1000
    assert result[0].end == result[1].end == 2500


def test_styled_ass_chinese_only_does_not_duplicate_events(tmp_path):
    settings = translator_settings(openai_styled_ass=True, openai_bilingual=False)
    namespace = load_translation_namespace(settings)
    source, destination = tmp_path / 'source.srt', tmp_path / 'episode.llm.zh.ass'
    subtitles = pysubs2.SSAFile()
    subtitles.append(pysubs2.SSAEvent(start=1000, end=2500, text='Hello'))
    subtitles.save(source, format_='srt', encoding='utf-8')
    service = make_service(namespace, source, destination)
    service._translate_batch = lambda targets, context, description: {
        item['index']: '你好' for item in targets}

    service.translate(job_id=1)
    result = pysubs2.load(destination, encoding='utf-8')
    assert [(cue.style, cue.text) for cue in result] == [('Chinese', '你好')]


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
                    '[0] 你好'}}]}
    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()
    namespace['requests'] = SimpleNamespace(post=post, RequestException=Exception)
    service = make_service(namespace, 'source.srt', 'translated.srt')
    target = [{'index': 0, 'content': 'Hello'}]
    assert service._request(target, [target[0]], '') == {0: '你好'}
    assert captured['json']['max_tokens'] == 512
    assert captured['json']['temperature'] == 0
    assert captured['json']['messages'][0]['role'] == 'user'
    assert 'Never add unstated specifications' in captured['json']['messages'][0]['content']
    assert captured['timeout'] == 300


def test_request_normalizes_and_requires_dual_speaker_markers():
    settings = translator_settings()
    namespace = load_translation_namespace(settings)
    content = {'value': '[0] —你好吗， —我很好。'}

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {'choices': [{'message': {'content': content['value']}}]}

    namespace['requests'] = SimpleNamespace(
        post=lambda *args, **kwargs: Response(), RequestException=Exception)
    service = make_service(namespace, 'source.srt', 'translated.srt')
    target = [{'index': 0, 'content': '-How are you? -I am fine.'}]

    assert service._request(target, target, '') == {0: '-你好吗 -我很好'}

    content['value'] = '[0] 你好吗 我很好'
    with pytest.raises(ValueError, match='speaker markers'):
        service._request(target, target, '')


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
    chinese = tmp_path / 'episode.llm.zh.srt'
    english = tmp_path / 'episode.en.srt'
    english.write_text('source', encoding='utf-8')
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
                                   {'settings': settings, 'logging': logging, 'os': os,
                                    'is_llm_subtitle': lambda path: '.llm.' in path,
                                    'mark_as_llm_subtitle': lambda path, video: path})
    assert function('video.mp4', str(english), 'en', False, 'series', metadata)
    assert queued[0]['to_lang'] == 'zh'
    assert queued[0]['low_priority'] is True
    queued.clear()
    modules['app.database'].get_subtitles = lambda **kwargs: [
        {'code2': 'zh', 'embedded_track_id': 0, 'path': None}]
    assert not function('video.mp4', str(english), 'en', False, 'series', metadata)
    assert not queued
    assert not function('video.mp4', str(english), 'en', True, 'series', metadata)


def test_authoritative_traditional_chinese_blocks_llm_translation(tmp_path, monkeypatch):
    queued = []
    settings = SimpleNamespace(translator=SimpleNamespace(auto_translate_missing_chinese=True))
    metadata = SimpleNamespace(sonarrEpisodeId=22, sonarrSeriesId=1)
    traditional = tmp_path / 'episode.zh-TW.srt'
    traditional.write_text('existing', encoding='utf-8')
    simplified = tmp_path / 'episode.zh.srt'
    modules = {
        'app.database': SimpleNamespace(get_subtitles=lambda **kwargs: [
            {'code2': 'zt', 'embedded_track_id': None, 'path': str(traditional)}]),
        'subzero.language': SimpleNamespace(Language=lambda code: code),
        'subliminal_patch.core': SimpleNamespace(get_subtitle_path=lambda *args, **kwargs: str(simplified)),
        'utilities.helper': SimpleNamespace(get_target_folder=lambda path: None),
        'subtitles.tools.translate.main': SimpleNamespace(
            translate_subtitles_file=lambda **kwargs: queued.append(kwargs)),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    function = processing_function('_queue_missing_chinese_translation',
                                   {'settings': settings, 'logging': logging, 'os': os,
                                    'is_llm_subtitle': lambda path: '.llm.' in path,
                                    'mark_as_llm_subtitle': lambda path, video: path})
    assert not function('video.mp4', None, None, False, 'series', metadata)
    assert not queued


def test_embedded_chinese_blocks_llm_but_embedded_english_is_preferred(tmp_path, monkeypatch):
    queued = []
    settings = SimpleNamespace(translator=SimpleNamespace(auto_translate_missing_chinese=True))
    metadata = SimpleNamespace(sonarrEpisodeId=22, sonarrSeriesId=1)
    destination = tmp_path / 'episode.llm.zh.srt'
    extracted = tmp_path / 'embedded.en.srt'
    extracted.write_text('embedded source', encoding='utf-8')
    current = {'items': [
        {'code2': 'en', 'embedded_track_id': 4, 'forced': False, 'hi': False, 'path': None},
    ]}
    modules = {
        'app.database': SimpleNamespace(get_subtitles=lambda **kwargs: current['items']),
        'app.get_args': SimpleNamespace(args=SimpleNamespace(config_dir=str(tmp_path))),
        'utilities.binaries': SimpleNamespace(get_binary=str),
        'subtitles.embedded_translation': SimpleNamespace(
            extract_embedded_subtitle=lambda *args: str(extracted)),
        'subzero.language': SimpleNamespace(Language=lambda code: code),
        'subliminal_patch.core': SimpleNamespace(get_subtitle_path=lambda *args, **kwargs: str(destination)),
        'utilities.helper': SimpleNamespace(get_target_folder=lambda path: None),
        'subtitles.tools.translate.main': SimpleNamespace(
            translate_subtitles_file=lambda **kwargs: queued.append(kwargs)),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    function = processing_function('_queue_missing_chinese_translation',
                                   {'settings': settings, 'logging': logging, 'os': os,
                                    'is_llm_subtitle': lambda path: '.llm.' in path,
                                    'mark_as_llm_subtitle': lambda path, video: path})
    assert function('video.mp4', None, None, False, 'series', metadata)
    assert queued[0]['source_srt_file'] == str(extracted)
    queued.clear()
    current['items'].append(
        {'code2': 'zt', 'embedded_track_id': 5, 'forced': False, 'hi': False, 'path': None})
    assert not function('video.mp4', None, None, False, 'series', metadata)
    assert not queued


def test_llm_and_embedded_chinese_do_not_satisfy_external_chinese_search():
    source = ROOT / 'bazarr/subtitles/translation_priority.py'
    namespace = {'os': os}
    exec(compile(ast.parse(source.read_text(encoding='utf-8')), str(source), 'exec'), namespace)
    counts = namespace['counts_as_available_subtitle']
    assert not counts({'path': 'episode.llm.zh.srt', 'code2': 'zh'}, True)
    assert not counts({'path': None, 'code2': 'zh', 'embedded_track_id': 3}, True)
    assert counts({'path': None, 'code2': 'en', 'embedded_track_id': 4}, True)
    assert counts({'path': 'episode.zh.srt', 'code2': 'zh'}, True)
    mark = namespace['mark_as_llm_subtitle']
    assert mark('/media/episode.zh.srt', '/media/episode.mkv') == '/media/episode.llm.zh.srt'
    assert mark('/media/episode.zh-TW.srt', '/media/episode.mkv') == '/media/episode.llm.zh-TW.srt'


def test_original_language_search_is_added_only_without_an_existing_source():
    source = ROOT / 'bazarr/subtitles/translation_priority.py'
    namespace = {'os': os}
    exec(compile(ast.parse(source.read_text(encoding='utf-8')), str(source), 'exec'), namespace)
    append = namespace['append_original_language']
    languages, supplemental = append([('zh', 'False', 'False')], 'ja', [])
    assert languages == [('zh', 'False', 'False'), ('ja', 'False', 'False')]
    assert supplemental == ('ja', 'False', 'False')
    for existing in (
            [{'code2': 'ja', 'embedded_track_id': 3, 'forced': False, 'path': None}],
            [{'code2': 'ja', 'embedded_track_id': None, 'forced': False, 'path': 'episode.ja.srt'}]):
        languages, supplemental = append([('zh', 'False', 'False')], 'ja', existing)
        assert languages == [('zh', 'False', 'False')]
        assert supplemental is None


def test_original_language_translation_precedes_english(tmp_path, monkeypatch):
    queued = []
    settings = SimpleNamespace(translator=SimpleNamespace(auto_translate_missing_chinese=True))
    metadata = SimpleNamespace(sonarrEpisodeId=22, sonarrSeriesId=1, originalLanguage='Japanese')
    destination = tmp_path / 'episode.llm.zh.srt'
    japanese = tmp_path / 'episode.ja.srt'
    english = tmp_path / 'episode.en.srt'
    japanese.write_text('Japanese source', encoding='utf-8')
    english.write_text('English source', encoding='utf-8')
    existing = [
        {'code2': 'en', 'embedded_track_id': None, 'forced': False, 'path': str(english)},
        {'code2': 'ja', 'embedded_track_id': None, 'forced': False, 'path': str(japanese)},
    ]
    modules = {
        'app.database': SimpleNamespace(get_subtitles=lambda **kwargs: existing),
        'languages.get_languages': SimpleNamespace(
            alpha2_from_language=lambda name: {'Japanese': 'ja'}.get(name)),
        'subzero.language': SimpleNamespace(Language=lambda code: code),
        'subliminal_patch.core': SimpleNamespace(get_subtitle_path=lambda *args, **kwargs: str(destination)),
        'utilities.helper': SimpleNamespace(get_target_folder=lambda path: None),
        'subtitles.tools.translate.main': SimpleNamespace(
            translate_subtitles_file=lambda **kwargs: queued.append(kwargs)),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    function = processing_function('_queue_missing_chinese_translation',
                                   {'settings': settings, 'logging': logging, 'os': os,
                                    'is_llm_subtitle': lambda path: '.llm.' in path,
                                    'mark_as_llm_subtitle': lambda path, video: path})
    assert function('video.mp4', None, None, False, 'series', metadata)
    assert queued[0]['source_srt_file'] == str(japanese)
    assert queued[0]['from_lang'] == 'ja'


def test_embedded_subtitle_extraction_uses_selected_track_and_cache(tmp_path, monkeypatch):
    source = ROOT / 'bazarr/subtitles/embedded_translation.py'
    spec = importlib.util.spec_from_file_location('embedded_translation_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    video = tmp_path / 'episode.mkv'
    video.write_bytes(b'video')
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_text(
            '1\n00:00:01,000 --> 00:00:02,000\nEmbedded English subtitle\n', encoding='utf-8')

    monkeypatch.setattr(module.subprocess, 'run', run)
    first = module.extract_embedded_subtitle(video, 4, tmp_path / 'cache', lambda name: name, language='ja')
    second = module.extract_embedded_subtitle(video, 4, tmp_path / 'cache', lambda name: name, language='ja')
    assert first == second
    assert first.endswith('.ja.srt')
    assert len(commands) == 1
    assert commands[0][commands[0].index('-map') + 1] == '0:4'


def test_local_simplified_to_traditional_conversion_preserves_srt(tmp_path, monkeypatch):
    source = ROOT / 'bazarr/subtitles/tools/translate/traditional.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    namespace = {'os': os, 'Path': Path}
    exec(compile(tree, str(source), 'exec'), namespace)

    class FakeOpenCC:
        def __init__(self, config):
            assert config == 's2twp'

        @staticmethod
        def convert(text):
            return text.replace('简体字幕', '繁體字幕')

    monkeypatch.setitem(sys.modules, 'opencc', SimpleNamespace(OpenCC=FakeOpenCC))
    source_file = tmp_path / 'episode.zh.srt'
    destination = tmp_path / 'episode.zh-TW.srt'
    content = '1\n00:00:01,000 --> 00:00:02,000\nEnglish\\N简体字幕\n'
    source_file.write_text(content, encoding='utf-8')
    assert namespace['convert_simplified_file'](source_file, destination) == str(destination)
    assert destination.read_text(encoding='utf-8') == content.replace('简体字幕', '繁體字幕')
    assert not list(tmp_path.glob('*.tmp'))
