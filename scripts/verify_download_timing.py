"""Read-only provider and timing smoke check: never writes to the media folder."""

import argparse
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import shutil
import sys
import time
import types


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--video', required=True)
    parser.add_argument('--duration', type=float, required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--year', type=int)
    parser.add_argument('--original-language', required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--subtitle-id')
    source.add_argument('--existing-subtitle')
    parser.add_argument('--cache', required=True)
    parser.add_argument('--config', default='/config')
    parser.add_argument('--timing-module', type=Path)
    args = parser.parse_args()
    sys.argv = ['bazarr', '-c', args.config]
    source_path = args.timing_module or Path(__file__).resolve().parents[1] / 'bazarr/subtitles/audio_validation.py'
    if not source_path.exists():
        source_path = Path('/app/bazarr/bin/bazarr/subtitles/audio_validation.py')
    # Loading subtitles.__init__ would start unrelated application components.
    package = types.ModuleType('_timing_diagnostics')
    package.__path__ = [str(Path('/app/bazarr/bin/bazarr/subtitles') if source_path.parent.name != 'subtitles'
                            else source_path.parent)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location(package.__name__ + '.audio_validation', source_path)
    timing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(timing)
    from subliminal import Movie
    from subliminal_patch.providers.subhd import SubhdSubtitle, SubhdProvider
    from subliminal_patch.subtitle_coverage import subtitle_coverage
    from subzero.language import Language
    get_binary = shutil.which

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    video = Movie(args.video, args.title, year=args.year)
    video.original_path = args.video
    video.duration = args.duration
    video.original_language = args.original_language
    existing = [p for p in Path(args.video).parent.iterdir() if p.suffix.lower() in ('.srt', '.ass', '.ssa', '.vtt')]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in existing}
    started = time.monotonic()
    if args.subtitle_id:
        sub = SubhdSubtitle(Language('zho'), args.subtitle_id,
                            'https://subhd.me/a/' + args.subtitle_id, args.title, video)
        provider = SubhdProvider()
        provider.initialize()
        try:
            provider.download_subtitle(sub)
        finally:
            provider.terminate()
        if not sub.content:
            raise ValueError('No eligible subtitle in the selected package')
        text = sub.text
        print(json.dumps({'stage': 'provider_download', 'seconds': time.monotonic()-started,
                          'selected_member': getattr(sub, 'selected_archive_member', None),
                          'coverage': subtitle_coverage(text, video.duration)}, ensure_ascii=False), flush=True)
    else:
        text = Path(args.existing_subtitle).read_text(encoding='utf-8-sig')

    selected = timing.select_audio_reference(video, get_binary)
    duration, starts, activity = timing.extract_activity(args.video, args.cache, get_binary, selected['audio_index'])
    intervals = timing.parse_intervals(text)
    checked = time.monotonic()
    initial = timing.evaluate_activity(duration, starts, activity, intervals)
    print(json.dumps({'stage': 'initial', 'seconds': time.monotonic()-checked, 'result': initial}), flush=True)
    if not initial['accepted']:
        checked = time.monotonic()
        correction = timing.evaluate_activity(duration, starts, activity, intervals, search=True)
        print(json.dumps({'stage': 'sampled_search', 'seconds': time.monotonic()-checked, 'result': correction}), flush=True)
        if correction['accepted']:
            corrected = timing.apply_timing_transform(text, correction['rate'], correction['offset_seconds'])
            result = timing.evaluate_activity(duration, starts, activity, timing.parse_intervals(corrected))
            print(json.dumps({'stage': 'verified', 'result': result}), flush=True)
    after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in existing}
    assert before == after, 'Existing media subtitles changed during read-only verification'
    print(json.dumps({'stage': 'finished', 'total_seconds': time.monotonic()-started,
                      'media_subtitles_unchanged': before == after}), flush=True)


if __name__ == '__main__':
    main()
