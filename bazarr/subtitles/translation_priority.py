"""Helpers for keeping generated subtitles separate from authoritative subtitles."""

import os


def is_llm_subtitle(path):
    return bool(path and '.llm.' in os.path.basename(path).lower())


def mark_as_llm_subtitle(path, video_path):
    """Insert the source marker before the final language/region suffix."""
    video_root = os.path.splitext(video_path)[0]
    if not path.startswith(video_root):
        raise ValueError('Subtitle path does not belong to the video')
    return video_root + '.llm' + path[len(video_root):]


def counts_as_available_subtitle(item, use_embedded_subs):
    """Return whether a subtitle should stop the normal provider search."""
    if item.get('path'):
        return not is_llm_subtitle(item['path'])
    if item.get('embedded_track_id') is not None:
        return bool(use_embedded_subs and item.get('code2') not in ('zh', 'zt'))
    return False


def append_original_language(languages, original_code2, existing):
    """Add an external original-language search when no usable source already exists."""
    if not original_code2 or original_code2 in ('zh', 'zt'):
        return list(languages), None
    available = any(
        item.get('code2') == original_code2 and not item.get('forced') and
        (item.get('embedded_track_id') is not None or item.get('path'))
        for item in existing)
    result = list(languages)
    candidate = (original_code2, 'False', 'False')
    if available or candidate in result:
        return result, None
    result.append(candidate)
    return result, candidate
