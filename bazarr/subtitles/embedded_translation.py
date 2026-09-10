"""Extract a text subtitle stream for use as an LLM translation source."""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile


def extract_embedded_subtitle(video_path, track_id, cache_dir, binary, language='en'):
    video = Path(video_path).resolve(strict=True)
    stat = video.stat()
    identity = '%s:%s:%s:%s' % (video, stat.st_size, stat.st_mtime_ns, track_id)
    language = ''.join(character for character in str(language) if character.isalnum() or character in '-_') or 'und'
    destination = Path(cache_dir) / (hashlib.sha256(identity.encode()).hexdigest() + f'.{language}.srt')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size:
        return str(destination)

    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.srt', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        subprocess.run(
            [binary('ffmpeg'), '-nostdin', '-v', 'error', '-i', str(video),
             '-map', '0:%s' % track_id, '-c:s', 'srt', '-f', 'srt', '-y', str(temporary)],
            check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if not temporary.is_file() or not 32 <= temporary.stat().st_size <= 2_000_000:
            raise ValueError('Embedded subtitle extraction produced invalid output')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return str(destination)
