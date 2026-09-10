# coding=utf-8

import os
import logging

from subliminal_patch import core
from subzero.language import Language
from charset_normalizer import detect

from constants import MAXIMUM_SUBTITLE_SIZE
from app.config import settings
from utilities.path_mappings import path_mappings
from languages.custom_lang import CustomLanguage


def get_external_subtitles_path(file, subtitle):
    fld = os.path.dirname(file)

    if settings.general.subfolder == "current":
        path = os.path.join(fld, subtitle)
    elif settings.general.subfolder == "absolute":
        custom_fld = settings.general.subfolder_custom
        if os.path.exists(os.path.join(fld, subtitle)):
            path = os.path.join(fld, subtitle)
        elif os.path.exists(os.path.join(custom_fld, subtitle)):
            path = os.path.join(custom_fld, subtitle)
        else:
            path = None
    elif settings.general.subfolder == "relative":
        custom_fld = os.path.join(fld, settings.general.subfolder_custom)
        if os.path.exists(os.path.join(fld, subtitle)):
            path = os.path.join(fld, subtitle)
        elif os.path.exists(os.path.join(custom_fld, subtitle)):
            path = os.path.join(custom_fld, subtitle)
        else:
            path = None
    else:
        path = None

    return path


def guess_external_subtitles(dest_folder, subtitles, previously_indexed_subtitles_to_exclude=None, media_path=None):
    for subtitle, language in subtitles.items():
        subtitle_path = os.path.join(dest_folder, subtitle)
        subtitle_stem = os.path.splitext(os.path.basename(subtitle))[0]
        media_stem = os.path.splitext(os.path.basename(media_path))[0] if media_path else None
        while subtitle_stem.lower().endswith(('.forced', '.hi', '.sdh', '.cc')):
            subtitle_stem = os.path.splitext(subtitle_stem)[0]
        untagged = bool(media_stem and subtitle_stem.casefold() == media_stem.casefold())

        if untagged:
            logging.debug("BAZARR treating external subtitles without a language suffix as Unknown.")
            forced = True if os.path.splitext(os.path.splitext(subtitle)[0])[1] == '.forced' else False
            subtitles[subtitle] = Language.rebuild(Language('und'), forced=forced, hi=False)
            continue

        if not language:
            if os.path.exists(subtitle_path) and os.path.splitext(subtitle_path)[1] in core.SUBTITLE_EXTENSIONS:
                logging.debug("BAZARR treating external subtitles without a language suffix as Unknown.")
                forced = True if os.path.splitext(os.path.splitext(subtitle)[0])[1] == '.forced' else False
                subtitles[subtitle] = Language.rebuild(Language('und'), forced=forced, hi=False)

        # If language is still None (undetected), skip it
        if hasattr(subtitles[subtitle], 'basename') and not subtitles[subtitle].basename:
            continue
        if hasattr(subtitles[subtitle], 'basename') and subtitles[subtitle].basename == 'und':
            continue

        # Skip HI detection if forced
        if hasattr(language, 'forced') and language.forced:
            continue

        # Detect hearing-impaired external subtitles not identified in filename
        if hasattr(subtitles[subtitle], 'hi') and not subtitles[subtitle].hi:
            subtitle_path = os.path.join(dest_folder, subtitle)

            # check if file exist:
            if os.path.exists(subtitle_path) and os.path.splitext(subtitle_path)[1] in core.SUBTITLE_EXTENSIONS:
                # to improve performance, skip detection of files larger that 1M
                if os.path.getsize(subtitle_path) > MAXIMUM_SUBTITLE_SIZE:
                    logging.debug(f"BAZARR subtitles file is too large to be text based. Skipping this file: "
                                  f"{subtitle_path}")
                    continue

                with open(subtitle_path, 'rb') as f:
                    text = f.read()

                encoding = detect(text)
                if encoding and 'encoding' in encoding and encoding['encoding']:
                    encoding = detect(text)['encoding']
                else:
                    logging.debug(f"BAZARR skipping this subtitles because we can't guess the encoding. "
                                  f"It's probably a binary file: {subtitle_path}")
                    continue
                text = text.decode(encoding)

                if os.path.splitext(subtitle_path)[1] == 'srt':
                    if core.parse_for_hi_regex(subtitle_text=text,
                                               alpha3_language=language.alpha3 if hasattr(language, 'alpha3') else
                                               None):
                        subtitles[subtitle] = Language.rebuild(subtitles[subtitle], forced=False, hi=True)
    return subtitles


def _get_lang_from_str(x_found_lang, x_forced, x_hi):
    if len(x_found_lang) == 2:
        x_custom_lang_attr = "alpha2"
    elif len(x_found_lang) == 3:
        x_custom_lang_attr = "alpha3"
    else:
        x_custom_lang_attr = "language"

    x_custom_lang = CustomLanguage.from_value(x_found_lang, attr=x_custom_lang_attr)

    if x_custom_lang is not None:
        return Language.rebuild(x_custom_lang.subzero_language(), hi=x_hi, forced=x_forced)
    else:
        return Language.rebuild(Language.fromietf(x_found_lang), hi=x_hi, forced=x_forced)
