"""Origins are inferred from full-path write history, never language or filenames."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('subtitle_provenance', ROOT / 'bazarr/subtitles/provenance.py')
provenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provenance)
resolve_subtitle_source = provenance.resolve_subtitle_source

VIDEO = '/media/Film.mkv'
SUBTITLE = '/media/Film.zh.srt'
UNKNOWN = {'source': None, 'source_type': 'unknown'}


def event(identifier=1, action=2, provider='subhd', subtitles_path=SUBTITLE, **values):
    result = {
        'id': identifier, 'action': action, 'provider': provider,
        'media_id': 49, 'video_path': VIDEO, 'subtitles_path': subtitles_path,
    }
    result.update(values)
    return result


def resolve(history, path=SUBTITLE, **options):
    arguments = {'media_id': 49, 'video_path': VIDEO}
    arguments.update(options)
    return resolve_subtitle_source({'path': path, 'embedded_track_id': None}, history, **arguments)


@pytest.mark.parametrize('action', [1, 2, 3])
def test_automatic_manual_and_upgrade_writes_preserve_the_provider(action):
    assert resolve([event(action=action)]) == {'source': 'subhd', 'source_type': 'provider'}


def test_latest_insertion_id_wins_even_when_the_clock_moves_backwards():
    history = [event(1, provider='assrt', timestamp='2030-01-01'),
               event(3, provider='zimuku', timestamp='2020-01-01'),
               event(2, provider='subhd', timestamp='2026-01-01')]
    assert resolve(history) == {'source': 'zimuku', 'source_type': 'provider'}


def test_unnumbered_history_keeps_the_supplied_newest_first_order():
    newest, older = event(provider='r3sub'), event(provider='subhd')
    newest.pop('id')
    older.pop('id')
    assert resolve([newest, older]) == {'source': 'r3sub', 'source_type': 'provider'}


def test_numbered_rows_precede_unnumbered_rows_which_keep_their_input_order():
    unnumbered = event(provider='r3sub')
    unnumbered.pop('id')
    assert resolve([unnumbered, event(2)]) == {'source': 'subhd', 'source_type': 'provider'}


def test_equal_ids_preserve_the_original_input_order():
    assert resolve([event(2, provider='r3sub'), event(2, provider='subhd')]) == {
        'source': 'r3sub', 'source_type': 'provider'}


def test_full_subtitle_path_and_media_id_prevent_cross_file_or_media_attribution():
    history = [event(8, provider='wrong-file', subtitles_path='/other/Film.zh.srt'),
               event(9, provider='wrong-media', media_id=50), event(1)]
    assert resolve(history) == {'source': 'subhd', 'source_type': 'provider'}
    assert resolve(history[:2]) == UNKNOWN


def test_history_without_media_id_remains_usable_when_the_caller_already_scoped_it():
    row = event()
    row.pop('media_id')
    assert resolve([row]) == {'source': 'subhd', 'source_type': 'provider'}


def test_explicit_history_media_id_requires_the_same_requested_media_id():
    assert resolve_subtitle_source({'path': SUBTITLE}, [event()]) == UNKNOWN
    assert resolve([event()], media_id=50) == UNKNOWN


def test_video_path_is_strict_when_provided_and_optional_when_not_provided():
    assert resolve([event(video_path='/media/Another.encode.mkv')]) == UNKNOWN
    assert resolve([event(video_path=None)]) == UNKNOWN
    assert resolve([event(video_path='/media/Another.encode.mkv')], video_path=None) == {
        'source': 'subhd', 'source_type': 'provider'}


@pytest.mark.parametrize('action', [1, 2, 3, 4, 6])
@pytest.mark.parametrize('new_video_path', [None, '', '/media/Another.encode.mkv'])
def test_new_write_without_matching_video_identity_cannot_borrow_an_older_provider(action, new_video_path):
    history = [event(1), event(2, action=action, provider='assrt', video_path=new_video_path)]
    assert resolve(history) == UNKNOWN


@pytest.mark.parametrize('delete_video_path', [None, '/media/Another.encode.mkv'])
def test_delete_remains_a_barrier_even_when_its_video_path_is_missing_or_different(delete_video_path):
    history = [event(1), event(2, action=0, provider=None, video_path=delete_video_path)]
    assert resolve(history) == UNKNOWN


@pytest.mark.parametrize('sync_video_path', [None, '/media/Another.encode.mkv'])
def test_sync_rows_with_unconfirmed_video_identity_still_inherit_the_existing_origin(sync_video_path):
    history = [event(1), event(2, action=5, provider=None, video_path=sync_video_path)]
    assert resolve(history) == {'source': 'subhd', 'source_type': 'provider'}


def test_path_mapping_is_applied_to_current_and_historical_video_and_subtitle_paths():
    def mapper(path):
        return path.replace('/remote/library', '/media')

    row = event(subtitles_path='/remote/library/Film.zh.srt', video_path='/remote/library/Film.mkv')
    assert resolve([row], path_mapper=mapper) == {'source': 'subhd', 'source_type': 'provider'}
    assert resolve([event()], path='/remote/library/Film.zh.srt', video_path='/remote/library/Film.mkv',
                   path_mapper=mapper) == {'source': 'subhd', 'source_type': 'provider'}


def test_lexical_path_normalization_supports_slashes_and_dot_segments_without_io():
    row = event(subtitles_path='D:/media/Film.zh.srt', video_path='D:/media/Film.mkv')
    assert resolve([row], path=r'D:\media\folder\..\Film.zh.srt', video_path=r'D:\media\Film.mkv') == {
        'source': 'subhd', 'source_type': 'provider'}


def test_path_case_is_preserved_even_on_a_windows_test_host():
    assert resolve([event(subtitles_path='/media/film.zh.srt')]) == UNKNOWN
    assert resolve([event(video_path='/media/film.mkv')]) == UNKNOWN


def test_sync_rows_inherit_the_previous_write_origin():
    assert resolve([event(3, action=5, provider=None), event(2, action=5, provider=None), event(1)]) == {
        'source': 'subhd', 'source_type': 'provider'}
    assert resolve([event(action=5, provider=None)]) == UNKNOWN


def test_delete_is_a_barrier_even_if_the_indexed_path_exists_again_after_a_sync():
    assert resolve([event(3, action=5), event(2, action=0, provider=None), event(1)]) == UNKNOWN


def test_new_write_after_deletion_restores_only_the_new_origin():
    assert resolve([event(1), event(2, action=0, provider=None), event(3, provider='assrt')]) == {
        'source': 'assrt', 'source_type': 'provider'}


def test_deleting_a_different_full_path_does_not_invalidate_the_current_file():
    assert resolve([event(2, action=0, subtitles_path='/media/Film.zt.srt'), event(1)]) == {
        'source': 'subhd', 'source_type': 'provider'}


@pytest.mark.parametrize('action', [1, 2, 3])
@pytest.mark.parametrize('provider', [None, '', '   ', 123])
def test_new_download_without_provider_does_not_reuse_a_stale_older_provider(action, provider):
    assert resolve([event(1), event(2, action=action, provider=provider)]) == UNKNOWN


def test_upload_is_distinct_from_manual_download_and_sync_does_not_replace_it():
    assert resolve([event(1), event(2, action=4, provider='manual'), event(3, action=5)]) == {
        'source': 'manual', 'source_type': 'uploaded'}


def test_translation_overwrites_provider_origin_without_guessing_an_llm_model():
    assert resolve([event(1), event(2, action=6, provider=None)]) == {
        'source': None, 'source_type': 'translated'}


@pytest.mark.parametrize('action', [1, 2, 3])
def test_an_external_file_downloaded_from_embedded_subtitles_is_extracted(action):
    assert resolve([event(action=action, provider='embeddedsubtitles')]) == {
        'source': 'embeddedsubtitles', 'source_type': 'extracted'}


def test_provider_names_are_normalized_to_their_slug():
    assert resolve([event(provider=' SubHD ')]) == {'source': 'subhd', 'source_type': 'provider'}


def test_language_hi_and_forced_metadata_do_not_override_an_exact_path_match():
    subtitle = {'path': SUBTITLE, 'code2': 'zh', 'hi': False, 'forced': False}
    row = event(language='en:hi', hearing_impaired=True, forced=True)
    assert resolve_subtitle_source(subtitle, [row], media_id=49, video_path=VIDEO) == {
        'source': 'subhd', 'source_type': 'provider'}


def test_no_source_is_guessed_from_an_llm_filename_or_renamed_subtitle():
    assert resolve([], path='/media/Film.llm.zh.ass') == UNKNOWN
    assert resolve([event()], path='/media/Film.zh.hi.srt') == UNKNOWN
    assert resolve([event(subtitles_path='/media/Film.en.srt')]) == UNKNOWN


@pytest.mark.parametrize('track_id', [0, 17])
@pytest.mark.parametrize('path', [None, ''])
def test_explicit_embedded_track_id_including_zero_identifies_embedded_subtitles(track_id, path):
    assert resolve_subtitle_source({'path': path, 'embedded_track_id': track_id}, []) == {
        'source': 'embedded', 'source_type': 'embedded'}


def test_missing_path_without_explicit_embedded_track_does_not_invent_an_origin():
    assert resolve_subtitle_source({'path': None, 'embedded_track_id': None}, [event()]) == UNKNOWN
    assert resolve_subtitle_source({}, []) == UNKNOWN


def test_an_external_path_has_priority_over_inconsistent_embedded_metadata():
    assert resolve_subtitle_source({'path': SUBTITLE, 'embedded_track_id': 0}, [event()],
                                   media_id=49, video_path=VIDEO) == {
        'source': 'subhd', 'source_type': 'provider'}


def test_unknown_actions_are_skipped_and_do_not_become_download_sources():
    assert resolve([event(2, action=7, provider='unsupported'), event(1)]) == {
        'source': 'subhd', 'source_type': 'provider'}
    assert resolve([event(action=7, provider='unsupported')]) == UNKNOWN


def test_string_action_and_id_values_are_supported_without_treating_booleans_as_downloads():
    assert resolve([event('2', action='2', provider='assrt'), event(1)]) == {
        'source': 'assrt', 'source_type': 'provider'}
    assert resolve([event(action=True)]) == UNKNOWN


def test_invalid_paths_and_non_dictionary_history_rows_are_ignored():
    assert resolve([None, 'not a history row', event(subtitles_path=None)]) == UNKNOWN
    assert resolve([], path=123) == UNKNOWN
    assert resolve([event()], path_mapper=lambda path: None) == UNKNOWN
