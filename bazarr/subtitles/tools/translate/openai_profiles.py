"""Resolve the active OpenAI-compatible translation API profile."""


PROFILE_IDS = ('default', 'profile_1', 'profile_2', 'profile_3')


def get_active_openai_profile(translator=None):
    if translator is None:
        from app.config import settings
        translator = settings.translator

    active = str(getattr(translator, 'openai_active_profile', 'default') or 'default')
    if active not in PROFILE_IDS:
        active = 'default'

    if active == 'default':
        return {
            'id': active,
            'name': 'Default',
            'base_url': str(translator.openai_base_url).strip(),
            'api_key': str(translator.openai_api_key).strip(),
            'model': str(translator.openai_model).strip(),
        }

    prefix = f'openai_{active}_'
    return {
        'id': active,
        'name': str(getattr(translator, prefix + 'name', '') or '').strip() or active.replace('_', ' ').title(),
        'base_url': str(getattr(translator, prefix + 'base_url', '') or '').strip(),
        'api_key': str(getattr(translator, prefix + 'api_key', '') or '').strip(),
        'model': str(getattr(translator, prefix + 'model', '') or '').strip(),
    }
