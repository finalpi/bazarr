"""SubHD labels come from its metadata badges, never from release prose."""

import ast
from pathlib import Path
from unittest.mock import Mock

from flask import Flask
from flask_restx import Namespace, fields, marshal
import pytest

from subliminal_patch.providers.subhd import SubhdProvider, SubhdSubtitle
from test_manual_search_options import CHINESE, episode, load_search_api, manual_namespace


ROOT = Path(__file__).resolve().parents[2]


def search_row(subtitle_id, tags=(), description='', extra_metadata='', extra_regions=''):
    badges = []
    for tag in tags:
        if tag.strip() == '其他来源':
            classes = 'rounded p-1 me-1 text-white'
        elif tag.strip() in ('ASS', 'SUP', 'SRT', 'SSA'):
            classes = 'p-1 text-secondary'
        else:
            classes = 'p-1 fw-bold'
        badges.append('<span class="%s">%s</span>' % (classes, tag))
    return '''
      <div class="row g-3">
        <div class="col-auto"><a href="/a/%s"><img alt="双语 简体 ASS 1997"></a></div>
        <div class="col">
          <div class="text-truncate"><a class="view-text" href="/a/%s">Canonical Show S03E02</a></div>
          <div class="subtitle-description">%s</div>
          <div class="text-truncate py-2 f11">%s%s</div>
          %s
        </div>
      </div>
    ''' % (subtitle_id, subtitle_id, description, ''.join(badges), extra_metadata, extra_regions)


def test_real_metadata_badges_keep_site_language_format_and_source_labels():
    expected = {
        'bqXP2Z': ['其他来源', '双语', '简体', '繁体', '英语', 'SUP'],
        'miT4Hx': ['其他来源', '双语', '英语', '简体', 'ASS'],
        '31ghJW': ['其他来源', '简体', 'ASS'],
    }
    html = ''.join(search_row(subtitle_id, labels) for subtitle_id, labels in expected.items())
    assert SubhdProvider._parse_result_tags(html) == expected
    # Existing consumers and parser tests continue to receive exactly two values.
    parsed = SubhdProvider._parse_results(html)
    assert {subtitle_id for subtitle_id, release in parsed} == set(expected)
    assert all(len(row) == 2 for row in parsed)


def test_release_words_years_and_image_alt_text_do_not_invent_labels():
    html = search_row('NoTags', description='1997 双语 简体 繁体 ASS SUP 英语 其他来源')
    assert SubhdProvider._parse_result_tags(html).get('NoTags', []) == []


def test_labels_do_not_leak_between_search_rows_or_from_global_navigation():
    html = '<div class="text-truncate py-2 f11"><span class="p-1">全站标签</span></div>'
    html += search_row('First', ['双语', 'ASS']) + search_row('Second', ['简体', 'SUP'])
    parsed = SubhdProvider._parse_result_tags(html)
    assert parsed == {'First': ['双语', 'ASS'], 'Second': ['简体', 'SUP']}


def test_duplicate_result_anchors_and_repeated_badges_are_trimmed_and_deduplicated():
    html = search_row('Dup', ['  双语  ', '简体', '双语', '&nbsp;ASS&nbsp;', '简体'])
    html += search_row('Dup', ['双语', '简体', 'ASS'])
    assert SubhdProvider._parse_result_tags(html) == {'Dup': ['双语', '简体', 'ASS']}


def test_comment_like_numbers_and_nested_non_badge_metadata_are_not_labels():
    extra = '''
      <span class="p-1 text-primary"><svg class="comment-icon"></svg>12</span>
      <span class="p-1 text-danger"><svg class="like-icon"></svg>8</span>
      <span class="p-1">37</span>
      <a href="/user/uploader"><span class="p-1">上传者昵称</span></a>
    '''
    regions = '''
      <div class="pt-2 text-secondary f12">
        <svg class="size-icon"></svg><span class="align-text-top me-3">180k</span>
        <svg class="download-icon"></svg><span class="align-text-top me-3">142</span>
        <svg class="clock-icon"></svg><span class="align-text-top me-3">
          <time data-local-time="relative" datetime="2026-02-10T16:24:00.000Z">02-10 16:24</time>
        </span>
      </div>
      <div class="pt-1 f12 text-secondary">发布人
        <a class="fw-bold text-dark" href="/u/SubKing">SubKing</a>
      </div>
    '''
    html = search_row('Counts', ['双语', 'ASS'], extra_metadata=extra, extra_regions=regions)
    assert SubhdProvider._parse_result_tags(html) == {'Counts': ['双语', 'ASS']}


def test_ordinary_spans_outside_the_site_metadata_row_are_ignored():
    html = search_row('BodyOnly', description='<span class="p-1">双语</span>')
    html = html.replace('<div class="text-truncate py-2 f11"></div>',
                        '<div class="text-truncate py-2 f11"><span>ASS</span></div>')
    assert SubhdProvider._parse_result_tags(html).get('BodyOnly', []) == []


def test_missing_metadata_does_not_drop_the_result_or_invent_labels():
    html = '<div class="row"><a href="/a/Bare">Canonical Show S03E02 双语 ASS</a></div>'
    assert SubhdProvider._parse_result_tags(html).get('Bare', []) == []
    assert SubhdProvider._parse_results(html) == [('Bare', 'Canonical Show S03E02 双语 ASS')]


def test_list_subtitles_attaches_each_results_own_site_badges_without_changing_identity():
    video = episode()
    html = (search_row('Bilingual', ['双语', '简体', 'ASS']) +
            search_row('Simplified', ['简体', 'SRT']) + search_row('Unlabelled'))
    provider = SubhdProvider()
    provider._request = Mock(return_value=html.encode('utf-8'))
    results = provider.list_subtitles(video, {CHINESE})
    assert {subtitle.id: subtitle.subtitle_tags for subtitle in results} == {
        'Bilingual': ['双语', '简体', 'ASS'], 'Simplified': ['简体', 'SRT'], 'Unlabelled': []}
    assert all(subtitle.video is video for subtitle in results)
    assert (video.series, video.season, video.episode) == ('Canonical Show', 3, 2)
    assert provider._request.call_count == 1


def manual_result(tags=None, missing=False):
    video = episode()
    candidate = SubhdSubtitle(CHINESE, 'candidate', '/a/candidate', 'Canonical Show S03E02', video)
    if missing:
        if hasattr(candidate, 'subtitle_tags'):
            del candidate.subtitle_tags
        candidate.provider_name = 'legacyprovider'
    else:
        candidate.subtitle_tags = tags
    pool = Mock()
    pool.__enter__ = Mock(return_value=pool)
    pool.__exit__ = Mock(return_value=False)
    cached = []
    namespace = manual_namespace(pool, video, [candidate], cached)
    result = namespace['manual_search']('/tmp/media.mkv', 1, ['subhd'], 'None', 'Canonical Show', 'series')
    assert cached == [candidate]
    return result[0]


def test_manual_response_normalizes_only_explicit_string_tags_and_preserves_existing_fields():
    result = manual_result([' 双语 ', '简体', '双语', '', '  ', None, 3, 'ASS'])
    assert result['tags'] == ['双语', '简体', 'ASS']
    assert result['subtitle'] == 'cached-id'
    assert result['provider'] == 'subhd'
    assert result['release_info'] == ['Canonical Show S03E02']


@pytest.mark.parametrize('tags', [None, '双语 简体 ASS', ('双语',), {'tag': '双语'}, 3])
def test_manual_response_keeps_empty_tags_for_non_list_provider_metadata(tags):
    assert manual_result(tags)['tags'] == []


def test_legacy_provider_without_tags_remains_searchable():
    result = manual_result(missing=True)
    assert result['tags'] == []
    assert result['provider'] == 'legacyprovider'
    assert result['subtitle'] == 'cached-id'


def response_model(media_type):
    filename = 'providers_episodes.py' if media_type == 'series' else 'providers_movies.py'
    source = ROOT / 'bazarr/api/providers' / filename
    tree = ast.parse(source.read_text(encoding='utf-8'))
    resource = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    assignment = next(node for node in resource.body if isinstance(node, ast.Assign)
                      and isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'get_response_model')
    namespace_name = 'api_ns_providers_episodes' if media_type == 'series' else 'api_ns_providers_movies'
    rejection_assignment = next(node for node in resource.body if isinstance(node, ast.Assign)
                                and isinstance(node.targets[0], ast.Name)
                                and node.targets[0].id == 'rejection_model')
    namespace = {namespace_name: Namespace('TagResponse'), 'fields': fields}
    exec(compile(ast.Module(body=[rejection_assignment], type_ignores=[]), str(source), 'exec'), namespace)
    return eval(compile(ast.Expression(assignment.value), str(source), 'eval'), namespace)


@pytest.mark.parametrize('media_type,id_name', [('series', 'episodeid'), ('movie', 'radarrid')])
@pytest.mark.parametrize('legacy', [False, True])
def test_real_manual_get_marshalling_keeps_tags_and_legacy_provider_results(media_type, id_name, legacy):
    result = manual_result(['双语', 'ASS'], missing=legacy)
    resource = load_search_api(media_type, Mock(return_value=[result]))
    resource.get_response_model = response_model(media_type)
    resource.get.__func__.__globals__['marshal'] = marshal
    with Flask(__name__).test_request_context('/?' + id_name + '=1'):
        payload = resource.get()['data'][0]
    assert payload['tags'] == ([] if legacy else ['双语', 'ASS'])
    assert payload['subtitle'] == 'cached-id'
    assert payload['provider'] == ('legacyprovider' if legacy else 'subhd')
    assert payload['release_info'] == ['Canonical Show S03E02']
