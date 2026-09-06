"""Read-only timing benchmark against an existing Criminal Minds season-one folder."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--module', required=True)
    parser.add_argument('--media', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--episodes', default='1,2,3,4,5,6,7,10,13,17,22')
    parser.add_argument('--all-wrong-episodes', action='store_true')
    parser.add_argument('--suite', choices=['criminal-minds', 'sitcom'], default='criminal-minds')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('timing', args.module)
    timing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(timing)
    root = Path(args.media)
    results = []
    if args.suite == 'sitcom':
        return sitcom_benchmark(timing, root, args.cache, args.output)
    for episode in map(int, args.episodes.split(',')):
        video = next(root.glob('*.S01E%02d.*.mp4' % episode))
        started = time.monotonic()
        duration, starts, activity = timing.extract_activity(video, args.cache, shutil.which)
        extraction_seconds = time.monotonic() - started
        paths = [(path, 'existing_same_episode') for path in root.glob(video.stem + '.zh.*')
                 if path.suffix in ('.srt', '.ass')]
        paths += [(path, 'wrong_show') for path in root.glob('*.wrong-show-backup-*')]
        wrong_episodes = range(7, 23) if args.all_wrong_episodes else [10 if episode != 10 else 7]
        for wrong_episode in wrong_episodes:
            if wrong_episode != episode:
                paths += [(path, 'wrong_episode') for path in root.glob('*.S01E%02d.*.zh.srt' % wrong_episode)]
        for subtitle, label in paths:
            text = subtitle.read_text(encoding='utf-8-sig', errors='replace')
            intervals = timing.parse_intervals(text)
            checked = time.monotonic()
            result = timing.evaluate_activity(duration, starts, activity, intervals)
            row = dict(result, video=video.name, subtitle=subtitle.name, label=label,
                       extraction_seconds=round(extraction_seconds, 3),
                       comparison_seconds=round(time.monotonic() - checked, 3))
            results.append(row)
            print(json.dumps(row), flush=True)
        Path(args.output).write_text(json.dumps(results, indent=2), encoding='utf-8')


def sitcom_benchmark(timing, root, cache, output):
    cases = []
    for show, episodes in [('Silicon Valley', [1, 3]), ('Veep', [1, 2, 3])]:
        for episode in episodes:
            video = next((root / show / 'Season 1').glob('*S01E%02d*.mp4' % episode))
            cases.append((show, episode, video))
    negatives = list((root / 'Criminal Minds').glob('*.wrong-show-backup-*'))
    candidates = [(show, episode, path) for show, episode, video in cases
                  for path in video.parent.glob(video.stem + '.*srt')
                  if '.en.' in path.name or '.zh.hi.' in path.name]
    results = []
    for show, episode, video in cases:
        start = time.monotonic()
        duration, starts, activity = timing.extract_activity(video, cache, shutil.which)
        elapsed = time.monotonic() - start
        for candidate_show, candidate_episode, subtitle in candidates + [('wrong', 0, x) for x in negatives]:
            expected = 'match' if (show, episode) == (candidate_show, candidate_episode) else 'mismatch'
            intervals = timing.parse_intervals(subtitle.read_text(encoding='utf-8-sig'))
            variants = [(0, 1)]
            if expected == 'match':
                variants += [(35, 1), (-45, 1), (10, 25 / 24)]
            for offset, rate in variants:
                started = time.monotonic()
                result = timing.evaluate_activity(duration, starts, activity, (intervals - offset) / rate)
                row = dict(result, video=video.name, subtitle=subtitle.name, expected=expected,
                           injected_offset=offset, injected_rate=rate, extraction_seconds=round(elapsed, 3),
                           comparison_seconds=round(time.monotonic() - started, 3))
                results.append(row)
                print(json.dumps(row), flush=True)
        Path(output).write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
