# Audio Timing Benchmark (2026-09-06)

These are historical results for the original offset-search filter, not the
current validate/synchronize/revalidate pipeline. S01E22 SRT and ASS were later
manually reported to have incorrect timing: correct show content did not imply
correct alignment. See the pipeline regression section below.

Measurements were made inside the user's existing Bazarr Docker container on a
Mac, reading videos over its existing NFS mounts. No media files were changed by
these benchmarks. Reports contain filenames and scores only, not subtitle dialogue.

## Calibration

### Pipeline Regression (2026-09-07)

The new pipeline checks zero offset/rate 1 first, runs ffsubsync only on failure,
then checks the corrected timeline with the unchanged correlation thresholds.
`scripts/benchmark_audio_alignment.py` exercised real Subtitle objects and the
production save function in an isolated output directory on the Mac:

- S01E22 ASS: corrected, accepted, saved timing verified.
- S01E22 SRT: corrected candidate still rejected.
- S01E03 ASS: corrected, accepted, saved timing verified.
- S01E01 known wrong-show subtitle against E22: rejected after attempted sync.
- S01E03 subtitle against E22 (wrong episode): rejected after attempted sync.
- Both accepted outputs passed a second validation without invoking sync.
- Original media-library subtitle files remained unchanged.

The preceding isolated E22 experiment also checked a second set of five audio
windows: corrected ASS passed both sets, while corrected SRT failed both.
These small regressions are not a universal accuracy estimate. The unit suite
contains 24 tests, including fixed-time rejection, conditional sync, timeout
cleanup, no mutation on failure, and suppression of later duplicate auto-sync.

### Original Filter Calibration

The first 120-second-window trial accepted one wrong-show pair out of 77
deliberately mismatched pairs. That version was not deployed. We increased sample
length to 180 seconds and calibrated the correlation/peak/window thresholds using
Criminal Minds season one. The final parameters are documented in
`audio-timing-filter.md` and were frozen before the independent sitcom evaluation.

Final season-one matrix: 22 videos, 501 subtitle/video pairs.

| Group | Accepted | Not Confirmed |
| --- | ---: | ---: |
| Six known wrong-show subtitles crossed with 22 videos | 0 | 132 |
| Same-show wrong episodes (after checking the S08/S09 swap) | 0 | 338 |
| Correct-content candidates (including the swapped files at their correct episode) | 30 | 1 |

The 338 wrong-episode cases include four same-filename SRT/ASS pairs for S08/S09
whose subtitle content identifies the opposite episode. Conversely, two cross-file
pairs, video S08 + subtitle S09 and video S09 + subtitle S08, are correct-content
pairs and passed. The original filename-based benchmark labels are retained in
raw reports; this correction is explicit rather than silently relabeling them.

The unconfirmed correct-content candidate is S01E22's SRT. Its timing correlation
was 0.209 with a weak peak z-score of 3.12 and inconsistent window scores. The
same episode's ASS passed. This is a conservative rejection, not evidence that
the SRT belongs to another show. Existing files were not removed by the filter.

## Independent Evaluation

Parameters were not changed after viewing this evaluation. It used Silicon Valley
S01E01/S01E03 and Veep S01E01/S01E02/S01E03, including English and Chinese subtitles.
Candidates were the English and Chinese HI files, excluding older unverified
Chinese files in the same directories.

| Group | Accepted | Not Confirmed |
| --- | ---: | ---: |
| Original correct candidates | 8 | 0 |
| Correct candidates shifted +35s, -45s, or scaled 25/24 with +10s offset | 24 | 0 |
| Cross-episode/show pairs and known wrong-show files | 0 | 62 |

First extraction per video took 2.94-3.37 seconds on this machine; comparison
median was 0.021 seconds per candidate. These timings depend on storage, CPU,
codec and cache state and are not general performance guarantees.

SRT/ASS copies and generated timing variants are correlated samples, not separate
episodes. This is a small personal-library evaluation, not a universal accuracy
claim. Music, sparse captions, short clips, edits and unusual frame rates remain
limitations. The filter can still accept coincidentally similar speech timing.
