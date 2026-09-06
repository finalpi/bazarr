# Audio Timing Benchmark (2026-09-06)

Measurements were made inside the user's existing Bazarr Docker container on a
Mac, reading videos over its existing NFS mounts. No media files were changed by
these benchmarks. Reports contain filenames and scores only, not subtitle dialogue.

## Calibration

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
