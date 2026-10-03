"""Resolve indexed subtitle origins from exact-path history, without filesystem I/O."""

import posixpath


def _unknown():
    return {'source': None, 'source_type': 'unknown'}


def _path_key(path, path_mapper):
    if not isinstance(path, str) or not path:
        return None
    if path_mapper is not None:
        path = path_mapper(path)
    if not isinstance(path, str) or not path:
        return None
    return posixpath.normpath(path.replace('\\', '/'))


def _integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return None


def _history_order(entry):
    index, row = entry
    identifier = _integer(row.get('id'))
    return (identifier is not None, identifier or 0, -index)


def resolve_subtitle_source(subtitle, history, media_id=None, video_path=None, path_mapper=None):
    """Return the latest attributable origin; sync preserves it and deletion invalidates it.

    Callers provide history dictionaries for the relevant media. Numbered rows
    are ordered by insertion ID, followed by unnumbered rows in their input order.
    Both history and indexed paths are compared in the same mapped coordinate
    space, preserving case even when the resolver runs on Windows.
    """
    path = subtitle.get('path')
    if not path:
        if subtitle.get('embedded_track_id') is not None:
            return {'source': 'embedded', 'source_type': 'embedded'}
        return _unknown()

    current_path = _path_key(path, path_mapper)
    if current_path is None:
        return _unknown()
    current_video = _path_key(video_path, path_mapper) if video_path else None
    if video_path and current_video is None:
        return _unknown()

    rows = [(index, row) for index, row in enumerate(history or []) if isinstance(row, dict)]
    for _, row in sorted(rows, key=_history_order, reverse=True):
        if 'media_id' in row and row['media_id'] != media_id:
            continue
        if _path_key(row.get('subtitles_path'), path_mapper) != current_path:
            continue
        action = _integer(row.get('action'))
        if action == 5:
            continue
        if action == 0:
            return _unknown()
        if action not in (1, 2, 3, 4, 6):
            continue
        # A newer write at this same media/path supersedes older origins even
        # when its video identity is missing or cannot be confirmed.
        if current_video is not None and _path_key(row.get('video_path'), path_mapper) != current_video:
            return _unknown()
        if action in (1, 2, 3):
            provider = row.get('provider')
            if not isinstance(provider, str) or not provider.strip():
                return _unknown()
            provider = provider.strip().lower()
            return {'source': provider,
                    'source_type': 'extracted' if provider == 'embeddedsubtitles' else 'provider'}
        if action == 4:
            return {'source': 'manual', 'source_type': 'uploaded'}
        if action == 6:
            return {'source': None, 'source_type': 'translated'}

    return _unknown()
