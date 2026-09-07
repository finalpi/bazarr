"""Create Traditional Chinese subtitles locally from Simplified Chinese."""

import os
from pathlib import Path


def convert_simplified_file(source, destination):
    """Convert an UTF-8 subtitle with OpenCC and replace the destination atomically."""
    from opencc import OpenCC

    source = Path(source)
    destination = Path(destination)
    converted = OpenCC('s2twp').convert(source.read_text(encoding='utf-8-sig'))
    temporary = destination.with_name(destination.name + '.simplified-to-traditional.tmp')
    temporary.write_text(converted, encoding='utf-8')
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return str(destination)
