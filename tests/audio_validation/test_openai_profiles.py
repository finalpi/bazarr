from pathlib import Path
from types import SimpleNamespace
import runpy


ROOT = Path(__file__).resolve().parents[2]
get_active_openai_profile = runpy.run_path(
    str(ROOT / 'bazarr/subtitles/tools/translate/openai_profiles.py'))['get_active_openai_profile']


def translator_settings(**overrides):
    values = {
        'openai_active_profile': 'default',
        'openai_base_url': 'http://localhost:11434/v1',
        'openai_api_key': '',
        'openai_model': 'qwen',
        'openai_profile_1_name': 'Gemini',
        'openai_profile_1_base_url': 'https://example.test/v1',
        'openai_profile_1_api_key': 'profile-key',
        'openai_profile_1_model': 'gemini-test',
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_resolves_default_and_selected_openai_profiles():
    default = get_active_openai_profile(translator_settings())
    assert default == {
        'id': 'default',
        'name': 'Default',
        'base_url': 'http://localhost:11434/v1',
        'api_key': '',
        'model': 'qwen',
    }

    selected = get_active_openai_profile(
        translator_settings(openai_active_profile='profile_1'))
    assert selected == {
        'id': 'profile_1',
        'name': 'Gemini',
        'base_url': 'https://example.test/v1',
        'api_key': 'profile-key',
        'model': 'gemini-test',
    }


def test_invalid_profile_falls_back_to_default():
    selected = get_active_openai_profile(
        translator_settings(openai_active_profile='missing'))
    assert selected['id'] == 'default'
    assert selected['model'] == 'qwen'
