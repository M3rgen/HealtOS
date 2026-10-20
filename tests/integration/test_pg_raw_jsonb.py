"""Migration 20261020_0010: raw_json json -> jsonb keeps every stored value and hash."""

from __future__ import annotations

import json
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.google_health.data_types import REGISTRY
from app.models.state import RawHealthData, WebhookEvent, WebhookNotification
from app.parsers.common import payload_hash, source_id
from app.services.storage import store_records
from app.services.webhook_processing import WebhookBatchProcessor

pytestmark = pytest.mark.integration

BEFORE = "20261020_0009"
AFTER = "20261020_0010"
TABLES = ("raw_health_data", "webhook_events", "webhook_notifications")

# Synthetic payloads covering what jsonb normalizes: key order, nesting, non-ASCII text,
# escapes, floats in both notations Python writes, big integers and non-object values.
STEPS = {
    "steps": {
        "interval": {"startTime": "2026-08-01T10:00:00Z", "endTime": "2026-08-01T10:01:00Z"},
        "count": "42",
    },
    "dataSource": {"platform": "FITBIT", "device": {"displayName": "Örnek Saat ğüşıöç ✓"}},
}
RECORDS: list[object] = [
    STEPS,
    {"z": 1, "a": [3, 2, 1], "m": {"y": None, "b": True}, "note": 'quote " and \\ and \n'},
    {"heartRate": {"beatsPerMinute": 71, "variance": 0.1, "tiny": 1e-05, "w": 72.5}},
    {"bigInt": 12345678901234567890, "neg": -3, "zero": 0, "float_zero": 0.0},
    "corrupt-but-preserved",
    [1, "two", {"three": 3.0}],
]
WEBHOOK_BATCH = [
    {"data": {"dataType": "steps", "operation": "UPSERT", "healthUserId": "user-1"}},
    {"data": {"dataType": "sleep", "operation": "DELETE", "healthUserId": "user-1"}},
]


def _sync_url(database_url: str) -> str:
    return database_url.replace("+asyncpg", "+psycopg")


def _column_types(engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, data_type FROM information_schema.columns "
                "WHERE column_name = 'raw_json' AND table_name = ANY(:tables)"
            ),
            {"tables": list(TABLES)},
        )
        return dict(rows.tuples().all())


def _version(engine) -> str:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _insert_as_json(engine) -> dict[str, object]:
    """Write rows the way the 0009 app did: json.dumps text into a json column."""
    expected: dict[str, object] = {}
    with engine.begin() as connection:
        for index, record in enumerate(RECORDS):
            # The steps record keeps the id store_records derives, so re-storing finds it.
            google_id = source_id(REGISTRY["steps"], record) if index == 0 else f"other-{index}"
            # Non-default spacing: json keeps the text as written, jsonb normalizes it.
            raw = json.dumps(record, indent=2 if index % 2 else None)
            connection.execute(
                text(
                    "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                    "raw_json, payload_hash, first_seen_at, last_seen_at, is_quarantined, "
                    "created_at, updated_at) VALUES (:id, :gid, 'steps', CAST(:raw AS json), "
                    ":hash, now(), now(), false, now(), now())"
                ),
                {"id": uuid.uuid4(), "gid": google_id, "raw": raw, "hash": payload_hash(record)},
            )
            expected[google_id] = record
        connection.execute(
            text(
                "INSERT INTO webhook_events (payload_hash, raw_json, element_count, "
                "failure_count, created_at, updated_at) "
                "VALUES ('batch-1', CAST(:raw AS json), 0, 0, now(), now())"
            ),
            {"raw": json.dumps(WEBHOOK_BATCH)},
        )
        for item in WEBHOOK_BATCH:
            connection.execute(
                text(
                    "INSERT INTO webhook_notifications (element_hash, raw_json, is_valid, "
                    "status, attempt_count, sync_attempt_count, sync_success_count, "
                    "created_at, updated_at) VALUES (:hash, CAST(:raw AS json), true, "
                    "'pending', 0, 0, 0, now(), now())"
                ),
                {"hash": payload_hash(item), "raw": json.dumps(item)},
            )
    return expected


async def _assert_app_reads_unchanged(database_url: str, expected: dict[str, object]) -> None:
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            rows = (await session.execute(select(RawHealthData))).scalars().all()
            assert {row.google_data_point_id: row.raw_json for row in rows} == expected
            for row in rows:
                # What `health-mirror verify` checks for every archived record.
                assert payload_hash(row.raw_json) == row.payload_hash
            batch = await session.get(WebhookEvent, "batch-1")
            assert batch is not None and batch.raw_json == WEBHOOK_BATCH
            notifications = (await session.execute(select(WebhookNotification))).scalars().all()
            assert {n.element_hash: n.raw_json for n in notifications} == {
                payload_hash(item): item for item in WEBHOOK_BATCH
            }

        # Re-storing the same Google record compares hashes and leaves it unchanged.
        async with sessions.begin() as session:
            stats = await store_records(session, REGISTRY["steps"], [STEPS])
        assert (stats.inserted, stats.updated, stats.unchanged) == (0, 0, 1)

        # Rebuilding a legacy batch from its stored raw_json yields the same element
        # hashes, so no notification is duplicated.
        processor = WebhookBatchProcessor(sessions, None, {"steps", "sleep"})  # type: ignore[arg-type]
        await processor.prepare_existing_batch("batch-1")
        async with sessions() as session:
            hashes = (await session.scalars(select(WebhookNotification.element_hash))).all()
        assert sorted(hashes) == sorted(payload_hash(item) for item in WEBHOOK_BATCH)
    finally:
        await engine.dispose()


async def test_raw_json_jsonb_round_trip_keeps_values_and_hashes(
    database_url, alembic_config
) -> None:
    engine = create_engine(_sync_url(database_url))
    try:
        command.upgrade(alembic_config, BEFORE)
        assert set(_column_types(engine).values()) == {"json"}
        expected = _insert_as_json(engine)
        await _assert_app_reads_unchanged(database_url, expected)

        command.upgrade(alembic_config, AFTER)
        assert _version(engine) == AFTER
        assert _column_types(engine) == dict.fromkeys(TABLES, "jsonb")
        await _assert_app_reads_unchanged(database_url, expected)
        command.check(alembic_config)

        # New writes after the upgrade go through the JSONB type too.
        writer = create_async_engine(database_url)
        try:
            async with async_sessionmaker(writer, expire_on_commit=False).begin() as session:
                new = {**STEPS, "name": "new-1"}
                stats = await store_records(session, REGISTRY["steps"], [new])
                stored = await session.scalar(
                    select(RawHealthData).where(RawHealthData.google_data_point_id == "new-1")
                )
        finally:
            await writer.dispose()
        assert stats.inserted == 1
        assert stored is not None and stored.raw_json == new
        assert payload_hash(stored.raw_json) == stored.payload_hash
        expected["new-1"] = new

        command.downgrade(alembic_config, BEFORE)
        assert _version(engine) == BEFORE
        assert set(_column_types(engine).values()) == {"json"}
        await _assert_app_reads_unchanged(database_url, expected)

        command.upgrade(alembic_config, "head")
        assert _column_types(engine) == dict.fromkeys(TABLES, "jsonb")
        await _assert_app_reads_unchanged(database_url, expected)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        # jsonb has no negative zero, and stores 1e+16 as an integer: both would hash
        # differently after the upgrade, so the migration refuses.
        ('{"elevation": -0.0}', "would change"),
        ('{"huge": 1e+16}', "would change"),
        # jsonb cannot hold \u0000 at all; the cast fails.
        ('{"label": "a\\u0000b"}', "u0000"),
    ],
)
def test_upgrade_refuses_payloads_jsonb_would_alter(
    database_url, alembic_config, raw: str, message: str
) -> None:
    engine = create_engine(_sync_url(database_url))
    try:
        command.upgrade(alembic_config, BEFORE)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                    "raw_json, payload_hash, first_seen_at, last_seen_at, is_quarantined, "
                    "created_at, updated_at) VALUES (gen_random_uuid(), 'edge', 'steps', "
                    "CAST(:raw AS json), :hash, now(), now(), false, now(), now())"
                ),
                {"raw": raw, "hash": payload_hash(json.loads(raw))},
            )
        with pytest.raises(Exception, match=message):
            command.upgrade(alembic_config, AFTER)
        # Rolled back: still on 0009, still json, the stored text untouched.
        assert _version(engine) == BEFORE
        assert set(_column_types(engine).values()) == {"json"}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT raw_json::text FROM raw_health_data")) == raw
    finally:
        engine.dispose()
