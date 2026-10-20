"""Raw payload versions (migration 20261020_0011) against PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import OAuthToken, RawHealthData
from app.parsers.common import payload_hash
from app.services.raw_history import import_versions, record_history, verify_versions
from app.services.storage import store_records
from app.services.webhook_processing import WebhookBatchProcessor
from app.sync.incremental import IncrementalSync

pytestmark = pytest.mark.integration

SPEC = REGISTRY["steps"]
NAME = "users/me/dataTypes/steps/dataPoints/a"


def steps(count: int, name: str = NAME) -> dict:
    return {
        "name": name,
        "dataSource": {"platform": "FITBIT"},
        "steps": {
            "interval": {
                "startTime": "2026-08-17T10:00:00Z",
                "endTime": "2026-08-17T10:01:00Z",
            },
            "count": str(count),
        },
    }


async def store(sessions, *records, **options):
    async with sessions.begin() as session:
        return await store_records(session, SPEC, list(records), **options)


async def chain(sessions, google_id: str = NAME) -> list[tuple[int, bool, int, str]]:
    async with sessions() as session:
        records = await record_history(session, SPEC.name, google_id=google_id)
    if not records:
        return []
    return [
        (v["version"], v["is_current"], int(v["payload"]["steps"]["count"]), v["source"])
        for v in records[0]["versions"]
    ]


def _sync_url(database_url: str) -> str:
    return database_url.replace("+asyncpg", "+psycopg")


async def test_changes_keep_every_version_and_repeats_add_none(pg_sessions) -> None:
    await store(pg_sessions, steps(1))
    await store(pg_sessions, steps(1))
    assert await chain(pg_sessions) == [(1, True, 1, "sync")]
    for count in (2, 3, 4):
        await store(pg_sessions, steps(count))
    await store(pg_sessions, steps(4))
    assert await chain(pg_sessions) == [
        (1, False, 1, "sync"),
        (2, False, 2, "sync"),
        (3, False, 3, "sync"),
        (4, True, 4, "sync"),
    ]
    async with pg_sessions() as session:
        assert await verify_versions(session) == (3, [])
        records = await record_history(session, SPEC.name, google_id=NAME)
    versions = records[0]["versions"]
    for older, newer in zip(versions, versions[1:], strict=False):
        assert older["valid_to"] == newer["valid_from"]


async def test_batch_duplicates_with_different_payloads_become_versions(pg_sessions) -> None:
    stats = await store(pg_sessions, steps(5), steps(6), steps(6), steps(5))
    assert (stats.inserted, stats.versions_kept) == (1, 2)
    assert [item[2] for item in await chain(pg_sessions)] == [5, 6, 5]


async def test_concurrent_writers_lose_no_payload(database_url, pg_sessions) -> None:
    """A writer blocked on the row lock still archives the version it really replaces."""
    await store(pg_sessions, steps(1))
    other_engine = create_async_engine(database_url)
    other = async_sessionmaker(other_engine, expire_on_commit=False)
    try:
        async with pg_sessions.begin() as first:
            # Both writers read version 1 as the stored payload before either writes.
            await store_records(first, SPEC, [steps(2)])
            second = asyncio.create_task(store(other, steps(3)))
            await asyncio.sleep(0.3)
            assert not second.done()  # waiting for the row lock held by `first`
        await second
        # Two brand-new records racing on the unique key: the loser becomes version 2.
        fresh = "users/me/dataTypes/steps/dataPoints/fresh"
        await asyncio.gather(store(pg_sessions, steps(7, fresh)), store(other, steps(8, fresh)))
    finally:
        await other_engine.dispose()
    assert await chain(pg_sessions) == [
        (1, False, 1, "sync"),
        (2, False, 2, "sync"),
        (3, True, 3, "sync"),
    ]
    assert sorted(item[2] for item in await chain(pg_sessions, fresh)) == [7, 8]
    async with pg_sessions() as session:
        assert (await verify_versions(session))[1] == []


async def test_sync_and_webhook_paths_are_versioned(pg_sessions) -> None:
    class Client:
        def __init__(self) -> None:
            self.pages: list[Page] = []

        async def list_data_points(self, spec, start, end, page_token=None):
            return self.pages.pop(0)

    client = Client()
    incremental = IncrementalSync(
        Settings(_env_file=None, app_env="test"), pg_sessions, client, {"steps": SPEC}
    )
    client.pages = [Page([steps(10)], None)]
    await incremental.run("steps")
    client.pages = [Page([steps(11)], None)]
    await incremental.run("steps")

    async with pg_sessions.begin() as session:
        session.add(OAuthToken(subject="me", encrypted_refresh_token="x", health_user_id="u-1"))
    processor = WebhookBatchProcessor(pg_sessions, incremental, {"steps"})
    payload = {
        "data": {
            "dataType": "steps",
            "healthUserId": "u-1",
            "operation": "UPSERT",
            "intervals": [
                {
                    "physicalTimeInterval": {
                        "startTime": "2026-08-17T10:00:00Z",
                        "endTime": "2026-08-17T10:01:00Z",
                    }
                }
            ],
        }
    }
    client.pages = [Page([steps(12)], None)]
    ingested = await processor.ingest_verified(json.dumps(payload).encode(), payload)
    result = await processor.process_batch(ingested.batch_hash)
    assert result.completed_elements == 1
    await store(pg_sessions, steps(13), source="backfill")
    assert await chain(pg_sessions) == [
        (1, False, 10, "sync"),
        (2, False, 11, "sync"),
        (3, False, 12, "webhook"),
        (4, True, 13, "backfill"),
    ]


async def test_history_is_append_only_and_verify_catches_tampering(pg_sessions) -> None:
    for count in (1, 2, 3):
        await store(pg_sessions, steps(count))
    for statement in (
        "UPDATE raw_health_data_versions SET raw_json = '{}'::jsonb",
        "DELETE FROM raw_health_data_versions",
        "TRUNCATE raw_health_data_versions",
        "DELETE FROM raw_health_data",  # the history keeps its current row
    ):
        with pytest.raises(DBAPIError):
            async with pg_sessions.begin() as session:
                await session.execute(text(statement))
    async with pg_sessions.begin() as session:
        await session.execute(
            text(
                "ALTER TABLE raw_health_data_versions "
                "DISABLE TRIGGER raw_health_data_versions_append_only"
            )
        )
        await session.execute(
            text(
                "UPDATE raw_health_data_versions SET raw_json = "
                "jsonb_set(raw_json, '{steps,count}', '\"99\"') WHERE version = 2"
            )
        )
    async with pg_sessions() as session:
        count, errors = await verify_versions(session)
    assert count == 2
    assert len(errors) == 1 and "version payload hash mismatch" in errors[0]


def test_migration_seeds_existing_rows_and_guards_downgrade(database_url, alembic_config) -> None:
    engine = create_engine(_sync_url(database_url))
    try:
        command.upgrade(alembic_config, "20261020_0010")
        with engine.begin() as connection:
            for index in range(50):
                record = steps(index, f"{NAME}-{index}")
                connection.execute(
                    text(
                        "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                        "raw_json, payload_hash, first_seen_at, last_seen_at, is_quarantined, "
                        "created_at, updated_at) VALUES (gen_random_uuid(), :gid, 'steps', "
                        "CAST(:raw AS jsonb), :hash, '2026-08-01T00:00:00Z', now(), false, "
                        "now(), now())"
                    ),
                    {
                        "gid": f"{NAME}-{index}",
                        "raw": json.dumps(record),
                        "hash": payload_hash(record),
                    },
                )
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        with engine.connect() as connection:
            seeded = connection.execute(
                text(
                    "SELECT count(*), min(version), max(version), bool_and(is_current), "
                    "min(valid_from) FROM raw_health_data_history"
                )
            ).one()
            assert connection.scalar(text("SELECT count(*) FROM raw_health_data_versions")) == 0
        assert tuple(seeded[:4]) == (50, 1, 1, True)
        assert seeded[4] == datetime(2026, 8, 1, tzinfo=UTC)

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE raw_health_data SET raw_json = '{}'::jsonb, payload_hash = 'x', "
                    "last_seen_at = now() WHERE google_data_point_id = :gid"
                ),
                {"gid": f"{NAME}-0"},
            )
        with pytest.raises(RuntimeError, match="allow-version-loss"):
            command.downgrade(alembic_config, "20261020_0010")
        alembic_config.cmd_opts = argparse.Namespace(x=["allow-version-loss=1"])
        command.downgrade(alembic_config, "20261020_0010")
        alembic_config.cmd_opts = None
        command.upgrade(alembic_config, "head")
    finally:
        engine.dispose()


@pytest.fixture
def archive_url(database_url):
    """A second database shaped like a pre-0010 dump: raw_json is `json`."""
    url = make_url(database_url)
    name = f"{url.database}_archive"
    admin = create_engine(
        _sync_url(url.set(database="postgres").render_as_string(hide_password=False)),
        isolation_level="AUTOCOMMIT",
    )
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    archive = url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(_sync_url(archive))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE raw_health_data (id uuid PRIMARY KEY, "
                "google_data_point_id varchar(512) NOT NULL, data_type varchar(128) NOT NULL, "
                "source_start_time timestamptz, source_end_time timestamptz, "
                "source_timezone varchar(128), raw_json json NOT NULL, "
                "payload_hash varchar(64) NOT NULL, first_seen_at timestamptz NOT NULL, "
                "last_seen_at timestamptz NOT NULL, source_deleted_at timestamptz, "
                "UNIQUE (data_type, google_data_point_id))"
            )
        )
    engine.dispose()
    try:
        yield archive
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _fill_archive(url: str, rows: list[tuple[dict, str]]) -> None:
    engine = create_engine(_sync_url(url))
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM raw_health_data"))
        for record, seen in rows:
            connection.execute(
                text(
                    "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                    "raw_json, payload_hash, first_seen_at, last_seen_at) VALUES "
                    "(:id, :gid, 'steps', CAST(:raw AS json), :hash, '2026-08-01T00:00:00Z', "
                    ":seen)"
                ),
                {
                    "id": uuid.uuid4(),
                    "gid": record["name"],
                    # json keeps the text as written: spacing jsonb would normalize.
                    "raw": json.dumps(record, indent=2),
                    "hash": payload_hash(record),
                    "seen": seen,
                },
            )
    engine.dispose()


async def test_import_from_json_typed_dump_orders_by_time_and_is_idempotent(
    pg_sessions, archive_url
) -> None:
    await store(pg_sessions, steps(30))
    async with pg_sessions.begin() as session:
        await session.execute(
            text("UPDATE raw_health_data SET payload_source = NULL, last_seen_at = :seen"),
            {"seen": datetime(2026, 8, 10, tzinfo=UTC)},
        )
    await store(pg_sessions, steps(40))
    source = create_async_engine(archive_url)
    try:
        imported = []
        # Newest dump first: the ordering must come from the data, not the import order.
        for count, seen in ((20, "2026-08-08T03:30:00Z"), (10, "2026-08-05T03:30:00Z")):
            _fill_archive(archive_url, [(steps(count), seen)])
            imported.append((await import_versions(pg_sessions, source)).imported)
        again = await import_versions(pg_sessions, source)
    finally:
        await source.dispose()
    assert imported == [1, 1]
    assert (again.imported, again.already_present) == (0, 1)
    assert await chain(pg_sessions) == [
        (1, False, 10, "archive-import"),
        (2, False, 20, "archive-import"),
        (3, False, 30, "legacy"),
        (4, True, 40, "sync"),
    ]
    async with pg_sessions() as session:
        assert await verify_versions(session) == (3, [])
        current = await session.scalar(select(func.count()).select_from(RawHealthData))
        history = await session.scalar(select(func.count()).select_from(RawHealthDataVersion))
    assert (current, history) == (1, 3)
