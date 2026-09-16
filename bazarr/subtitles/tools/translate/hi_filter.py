# coding=utf-8

"""Prepare a disposable subtitle source using Bazarr's existing HI removal mod."""

import os
import re
import tempfile
from contextlib import contextmanager

import pysubs2
from charset_normalizer import detect
from subzero.modification import SubtitleModifications


PURE_BRACKET_CUE = re.compile(r'^\s*-?\s*(?:\[[^\[\]\n]{3,}\]|\([^()\n]{3,}\))\s*$')
PURE_MUSIC_CUE = re.compile(r'^\s*-?\s*[♪♫]+\s*$')


def _is_pure_hi_line(text):
    return bool(PURE_BRACKET_CUE.fullmatch(text.strip()) or PURE_MUSIC_CUE.fullmatch(text.strip()))


@contextmanager
def translation_source(source_path, language, remove_hearing_impaired=False):
    if not remove_hearing_impaired:
        yield source_path
        return

    try:
        source = pysubs2.load(source_path, encoding='utf-8')
    except UnicodeDecodeError:
        with open(source_path, 'rb') as subtitle_file:
            encoding = (detect(subtitle_file.read()) or {}).get('encoding')
        if not encoding:
            raise
        source = pysubs2.load(source_path, encoding=encoding)

    retained = []
    for entry in source:
        lines = [re.sub(r'[♪♫]', '', line).strip()
                 for line in entry.text.split(r'\N') if not _is_pure_hi_line(line)]
        lines = [line for line in lines if line]
        if lines:
            entry.text = r'\N'.join(lines)
            retained.append(entry)
    source.events = retained
    if not source:
        raise ValueError('No dialogue remains after hearing-impaired cue removal')

    modifications = SubtitleModifications()
    if not modifications.load(content=source.to_string(format_='srt'),
                              language=language, mods=['remove_HI']):
        raise ValueError('Hearing-impaired cue removal produced no subtitles')
    modifications.modify('remove_HI')
    if not modifications.f:
        raise ValueError('No dialogue remains after hearing-impaired cue removal')
    text = modifications.f.to_string(format_='srt')

    with tempfile.TemporaryDirectory(prefix='bazarr-translate-hi-') as directory:
        path = os.path.join(directory, 'filtered.srt')
        with open(path, 'w', encoding='utf-8') as destination:
            destination.write(text)
        yield path
