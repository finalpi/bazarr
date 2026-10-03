# coding=utf-8

import os

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from app.database import TableEpisodes, TableShows, database, select, get_subtitles
from utilities.path_mappings import path_mappings
from app.get_providers import get_providers
from subtitles.manual import manual_search, episode_manually_download_specific_subtitle, validate_manual_search_options, clear_manual_rejection
from app.config import settings
from app.jobs_queue import jobs_queue
from subtitles.indexer.series import store_subtitles, list_missing_subtitles

from ..utils import authenticate

api_ns_providers_episodes = Namespace('Providers Episodes', description='List and download episodes subtitles manually')


@api_ns_providers_episodes.route('providers/episodes')
class ProviderEpisodes(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('episodeid', type=int, required=True, help='Episode ID')
    get_request_parser.add_argument('keyword', type=str, location='args', required=False,
                                    help='Optional search keyword, up to 200 characters')
    get_request_parser.add_argument('providers', type=str, action='append', location='args', required=False,
                                    help='Enabled subtitle providers to search (repeat for multiple providers)')

    rejection_model = api_ns_providers_episodes.model('ProvidersEpisodeRejection', {
        'id': fields.Integer(), 'reason': fields.String(), 'detail': fields.String(),
        'member': fields.String(), 'timestamp': fields.String(),
    })
    get_response_model = api_ns_providers_episodes.model('ProviderEpisodesGetResponse', {
        'dont_matches': fields.List(fields.String),
        'forced': fields.String(),
        'hearing_impaired': fields.String(),
        'language': fields.String(),
        'matches': fields.List(fields.String),
        'original_format': fields.String(),
        'orig_score': fields.Integer(),
        'provider': fields.String(),
        'release_info': fields.List(fields.String),
        'tags': fields.List(fields.String),
        'rejected': fields.Boolean(default=False),
        'rejection': fields.Nested(rejection_model, allow_null=True),
        'score': fields.Integer(),
        'score_without_hash': fields.Integer(),
        'subtitle': fields.String(),
        'uploader': fields.String(),
        'url': fields.String(),
    })

    @authenticate
    @api_ns_providers_episodes.response(401, 'Not Authenticated')
    @api_ns_providers_episodes.response(400, 'Invalid search options')
    @api_ns_providers_episodes.response(404, 'Episode not found')
    @api_ns_providers_episodes.response(500, 'Custom error messages')
    @api_ns_providers_episodes.doc(parser=get_request_parser)
    def get(self):
        """Search manually for an episode subtitles"""
        args = self.get_request_parser.parse_args()
        sonarrEpisodeId = args.get('episodeid')
        try:
            keyword, providers_list = validate_manual_search_options(
                args.get('keyword'), args.get('providers'), get_providers())
        except ValueError as error:
            return str(error), 400
        stmt = select(TableEpisodes.path,
                      TableEpisodes.sceneName,
                      TableShows.title,
                      TableShows.profileId,
                      TableEpisodes.missing_subtitles) \
            .select_from(TableEpisodes) \
            .join(TableShows) \
            .where(TableEpisodes.sonarrEpisodeId == sonarrEpisodeId)
        episodeInfo = database.execute(stmt).first()

        previously_indexed_subtitles = get_subtitles(sonarr_episode_id=sonarrEpisodeId)

        if not episodeInfo:
            return 'Episode not found', 404
        elif not len(previously_indexed_subtitles) or \
                any([not x['embedded_track_id'] for x in previously_indexed_subtitles if not x['path']]):
            # subtitles indexing for this episode might be incomplete, we'll do it again
            store_subtitles(sonarrEpisodeId)
            episodeInfo = database.execute(stmt).first()
        elif episodeInfo.missing_subtitles is None:
            # missing subtitles calculation for this episode is incomplete, we'll do it again
            list_missing_subtitles(epno=sonarrEpisodeId)
            episodeInfo = database.execute(stmt).first()

        title = episodeInfo.title
        episodePath = path_mappings.path_replace(episodeInfo.path)

        if not os.path.exists(episodePath):
            return 'Episode file not found. Path mapping issue?', 500

        sceneName = episodeInfo.sceneName or "None"
        profileId = episodeInfo.profileId

        data = manual_search(episodePath, profileId, providers_list, sceneName, title, 'series', keyword=keyword)
        if isinstance(data, str):
            return data, 500
        return marshal(data, self.get_response_model, envelope='data')

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('seriesid', type=int, required=True, help='Series ID')
    post_request_parser.add_argument('episodeid', type=int, required=True, help='Episode ID')
    post_request_parser.add_argument('hi', type=str, required=True, help='HI subtitles from ["True", "False"]')
    post_request_parser.add_argument('forced', type=str, required=True, help='Forced subtitles from ["True", "False"]')
    post_request_parser.add_argument('original_format', type=str, required=True,
                                     help='Use original subtitles format from ["True", "False"]')
    post_request_parser.add_argument('provider', type=str, required=True, help='Provider name')
    post_request_parser.add_argument('subtitle', type=str, required=True, help='Subtitle ID as returned by GET')

    @authenticate
    @api_ns_providers_episodes.doc(parser=post_request_parser)
    @api_ns_providers_episodes.response(204, 'Success')
    @api_ns_providers_episodes.response(401, 'Not Authenticated')
    @api_ns_providers_episodes.response(404, 'Episode not found')
    @api_ns_providers_episodes.response(500, 'Custom error messages')
    def post(self):
        """Manually download an episode subtitles"""
        args = self.post_request_parser.parse_args()

        episode_manually_download_specific_subtitle(sonarr_series_id=args.get('seriesid'),
                                                    sonarr_episode_id=args.get('episodeid'),
                                                    hi=args.get('hi').capitalize(),
                                                    forced=args.get('forced').capitalize(),
                                                    use_original_format=args.get('original_format').capitalize(),
                                                    selected_provider=args.get('provider'),
                                                    subtitle=args.get('subtitle'),
                                                    job_id=None)

        return '', 204

    delete_request_parser = reqparse.RequestParser()
    delete_request_parser.add_argument('episodeid', type=int, required=True, help='Media ID')
    delete_request_parser.add_argument('subtitle', type=str, required=True, help='Candidate cache UUID')

    @authenticate
    @api_ns_providers_episodes.doc(parser=delete_request_parser)
    @api_ns_providers_episodes.response(204, 'Rejection cleared; search again before downloading')
    @api_ns_providers_episodes.response(400, 'Candidate belongs to different media')
    @api_ns_providers_episodes.response(404, 'Candidate cache expired')
    def delete(self):
        """Allow retrying this rejected candidate without starting a download."""
        args = self.delete_request_parser.parse_args()
        return clear_manual_rejection('series', args.get('episodeid'), args.get('subtitle'))
