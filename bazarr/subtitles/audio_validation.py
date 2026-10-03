"""Offline speech-timing filter. It checks timing evidence, not dialogue identity."""

import hashlib
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
VALIDATION_SECONDS = 180


def _safe_log_value(value):
    if value is None:
        return None
    text = str(value)
    text = re.sub(r'https?://\S+', '[redacted-url]', text, flags=re.I)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', text)
    text = re.sub(r'(?i)\b(token|api[_-]?key|password|cookie|authorization|secret|skey)\s*[=:]\s*\S+',
                  r'\1=[redacted]', text)
    return re.sub(r'\s+', ' ', text).strip()[:500]


def _timeout(deadline, limit):
    remaining = limit if deadline is None else min(limit, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError('Timing validation budget exhausted')
    return remaining


def _progress(callback, stage, details=None):
    if callback is not None:
        try:
            callback(stage, details or {})
        except Exception as error:
            logging.warning('BAZARR Timing progress callback failed at %s (%s)', stage, type(error).__name__)


def _run(command, timeout=90):
    return subprocess.run(command, capture_output=True, check=True, timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout


def _run_with_budget(command, deadline=None, limit=90):
    if deadline is None:
        return _run(command)
    return _run(command, timeout=_timeout(deadline, limit))


def select_audio_reference(video, binary, deadline=None):
    """Select a dialogue reference from the media's original language, not track order."""
    from .audio_reference import select_original_audio_stream

    probe = json.loads(_run_with_budget([
        binary('ffprobe'), '-v', 'error', '-select_streams', 'a', '-show_entries',
        'format=duration:stream=index,codec_type:stream_tags=language,title:stream_disposition=default,comment,visual_impaired,dub',
        '-of', 'json', str(video.original_path)], deadline, 15))
    try:
        duration = float(probe.get('format', {}).get('duration'))
        if math.isfinite(duration) and duration > 0:
            video.duration = duration
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        reference = select_original_audio_stream(
            probe.get('streams', []), getattr(video, 'original_language', None))
    except ValueError:
        logging.warning('BAZARR No original dialogue audio reference for %s (original language: %s)',
                        video.original_path, getattr(video, 'original_language', None))
        raise
    logging.info('BAZARR Original audio reference for %s: %s',
                 _safe_log_value(video.original_path),
                 json.dumps({key: _safe_log_value(value) if isinstance(value, str) else value
                             for key, value in reference.items()}))
    return reference


def extract_activity(path, cache_dir, binary, audio_stream=0, deadline=None, progress=None):
    started = time.monotonic()
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    identity = [VERSION, str(path), stat.st_size, stat.st_mtime_ns, audio_stream]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / ('timing-' + key + '.npz')
    lock = LOCKS[int(key[:2], 16) % len(LOCKS)]
    if not lock.acquire(timeout=_timeout(deadline, VALIDATION_SECONDS)):
        raise TimeoutError('Timed out waiting for audio cache')
    try:
        if FAILURES.get(key, 0) > time.monotonic():
            raise ValueError('Audio extraction temporarily unavailable')
        if cache.exists():
            try:
                with np.load(cache, allow_pickle=False) as saved:
                    duration, starts, activity = float(saved['duration']), saved['starts'], saved['activity']
                    if not math.isfinite(duration) or not 1200 <= duration <= 14400 or \
                            starts.shape != (len(FRACTIONS),) or \
                            activity.shape != (len(FRACTIONS), WINDOW_SECONDS * HZ) or \
                            not np.isfinite(starts).all() or not np.isfinite(activity).all():
                        raise ValueError('Invalid timing cache')
                    logging.info('BAZARR Audio timing cache hit for %s: audio_stream=%s duration=%.3f windows=%s elapsed=%.3fs',
                                 path, audio_stream, duration, len(starts), time.monotonic() - started)
                    _progress(progress, 'audio_cache', {'cache_hit': True, 'total': len(starts)})
                    return duration, starts, activity
            except (OSError, ValueError, KeyError):
                logging.warning('BAZARR Audio timing cache invalid for %s; resampling audio_stream=%s', path, audio_stream)
        logging.info('BAZARR Audio timing cache miss for %s: audio_stream=%s size_bytes=%s', path, audio_stream, stat.st_size)
        _progress(progress, 'audio_cache', {'cache_hit': False, 'total': len(FRACTIONS)})
        # Back off on extraction errors instead of timing out once per provider candidate.
        FAILURES[key] = time.monotonic() + 300
        for expired in [item for item, deadline in list(FAILURES.items()) if deadline < time.monotonic()]:
            FAILURES.pop(expired, None)
        probe = json.loads(_run_with_budget([binary('ffprobe'), '-v', 'error', '-show_entries',
                                 'format=duration', '-of', 'json', str(path)], deadline, 15))
        duration = float(probe['format']['duration'])
        if not math.isfinite(duration) or not 1200 <= duration <= 14400:
            raise ValueError('Duration outside timing-filter range (20 minutes to 4 hours)')
        starts = np.array([round((duration - WINDOW_SECONDS) * fraction, 1) for fraction in FRACTIONS])
        activity = []
        for index, start in enumerate(starts, 1):
            sampled = time.monotonic()
            _progress(progress, 'audio_sample', {'index': index, 'total': len(starts), 'start_seconds': float(start)})
            logging.info('BAZARR Audio sample started for %s: window=%s/%s start=%.3f duration=%s audio_stream=%s remaining=%.3fs',
                         path, index, len(starts), start, WINDOW_SECONDS, audio_stream, _timeout(deadline, VALIDATION_SECONDS))
            raw = _run_with_budget([binary('ffmpeg'), '-nostdin', '-v', 'error', '-ss', str(start),
                        '-i', str(path), '-map', '0:a:%d' % audio_stream, '-t', str(WINDOW_SECONDS),
                        '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', '-c:a', 'pcm_s16le', 'pipe:1'], deadline, 35)
            # WebRTC supports 20 ms PCM frames; average five frames into each 100 ms bin.
            expected = WINDOW_SECONDS * 16000 * 2
            if len(raw) < expected - 3200:
                raise ValueError('Incomplete audio sample')
            raw = raw[:expected].ljust(expected, b'\0')
            vad = webrtcvad.Vad(3)
            frames = [vad.is_speech(raw[i:i + 640], 16000) for i in range(0, expected, 640)]
            activity.append(np.asarray(frames, dtype=np.float64).reshape(-1, 5).mean(axis=1))
            logging.info('BAZARR Audio sample finished for %s: window=%s/%s bytes=%s speech_ratio=%.4f elapsed=%.3fs',
                         path, index, len(starts), len(raw), activity[-1].mean(), time.monotonic() - sampled)
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
        logging.info('BAZARR Audio timing cache stored for %s: audio_stream=%s windows=%s elapsed=%.3fs',
                     path, audio_stream, len(starts), time.monotonic() - started)
        return duration, starts, activity
    finally:
        lock.release()


def parse_intervals(text):
    if not isinstance(text, str) or not text or len(text) > 2_000_000:
        raise ValueError('Missing or excessive subtitle content')
    from subliminal_patch.subtitle_coverage import _subtitle_text_for_parse
    cues = pysubs2.SSAFile.from_string(_subtitle_text_for_parse(text))
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



def apply_timing_transform(text, rate, offset_seconds):
    """Apply only the measured linear correction, preserving ASS/SSA styles."""
    if not math.isfinite(rate) or not math.isfinite(offset_seconds) or rate <= 0:
        raise ValueError('Invalid timing correction')
    subtitles = pysubs2.SSAFile.from_string(text)
    format_ = subtitles.format
    if format_ not in ('srt', 'ass', 'ssa'):
        raise ValueError('Unsupported synchronization format')
    retained = []
    for cue in subtitles:
        cue.start = round(cue.start * rate + offset_seconds * 1000)
        cue.end = round(cue.end * rate + offset_seconds * 1000)
        if cue.end > 0 and cue.end > cue.start:
            cue.start = max(0, cue.start)
            retained.append(cue)
    subtitles.events = retained
    corrected = subtitles.to_string(format_)
    if len(corrected) > 2_000_000:
        raise ValueError('Excessive synchronized subtitle content')
    return corrected


def validate_download(video, subtitle, progress=None):
    from app.config import settings
    if not settings.audio_validation.enabled:
        return True
    from subliminal_patch.subtitle_coverage import subtitle_coverage

    started = time.monotonic()
    deadline = started + VALIDATION_SECONDS
    context = {'video': _safe_log_value(video.original_path),
               'provider': _safe_log_value(getattr(subtitle, 'provider_name', None)),
               'subtitle_id': _safe_log_value(getattr(subtitle, 'id', '')),
               'member': _safe_log_value(getattr(subtitle, 'selected_archive_member', None)),
               'language': _safe_log_value(getattr(subtitle, 'language', ''))}
    subtitle.audio_timing_validated = False
    subtitle.audio_timing_failure_reason = None
    subtitle.audio_timing_failure_detail = None
    stage = 'subtitle_coverage'

    def rejected(reason, detail=None):
        subtitle.audio_timing_failure_reason = reason
        subtitle.audio_timing_failure_detail = detail or reason
        logging.warning('BAZARR Timing validation rejected: context=%s stage=%s reason=%s detail=%s elapsed=%.3fs',
                        json.dumps(context, ensure_ascii=False), stage, reason, detail or reason,
                        time.monotonic() - started)
        try:
            from .rejections import record_rejection
            record_rejection(video, subtitle, reason, detail)
        except Exception as error:
            logging.warning('BAZARR Unable to persist subtitle rejection: context=%s error_type=%s',
                            json.dumps(context, ensure_ascii=False), type(error).__name__)
        _progress(progress, 'timing_rejected', {'reason': reason})
        return False

    def check_coverage(duration):
        nonlocal stage
        stage = 'subtitle_coverage'
        result = subtitle_coverage(text, duration,
                                   forced=bool(getattr(getattr(subtitle, 'language', None), 'forced', False)),
                                   partial=getattr(subtitle, 'is_partial', False) is True)
        subtitle.audio_timing_coverage = result
        logging.info('BAZARR Subtitle coverage: context=%s result=%s elapsed=%.3fs',
                     json.dumps(context, ensure_ascii=False), json.dumps(result, ensure_ascii=False),
                     time.monotonic() - started)
        _progress(progress, 'subtitle_coverage', result)
        return result

    try:
        from app.get_args import args
        from utilities.binaries import get_binary
        text = subtitle.text
        if not check_coverage(getattr(video, 'duration', None))['accepted']:
            return rejected(subtitle.audio_timing_coverage['reason'])
        intervals = parse_intervals(text)
        stage = 'original_audio'
        _progress(progress, stage)
        reference = select_audio_reference(video, get_binary, deadline=deadline)
        audio_stream = reference['audio_index']
        if not check_coverage(getattr(video, 'duration', None))['accepted']:
            return rejected(subtitle.audio_timing_coverage['reason'])
        stage = 'audio_sample'
        duration, starts, activity = extract_activity(
            video.original_path, os.path.join(args.config_dir, 'cache', 'audio-validation'),
            get_binary, audio_stream, deadline=deadline, progress=progress)
        if not check_coverage(duration)['accepted']:
            return rejected(subtitle.audio_timing_coverage['reason'])
        stage = 'timing_check'
        _timeout(deadline, VALIDATION_SECONDS)
        _progress(progress, stage)
        initial_result = evaluate_activity(duration, starts, activity, intervals)
        logging.info('BAZARR Audio timing validation before sync for %s: %s elapsed=%.3fs',
                     video.original_path, json.dumps(initial_result), time.monotonic() - started)
        if initial_result['accepted']:
            _timeout(deadline, VALIDATION_SECONDS)
            subtitle.audio_timing_validated = True
            _progress(progress, 'timing_accepted')
            return True
        if initial_result['reason'] == 'insufficient_speech':
            return rejected('insufficient_speech')

        # Search the existing original-audio windows. No repeated full-film reads,
        # subtitle-track extraction, or long-lived ffsubsync/ffmpeg descendants.
        stage = 'timing_search'
        _progress(progress, stage)
        _timeout(deadline, VALIDATION_SECONDS)
        correction = evaluate_activity(duration, starts, activity, intervals, search=True)
        logging.info('BAZARR Sampled original-audio timing search for %s: %s elapsed=%.3fs',
                     video.original_path, json.dumps(correction), time.monotonic() - started)
        if not correction['accepted']:
            return rejected('timing_not_confirmed')
        candidate = apply_timing_transform(text, correction['rate'], correction['offset_seconds'])
        stage = 'timing_verify'
        _progress(progress, stage)
        _timeout(deadline, VALIDATION_SECONDS)
        corrected_intervals = parse_intervals(candidate)
        result = evaluate_activity(duration, starts, activity, corrected_intervals)
        logging.info('BAZARR Audio timing validation after sampled sync for %s: %s offset_seconds=%.3f rate=%.8f elapsed=%.3fs',
                     video.original_path, json.dumps(result), correction['offset_seconds'], correction['rate'],
                     time.monotonic() - started)
        if not result['accepted']:
            return rejected('corrected_timing_not_confirmed')
        _timeout(deadline, VALIDATION_SECONDS)
        subtitle.content = candidate.encode('utf-8')
        subtitle.encoding = subtitle._guessed_encoding = 'utf-8'
        subtitle.audio_timing_validated = True
        _progress(progress, 'timing_accepted')
        logging.info('BAZARR Timing validation accepted after sampled sync: context=%s elapsed=%.3fs',
                     json.dumps(context, ensure_ascii=False), time.monotonic() - started)
        return True
    except (TimeoutError, subprocess.TimeoutExpired):
        return rejected('validation_timeout', 'Original-audio timing validation exceeded its time limit')
    except Exception as error:
        # Exception messages can contain external provider URLs; log safe context
        # and the exception type, never subtitle dialogue or signed URLs.
        return rejected('validation_unavailable', type(error).__name__)
