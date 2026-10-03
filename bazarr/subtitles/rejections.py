"""Durable, media-version-scoped rejection evidence for provider subtitles."""

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re

from app.database import TableSubtitleRejections, engine, insert, select, delete


RECORDABLE_REASONS = frozenset({
    'timing_mismatch',
    'obvious_fragment', 'parse_loss', 'script_mismatch',
})
_KEY_COLUMNS = ('media_type', 'media_id', 'video_fingerprint', 'provider',
                'subtitle_id_digest', 'language', 'forced')
_DETAIL_KEYS = frozenset({
    'reason', 'stage', 'score', 'peak_z', 'offset_seconds', 'rate', 'windows', 'window_scores',
    'coverage_class', 'total_cues', 'dialogue_cues', 'declared_cues', 'parse_ratio',
    'first_seconds', 'last_seconds', 'span_seconds', 'span_ratio', 'occupied_bins',
    'duration_seconds', 'elapsed_seconds', 'exception_type',
})


def _safe_text(value, limit=1024):
    if value is None:
        return None
    if isinstance(value, dict):
        value = json.dumps({key: item for key, item in value.items() if key in _DETAIL_KEYS},
                           ensure_ascii=False, default=str)
    else:
        value = str(value)
    if '-->' in value or ('[Events]' in value and 'Dialogue:' in value):
        return '[subtitle content omitted]'
    value = value.replace('\\/', '/')
    value = re.sub(r'(?i)(?:https?|socks5h?)://[^\s\"\'<>]+', '[URL omitted]', value)
    value = re.sub(r'(?im)\b(?:authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+',
                   '[credential omitted]', value)
    value = re.sub(r'(?i)\b(?:api[_-]?key|token|password|signature|secret|skey)\b[\"\']?\s*[:=]\s*'
                   r'(?:\"[^\"]*\"|\'[^\']*\'|[^\s,;]+)', '[credential omitted]', value)
    value = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*', '[credential omitted]', value)
    value = re.sub(r'\bsk-[A-Za-z0-9_-]{16,}\b', '[credential omitted]', value)
    return re.sub(r'\s+', ' ', value).strip()[:limit] or None


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
        return value if value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _video_identity(video):
    episode_id = _positive_id(getattr(video, 'sonarrEpisodeId', None))
    movie_id = _positive_id(getattr(video, 'radarrId', None))
    if bool(episode_id) == bool(movie_id):
        return None
    original_path = getattr(video, 'original_path', None)
    if not original_path:
        return None
    try:
        path = Path(original_path).resolve(strict=True)
        stat = path.stat()
        if not path.is_file():
            return None
    except (OSError, ValueError, TypeError):
        return None
    original_language = str(getattr(video, 'original_language', '') or '').strip().casefold()
    payload = [str(path), stat.st_size, stat.st_mtime_ns, original_language]
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode('utf-8')).hexdigest()
    return {
        'media_type': 'episode' if episode_id else 'movie',
        'media_id': episode_id or movie_id, 'video_fingerprint': fingerprint,
        'video_path': str(path), 'video_size': stat.st_size, 'video_mtime_ns': stat.st_mtime_ns,
        'original_language': _safe_text(original_language, 128) or '',
    }


def _language_key(language):
    if language is None:
        return None
    alpha3 = str(getattr(language, 'alpha3', '') or '').casefold()
    raw_code = str(getattr(language, 'basename', None) or language).strip().replace('_', '-').casefold()
    parts = raw_code.split(':')
    # Strip only real language modifiers; a URL must not become the code "http".
    if any(suffix not in ('hi', 'forced') for suffix in parts[1:]) or not re.fullmatch(
            r'[a-z0-9-]{1,32}', parts[0]):
        return None
    code = parts[0]
    country = str(getattr(language, 'country', '') or '').upper()
    script = str(getattr(language, 'script', '') or '').casefold()
    if alpha3 in ('zho', 'chi', 'zht') or code in (
            'zh', 'zh-cn', 'zh-hans', 'zho', 'chi', 'chs', 'zt', 'zht', 'cht', 'zh-tw', 'zh-hant'):
        return 'zt' if country in ('TW', 'HK', 'MO') or script == 'hant' or code in (
            'zt', 'zht', 'cht', 'zh-tw', 'zh-hant') else 'zh'
    return code if re.fullmatch(r'[a-z0-9-]{1,32}', code) else None


def _subtitle_identity(subtitle):
    provider = str(getattr(subtitle, 'provider_name', '') or '').strip().casefold()
    if not re.fullmatch(r'[a-z0-9_.-]{1,128}', provider):
        return None
    try:
        raw_id = subtitle.id
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        return None
    if raw_id is None or not str(raw_id):
        return None
    language = _language_key(getattr(subtitle, 'language', None))
    if language is None:
        return None
    forced = getattr(getattr(subtitle, 'language', None), 'forced', False) is True
    return {'provider': provider, 'subtitle_id_digest': hashlib.sha256(str(raw_id).encode('utf-8')).hexdigest(),
            'language': language, 'forced': forced}


def _record_key(values):
    return tuple(values[column] for column in _KEY_COLUMNS)


def _where(identity):
    return tuple(getattr(TableSubtitleRejections, column) == identity[column] for column in _KEY_COLUMNS)


def _info(row):
    timestamp = row['timestamp']
    if isinstance(timestamp, datetime):
        timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
        timestamp = timestamp.isoformat()
    return {'id': row['id'], 'reason': row['reason'], 'detail': _safe_text(row['detail']),
            'member': _safe_text(row['member'], 512), 'timestamp': timestamp}


def _log(action, identity, info):
    logging.info('BAZARR Subtitle rejection %s: %s', action, json.dumps({
        'id': info['id'], 'media_type': identity['media_type'], 'media_id': identity['media_id'],
        'video_fingerprint': identity['video_fingerprint'][:12], 'provider': identity['provider'],
        'subtitle_id_digest': identity['subtitle_id_digest'][:12],
        'language': identity['language'], 'forced': identity['forced'], 'reason': info['reason'],
    }))


def load_rejections(video):
    """Read active mismatch evidence; retain older inconclusive rows for audit."""
    identity = _video_identity(video)
    if identity is None:
        return {}
    table = TableSubtitleRejections
    with engine.connect() as connection:
        rows = connection.execute(select(table).where(
            table.media_type == identity['media_type'], table.media_id == identity['media_id'],
            table.video_fingerprint == identity['video_fingerprint'],
            table.reason.in_(RECORDABLE_REASONS))).mappings().all()
    return {_record_key(row): _info(row) for row in rows}


def get_rejection(video, subtitle, records=None):
    """Look up only this media, physical file version, provider ID and language."""
    video_identity, subtitle_identity = _video_identity(video), _subtitle_identity(subtitle)
    if video_identity is None or subtitle_identity is None:
        return None
    identity = dict(video_identity, **subtitle_identity)
    if records is None:
        records = load_rejections(video)
    info = records.get(_record_key(identity))
    # Callers may hold records loaded before the reason policy changed. A lack
    # of timing evidence must never become proof of a persistent mismatch.
    if info and info.get('reason') not in RECORDABLE_REASONS:
        return None
    if info:
        _log('skip', identity, info)
    return info


def record_rejection(video, subtitle, reason, detail=None):
    """Persist only demonstrated mismatch evidence, never transport or cache errors."""
    if reason not in RECORDABLE_REASONS:
        return None
    video_identity, subtitle_identity = _video_identity(video), _subtitle_identity(subtitle)
    if video_identity is None or subtitle_identity is None:
        return None
    identity = dict(video_identity, **subtitle_identity)
    values = dict(identity, reason=reason, detail=_safe_text(detail),
                  member=_safe_text(getattr(subtitle, 'selected_archive_member', None), 512),
                  timestamp=datetime.now(timezone.utc))
    statement = insert(TableSubtitleRejections).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[getattr(TableSubtitleRejections, column) for column in _KEY_COLUMNS],
        set_={column: getattr(statement.excluded, column) for column in ('reason', 'detail', 'member', 'timestamp')})
    with engine.begin() as connection:
        row = connection.execute(statement.returning(
            TableSubtitleRejections.id, TableSubtitleRejections.reason, TableSubtitleRejections.detail,
            TableSubtitleRejections.member, TableSubtitleRejections.timestamp)).mappings().one()
    info = _info(row)
    _log('record', identity, info)
    return info


def clear_rejection(video, subtitle):
    """Restore one current-file candidate without clearing other media/languages."""
    video_identity, subtitle_identity = _video_identity(video), _subtitle_identity(subtitle)
    if video_identity is None or subtitle_identity is None:
        return False
    identity = dict(video_identity, **subtitle_identity)
    with engine.begin() as connection:
        row = connection.execute(delete(TableSubtitleRejections).where(*_where(identity)).returning(
            TableSubtitleRejections.id, TableSubtitleRejections.reason, TableSubtitleRejections.detail,
            TableSubtitleRejections.member, TableSubtitleRejections.timestamp)).mappings().first()
    if row is None:
        return False
    _log('clear', identity, _info(row))
    return True
