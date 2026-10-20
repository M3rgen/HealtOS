"""Hardening of the raw archive (migration 20261020_0011) against PostgreSQL.

One test per review finding; each failed on the first version of 0011.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from app.google_health.data_types import REGISTRY
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.services.raw_history import import_versions, record_history
from app.services.storage import store_records

pytestmark = pytest.mark.integration

SPEC = REGISTRY["steps"]
NAME = "users/me/dataTypes/steps/dataPoints/a"


def steps(count, name: str = NAME, **extra) -> dict:
    return {
        "name": name,
        "dataSource": {"platform": "FITBIT"},
        "steps": {
            "interval": {
                "startTime": "2026-08-17T10:00:00Z",
                "endTime": "2026-08-17T10:01:00Z",
            },
            "count": count if isinstance(count, float) else str(count),
        },
        **extra,
    }


async def store(sessions, *records, **options):
    async with sessions.begin() as session:
        return await store_records(session, SPEC, list(records), **options)


async def chain(sessions, google_id: str = NAME) -> list[tuple]:
    async with sessions() as session:
        records = await record_history(session, SPEC.name, google_id=google_id)
    if not records:
        return []
    return [
        (v["version"], v["is_current"], v["payload"]["steps"]["count"], v["source"])
        for v in records[0]["versions"]
    ]


async def execute(sessions, *statements: str) -> None:
    async with sessions.begin() as session:
        for statement in statements:
            await session.execute(text(statement))


async def history_count(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(RawHealthDataVersion))


# 1. An overwrite that keeps payload_hash (e.g. a hand-written UPDATE) still keeps the
#    old payload.
async def test_overwrite_with_the_same_hash_keeps_the_old_payload(pg_sessions) -> None:
    await store(pg_sessions, steps(1))
    await execute(
        pg_sessions,
        "UPDATE raw_health_data SET raw_json = jsonb_set(raw_json, '{steps,count}', '\"9\"')",
    )
    async with pg_sessions() as session:
        kept = (await session.execute(select(RawHealthDataVersion.raw_json))).scalars().all()
    assert kept == [steps(1)]


# 2. Rows without history (every row before the first change) could be deleted.
async def test_raw_records_cannot_be_deleted_truncated_or_rekeyed(pg_sessions) -> None:
    await store(pg_sessions, steps(1), steps(1, f"{NAME}-2"))
    await store(pg_sessions, steps(2, f"{NAME}-2"))
    for statement in (
        f"DELETE FROM raw_health_data WHERE google_data_point_id = '{NAME}'",
        "DELETE FROM raw_health_data",
        "TRUNCATE raw_health_data CASCADE",
        "TRUNCATE raw_health_data_versions CASCADE",
        "UPDATE raw_health_data SET google_data_point_id = 'other'",
        "UPDATE raw_health_data SET data_type = 'heart-rate'",
        "UPDATE raw_health_data SET first_seen_at = now() - interval '1 day'",
    ):
        with pytest.raises(DBAPIError, match="refused"):
            await execute(pg_sessions, statement)
    async with pg_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 2
    assert await history_count(pg_sessions) == 1


# 3. The frozen-column check compared raw_json as jsonb, where 1.0 equals 1.
async def test_history_payload_text_is_frozen(pg_sessions) -> None:
    await store(pg_sessions, steps(1, version=1))
    await store(pg_sessions, steps(2, version=1))
    with pytest.raises(DBAPIError, match="append-only"):
        await execute(
            pg_sessions,
            "SELECT set_config('healthos.renumber', '1', true)",
            "UPDATE raw_health_data_versions "
            "SET raw_json = jsonb_set(raw_json, '{version}', '1.0')",
        )
    async with pg_sessions() as session:
        stored = await session.scalar(text("SELECT raw_json::text FROM raw_health_data_versions"))
    assert json.loads(stored)["version"] == 1 and '"version": 1.0' not in stored


# 4. Renumbering was open to any session.
async def test_renumbering_needs_the_import_flag(pg_sessions) -> None:
    for count in (1, 2, 3):
        await store(pg_sessions, steps(count))
    with pytest.raises(DBAPIError, match="append-only"):
        await execute(pg_sessions, "UPDATE raw_health_data_versions SET version = version + 10")
    assert [item[2] for item in await chain(pg_sessions)] == ["1", "2", "3"]
    # The import command's own transaction may renumber (only `version` changes).
    await execute(
        pg_sessions,
        "SELECT set_config('healthos.renumber', '1', true)",
        "UPDATE raw_health_data_versions SET version = -version",
        "UPDATE raw_health_data_versions SET version = -version",
    )
    # The flag is transaction-local: the next transaction is guarded again.
    with pytest.raises(DBAPIError, match="append-only"):
        await execute(pg_sessions, "UPDATE raw_health_data_versions SET version = version + 10")


# 5. `verify` passed while the triggers were disabled.
def test_verify_fails_when_a_guard_is_missing_disabled_or_replaced(
    database_url, migrated, monkeypatch
) -> None:
    from app.cli import main as cli  # imports the app engine: after DATABASE_URL is set

    engine = create_async_engine(database_url, poolclass=NullPool)
    monkeypatch.setattr(cli, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    sync = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    runner = CliRunner()

    def verify() -> tuple[int, str]:
        result = runner.invoke(cli.app, ["verify"])
        return result.exit_code, result.output

    try:
        assert verify()[0] == 0
        tampering = (
            (
                "ALTER TABLE raw_health_data DISABLE TRIGGER raw_health_data_no_delete",
                "raw_health_data.raw_health_data_no_delete is disabled",
            ),
            (
                "ALTER TABLE raw_health_data ENABLE TRIGGER raw_health_data_no_delete",
                "is enabled (not ALWAYS)",
            ),
            (
                "CREATE OR REPLACE FUNCTION healthos_refuse_change() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$",
                "protective function healthos_refuse_change() was changed",
            ),
            (
                "DROP TRIGGER raw_health_data_versions_no_truncate ON raw_health_data_versions",
                "protective trigger missing: "
                "raw_health_data_versions.raw_health_data_versions_no_truncate",
            ),
        )
        for statement, message in tampering:
            with sync.begin() as connection:
                connection.execute(text(statement))
            code, output = verify()
            assert code == 1 and message in output, output
    finally:
        sync.dispose()
        asyncio.run(engine.dispose())


@pytest.fixture
def archive_url(database_url):
    """A second database shaped like a pre-0010 dump: raw_json is `json`."""
    url = make_url(database_url)
    name = f"{url.database}_archive"
    admin = create_engine(
        url.set(drivername="postgresql+psycopg", database="postgres").render_as_string(
            hide_password=False
        ),
        isolation_level="AUTOCOMMIT",
    )
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    archive = url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(archive.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE raw_health_data (id uuid PRIMARY KEY, "
                "google_data_point_id varchar(512) NOT NULL, data_type varchar(128) NOT NULL, "
                "source_start_time timestamptz, source_end_time timestamptz, "
                "source_timezone varchar(128), raw_json json NOT NULL, "
                "payload_hash varchar(64) NOT NULL, first_seen_at timestamptz NOT NULL, "
                "last_seen_at timestamptz NOT NULL, source_deleted_at timestamptz)"
            )
        )
    engine.dispose()
    try:
        yield archive
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def fill_archive(url: str, rows: list[tuple[dict, str]]) -> None:
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
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
                    "raw": json.dumps(record),
                    "hash": payload_hash(record),
                    "seen": seen,
                },
            )
    engine.dispose()


async def run_import(sessions, url: str, **options):
    source = create_async_engine(url)
    try:
        return await import_versions(sessions, source, **options)
    finally:
        await source.dispose()


async def make_legacy(sessions, seen: datetime) -> None:
    await execute(sessions, "UPDATE raw_health_data SET payload_source = NULL")
    async with sessions.begin() as session:
        await session.execute(
            text("UPDATE raw_health_data SET last_seen_at = :seen"), {"seen": seen}
        )


# 6. One payload jsonb cannot store unchanged aborted the whole chunk.
async def test_import_skips_and_reports_payloads_jsonb_cannot_store(
    pg_sessions, archive_url
) -> None:
    await store(pg_sessions, steps(30))
    await make_legacy(pg_sessions, datetime(2026, 8, 10, tzinfo=UTC))
    others = [f"{NAME}-{index}" for index in range(3)]
    for google_id in others:
        await store(pg_sessions, steps(30, google_id))
    fill_archive(
        archive_url,
        [
            (steps(20), "2026-08-01T00:00:00Z"),
            (steps(1e16, others[0]), "2026-08-01T00:00:00Z"),  # jsonb: 10000000000000000
            (steps(1, others[1], note="a\u0000b"), "2026-08-01T00:00:00Z"),  # jsonb refuses
            (steps(-0.0, others[2]), "2026-08-01T00:00:00Z"),  # jsonb: 0.0
        ],
    )
    dry = await run_import(pg_sessions, archive_url, dry_run=True)
    assert sorted(dry.unrepresentable) == sorted(f"steps/{item}" for item in others)
    assert dry.imported == 1
    assert await history_count(pg_sessions) == 0
    stats = await run_import(pg_sessions, archive_url)
    assert (stats.imported, len(stats.unrepresentable)) == (1, 3)
    assert await chain(pg_sessions) == [
        (1, False, "20", "archive-import"),
        (2, True, "30", "legacy"),
    ]


# 7. Records only the dump has were only counted, and a payload that returned (A, B, A)
#    was deduplicated against the current row and lost its first occurrence.
async def test_import_restores_missing_records_and_keeps_returning_payloads(
    pg_sessions, archive_url
) -> None:
    await store(pg_sessions, steps(1))  # A, current, last seen 2026-08-10
    await make_legacy(pg_sessions, datetime(2026, 8, 10, tzinfo=UTC))
    gone = f"{NAME}-gone"
    # The newer dump first (the CLI orders several --from-db that way): B, then A before it.
    fill_archive(
        archive_url, [(steps(2), "2026-08-05T00:00:00Z"), (steps(7, gone), "2026-08-01T00:00:00Z")]
    )
    first = await run_import(pg_sessions, archive_url)
    fill_archive(
        archive_url, [(steps(1), "2026-08-01T00:00:00Z"), (steps(8, gone), "2026-08-03T00:00:00Z")]
    )
    second = await run_import(pg_sessions, archive_url)
    assert first.restored_records == {"steps": 1}
    assert (second.imported, second.restored_records) == (2, {})
    assert await chain(pg_sessions) == [
        (1, False, "1", "archive-import"),  # A
        (2, False, "2", "archive-import"),  # B
        (3, True, "1", "legacy"),  # A again
    ]
    # The restored record is a row of its own, marked deleted at the source, and the
    # newer sighting of the later dump became its current version.
    assert await chain(pg_sessions, gone) == [
        (1, False, "7", "archive-import"),
        (2, True, "8", "archive-import"),
    ]
    async with pg_sessions() as session:
        restored = await session.scalar(
            select(RawHealthData).where(RawHealthData.google_data_point_id == gone)
        )
        # 9. Imported versions carry superseded_at, so `history --since` lists them.
        since = await record_history(session, SPEC.name, since=datetime(2026, 8, 1, tzinfo=UTC))
    assert restored is not None and restored.source_deleted_at is not None
    assert {item["google_data_point_id"] for item in since} == {NAME, gone}
    again = await run_import(pg_sessions, archive_url)
    assert (again.imported, again.restored_records) == (0, {})


# 8. Two writers upserting overlapping pages in different orders deadlocked.
async def test_overlapping_pages_lock_in_key_order(database_url, pg_sessions) -> None:
    first, last = f"{NAME}-a", f"{NAME}-z"
    await store(pg_sessions, steps(1, first), steps(1, last))
    other_engine = create_async_engine(database_url)
    other = async_sessionmaker(other_engine, expire_on_commit=False)
    try:
        async with other.begin() as holder:
            await store_records(holder, SPEC, [steps(2, first)])  # holds `first`
            # Page order z, a: without sorting it would lock `last`, then wait for `first`.
            writer = asyncio.create_task(store(pg_sessions, steps(3, last), steps(3, first)))
            await asyncio.sleep(0.3)
            assert not writer.done()
            await asyncio.wait_for(store_records(holder, SPEC, [steps(2, last)]), 10)
        await asyncio.wait_for(writer, 10)
    finally:
        await other_engine.dispose()
    assert [item[2] for item in await chain(pg_sessions, last)] == ["1", "2", "3"]
    assert [item[2] for item in await chain(pg_sessions, first)] == ["1", "2", "3"]
