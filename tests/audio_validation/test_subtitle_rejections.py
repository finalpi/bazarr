"""Exercise durable rejection evidence against the real model and SQLite SQL."""

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
from threading import Barrier
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Text, UniqueConstraint
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.dialects.postgresql import dialect as postgres_dialect, insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import declarative_base, mapped_column
from sqlalchemy.pool import NullPool, StaticPool
from subzero.language import Language


ROOT = Path(__file__).resolve().parents[2]
INFO_KEYS = {'id', 'reason', 'detail', 'member', 'timestamp'}
REASONS = ('timing_not_confirmed', 'corrected_timing_not_confirmed',
           'obvious_fragment', 'parse_loss', 'script_mismatch')


def real_model():
    tree = ast.parse((ROOT / 'bazarr/app/database.py').read_text(encoding='utf-8'))
    model = next(node for node in tree.body
                 if isinstance(node, ast.ClassDef) and node.name == 'TableSubtitleRejections')
    base = declarative_base()
    namespace = dict(Base=base, mapped_column=mapped_column, Integer=Integer, Text=Text,
                     BigInteger=BigInteger, Boolean=Boolean, DateTime=DateTime,
                     UniqueConstraint=UniqueConstraint, datetime=datetime, timezone=timezone)
    exec(compile(ast.Module(body=[model], type_ignores=[]), str(ROOT / 'bazarr/app/database.py'), 'exec'),
         namespace)
    return base, namespace['TableSubtitleRejections']


@pytest.fixture
def database_factory(monkeypatch):
    databases = []

    def build(path=None):
        base, model = real_model()
        options = {'connect_args': {'check_same_thread': False, 'timeout': 30},
                   'isolation_level': 'AUTOCOMMIT'}
        if path is None:
            engine = create_engine('sqlite://', poolclass=StaticPool, **options)
        else:
            engine = create_engine(f'sqlite:///{path}', poolclass=NullPool, **options)
        base.metadata.create_all(engine)
        statements = []

        @event.listens_for(engine, 'before_cursor_execute')
        def capture_statement(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        stub = ModuleType('app.database')
        stub.TableSubtitleRejections = model
        stub.engine, stub.insert, stub.select, stub.delete = engine, insert, select, delete
        monkeypatch.setitem(sys.modules, 'app.database', stub)
        if 'app' not in sys.modules:
            app = ModuleType('app')
            app.__path__ = []
            monkeypatch.setitem(sys.modules, 'app', app)
        spec = importlib.util.spec_from_file_location(
            f'isolated_subtitle_rejections_{len(databases)}', ROOT / 'bazarr/subtitles/rejections.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def rows():
            with engine.connect() as connection:
                return [dict(row) for row in connection.execute(select(model)).mappings()]

        database = SimpleNamespace(module=module, engine=engine, model=model, rows=rows,
                                   statements=statements)
        databases.append(database)
        return database

    yield build
    for database in databases:
        database.engine.dispose()


@pytest.fixture
def db(database_factory):
    return database_factory()


@pytest.fixture
def media_file(tmp_path):
    path = tmp_path / 'Film.mkv'
    path.write_bytes(b'media fixture bytes')
    os.utime(path, ns=(1_770_000_000_000_000_000, 1_770_000_000_000_000_000))
    return path


def video(path, media_type='movie', identifier=49, original_language='English'):
    return SimpleNamespace(original_path=str(path), original_language=original_language,
                           **{'sonarrEpisodeId' if media_type == 'episode' else 'radarrId': identifier})


def subtitle(raw_id='site-123', language=None, provider='subhd', member='Film.CHS&ENG.srt'):
    return SimpleNamespace(id=raw_id, language=Language('zho') if language is None else language,
                           provider_name=provider, selected_archive_member=member)


@pytest.mark.parametrize('reason', REASONS)
def test_only_demonstrated_mismatch_reasons_are_persisted(db, media_file, reason):
    media, candidate = video(media_file), subtitle()
    info = db.module.record_rejection(media, candidate, reason, {'score': 0, 'stage': 'initial'})
    assert set(info) == INFO_KEYS
    assert info['reason'] == reason
    assert json.loads(info['detail']) == {'score': 0, 'stage': 'initial'}
    assert datetime.fromisoformat(info['timestamp']).utcoffset().total_seconds() == 0
    assert db.module.get_rejection(media, candidate) == info
    assert len(db.rows()) == 1


@pytest.mark.parametrize('reason', [
    'timeout', 'connection_timeout', 'network_timeout', 'invalid_format', 'invalidformat',
    'cache_expired', 'cacheexpired', 'insufficient_speech', 'insufficientspeech',
    'no_eligible_audio', 'noeligible', 'audio_reference_unavailable', 'validation_timeout',
    'alignment_failed', 'unknown', None, '',
])
def test_transport_parse_format_cache_and_inconclusive_errors_never_write(db, media_file, reason):
    assert db.module.record_rejection(video(media_file), subtitle(), reason) is None
    assert db.statements == []
    assert db.rows() == []


def test_recordable_reason_whitelist_is_exactly_the_five_demonstrated_mismatches(db):
    assert db.module.RECORDABLE_REASONS == frozenset(REASONS)


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_media_identity_and_physical_file_metadata_are_saved(db, media_file, media_type):
    media = video(media_file, media_type)
    info = db.module.record_rejection(media, subtitle(), 'parse_loss')
    row, = db.rows()
    assert row['id'] == info['id']
    assert row['media_type'] == media_type and row['media_id'] == 49
    assert row['video_path'] == str(media_file.resolve())
    assert row['video_size'] == media_file.stat().st_size
    assert row['video_mtime_ns'] == media_file.stat().st_mtime_ns
    assert row['original_language'] == 'english'
    assert len(row['video_fingerprint']) == 64


@pytest.mark.parametrize('changed', ['media_type', 'media_id', 'path', 'size', 'mtime_ns', 'original_language'])
def test_another_media_or_file_version_never_reuses_old_rejection(db, media_file, tmp_path, changed):
    media, candidate = video(media_file), subtitle()
    info = db.module.record_rejection(media, candidate, 'timing_not_confirmed')
    cached = db.module.load_rejections(media)
    if changed == 'media_type':
        other = video(media_file, 'episode')
    elif changed == 'media_id':
        other = video(media_file, identifier=50)
    elif changed == 'path':
        path = tmp_path / 'Other.mkv'
        path.write_bytes(media_file.read_bytes())
        os.utime(path, ns=(media_file.stat().st_atime_ns, media_file.stat().st_mtime_ns))
        other = video(path)
    elif changed == 'size':
        old = media_file.stat()
        media_file.write_bytes(media_file.read_bytes() + b'more')
        os.utime(media_file, ns=(old.st_atime_ns, old.st_mtime_ns))
        other = media
    elif changed == 'mtime_ns':
        old = media_file.stat()
        os.utime(media_file, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
        other = media
    else:
        other = video(media_file, original_language='Japanese')
    assert db.module.get_rejection(other, candidate, records=cached) is None
    assert db.module.load_rejections(other) == {}
    assert db.module.get_rejection(other, candidate) is None
    assert db.module.clear_rejection(other, candidate) is False
    assert db.rows()[0]['id'] == info['id']


def test_original_language_case_and_whitespace_are_normalized(db, media_file):
    info = db.module.record_rejection(video(media_file, original_language=' English '), subtitle(), 'parse_loss')
    assert db.module.get_rejection(video(media_file, original_language='ENGLISH'), subtitle()) == info


def test_provider_and_raw_candidate_id_are_isolated(db, media_file):
    media = video(media_file)
    raw_id = 'https://subtitle.example/download/123?token=private-download-token'
    info = db.module.record_rejection(media, subtitle(raw_id=raw_id), 'script_mismatch')
    row, = db.rows()
    assert row['subtitle_id_digest'] == hashlib.sha256(raw_id.encode('utf-8')).hexdigest()
    assert raw_id not in repr(row)
    assert 'private-download-token' not in repr(row)
    assert 'subtitle.example' not in repr(row)
    assert 'subtitle_id' not in row
    assert db.module.get_rejection(media, subtitle(raw_id=raw_id)) == info
    assert db.module.get_rejection(media, subtitle(raw_id='other-id')) is None
    assert db.module.get_rejection(media, subtitle(raw_id=raw_id, provider='assrt')) is None
    assert db.module.get_rejection(media, subtitle(raw_id=raw_id, provider=' SubHD ')) == info


@pytest.mark.parametrize('generic', [Language('zho'), 'zh', 'zh-CN', 'zh-Hans', 'chs'])
def test_simplified_language_aliases_share_one_key(db, media_file, generic):
    media = video(media_file)
    info = db.module.record_rejection(media, subtitle(language=generic), 'obvious_fragment')
    assert db.module.get_rejection(media, subtitle(language=Language('zho', 'CN'))) == info
    assert db.module.get_rejection(media, subtitle(language=Language('zho', 'TW'))) is None
    assert db.rows()[0]['language'] == 'zh'


@pytest.mark.parametrize('traditional', [Language('zho', 'TW'), Language('zho', 'HK'),
                                         Language('zho', script='Hant'), 'zh-TW', 'zh-Hant', 'zt'])
def test_traditional_candidates_are_distinct_from_simplified(db, media_file, traditional):
    media = video(media_file)
    info = db.module.record_rejection(media, subtitle(language=traditional), 'script_mismatch')
    assert db.module.get_rejection(media, subtitle(language=Language('zho', 'TW'))) == info
    assert db.module.get_rejection(media, subtitle(language=Language('zho'))) is None
    assert db.rows()[0]['language'] == 'zt'


def test_forced_is_separate_but_hearing_impaired_does_not_split_same_candidate(db, media_file):
    media = video(media_file)
    ordinary = subtitle(language=Language('zho'))
    hi = subtitle(language=Language('zho', hi=True))
    forced = subtitle(language=Language('zho', forced=True))
    info = db.module.record_rejection(media, ordinary, 'parse_loss')
    assert db.module.get_rejection(media, hi) == info
    assert db.module.get_rejection(media, forced) is None
    forced_info = db.module.record_rejection(media, forced, 'obvious_fragment')
    assert forced_info['id'] != info['id']
    assert len(db.rows()) == 2


def test_same_key_upsert_retains_id_and_updates_evidence(db, media_file):
    media, candidate = video(media_file), subtitle()
    first = db.module.record_rejection(media, candidate, 'parse_loss', {'parse_ratio': 0.03})
    candidate.selected_archive_member = 'FullMovie.CHS.srt'
    second = db.module.record_rejection(media, candidate, 'timing_not_confirmed', {'score': 0.2})
    assert second['id'] == first['id']
    assert second['reason'] == 'timing_not_confirmed'
    assert second['member'] == 'FullMovie.CHS.srt'
    assert json.loads(second['detail']) == {'score': 0.2}
    assert datetime.fromisoformat(second['timestamp']) >= datetime.fromisoformat(first['timestamp'])
    assert len(db.rows()) == 1


def test_one_batch_load_and_cached_candidate_checks_execute_no_extra_sql(db, media_file):
    media = video(media_file)
    for number in range(8):
        db.module.record_rejection(media, subtitle(raw_id=f'site-{number}'), 'parse_loss')
    db.statements.clear()
    records = db.module.load_rejections(media)
    assert len(db.statements) == 1 and db.statements[0].lstrip().upper().startswith('SELECT')
    db.statements.clear()
    for number in range(10):
        info = db.module.get_rejection(media, subtitle(raw_id=f'site-{number}'), records=records)
        assert bool(info) is (number < 8)
    assert db.module.get_rejection(video(media_file, identifier=50), subtitle(raw_id='site-0'), records) is None
    assert db.statements == []


def test_clear_removes_only_exact_current_file_provider_candidate_language_and_forced(db, media_file):
    media, candidate = video(media_file), subtitle()
    alternatives = [
        (video(media_file, identifier=50), subtitle()),
        (video(media_file, 'episode'), subtitle()),
        (media, subtitle(raw_id='other-id')),
        (media, subtitle(provider='assrt')),
        (media, subtitle(language=Language('zho', 'TW'))),
        (media, subtitle(language=Language('zho', forced=True))),
    ]
    db.module.record_rejection(media, candidate, 'parse_loss')
    for other_media, other_candidate in alternatives:
        db.module.record_rejection(other_media, other_candidate, 'script_mismatch')
    assert db.module.clear_rejection(media, candidate) is True
    assert db.module.get_rejection(media, candidate) is None
    assert db.module.clear_rejection(media, candidate) is False
    assert len(db.rows()) == len(alternatives)
    for other_media, other_candidate in alternatives:
        assert db.module.get_rejection(other_media, other_candidate)['reason'] == 'script_mismatch'


def test_allow_retry_preserves_previous_physical_file_rejections(db, media_file):
    media, candidate = video(media_file), subtitle()
    old = db.module.record_rejection(media, candidate, 'parse_loss')
    stat = media_file.stat()
    os.utime(media_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    new = db.module.record_rejection(media, candidate, 'script_mismatch')
    assert old['id'] != new['id']
    assert db.module.clear_rejection(media, candidate) is True
    remaining, = db.rows()
    assert remaining['id'] == old['id']
    assert db.module.get_rejection(media, candidate) is None


@pytest.mark.parametrize('invalid', ['missing_id', 'both_ids', 'zero_id', 'negative_id', 'boolean_id',
                                   'nonnumeric_id', 'missing_path', 'missing_file', 'directory'])
def test_missing_or_ambiguous_media_identity_performs_no_database_operations(db, media_file, tmp_path, invalid):
    media = video(media_file)
    if invalid == 'missing_id':
        del media.radarrId
    elif invalid == 'both_ids':
        media.sonarrEpisodeId = 49
    elif invalid == 'zero_id':
        media.radarrId = 0
    elif invalid == 'negative_id':
        media.radarrId = -1
    elif invalid == 'boolean_id':
        media.radarrId = True
    elif invalid == 'nonnumeric_id':
        media.radarrId = 'no-id'
    elif invalid == 'missing_path':
        del media.original_path
    elif invalid == 'missing_file':
        media.original_path = str(tmp_path / 'missing.mkv')
    else:
        media.original_path = str(tmp_path)
    assert db.module.record_rejection(media, subtitle(), 'parse_loss') is None
    assert db.module.load_rejections(media) == {}
    assert db.module.get_rejection(media, subtitle()) is None
    assert db.module.clear_rejection(media, subtitle()) is False
    assert db.statements == []


@pytest.mark.parametrize('invalid', ['no_id', 'none_id', 'empty_id', 'no_provider', 'unsafe_provider',
                                   'none_language', 'invalid_language'])
def test_incomplete_candidate_identity_performs_no_database_operations(db, media_file, invalid):
    candidate = subtitle()
    if invalid == 'no_id':
        del candidate.id
    elif invalid == 'none_id':
        candidate.id = None
    elif invalid == 'empty_id':
        candidate.id = ''
    elif invalid == 'no_provider':
        candidate.provider_name = ''
    elif invalid == 'unsafe_provider':
        candidate.provider_name = 'https://subtitle.example/provider'
    elif invalid == 'none_language':
        candidate.language = None
    else:
        candidate.language = 'http://unsafe.example/language'
    media = video(media_file)
    assert db.module.record_rejection(media, candidate, 'parse_loss') is None
    assert db.module.get_rejection(media, candidate) is None
    assert db.module.clear_rejection(media, candidate) is False
    assert db.statements == []


def test_rejection_survives_module_reload_and_database_reopening(database_factory, media_file, tmp_path):
    path = tmp_path / 'durable.db'
    first = database_factory(path)
    media, candidate = video(media_file), subtitle()
    info = first.module.record_rejection(media, candidate, 'script_mismatch', {'stage': 'download'})
    first.engine.dispose()
    reopened = database_factory(path)
    assert reopened.module is not first.module and reopened.engine is not first.engine
    assert reopened.module.get_rejection(media, candidate) == info
    assert len(reopened.rows()) == 1


def test_concurrent_upserts_create_one_unique_durable_row(database_factory, media_file, tmp_path):
    db = database_factory(tmp_path / 'concurrent.db')
    media, candidate = video(media_file), subtitle()
    barrier = Barrier(8)

    def worker(number):
        barrier.wait(timeout=20)
        return [db.module.record_rejection(media, candidate, REASONS[(number + turn) % len(REASONS)],
                                           {'score': number / 10})['id'] for turn in range(3)]

    with ThreadPoolExecutor(max_workers=8) as workers:
        ids = [identifier for result in workers.map(worker, range(8)) for identifier in result]
    assert len(ids) == 24 and len(set(ids)) == 1
    assert len(db.rows()) == 1
    assert db.module.get_rejection(media, candidate)['id'] == ids[0]


@pytest.mark.parametrize('secret_text,secret', [
    ('Request https://subtitle.example/get?id=4&token=SIGNED-PRIVATE failed', 'SIGNED-PRIVATE'),
    ('token=PRIVATE-TOKEN', 'PRIVATE-TOKEN'),
    ('api_key="PRIVATE-API-KEY"', 'PRIVATE-API-KEY'),
    ('Cookie: session=PRIVATE-COOKIE', 'PRIVATE-COOKIE'),
    ('Authorization: Bearer PRIVATE-BEARER', 'PRIVATE-BEARER'),
    ('Bearer PRIVATE-DIRECT-BEARER', 'PRIVATE-DIRECT-BEARER'),
    ('password=PRIVATE-PASSWORD', 'PRIVATE-PASSWORD'),
    ('signature=PRIVATE-SIGNATURE', 'PRIVATE-SIGNATURE'),
    ('skey=PRIVATE-SKEY', 'PRIVATE-SKEY'),
    ('sk-1234567890abcdefPRIVATE', 'sk-1234567890abcdefPRIVATE'),
])
def test_detail_and_archive_member_credentials_are_redacted_at_rest_and_in_api(db, media_file, secret_text, secret):
    candidate = subtitle(member=secret_text)
    info = db.module.record_rejection(video(media_file), candidate, 'parse_loss', detail=secret_text)
    assert set(info) == INFO_KEYS
    assert secret not in repr(info)
    assert secret not in repr(db.rows())
    assert secret not in repr(db.module.get_rejection(video(media_file), candidate))


@pytest.mark.parametrize('payload', [
    '1\n00:00:01,000 --> 00:00:02,000\nPRIVATE SUBTITLE DIALOGUE\n',
    '[Events]\nFormat: Start, End, Text\nDialogue: 0,0:00:01.00,0:00:02.00,PRIVATE SUBTITLE DIALOGUE',
])
def test_full_subtitle_payloads_are_omitted_from_detail_member_and_logs(db, media_file, caplog, payload):
    caplog.set_level(logging.INFO)
    media = video(media_file)
    candidate = subtitle(raw_id='https://subtitle.example/?token=RAW-ID-PRIVATE', member=payload)
    info = db.module.record_rejection(media, candidate, 'parse_loss', detail={'stage': payload})
    assert info['detail'] == '[subtitle content omitted]'
    assert info['member'] == '[subtitle content omitted]'
    assert 'PRIVATE SUBTITLE DIALOGUE' not in repr(db.rows())
    db.module.get_rejection(media, candidate)
    db.module.clear_rejection(media, candidate)
    assert 'PRIVATE SUBTITLE DIALOGUE' not in caplog.text
    assert 'RAW-ID-PRIVATE' not in caplog.text and 'subtitle.example' not in caplog.text
    assert all(set(json.loads(record.message.split(': ', 1)[1])) == {
        'id', 'media_type', 'media_id', 'video_fingerprint', 'provider', 'subtitle_id_digest',
        'language', 'forced', 'reason',
    } for record in caplog.records if record.message.startswith('BAZARR Subtitle rejection '))


def test_detail_only_keeps_safe_known_fields_and_bounds_api_text(db, media_file):
    detail = {'score': 0.4, 'windows': 5, 'subtitle_text': 'PRIVATE CONTENT',
              'request_url': 'https://unsafe.example/?token=PRIVATE-TOKEN', 'token': 'PRIVATE-TOKEN'}
    info = db.module.record_rejection(video(media_file), subtitle(member='x' * 800), 'parse_loss', detail)
    assert json.loads(info['detail']) == {'score': 0.4, 'windows': 5}
    assert len(info['member']) == 512
    assert 'PRIVATE' not in repr(db.rows())
    updated = db.module.record_rejection(video(media_file), subtitle(), 'parse_loss', 'x' * 2000)
    assert len(updated['detail']) == 1024


def test_readback_sanitizes_legacy_or_manually_written_unsafe_detail(db, media_file):
    media, candidate = video(media_file), subtitle()
    info = db.module.record_rejection(media, candidate, 'parse_loss')
    with db.engine.begin() as connection:
        connection.execute(db.model.__table__.update().where(db.model.id == info['id']).values(
            detail='Bearer LEGACY-PRIVATE-BEARER', member='https://unsafe.example/?token=LEGACY-PRIVATE'))
    loaded = db.module.get_rejection(media, candidate)
    assert set(loaded) == INFO_KEYS
    assert 'LEGACY-PRIVATE' not in repr(loaded)


def test_real_upsert_statement_compiles_for_postgresql_without_connecting(db, media_file, monkeypatch):
    captured = []

    class Connection:
        def execute(self, statement):
            captured.append(str(statement.compile(dialect=postgres_dialect())))
            row = {'id': 1, 'reason': 'parse_loss', 'detail': None, 'member': 'Film.CHS&ENG.srt',
                   'timestamp': datetime.now(timezone.utc)}
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: row))

    @contextmanager
    def begin():
        yield Connection()

    monkeypatch.setattr(db.module, 'insert', postgres_insert)
    monkeypatch.setattr(db.module, 'engine', SimpleNamespace(begin=begin))
    assert db.module.record_rejection(video(media_file), subtitle(), 'parse_loss')['id'] == 1
    sql, = captured
    assert 'ON CONFLICT (media_type, media_id, video_fingerprint, provider, subtitle_id_digest, language, forced)' in sql
    assert 'DO UPDATE SET' in sql and 'RETURNING table_subtitle_rejections.id' in sql
    assert db.statements == []
