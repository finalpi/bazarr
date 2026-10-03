"""Site script labels and pool identity must agree with each language candidate."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from subzero.language import Language

from subliminal_patch.core import SZProviderPool
from subliminal_patch.providers.r3sub import R3subSubtitle
from subliminal_patch.providers.subhd import SubhdProvider, SubhdSubtitle
from test_manual_search_options import episode, manual_namespace
from test_subhd_search_tags import search_row


CN = Language('zho', 'CN')
TW = Language('zho', 'TW')
ZH = Language('zho')


@pytest.fixture
def pool_factory():
    pools = []

    def create(provider_name='subhd', provider=None, results=None, **options):
        if provider is None:
            provider = SimpleNamespace(list_subtitles=Mock(return_value=results), terminate=lambda: None)
        pool = SZProviderPool(providers=[provider_name], **options)
        pool.initialized_providers[provider_name] = provider
        pools.append(pool)
        return pool

    yield create
    for pool in pools:
        pool.terminate()


def candidate(language, video, subtitle_id='Shared', provider_name='subhd'):
    if provider_name == 'r3sub':
        return R3subSubtitle(language, subtitle_id, 'Canonical Show S03E02', video, [])
    return SubhdSubtitle(language, subtitle_id, '/a/' + subtitle_id, 'Canonical Show S03E02', video)


def test_actual_pool_keeps_both_languages_under_the_same_raw_subhd_id(pool_factory):
    video = episode()
    raw = [candidate(TW, video), candidate(CN, video)]
    pool = pool_factory(results=raw)
    output = pool.list_subtitles_provider('subhd', video, {CN, TW})
    assert output == raw
    assert {str(subtitle.language) for subtitle in output} == {'zh-CN', 'zh-TW'}
    assert all(subtitle.id == subtitle.subtitle_id == 'Shared' for subtitle in output)


def test_actual_pool_still_deduplicates_repeated_subhd_id_and_language(pool_factory):
    video = episode()
    first_cn, duplicate_cn = candidate(CN, video), candidate(CN, video)
    first_tw, duplicate_tw = candidate(TW, video), candidate(TW, video)
    pool = pool_factory(results=[first_cn, duplicate_cn, first_tw, duplicate_tw])
    assert pool.list_subtitles_provider('subhd', video, {CN, TW}) == [first_cn, first_tw]


def test_other_providers_keep_the_existing_raw_id_deduplication(pool_factory):
    video = episode()
    first, second = candidate(TW, video, provider_name='r3sub'), candidate(CN, video, provider_name='r3sub')
    pool = pool_factory(provider_name='r3sub', results=[first, second])
    assert pool.list_subtitles_provider('r3sub', video, {CN, TW}) == [first]


def test_existing_raw_subhd_blacklist_blocks_both_language_variants(pool_factory):
    video = episode()
    raw = [candidate(CN, video), candidate(TW, video), candidate(CN, video, subtitle_id='Allowed')]
    pool = pool_factory(results=raw, blacklist=[('subhd', 'Shared')])
    output = pool.list_subtitles_provider('subhd', video, {CN, TW})
    assert [subtitle.id for subtitle in output] == ['Allowed']


def test_configured_language_equals_normalizes_before_subhd_variant_deduplication(pool_factory):
    video = episode()
    first, second = candidate(CN, video), candidate(TW, video)
    pool = pool_factory(results=[first, second], language_equals=[(CN, TW)])
    output = pool.list_subtitles_provider('subhd', video, {TW})
    assert output == [first]
    assert str(output[0].language) == 'zh-TW'
    assert output[0].id == 'Shared'


def site_provider(subtitle_id, tags):
    provider = SubhdProvider()
    provider._request = Mock(return_value=search_row(subtitle_id, tags).encode('utf-8'))
    return provider


def assert_search_only(provider):
    assert provider._request.called
    for call in provider._request.call_args_list:
        assert '/search/' in call.args[0]
        assert '/api/sub/' not in call.args[0]
        assert '/down/' not in call.args[0]
        assert call.kwargs.get('method', 'GET') == 'GET'


@pytest.mark.parametrize('tags,expected', [
    (['其他来源', '双语', '简体', '英语', 'ASS'], {'zh-CN'}),
    (['其他来源', '双语', '繁体', '英语', 'ASS'], {'zh-TW'}),
    (['其他来源', '双语', '简体', '繁体', '英语', 'SUP'], {'zh-CN', 'zh-TW'}),
    (['其他来源', '英语', 'SRT'], set()),
    ([], {'zh-CN', 'zh-TW'}),
])
def test_real_site_metadata_limits_requested_chinese_scripts(tags, expected, pool_factory):
    provider = site_provider('SiteRow', tags)
    pool = pool_factory(provider=provider)
    output = pool.list_subtitles_provider('subhd', episode(), {CN, TW})
    assert {str(subtitle.language) for subtitle in output} == expected
    assert all(subtitle.subtitle_tags == tags for subtitle in output)
    assert all(subtitle.id == subtitle.subtitle_id == 'SiteRow' for subtitle in output)
    assert_search_only(provider)


@pytest.mark.parametrize('tags,requested', [(['简体', '英语', 'ASS'], TW),
                                          (['繁体', '英语', 'ASS'], CN)])
def test_an_explicit_single_script_row_is_not_relabelled_as_the_other_requested_script(tags, requested, pool_factory):
    provider = site_provider('WrongScript', tags)
    pool = pool_factory(provider=provider)
    assert pool.list_subtitles_provider('subhd', episode(), {requested}) == []
    assert_search_only(provider)


def test_different_rows_keep_their_own_scripts_when_the_requested_language_order_is_reversed(pool_factory):
    body = (search_row('ScOnly', ['简体', '英语', 'ASS']) +
            search_row('TcOnly', ['繁体', '英语', 'ASS']) +
            search_row('Both', ['简体', '繁体', '英语', 'ASS']))
    provider = SubhdProvider()
    provider._request = Mock(return_value=body.encode('utf-8'))
    raw = provider.list_subtitles(episode(), [TW, CN])
    assert {(subtitle.id, str(subtitle.language)) for subtitle in raw} == {
        ('ScOnly', 'zh-CN'), ('TcOnly', 'zh-TW'), ('Both', 'zh-CN'), ('Both', 'zh-TW')}
    assert_search_only(provider)


@pytest.mark.parametrize('tags,expected', [
    (['简体', '英语', 'ASS'], {'zh'}),
    (['繁体', '英语', 'ASS'], {'zh-TW'}),
    (['简体', '繁体', '英语', 'SUP'], {'zh', 'zh-TW'}),
    (['英语', 'SRT'], set()),
    ([], {'zh', 'zh-TW'}),
])
def test_canonical_production_profile_language_is_supported_and_survives_real_pool_filtering(tags, expected, pool_factory):
    # A regular zh profile builds Language('zho'), with no CN country.
    assert ZH.country is None
    assert ZH in SubhdProvider.languages
    provider = site_provider('CanonicalProfile', tags)
    pool = pool_factory(provider=provider)
    output = pool.list_subtitles_provider('subhd', episode(), {ZH, TW})
    assert {str(subtitle.language) for subtitle in output} == expected
    assert all(subtitle.id == 'CanonicalProfile' for subtitle in output)
    assert_search_only(provider)


def test_canonical_production_profile_manual_wire_is_zh_and_zh_tw_without_region_rewriting(pool_factory):
    video = episode()
    provider = site_provider('ProductionBoth', ['简体', '繁体', '双语', 'ASS'])
    pool = pool_factory(provider=provider)
    candidates = pool.list_subtitles_provider('subhd', video, {ZH, TW})
    cached = []
    namespace = manual_namespace(pool, video, candidates, cached)
    namespace['_get_language_obj'] = lambda **kwargs: ({ZH, TW}, True)

    def listed(videos, languages, requested_pool):
        assert videos == [video]
        assert languages == {ZH, TW}
        assert requested_pool is pool
        return {video: candidates}

    namespace['list_all_subtitles'] = listed
    result = namespace['manual_search']('/tmp/media.mkv', 1, ['subhd'], 'None', 'Canonical Show', 'series')
    assert {row['language'] for row in result} == {'zh', 'zh-TW'}
    assert {str(subtitle.language) for subtitle in cached} == {'zh', 'zh-TW'}
    assert_search_only(provider)


def test_manual_wire_uses_simplified_basename_and_traditional_region_and_retains_site_tags(pool_factory):
    video = episode()
    provider = site_provider('Both', ['简体', '繁体', '双语', 'ASS'])
    pool = pool_factory(provider=provider)
    candidates = pool.list_subtitles_provider('subhd', video, {CN, TW})
    cached = []
    namespace = manual_namespace(pool, video, candidates, cached)
    namespace['_get_language_obj'] = lambda **kwargs: ({CN, TW}, True)

    def listed(videos, languages, requested_pool):
        assert videos == [video]
        assert languages == {CN, TW}
        assert requested_pool is pool
        return {video: candidates}

    namespace['list_all_subtitles'] = listed
    result = namespace['manual_search']('/tmp/media.mkv', 1, ['subhd'], 'None', 'Canonical Show', 'series')
    assert {row['language'] for row in result} == {'zh-CN', 'zh-TW'}
    assert all(row['tags'] == ['简体', '繁体', '双语', 'ASS'] for row in result)
    assert len(cached) == 2
    assert all(subtitle.id == 'Both' for subtitle in cached)
    assert_search_only(provider)
