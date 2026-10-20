"""Raw payload versions (migration 20261020_0011) on SQLite: write paths, view, verify, CLI."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from app.cli import main as cli
from app.core.config import get_settings
from app.db.base import Base
from app.google_health.data_types import REGISTRY
from app.models import raw_versions
from app.models.health import HeartRate
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.common import payload_hash, source_id
from app.services.raw_history import (
    import_versions,
    record_history,
    source_database_url,
    verify_versions,
)
from app.services.storage import recording_source, store_records

SPEC = REGISTRY["heart-rate"]
NAME = "users/1/dataTypes/heart-rate/dataPoints/abc"


def heart_rate(bpm: int, name: str = NAME) -> dict:
    return {
        "name": name,
        "heartRate": {
            "sampleTime": {"physicalTime": "2026-08-01T10:00:00Z", "utcOffset": "0s"},
            "beatsPerMinute": bpm,
        },
    }


async def store(sessions, *records, **kwargs):
    async with sessions.begin() as session:
        return await store_records(session, SPEC, list(records), **kwargs)


async def chain(sessions, google_id: str = NAME) -> list[tuple[int, bool, int, str]]:
    """(version, is_current, bpm, source) for one record, oldest first."""
    async with sessions() as session:
        records = await record_history(session, SPEC.name, google_id=google_id)
    if not records:
        return []
    return [
        (
            item["version"],
            item["is_current"],
            item["payload"]["heartRate"]["beatsPerMinute"],
            item["source"],
        )
        for item in records[0]["versions"]
    ]


async def test_changed_payload_keeps_both_versions(sessions):
    first = await store(sessions, heart_rate(60))
    changed = await store(sessions, heart_rate(65))
    assert (first.inserted, first.versions_kept) == (1, 0)
    assert (changed.updated, changed.versions_kept) == (1, 1)
    assert await chain(sessions) == [(1, False, 60, "sync"), (2, True, 65, "sync")]
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
        old = await session.scalar(select(RawHealthDataVersion))
        rate = await session.scalar(select(HeartRate))
        records = await record_history(session, SPEC.name, google_id=NAME)
    assert raw is not None and old is not None and rate is not None
    assert old.raw_id == raw.id
    assert old.payload_hash == payload_hash(heart_rate(60))
    assert old.superseded_at == raw.payload_received_at
    assert int(rate.bpm) == 65  # normalized tables keep the current version
    first_version, current = records[0]["versions"]
    assert first_version["valid_to"] == current["valid_from"]
    assert current["valid_to"] is None


async def test_same_payload_twice_adds_no_version(sessions):
    await store(sessions, heart_rate(60))
    again = await store(sessions, heart_rate(60))
    assert (again.unchanged, again.versions_kept) == (1, 0)
    assert await chain(sessions) == [(1, True, 60, "sync")]
    async with sessions() as session:
        assert await session.scalar(select(func.count(RawHealthDataVersion.id))) == 0


async def test_three_changes_number_versions_contiguously(sessions):
    for bpm in (60, 61, 62, 63):
        await store(sessions, heart_rate(bpm))
    assert await chain(sessions) == [
        (1, False, 60, "sync"),
        (2, False, 61, "sync"),
        (3, False, 62, "sync"),
        (4, True, 63, "sync"),
    ]
    async with sessions() as session:
        assert await verify_versions(session) == (3, [])


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        ((60, 70), [60, 70]),
        ((60, 60), [60]),
        ((60, 70, 70, 60), [60, 70, 60]),
    ],
    ids=["two-payloads", "identical-copies", "back-and-forth"],
)
async def test_batch_duplicates_become_versions_in_page_order(sessions, batch, expected):
    stats = await store(sessions, *(heart_rate(bpm) for bpm in batch))
    assert stats.inserted == 1 and stats.versions_kept == len(expected) - 1
    versions = await chain(sessions)
    assert [bpm for _, _, bpm, _ in versions] == expected
    assert [number for number, _, _, _ in versions] == list(range(1, len(expected) + 1))
    async with sessions() as session:
        assert (await verify_versions(session))[1] == []


async def test_batch_starting_with_the_stored_payload(sessions):
    await store(sessions, heart_rate(60))
    stats = await store(sessions, heart_rate(60), heart_rate(70))
    assert (stats.updated, stats.versions_kept) == (1, 1)
    assert [bpm for _, _, bpm, _ in await chain(sessions)] == [60, 70]


async def test_sources_follow_the_write_path(sessions):
    await store(sessions, heart_rate(60), source="backfill")
    with recording_source("webhook"):
        await store(sessions, heart_rate(61))
    with recording_source("reconciliation"):
        await store(sessions, heart_rate(62))
    await store(sessions, heart_rate(63))
    assert [item[3] for item in await chain(sessions)] == [
        "backfill",
        "webhook",
        "reconciliation",
        "sync",
    ]


async def test_reseeing_a_payload_keeps_its_arrival_and_source(sessions):
    await store(sessions, heart_rate(60), source="backfill")
    async with sessions() as session:
        before = await session.scalar(select(RawHealthData))
    await store(sessions, heart_rate(60))
    async with sessions() as session:
        after = await session.scalar(select(RawHealthData))
    assert before is not None and after is not None
    assert after.payload_source == "backfill"
    assert after.payload_received_at == before.payload_received_at
    assert after.last_seen_at > before.last_seen_at


async def test_history_rows_are_append_only(sessions):
    await store(sessions, heart_rate(60))
    await store(sessions, heart_rate(61))
    async with sessions.begin() as session:
        # Renumbering (archive import) is the one permitted change.
        await session.execute(text("UPDATE raw_health_data_versions SET version = 7"))
        await session.execute(text("UPDATE raw_health_data_versions SET version = 1"))
    for statement in (
        "UPDATE raw_health_data_versions SET raw_json = '{}'",
        "UPDATE raw_health_data_versions SET payload_hash = 'x'",
        "DELETE FROM raw_health_data_versions",
    ):
        with pytest.raises((IntegrityError, OperationalError), match="append-only"):
            async with sessions.begin() as session:
                await session.execute(text(statement))
    assert len(await chain(sessions)) == 2


async def test_verify_catches_tampered_history(sessions):
    for bpm in (60, 61, 62):
        await store(sessions, heart_rate(bpm))
    async with sessions.begin() as session:
        # A deliberate bypass of the append-only guard.
        await session.execute(text("DROP TRIGGER raw_health_data_versions_no_update"))
        await session.execute(
            text("UPDATE raw_health_data_versions SET raw_json = :raw WHERE version = 1"),
            {"raw": json.dumps(heart_rate(99))},
        )
        await session.execute(
            text("UPDATE raw_health_data_versions SET version = 5 WHERE version = 2")
        )
    async with sessions() as session:
        count, errors = await verify_versions(session)
    assert count == 2
    assert any(
        "version payload hash mismatch" in error and "version 1" in error for error in errors
    )
    assert any("not contiguous" in error for error in errors)


async def test_verify_catches_a_version_that_repeats_its_neighbour(sessions):
    await store(sessions, heart_rate(60))
    await store(sessions, heart_rate(61))
    async with sessions.begin() as session:
        raw_id = await session.scalar(select(RawHealthData.id))
        session.add(
            RawHealthDataVersion(
                raw_id=raw_id,
                data_type=SPEC.name,
                google_data_point_id=NAME,
                version=2,
                raw_json=heart_rate(61),
                payload_hash=payload_hash(heart_rate(61)),
                last_seen_at=datetime.now(UTC),
                source="sync",
            )
        )
    async with sessions() as session:
        _, errors = await verify_versions(session)
    assert len(errors) == 1
    assert errors[0].startswith("current version of raw row")
    assert errors[0].endswith("repeats the previous payload")


async def test_history_lists_changed_records_newest_change_first(sessions):
    other = "users/1/dataTypes/heart-rate/dataPoints/other"
    untouched = "users/1/dataTypes/heart-rate/dataPoints/untouched"
    await store(sessions, heart_rate(60), heart_rate(50, other), heart_rate(40, untouched))
    await store(sessions, heart_rate(61))
    await store(sessions, heart_rate(51, other))
    async with sessions() as session:
        listed = await record_history(session, SPEC.name, include_payload=False)
        future = await record_history(session, SPEC.name, since=datetime(2999, 1, 1, tzinfo=UTC))
        single = await record_history(session, SPEC.name, google_id=untouched)
    assert [item["google_data_point_id"] for item in listed] == [other, NAME]
    assert all("payload" not in version for item in listed for version in item["versions"])
    assert future == []
    assert [version["version"] for version in single[0]["versions"]] == [1]


def test_model_and_migration_ddl_are_the_same() -> None:
    path = Path("migrations/versions/20261020_0011_raw_versions.py")
    spec = importlib.util.spec_from_file_location("migration_0011", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def normalize(statements) -> list[str]:
        return [re.sub(r"\s+", " ", statement).strip() for statement in statements]

    assert normalize(migration.POSTGRESQL_DDL) == normalize(raw_versions.POSTGRESQL_DDL)
    assert normalize(migration.SQLITE_DDL) == normalize(raw_versions.SQLITE_DDL)


def _alembic(tmp_path, monkeypatch, name: str) -> tuple[Config, str]:
    database = (tmp_path / name).as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    get_settings.cache_clear()
    return Config("alembic.ini"), database


def test_migration_seeds_existing_rows_as_version_one(tmp_path, monkeypatch) -> None:
    config, database = _alembic(tmp_path, monkeypatch, "seed.db")
    try:
        command.upgrade(config, "20261020_0010")
        engine = create_engine(f"sqlite:///{database}")
        with engine.begin() as connection:
            for index in range(3):
                record = heart_rate(60 + index, f"{NAME}-{index}")
                connection.execute(
                    text(
                        "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                        "raw_json, payload_hash, first_seen_at, last_seen_at, is_quarantined, "
                        "created_at, updated_at) VALUES (:id, :gid, 'heart-rate', :raw, :hash, "
                        "'2026-08-01 10:00:00', '2026-08-02 10:00:00', 0, "
                        "'2026-08-01 10:00:00', '2026-08-02 10:00:00')"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "gid": f"{NAME}-{index}",
                        "raw": json.dumps(record),
                        "hash": payload_hash(record),
                    },
                )
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT version, is_current, source, valid_from, valid_to "
                    "FROM raw_health_data_history ORDER BY google_data_point_id"
                )
            ).all()
            history = connection.scalar(text("SELECT count(*) FROM raw_health_data_versions"))
        assert history == 0
        assert [tuple(row) for row in rows] == [(1, 1, "legacy", "2026-08-01 10:00:00", None)] * 3

        # The first change after the upgrade keeps the pre-upgrade payload as version 1.
        async def change() -> None:
            async_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
            try:
                sessions = async_sessionmaker(async_engine, expire_on_commit=False)
                await store(sessions, heart_rate(99, f"{NAME}-0"))
                assert await chain(sessions, f"{NAME}-0") == [
                    (1, False, 60, "legacy"),
                    (2, True, 99, "sync"),
                ]
            finally:
                await async_engine.dispose()

        asyncio.run(change())
        with pytest.raises(RuntimeError, match="allow-version-loss"):
            command.downgrade(config, "20261020_0010")
        engine.dispose()
    finally:
        get_settings.cache_clear()


def _legacy_dump(tmp_path, monkeypatch, name: str, rows: list[tuple[str, dict, str, str]]) -> str:
    """A restored pre-0010 dump stand-in at 0009: (google id, payload, hash, last_seen_at)."""
    config, database = _alembic(tmp_path, monkeypatch, f"{name}.db")
    command.upgrade(config, "20261020_0009")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        for google_id, record, digest, last_seen in rows:
            connection.execute(
                text(
                    "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                    "raw_json, payload_hash, first_seen_at, last_seen_at, is_quarantined, "
                    "created_at, updated_at) VALUES (:id, :gid, 'heart-rate', :raw, :hash, "
                    "'2026-07-01 00:00:00', :seen, 0, '2026-07-01 00:00:00', :seen)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "gid": google_id,
                    "raw": json.dumps(record),
                    "hash": digest,
                    "seen": last_seen,
                },
            )
    engine.dispose()
    get_settings.cache_clear()
    return f"sqlite+aiosqlite:///{database}"


async def test_import_slots_archived_versions_in_order_and_is_idempotent(
    sessions, tmp_path, monkeypatch
):
    # Live: 62 stored before the upgrade (legacy, arrival unknown), then replaced by 64.
    # Daily dumps saw 60, 61, 62 and 63; they are imported out of order on purpose.
    await store(sessions, heart_rate(62))
    async with sessions.begin() as session:
        await session.execute(
            text("UPDATE raw_health_data SET payload_source = NULL, last_seen_at = :seen"),
            {"seen": datetime(2026, 8, 3, tzinfo=UTC)},
        )
    corrupt = "users/1/dataTypes/heart-rate/dataPoints/corrupt"
    await store(sessions, heart_rate(70, corrupt))
    await store(sessions, heart_rate(64))
    missing = "users/1/dataTypes/heart-rate/dataPoints/only-in-archive"

    def row(bpm: int, seen: str, google_id: str = NAME) -> tuple[str, dict, str, str]:
        record = heart_rate(bpm, google_id)
        return google_id, record, payload_hash(record), seen

    # The stored hash no longer matches the text: reported, never imported.
    tampered = (
        corrupt,
        heart_rate(71, corrupt),
        payload_hash(heart_rate(72, corrupt)),
        "2026-08-03",
    )
    dumps = {
        "d4": [row(63, "2026-08-04 00:00:00")],
        "d2": [row(61, "2026-08-02 00:00:00"), row(1, "2026-08-02 00:00:00", missing)],
        "d1": [row(60, "2026-08-01 00:00:00")],
        "d3": [row(62, "2026-08-02 12:00:00"), tampered],
    }
    urls = {name: _legacy_dump(tmp_path, monkeypatch, name, rows) for name, rows in dumps.items()}

    async def run(url: str, **options):
        source = create_async_engine(url)
        try:
            return await import_versions(sessions, source, batch_size=1, **options)
        finally:
            await source.dispose()

    dry = await run(urls["d4"], dry_run=True)
    assert (dry.imported, len(dry.records)) == (1, 1)
    assert await chain(sessions) == [(1, False, 62, "legacy"), (2, True, 64, "sync")]

    results = {name: await run(url) for name, url in urls.items()}
    assert [results[name].imported for name in ("d4", "d2", "d1", "d3")] == [1, 1, 1, 0]
    # A record only the dump has is restored as its own row, marked source-deleted.
    assert results["d2"].restored_records == {"heart-rate": 1}
    assert results["d3"].already_present == 1
    assert results["d3"].source_hash_mismatches == [f"heart-rate/{corrupt}"]
    assert await chain(sessions) == [
        (1, False, 60, "archive-import"),
        (2, False, 61, "archive-import"),
        (3, False, 62, "legacy"),
        (4, False, 63, "archive-import"),
        (5, True, 64, "sync"),
    ]
    again = [await run(url) for url in urls.values()]
    assert sum(item.imported for item in again) == 0
    async with sessions() as session:
        raw = await session.scalar(
            select(RawHealthData).where(RawHealthData.google_data_point_id == NAME)
        )
        restored = await session.scalar(
            select(RawHealthData).where(RawHealthData.google_data_point_id == missing)
        )
        assert await verify_versions(session) == (4, [])
    assert raw is not None and raw.raw_json == heart_rate(64)
    assert restored is not None and restored.raw_json == heart_rate(1, missing)
    assert restored.source_deleted_at is not None
    assert restored.payload_source == "archive-import"
    assert sum(sum(item.restored_records.values()) for item in again) == 0
    # Later live changes continue the numbering.
    await store(sessions, heart_rate(65))
    assert [item[0] for item in await chain(sessions)] == [1, 2, 3, 4, 5, 6]


def test_source_database_url() -> None:
    target = "postgresql+asyncpg://health:secret@postgres:5432/health"
    assert source_database_url("health_restore", target) == (
        "postgresql+asyncpg://health:secret@postgres:5432/health_restore"
    )
    assert source_database_url("postgresql://u@h/x", target) == "postgresql+asyncpg://u@h/x"
    with pytest.raises(ValueError, match="live archive"):
        source_database_url("health", target)


def test_cli_history_prints_versions(tmp_path, monkeypatch) -> None:
    database = tmp_path / "cli.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await store(sessions, heart_rate(60))
        await store(sessions, heart_rate(65))

    asyncio.run(prepare())
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["history", "--data-type", "heart-rate", "--since", "2026-01-01"]
    )
    assert result.exit_code == 0, result.output
    records = json.loads(result.output)
    assert [item["version"] for item in records[0]["versions"]] == [1, 2]
    assert records[0]["versions"][0]["payload"] == heart_rate(60)
    missing = runner.invoke(cli.app, ["history", "--data-type", "heart-rate", "--id", "nope"])
    assert missing.exit_code == 1
    verify = runner.invoke(cli.app, ["verify"])
    assert "1 earlier versions" in verify.output
    asyncio.run(engine.dispose())


def test_synthetic_ids_are_versioned_too() -> None:
    # Rollup records without a Google name get a synthetic id from their interval, so
    # a recomputed value is a new version of the same record, not a new record.
    spec = REGISTRY["steps"]
    record = {"steps": {"interval": {"startTime": "a", "endTime": "b"}, "count": "1"}}
    changed = {"steps": {"interval": {"startTime": "a", "endTime": "b"}, "count": "2"}}
    assert source_id(spec, record) == source_id(spec, changed)


def test_offline_downgrade_script_checks_before_dropping_history(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        command.downgrade(Config("alembic.ini"), "20261020_0011:20261020_0010", sql=True)
        script = capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    guard = script.index("IF EXISTS (SELECT 1 FROM raw_health_data_versions)")
    assert "RAISE EXCEPTION" in script[guard:]
    assert guard < script.index("DROP TABLE raw_health_data_versions")


async def test_raw_records_cannot_be_deleted_or_rekeyed_on_sqlite(sessions):
    await store(sessions, heart_rate(60))
    for statement in (
        "DELETE FROM raw_health_data",
        "UPDATE raw_health_data SET google_data_point_id = 'other'",
    ):
        with pytest.raises((IntegrityError, OperationalError), match="refused"):
            async with sessions.begin() as session:
                await session.execute(text(statement))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 1


async def test_gap_repair_labels_its_versions(sessions, monkeypatch):
    from app.google_health.client import Page

    path = Path("scripts/audit_date_gaps.py")
    spec = importlib.util.spec_from_file_location("audit_date_gaps", path)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    await store(sessions, heart_rate(60))
    gaps = iter(
        [
            [
                {
                    "data_type": SPEC.name,
                    "missing_from": date(2026, 8, 1),
                    "missing_to": date(2026, 8, 1),
                }
            ],
            [],
        ]
    )

    async def audit(zone, end):
        return next(gaps)

    async def pages(client, spec_, start, end):
        yield Page([heart_rate(61)], None)

    class Runtime:
        google = None
        incremental = type("Incremental", (), {"specs": {SPEC.name: SPEC}})()

        async def shutdown(self):
            pass

    monkeypatch.setattr(script, "audit", audit)
    monkeypatch.setattr(script, "iter_pages", pages)
    monkeypatch.setattr(script, "SessionLocal", sessions)
    monkeypatch.setattr(script, "build_runtime", lambda settings, sessions_: Runtime())
    monkeypatch.setattr(script, "get_settings", lambda: None)
    await script.main(type("Args", (), {"repair": True, "timezone": "UTC"})())
    assert await chain(sessions) == [(1, False, 60, "sync"), (2, True, 61, "gap-repair")]
