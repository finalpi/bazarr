# Offline Audio Timing Filter

The filter checks whether downloaded subtitle intervals correlate with detected
speech before Bazarr saves them. It does not transcribe, translate, call an API,
or download a model. It uses existing FFmpeg, WebRTC VAD and NumPy dependencies.
It is a heuristic timing check, not proof that dialogue belongs to a particular show.

## Enable

In Settings > Subtitles > Audio Timing Filter, enable **Require Audio Timing Match
Before Saving**. Audio Track Index is zero-based among audio streams: `0` selects
the first audio stream. Choose the original-dialogue track rather than commentary.
The filter is disabled by default. Equivalent configuration:

```yaml
audio_validation:
  enabled: true
  audio_stream: 0
```

Automatic searches, upgrades and provider-manual downloads use the same filter.
An automatic search continues with its next candidate after rejection. Manual
downloads return a validation error instead of saving. Existing subtitles and
manual file uploads are not audited or removed. Disabling the filter restores
the original download behavior.

## Algorithm

- Sample five separated three-minute sections of the selected video audio.
- Detect speech in 20 ms frames and aggregate it into 100 ms bins.
- Parse SRT/ASS intervals with pysubs2; ignore ASS comments and events longer
  than 20 seconds (often signs or credits). Overlapping bilingual cues form a union.
- Validate the current timeline at zero offset and rate 1. Displaced windows
  within +/-120 seconds provide background statistics only; they cannot pass validation.
- Compare normalized correlation against the distribution of deliberately
  displaced subtitle windows. Require a distinct peak and at least three matching
  windows, not just a large fraction of time occupied by subtitles.
- Accept when mean correlation >=0.18 and peak z-score >=5, or mean correlation
  >=0.25 and peak z-score >=4; both paths require three window correlations >=0.15.
  These are heuristic thresholds, not probabilities of correctness.

Already aligned candidates pass directly without synchronization. On a timing
failure, run offline ffsubsync with WebRTC VAD against the selected audio track
on a temporary subtitle copy (maximum 120-second offset, 300-second timeout).
Validate its corrected timeline using the same fixed-time thresholds. Only a
passing corrected copy replaces candidate content; failures try the next candidate.
Temporary input/output files are removed. SRT, ASS and SSA synchronization is
supported, preserving the input format. The later automatic synchronization step
is skipped for validated downloads. Existing manual synchronization remains available.
Audio activity is cached by video path, size, modification time, audio track and
algorithm version. The cache contains timing arrays, not audio or transcript text.
The cache retains at most approximately 512 entries; extraction failures back off
for five minutes to avoid repeating a timeout for every candidate.

Unconfirmed results, malformed subtitles, missing audio and tool failures do not
save a subtitle. Short media under 20 minutes, media over four hours, sparse forced
subtitles, heavily edited releases and music-heavy tracks may be rejected despite
correct content. Disable this optional filter for such use cases. Cached activity
can be removed from `<config>/cache/audio-validation` at any time.

## Verification

Run focused tests without starting the application:

```sh
python -m pytest --confcutdir=tests/audio_validation tests/audio_validation -q
```

The read-only benchmark script accepts an existing media library and stores only
measurements and timing caches. It never changes videos or subtitle files:

```sh
PYTHONPATH=libs:custom_libs python scripts/benchmark_audio_validation.py \
  --module bazarr/subtitles/audio_validation.py \
  --media '/tv/Criminal Minds' --cache /tmp/timing-cache \
  --output /tmp/criminal-minds-timing.json --all-wrong-episodes

PYTHONPATH=libs:custom_libs python scripts/benchmark_audio_validation.py \
  --module bazarr/subtitles/audio_validation.py --media /tv \
  --cache /tmp/timing-cache --output /tmp/sitcom-timing.json --suite sitcom
```

Benchmark labels derived from filenames must be checked against content: in the
tested library, Criminal Minds S01E08 and S01E09 subtitle files were swapped.
SRT/ASS duplicates and generated offset variants are not independent episodes.
See `audio-timing-benchmark.md` for results and limitations.

## Docker

The custom image extends the exact LinuxServer image tested here. Build the UI
first, then the image from the repository root:

```sh
cd frontend
npm ci --ignore-scripts
npm run build
cd ..
docker build -f docker/audio-timing/Dockerfile -t finalpi/bazarr:audio-timing-v2 .
```

Merge `docker/audio-timing/compose.override.example.yml` into your existing Compose
configuration. Back up Bazarr's config and database before recreating its container.
The override changes only the image; preserve your existing volumes, environment,
network and ports. Do not enable upstream in-app auto-update on this custom build.

For rollback, disable the timing filter, remove only the custom-image override,
and recreate the service with its previous image. Do not remove media volumes.
