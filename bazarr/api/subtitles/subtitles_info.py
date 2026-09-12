# coding=utf-8

import os
import re

from flask_restx import Resource, Namespace, reqparse, fields, marshal
from subliminal_patch.core import guessit

from ..utils import authenticate


api_ns_subtitles_info = Namespace('Subtitles Info', description='Guess season number, episode number or language from '
                                                                'uploaded subtitles filename')


def _language_hint_from_filename(filename):
    stem = os.path.splitext(filename)[0]
    patterns = (
        (r'(?:^|[. _-])(?:sc|chs|zh-cn|zh-hans|zhs|gb|简中|简体)(?=$|[. _-])', 'zh'),
        (r'(?:^|[. _-])(?:tc|cht|zh-tw|zh-hant|zht|big5|繁中|繁体|繁體)(?=$|[. _-])', 'zt'),
        (r'(?:^|[. _-])(?:jpn|ja|jp|日语|日語|日文)(?=$|[. _-])', 'ja'),
        (r'(?:^|[. _-])(?:eng|en|英文|英语|英語)(?=$|[. _-])', 'en'),
        (r'(?:^|[. _-])(?:kor|ko|韩语|韓語|韩文|韓文)(?=$|[. _-])', 'ko'),
    )
    for pattern, language in patterns:
        if re.search(pattern, stem, flags=re.IGNORECASE):
            return language
    return None


@api_ns_subtitles_info.route('subtitles/info')
class SubtitleNameInfo(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('filenames[]', type=str, required=True, action='append',
                                    help='Subtitles filenames')

    get_response_model = api_ns_subtitles_info.model('SubtitlesInfoGetResponse', {
        'filename': fields.String(),
        'subtitle_language': fields.String(),
        'season': fields.Integer(),
        'episode': fields.Integer(),
    })

    @authenticate
    @api_ns_subtitles_info.response(200, 'Success')
    @api_ns_subtitles_info.response(401, 'Not Authenticated')
    @api_ns_subtitles_info.doc(parser=get_request_parser)
    def get(self):
        """Guessit over subtitles filename"""
        args = self.get_request_parser.parse_args()
        names = args.get('filenames[]')
        results = []
        for name in names:
            opts = dict()
            opts['type'] = 'episode'
            guessit_result = guessit(name, options=opts)
            result = {}
            result['filename'] = name
            language_hint = _language_hint_from_filename(name)
            if language_hint:
                result['subtitle_language'] = language_hint
            elif 'subtitle_language' in guessit_result:
                result['subtitle_language'] = str(guessit_result['subtitle_language'])

            result['episode'] = 0
            if 'episode' in guessit_result:
                if isinstance(guessit_result['episode'], list):
                    # for multiple episodes file, choose the first episode number
                    if len(guessit_result['episode']):
                        # make sure that guessit returned a list of more than 0 items
                        result['episode'] = guessit_result['episode'][0]
                elif isinstance(guessit_result['episode'], int):
                    # if single episode
                    result['episode'] = guessit_result['episode']

            if 'season' in guessit_result:
                if isinstance(guessit_result['season'], list):
                    # for multiple seasons file, choose the first season number
                    if len(guessit_result['season']):
                        # make sure that guessit returned a list of more than 0 items
                        result['season'] = guessit_result['season'][0]
                    else:
                        result['season'] = 0
                elif isinstance(guessit_result['season'], int):
                    # if single season
                    result['season'] = guessit_result['season']
                else:
                    result['season'] = 0
            else:
                result['season'] = 0

            results.append(result)

        return marshal(results, self.get_response_model, envelope='data')
