"""store_records upserts on PostgreSQL: batch duplicates collapse to one row, last wins."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from app.google_health.data_types import REGISTRY
from app.models.health import ScalarMetric, Steps
from app.models.state import RawHealthData
from app.services.storage import store_records

pytestmark = pytest.mark.integration


def steps(count: int, *, name: str | None = None) -> dict:
    record = {
        "dataSource": {"platform": "FITBIT"},
        "steps": {
            "interval": {
                "startTime": "2026-08-01T10:00:00Z",
                "endTime": "2026-08-01T10:01:00Z",
            },
            "count": str(count),
        },
    }
    if name:
        record["name"] = name
    return record


def body_fat(percentage: float) -> dict:
    # No "name": a synthetic id, stored in the shared scalar_metrics table.
    return {
        "dataSource": {"platform": "FITBIT"},
        "bodyFat": {
            "sampleTime": {"physicalTime": "2026-08-01T07:00:00Z"},
            "percentage": percentage,
        },
    }


async def counts(session, model) -> tuple[int, int]:
    raw = await session.scalar(select(func.count()).select_from(RawHealthData))
    normalized = await session.scalar(select(func.count()).select_from(model))
    return raw, normalized


async def test_single_statement_upsert_rejects_batch_duplicates(pg_sessions) -> None:
    """Why store_records dedupes: one multi-row INSERT ... ON CONFLICT DO UPDATE fails.

    Today's executemany path (asyncpg, no RETURNING) runs the statement once per row, so
    a duplicate silently overwrites and only skews the stats; batching the upsert into a
    single statement (insertmanyvalues, e.g. once RETURNING is added) would fail outright.
    """
    now = datetime.now(UTC)
    row = {
        "google_data_point_id": "dup",
        "data_type": "steps",
        "source_start_time": now,
        "source_end_time": now,
        "source_timezone": None,
        "raw_json": {},
        "payload_hash": "0" * 64,
        "first_seen_at": now,
        "last_seen_at": now,
        "source_deleted_at": None,
    }
    statement = insert(RawHealthData).values([row, {**row, "payload_hash": "1" * 64}])
    statement = statement.on_conflict_do_update(
        index_elements=["data_type", "google_data_point_id"],
        set_={"payload_hash": statement.excluded.payload_hash},
    )
    async with pg_sessions() as session:
        with pytest.raises(DBAPIError, match="cannot affect row a second time"):
            await session.execute(statement)


@pytest.mark.parametrize(
    ("data_type", "model", "make", "values", "column", "expected"),
    [
        ("steps", Steps, steps, (100, 250), Steps.count, 250),
        ("steps", Steps, lambda n: steps(n, name="users/me/dataPoints/a"), (5, 7), Steps.count, 7),
        ("body-fat", ScalarMetric, body_fat, (21.5, 22.0), ScalarMetric.value, 22.0),
    ],
    ids=["synthetic-steps", "named-steps", "synthetic-scalar"],
)
async def test_batch_duplicates_store_one_row_last_wins(
    pg_sessions, data_type, model, make, values, column, expected
) -> None:
    spec = REGISTRY[data_type]
    first, second = (make(value) for value in values)

    # Insert path: both copies in one batch, nothing stored yet.
    async with pg_sessions.begin() as session:
        stats = await store_records(session, spec, [first, second])
    assert (stats.inserted, stats.updated, stats.unchanged) == (1, 0, 0)

    async with pg_sessions() as session:
        assert await counts(session, model) == (1, 1)
        assert float(await session.scalar(select(column))) == expected

    # Update path: the row exists and the batch repeats it again, older copy last.
    async with pg_sessions.begin() as session:
        stats = await store_records(session, spec, [second, first, first])
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 1, 0)

    async with pg_sessions() as session:
        assert await counts(session, model) == (1, 1)
        assert float(await session.scalar(select(column))) == float(values[0])
        raw = await session.scalar(select(RawHealthData))
    assert raw.source_deleted_at is None
    assert raw.last_seen_at >= raw.first_seen_at
