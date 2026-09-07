"""Offline speech-timing filter. It checks timing evidence, not dialogue identity."""

import hashlib
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np
import pysubs2
import webrtcvad


VERSION = 3
HZ = 10
WINDOW_SECONDS = 180
MAX_OFFSET = 120
FRACTIONS = (0.12, 0.31, 0.50, 0.69, 0.88)
RATES = (1.0, 1000 / 1001, 1001 / 1000, 24 / 25, 25 / 24, 24000 / 25025, 25025 / 24000)
LOCKS = [threading.Lock() for _ in range(16)]
FAILURES = {}
TEXT_SUBTITLE_CODECS = {'ass', 'mov_text', 'ssa', 'srt', 'subrip', 'text', 'webvtt'}


def _run(command):
    return subprocess.run(command, capture_output=True, check=True, timeout=90,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout


def extract_activity(path, cache_dir, binary, audio_stream=0):
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    identity = [VERSION, str(path), stat.st_size, stat.st_mtime_ns, audio_stream]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / ('timing-' + key + '.npz')
    with LOCKS[int(key[:2], 16) % len(LOCKS)]:
        if FAILURES.get(key, 0) > time.monotonic():
            raise ValueError('Audio extraction temporarily unavailable')
        if cache.exists():
            try:
                with np.load(cache, allow_pickle=False) as saved:
                    return float(saved['duration']), saved['starts'], saved['activity']
            except (OSError, ValueError, KeyError):
                pass
        # Back off on extraction errors instead of timing out once per provider candidate.
        FAILURES[key] = time.monotonic() + 300
        for expired in [item for item, deadline in list(FAILURES.items()) if deadline < time.monotonic()]:
            FAILURES.pop(expired, None)
        probe = json.loads(_run([binary('ffprobe'), '-v', 'error', '-show_entries',
                                 'format=duration', '-of', 'json', str(path)]))
        duration = float(probe['format']['duration'])
        if not math.isfinite(duration) or not 1200 <= duration <= 14400:
            raise ValueError('Duration outside timing-filter range (20 minutes to 4 hours)')
        starts = np.array([round((duration - WINDOW_SECONDS) * fraction, 1) for fraction in FRACTIONS])
        activity = []
        for start in starts:
            raw = _run([binary('ffmpeg'), '-nostdin', '-v', 'error', '-ss', str(start),
                        '-i', str(path), '-map', '0:a:%d' % audio_stream, '-t', str(WINDOW_SECONDS),
                        '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', '-c:a', 'pcm_s16le', 'pipe:1'])
            # WebRTC supports 20 ms PCM frames; average five frames into each 100 ms bin.
            expected = WINDOW_SECONDS * 16000 * 2
            if len(raw) < expected - 3200:
                raise ValueError('Incomplete audio sample')
            raw = raw[:expected].ljust(expected, b'\0')
            vad = webrtcvad.Vad(3)
            frames = [vad.is_speech(raw[i:i + 640], 16000) for i in range(0, expected, 640)]
            activity.append(np.asarray(frames, dtype=np.float64).reshape(-1, 5).mean(axis=1))
        activity = np.asarray(activity)
        # Limit stale per-video entries without persisting media audio or subtitle text.
        entries = sorted(cache_dir.glob('timing-*.npz'), key=lambda file: file.stat().st_mtime)
        for old in entries[:-511]:
            old.unlink(missing_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache_dir, suffix='.npz', delete=False) as temp:
            temporary = Path(temp.name)
        try:
            np.savez_compressed(temporary, duration=duration, starts=starts, activity=activity)
            os.replace(temporary, cache)
        finally:
            temporary.unlink(missing_ok=True)
        FAILURES.pop(key, None)
        return duration, starts, activity


def parse_intervals(text):
    if not isinstance(text, str) or not text or len(text) > 2_000_000:
        raise ValueError('Missing or excessive subtitle content')
    cues = pysubs2.SSAFile.from_string(text)
    intervals = [(cue.start / 1000, cue.end / 1000) for cue in cues
                 if not cue.is_comment and cue.plaintext.strip() and 0 < cue.end - cue.start <= 20000
                 and cue.start >= 0]
    if len(intervals) < 30:
        raise ValueError('Too few dialogue cues')
    return np.asarray(intervals, dtype=np.float64)


def _correlations(audio, subtitle_window):
    centered = audio - audio.mean()
    n = len(audio)
    sums = np.concatenate(([0.0], np.cumsum(subtitle_window)))
    squares = np.concatenate(([0.0], np.cumsum(subtitle_window ** 2)))
    mean_sum = sums[n:] - sums[:-n]
    variance = np.maximum(squares[n:] - squares[:-n] - mean_sum ** 2 / n, 0)
    denominator = np.sqrt(np.sum(centered ** 2) * variance)
    numerator = np.correlate(subtitle_window, centered, mode='valid')
    return np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 1e-9)


def evaluate_activity(duration, starts, activity, intervals, search=False):
    """Validate actual timing; optional offset/rate search is diagnostic only."""
    valid = np.array([0.08 < sample.mean() < 0.92 and sample.std() > 0.12 for sample in activity])
    if valid.sum() < 3:
        return {'accepted': False, 'reason': 'insufficient_speech', 'windows': int(valid.sum())}
    offsets = MAX_OFFSET - np.arange(MAX_OFFSET * HZ * 2 + 1) / HZ
    candidates = []
    for rate in RATES if search else (1.0,):
        length = int(math.ceil((duration + MAX_OFFSET + WINDOW_SECONDS) * HZ)) + 2
        changes = np.zeros(length + 1)
        scaled = np.rint(intervals * rate * HZ).astype(np.int64)
        left, right = np.clip(scaled[:, 0], 0, length), np.clip(scaled[:, 1], 0, length)
        np.add.at(changes, left, 1)
        np.add.at(changes, right, -1)
        timeline = (np.cumsum(changes[:-1]) > 0).astype(float)
        scores = []
        for start, audio in zip(starts[valid], activity[valid]):
            first = int(round((start - MAX_OFFSET) * HZ))
            indices = first + np.arange(len(audio) + MAX_OFFSET * HZ * 2)
            window = np.zeros(len(indices))
            mask = (indices >= 0) & (indices < length)
            window[mask] = timeline[indices[mask]]
            scores.append(_correlations(audio, window))
        scores = np.array(scores)
        combined = scores.mean(axis=0)
        best = int(combined.argmax()) if search else MAX_OFFSET * HZ
        background = combined[np.abs(offsets - offsets[best]) > 10]
        peak_z = float((combined[best] - background.mean()) / max(background.std(), 0.001))
        candidates.append({'score': float(combined[best]), 'offset_seconds': float(offsets[best]),
                           'rate': rate, 'peak_z': peak_z,
                           'window_scores': scores[:, best].tolist()})
    best = max(candidates, key=lambda candidate: candidate['score'])
    window_scores = np.asarray(best['window_scores'])
    strong_peak = best['score'] >= 0.18 and best['peak_z'] >= 5
    strong_correlation = best['score'] >= 0.25 and best['peak_z'] >= 4
    best['accepted'] = bool((strong_peak or strong_correlation)
                            and (window_scores >= 0.15).sum() >= 3)
    best['reason'] = 'timing_match' if best['accepted'] else 'timing_not_confirmed'
    best['windows'] = int(valid.sum())
    return best


def _activity_from_intervals(duration, starts, intervals):
    length = int(math.ceil(duration * HZ)) + 2
    changes = np.zeros(length + 1)
    scaled = np.rint(intervals * HZ).astype(np.int64)
    left, right = np.clip(scaled[:, 0], 0, length), np.clip(scaled[:, 1], 0, length)
    np.add.at(changes, left, 1)
    np.add.at(changes, right, -1)
    timeline = (np.cumsum(changes[:-1]) > 0).astype(float)
    width = WINDOW_SECONDS * HZ
    windows = []
    for start in starts:
        first = int(round(start * HZ))
        window = np.zeros(width)
        available = timeline[first:min(first + width, len(timeline))]
        window[:len(available)] = available
        windows.append(window)
    return np.asarray(windows)


def evaluate_reference_alignment(duration, starts, reference_intervals, candidate_intervals):
    """Compare cue activity against a subtitle track muxed with the video."""
    reference_activity = _activity_from_intervals(duration, starts, reference_intervals)
    return evaluate_activity(duration, starts, reference_activity, candidate_intervals)


def _normalized_language(value):
    value = str(value or '').lower()
    aliases = {'chi': 'zh', 'zho': 'zh', 'zht': 'zh', 'zh-tw': 'zh', 'eng': 'en'}
    return aliases.get(value, value)


def embedded_reference_candidates(video_path, cache_dir, binary, preferred_language=None):
    """Return extractable full subtitle tracks, preferring the candidate language then English."""
    try:
        probe = json.loads(_run([
            binary('ffprobe'), '-v', 'error', '-select_streams', 's', '-show_entries',
            'stream=index,codec_name,disposition:stream_tags=language,title', '-of', 'json',
            str(video_path)]))
    except Exception:
        logging.debug('BAZARR unable to inspect embedded subtitle references', exc_info=True)
        return []

    preferred = _normalized_language(preferred_language)
    streams = []
    for stream in probe.get('streams', []):
        codec = str(stream.get('codec_name') or '').lower()
        tags = stream.get('tags') or {}
        title = str(tags.get('title') or '').lower()
        disposition = stream.get('disposition') or {}
        if codec not in TEXT_SUBTITLE_CODECS or disposition.get('forced') or any(
                marker in title for marker in ('commentary', 'forced', 'signs')):
            continue
        language = _normalized_language(tags.get('language'))
        priority = 0 if preferred and language == preferred else 1 if language == 'en' else 2
        streams.append((priority, bool(disposition.get('hearing_impaired') or 'sdh' in title),
                        int(stream['index'])))

    from subtitles.embedded_translation import extract_embedded_subtitle
    references = []
    for _, _, track_id in sorted(streams)[:3]:
        try:
            path = extract_embedded_subtitle(video_path, track_id, cache_dir, binary)
            intervals = parse_intervals(Path(path).read_text(encoding='utf-8-sig'))
            if intervals[:, 1].max() - intervals[:, 0].min() < 600:
                raise ValueError('Embedded subtitle reference covers less than 10 minutes')
            references.append((path, intervals, track_id))
        except Exception:
            logging.debug('BAZARR unable to use embedded subtitle track %s', track_id, exc_info=True)
    return references


def align_subtitle(video_path, text, binary, audio_stream=0, reference_path=None):
    """Synchronize a temporary copy; return content only after successful output."""
    subtitles = pysubs2.SSAFile.from_string(text)
    format_ = subtitles.format
    if format_ not in ('srt', 'ass', 'ssa'):
        raise ValueError('Unsupported synchronization format')
    with tempfile.TemporaryDirectory(prefix='bazarr-audio-sync-') as directory:
        source = Path(directory) / ('input.' + format_)
        output = Path(directory) / ('aligned.' + format_)
        source.write_text(text, encoding='utf-8')
        env = os.environ.copy()
        # Bazarr adds vendored libraries to sys.path instead of installing them.
        env['PYTHONPATH'] = os.pathsep.join(str(Path(path).resolve()) for path in sys.path if path)
        reference = str(reference_path or video_path)
        command = [sys.executable, '-m', 'ffsubsync.ffsubsync', reference,
                   '-i', str(source), '-o', str(output)]
        if reference_path is None:
            command.extend(['--vad', 'webrtc', '--reference-stream', '0:a:%d' % audio_stream])
        command.extend(['--max-offset-seconds', str(MAX_OFFSET), '--encoding', 'utf-8',
                        '--output-encoding', 'utf-8', '--ffmpeg-path', str(Path(binary('ffmpeg')).parent)])
        subprocess.run(command,
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=300,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if output.stat().st_size > 2_000_000:
            raise ValueError('Excessive synchronized subtitle content')
        return output.read_text(encoding='utf-8-sig')


def validate_download(video, subtitle):
    from app.config import settings
    if not settings.audio_validation.enabled:
        return True
    try:
        from app.get_args import args
        from utilities.binaries import get_binary
        subtitle.audio_timing_validated = False
        text = subtitle.text
        intervals = parse_intervals(text)
        duration, starts, activity = extract_activity(
            video.original_path, os.path.join(args.config_dir, 'cache', 'audio-validation'),
            get_binary, settings.audio_validation.audio_stream)
        initial_result = evaluate_activity(duration, starts, activity, intervals)
        logging.info('BAZARR Audio timing validation before sync for %s: %s',
                     video.original_path, json.dumps(initial_result))
        if initial_result['accepted']:
            subtitle.audio_timing_validated = True
            return True
        preferred_language = getattr(getattr(subtitle, 'language', None), 'alpha3', None)
        references = embedded_reference_candidates(
            video.original_path, os.path.join(args.config_dir, 'cache', 'embedded-translation'),
            get_binary, preferred_language)
        aligned = None
        for reference_path, reference_intervals, track_id in references:
            try:
                candidate = align_subtitle(video.original_path, text, get_binary,
                                           settings.audio_validation.audio_stream, reference_path)
                result = evaluate_reference_alignment(
                    duration, starts, reference_intervals, parse_intervals(candidate))
                logging.info('BAZARR Embedded subtitle timing validation after sync for %s track %s: %s',
                             video.original_path, track_id, json.dumps(result))
                if result['accepted']:
                    aligned = candidate
                    break
            except Exception:
                logging.debug('BAZARR embedded subtitle alignment failed for track %s', track_id, exc_info=True)
        if aligned is None:
            if initial_result['reason'] == 'insufficient_speech':
                return False
            candidate = align_subtitle(
                video.original_path, text, get_binary, settings.audio_validation.audio_stream)
            result = evaluate_activity(duration, starts, activity, parse_intervals(candidate))
            logging.info('BAZARR Audio timing validation after sync for %s: %s',
                         video.original_path, json.dumps(result))
            if not result['accepted']:
                return False
            aligned = candidate
        # Commit corrected content only after validation; rejected originals never reach saving.
        subtitle.content = aligned.encode('utf-8')
        subtitle.encoding = subtitle._guessed_encoding = 'utf-8'
        subtitle.audio_timing_validated = True
        return True
    except Exception as error:
        logging.warning('BAZARR Audio timing validation unavailable (%s); subtitle not saved', type(error).__name__)
        return False
