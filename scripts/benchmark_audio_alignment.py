"""Run the production validation/sync callback on real media, saving only to a test directory."""
import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from subliminal_patch.subtitle import Subtitle
from subliminal_patch.core import save_subtitles
from subzero.language import Language

parser = argparse.ArgumentParser()
parser.add_argument('--module', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
root = Path(args.output)
root.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location('timing', args.module)
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)
sys.modules['app.config'] = SimpleNamespace(settings=SimpleNamespace(
    audio_validation=SimpleNamespace(enabled=True, audio_stream=0)))
sys.modules['app.get_args'] = SimpleNamespace(args=SimpleNamespace(config_dir=str(root)))
sys.modules['utilities.binaries'] = SimpleNamespace(get_binary=shutil.which)
media = Path('/tv/Criminal Minds')
video22 = next(media.glob('*.S01E22.*.mp4'))
video03 = next(media.glob('*.S01E03.*.mp4'))
cases = [(video22, video22.with_suffix('.zh.ass'), True),
         (video22, video22.with_suffix('.zh.srt'), False),
         (video03, video03.with_suffix('.zh.ass'), True),
         (video22, next(media.glob('*.S01E01.*.wrong-show-backup-*')), False),
         (video22, video03.with_suffix('.zh.ass'), False)]
results = []
original_align = timing.align_subtitle
for index, (video, source, expected) in enumerate(cases):
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    subtitle = Subtitle(Language('zho'), original_format=True)
    subtitle.content = content
    assert subtitle.is_valid()
    attempts = []
    def align(*arguments):
        attempts.append(True)
        return original_align(*arguments)
    timing.align_subtitle = align
    accepted = timing.validate_download(SimpleNamespace(original_path=str(video)), subtitle)
    row = dict(video=video.name, subtitle=source.name, expected=expected,
               accepted=accepted, sync_attempts=len(attempts), content_changed=subtitle.content != content)
    assert accepted == expected, row
    if accepted:
        destination = root / str(index)
        destination.mkdir(exist_ok=True)
        saved = save_subtitles(str(video), [subtitle], directory=str(destination), formats=(subtitle.format,))
        assert len(saved) == 1
        disk_text = Path(saved[0].storage_path).read_text(encoding=subtitle.get_encoding())
        np.testing.assert_equal(timing.parse_intervals(disk_text), timing.parse_intervals(subtitle.text))
        before_second = subtitle.content
        def unexpected_sync(*arguments):
            raise AssertionError('Passing subtitle must not sync again')
        timing.align_subtitle = unexpected_sync
        assert timing.validate_download(SimpleNamespace(original_path=str(video)), subtitle)
        assert subtitle.content == before_second
        row['saved_timing_verified'] = True
        row['second_pass_without_sync'] = True
    else:
        assert subtitle.content == content
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    results.append(row)
    print(json.dumps(row), flush=True)
    (root / 'report.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
