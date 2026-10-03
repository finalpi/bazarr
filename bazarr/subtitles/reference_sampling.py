"""Read bounded English subtitle references without extracting a full movie."""

import hashlib
import html
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time


VERSION = 1
WINDOW_SECONDS = 60
MAX_WINDOWS = 5
MAX_CUE_SECONDS = 20
MAX_CUES = 1200
MAX_TEXT_BYTES = 4096
MAX_CACHE_BYTES = 1024 * 1024
MAX_PROBE_BYTES = 8 * 1024 * 1024
DEFAULT_BUDGET_SECONDS = 150
LOCKS = [threading.Lock() for _ in range(16)]
_COMMENTARY = re.compile(r'comment(?:ary|aire)?|director|解说|評論|评论|комментар|kommentar', re.I)
_FORCED = re.compile(r'\bforced\b|强制|強制', re.I)
_SDH = re.compile(r'\b(?:sdh|hi|cc)\b|hearing[ _-]?impaired|closed[ _-]?caption', re.I)


def _remaining(deadline, limit):
    remaining = min(limit, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError('Subtitle reference sampling budget exhausted')
    return remaining


def _progress(callback, stage, details):
    if callback is not None:
        try:
            callback(stage, details)
        except Exception as error:
            logging.warning('BAZARR Reference progress callback failed (%s)', type(error).__name__)


def _run(command, timeout):
    result = subprocess.run(command, capture_output=True, check=True, timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if len(result.stdout) > MAX_PROBE_BYTES:
        raise ValueError('Subtitle reference probe output exceeds limit')
    return json.loads(result.stdout)


def _flag(value):
    return value is True or value == 1 or value == '1'


def _select_stream(streams):
    eligible = []
    for stream in streams:
        if stream.get('codec_type') != 'subtitle' or stream.get('codec_name') not in ('subrip', 'srt'):
            continue
        tags = {str(key).casefold(): value for key, value in (stream.get('tags') or {}).items()}
        if str(tags.get('language') or '').strip().casefold() not in ('eng', 'en', 'english'):
            continue
        title = str(tags.get('title') or '')
        disposition = stream.get('disposition') or {}
        if _flag(disposition.get('forced')) or _flag(disposition.get('comment')) or \
                _COMMENTARY.search(title) or _FORCED.search(title):
            continue
        index = stream.get('index')
        if isinstance(index, bool) or not isinstance(index, (int, str)) or not str(index).isdigit():
            continue
        sdh = _flag(disposition.get('hearing_impaired')) or bool(_SDH.search(title))
        full = bool(re.search(r'\bfull\b|完整|全片', title, re.I))
        eligible.append(((sdh, not full, int(index)), int(index), title))
    return min(eligible, default=None)


def _decode_packet_text(dump):
    """Decode ffprobe's hex column; the right ASCII column is only a preview."""
    if not isinstance(dump, str) or len(dump) > MAX_TEXT_BYTES * 8:
        raise ValueError('Invalid or excessive subtitle packet text')
    payload = bytearray()
    for line in dump.splitlines():
        match = re.match(r'^\s*([0-9a-fA-F]{8,16}):\s+(.*)$', line)
        if not match:
            if line.strip():
                raise ValueError('Invalid subtitle packet hex dump')
            continue
        if int(match[1], 16) != len(payload):
            raise ValueError('Invalid subtitle packet hex offset')
        hex_column = re.split(r'\s{2,}', match[2], maxsplit=1)[0].strip()
        groups = hex_column.split()
        if not groups or any(not re.fullmatch(r'(?:[0-9a-fA-F]{2}){1,2}', group) for group in groups):
            raise ValueError('Invalid subtitle packet hex bytes')
        payload.extend(bytes.fromhex(''.join(groups)))
        if len(payload) > MAX_TEXT_BYTES:
            raise ValueError('Subtitle packet text exceeds limit')
    text = payload.rstrip(b'\0').decode('utf-8-sig')
    text = html.unescape(re.sub(r'<[^>]*>', ' ', text))
    return ' '.join(text.split())


def _packet_cues(packets, stream_index, origin, window, window_index):
    cues = []
    for packet in packets:
        if packet.get('stream_index') != stream_index:
            continue
        try:
            start, length = float(packet['pts_time']) - origin, float(packet['duration_time'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(start) or not math.isfinite(length) or not 0 < length <= MAX_CUE_SECONDS:
            continue
        end = start + length
        if end <= window[0] or start >= window[1]:
            continue
        text = _decode_packet_text(packet.get('data'))
        if text:
            cues.append({'start': start, 'end': end, 'text': text, 'window': window_index})
            if len(cues) > MAX_CUES:
                raise ValueError('Too many subtitle reference cues')
    return cues


def _load_cache(path, identity, windows):
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_CACHE_BYTES:
        return None
    with path.open('rb') as handle:
        raw = handle.read(MAX_CACHE_BYTES + 1)
    if len(raw) > MAX_CACHE_BYTES:
        return None
    saved = json.loads(raw)
    if not isinstance(saved, dict) or saved.get('identity') != identity or saved.get('version') != VERSION:
        return None
    cues = saved.get('cues')
    if not isinstance(cues, list) or not cues or len(cues) > MAX_CUES:
        return None
    checked, seen = [], set()
    for cue in cues:
        if not isinstance(cue, dict):
            return None
        start, end, text, index = (cue.get(key) for key in ('start', 'end', 'text', 'window'))
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float)) or \
                not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end) or \
                not 0 < end - start <= MAX_CUE_SECONDS + 1e-6 or isinstance(index, bool) or \
                not isinstance(index, int) or not 1 <= index <= len(windows) or \
                not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > MAX_TEXT_BYTES:
            return None
        if end <= windows[index - 1][0] or start >= windows[index - 1][1]:
            return None
        key = (start, end, text)
        if key in seen:
            return None
        seen.add(key)
        checked.append({'start': start, 'end': end, 'text': text, 'window': index})
    return checked


def extract_reference_cues(video_path, cache_dir, binary, audio_reference, starts, duration,
                           deadline=None, progress=None):
    """Return original-English cues with video-relative seconds and 1-based windows."""
    started = time.monotonic()
    deadline = started + DEFAULT_BUDGET_SECONDS if deadline is None else deadline
    if not isinstance(audio_reference, dict) or str(audio_reference.get('language') or '').casefold() != 'eng':
        return [], {'reason': 'reference_unavailable'}
    duration = float(duration)
    starts = [float(start) for start in starts]
    if not math.isfinite(duration) or duration <= 0 or not 1 <= len(starts) <= MAX_WINDOWS or any(
            not math.isfinite(start) or not 0 <= start < duration for start in starts):
        raise ValueError('Invalid subtitle reference window plan')
    windows = [[start, min(start + WINDOW_SECONDS, duration)] for start in starts]
    path = Path(video_path).resolve(strict=True)
    stat = path.stat()
    probe = _run([binary('ffprobe'), '-v', 'error', '-select_streams', 's', '-show_entries',
                  'format=start_time:stream=index,codec_type,codec_name:stream_tags=language,title:'
                  'stream_disposition=forced,comment,hearing_impaired', '-of', 'json', str(path)],
                 _remaining(deadline, 15))
    selected = _select_stream(probe.get('streams') or [])
    if selected is None:
        return [], {'reason': 'reference_unavailable'}
    _, stream_index, title = selected
    raw_origin = probe.get('format', {}).get('start_time')
    origin = 0.0 if raw_origin in (None, 'N/A') else float(raw_origin)
    if not math.isfinite(origin):
        raise ValueError('Invalid subtitle reference timeline origin')
    identity = {'path': str(path), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                'stream_index': stream_index, 'windows': windows, 'origin': origin, 'version': VERSION}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode('utf-8')).hexdigest()
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / ('reference-' + key + '.json')
    lock = LOCKS[int(key[:2], 16) % len(LOCKS)]
    metadata = {'reason': 'reference_available', 'stream_index': stream_index, 'language': 'eng',
                'title': title, 'origin': origin, 'window_starts': starts, 'window_seconds': WINDOW_SECONDS,
                'cache_hit': False}
    if not lock.acquire(timeout=_remaining(deadline, DEFAULT_BUDGET_SECONDS)):
        raise TimeoutError('Timed out waiting for subtitle reference cache')
    try:
        try:
            cached = _load_cache(cache, identity, windows)
        except (OSError, ValueError, TypeError, UnicodeError):
            cached = None
        if cached is not None:
            _remaining(deadline, DEFAULT_BUDGET_SECONDS)
            metadata.update(cache_hit=True, cue_count=len(cached))
            _progress(progress, 'reference_cache', {'cache_hit': True})
            logging.info('BAZARR Subtitle reference cache hit: track=%s windows=%s cues=%s elapsed=%.3fs',
                         stream_index, len(windows), len(cached), time.monotonic() - started)
            return cached, metadata
        _progress(progress, 'reference_cache', {'cache_hit': False})
        cues, seen = [], set()
        for index, window in enumerate(windows, 1):
            _progress(progress, 'reference_sample', {'index': index, 'total': len(windows)})
            # Include a possible cue crossing the left edge; read no later than
            # the absolute window end even when the demuxer seeks imprecisely.
            begin = origin + max(0, window[0] - MAX_CUE_SECONDS)
            end = origin + window[1]
            sampled = time.monotonic()
            sample = _run([binary('ffprobe'), '-v', 'error', '-select_streams', str(stream_index),
                           '-read_intervals', '%.6f%%%.6f' % (begin, end), '-show_packets', '-show_data',
                           '-show_entries', 'packet=stream_index,pts_time,duration_time,data',
                           '-of', 'json', str(path)], _remaining(deadline, 25))
            found = _packet_cues(sample.get('packets') or [], stream_index, origin, window, index)
            for cue in found:
                cue_key = (cue['start'], cue['end'], cue['text'])
                if cue_key not in seen:
                    seen.add(cue_key)
                    cues.append(cue)
            if len(cues) > MAX_CUES:
                raise ValueError('Too many subtitle reference cues')
            logging.info('BAZARR Subtitle reference sample: track=%s window=%s/%s cues=%s elapsed=%.3fs',
                         stream_index, index, len(windows), len(found), time.monotonic() - sampled)
        _remaining(deadline, DEFAULT_BUDGET_SECONDS)
        metadata.update(cue_count=len(cues))
        if not cues:
            metadata['reason'] = 'reference_unavailable'
            return cues, metadata
        content = json.dumps({'version': VERSION, 'identity': identity, 'cues': cues},
                             ensure_ascii=False).encode('utf-8')
        if len(content) > MAX_CACHE_BYTES:
            raise ValueError('Subtitle reference cache exceeds limit')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache_dir, prefix='reference-', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
            os.replace(temporary, cache)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        _remaining(deadline, DEFAULT_BUDGET_SECONDS)
        return cues, metadata
    finally:
        lock.release()
