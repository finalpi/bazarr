"""Offline speech-timing filter. It checks timing evidence, not dialogue identity."""

import hashlib
import json
import logging
import math
import os
from pathlib import Path
import subprocess
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


def evaluate_activity(duration, starts, activity, intervals):
    """Search one global offset/rate, then check independent-window evidence."""
    valid = np.array([0.08 < sample.mean() < 0.92 and sample.std() > 0.12 for sample in activity])
    if valid.sum() < 3:
        return {'accepted': False, 'reason': 'insufficient_speech', 'windows': int(valid.sum())}
    offsets = MAX_OFFSET - np.arange(MAX_OFFSET * HZ * 2 + 1) / HZ
    candidates = []
    for rate in RATES:
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
        best = int(combined.argmax())
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


def validate_download(video, subtitle):
    from app.config import settings
    if not settings.audio_validation.enabled:
        return True
    try:
        from app.get_args import args
        from utilities.binaries import get_binary
        intervals = parse_intervals(subtitle.text)
        duration, starts, activity = extract_activity(
            video.original_path, os.path.join(args.config_dir, 'cache', 'audio-validation'),
            get_binary, settings.audio_validation.audio_stream)
        result = evaluate_activity(duration, starts, activity, intervals)
        logging.info('BAZARR Audio timing validation for %s: %s', video.original_path, json.dumps(result))
        return result['accepted']
    except Exception as error:
        logging.warning('BAZARR Audio timing validation unavailable (%s); subtitle not saved', type(error).__name__)
        return False
