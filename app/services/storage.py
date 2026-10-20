from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import event
from app.google_health.data_types import DataTypeSpec
from app.models.state import RawHealthData
from app.parsers import normalize
from app.parsers.common import payload_hash, source_id, temporal

logger = logging.getLogger(__name__)


@dataclass
class StoreStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    normalized: int = 0
    normalization_skipped: int = 0
    # Archived records a complete window re-fetch no longer returned (see app.sync.deletions).
    source_deleted: int = 0

    def add(self, other: StoreStats) -> None:
        for field in self.__dataclass_fields__:
            setattr(self, field, getattr(self, field) + getattr(other, field))


async def store_records(
    session: AsyncSession, spec: DataTypeSpec, records: list[object]
) -> StoreStats:
    stats = StoreStats()
    now = datetime.now(UTC)
    # One INSERT ... ON CONFLICT statement may not touch the same row twice on
    # PostgreSQL, and synthetic rollup IDs can repeat within a page: last one wins.
    by_id: dict[str, tuple] = {}
    duplicates = 0
    for record in records:
        google_id = source_id(spec, record)
        digest = payload_hash(record)
        start = end = timezone = None
        if isinstance(record, dict):
            start, end, timezone = temporal(spec, record)
        if (previous := by_id.pop(google_id, None)) is not None:
            duplicates += 1
            if previous[2] != digest:
                event(logger, "RECORD_BATCH_DUPLICATE_CONFLICT", data_type=spec.name)
        by_id[google_id] = (record, google_id, digest, start, end, timezone)
    prepared = list(by_id.values())

    existing_hashes: dict[str, str] = {}
    google_ids = [item[1] for item in prepared]
    for offset in range(0, len(google_ids), 5000):
        rows = await session.execute(
            select(RawHealthData.google_data_point_id, RawHealthData.payload_hash).where(
                RawHealthData.data_type == spec.name,
                RawHealthData.google_data_point_id.in_(google_ids[offset : offset + 5000]),
            )
        )
        existing_hashes.update(rows.all())

    raw_values = []
    normalized_values = defaultdict(list)
    for record, google_id, digest, start, end, timezone in prepared:
        existing_hash = existing_hashes.get(google_id)
        inserted = existing_hash is None
        changed = inserted or existing_hash != digest
        raw_values.append(
            {
                "google_data_point_id": google_id,
                "data_type": spec.name,
                "source_start_time": start,
                "source_end_time": end,
                "source_timezone": timezone,
                "raw_json": record,
                "payload_hash": digest,
                "first_seen_at": now,
                "last_seen_at": now,
                "source_deleted_at": None,
            }
        )
        if inserted:
            stats.inserted += 1
        elif changed:
            stats.updated += 1
        else:
            stats.unchanged += 1
        normalized = normalize(spec, record)
        if normalized is None:
            stats.normalization_skipped += 1
        elif changed:
            normalized_values[normalized.model].append(normalized.values)
            stats.normalized += 1
    await _upsert_raw_many(session, raw_values)
    for model, values in normalized_values.items():
        await _upsert_normalized_many(session, model, values)
    if records:
        event(
            logger,
            "RECORD_BATCH_STORED",
            data_type=spec.name,
            received=len(records),
            inserted=stats.inserted,
            updated=stats.updated,
            unchanged=stats.unchanged,
            duplicates=duplicates,
        )
    return stats


async def _upsert_normalized_many(session: AsyncSession, model, rows: list[dict]) -> None:
    if not rows:
        return
    table = model.__table__
    statement = _insert(session, table)
    conflict_columns = ["google_data_point_id"]
    if "data_type" in rows[0]:
        conflict_columns.insert(0, "data_type")
    updates = {key: getattr(statement.excluded, key) for key in rows[0]}
    updates["updated_at"] = datetime.now(UTC)
    statement = statement.on_conflict_do_update(
        index_elements=conflict_columns,
        set_=updates,
    )
    await session.execute(statement, rows)


async def _upsert_raw_many(session: AsyncSession, rows: list[dict]) -> None:
    if not rows:
        return
    table = RawHealthData.__table__
    statement = _insert(session, table)
    updates = {
        "source_start_time": statement.excluded.source_start_time,
        "source_end_time": statement.excluded.source_end_time,
        "source_timezone": statement.excluded.source_timezone,
        "raw_json": statement.excluded.raw_json,
        "payload_hash": statement.excluded.payload_hash,
        "last_seen_at": statement.excluded.last_seen_at,
        "source_deleted_at": None,
        "updated_at": datetime.now(UTC),
    }
    statement = statement.on_conflict_do_update(
        index_elements=["data_type", "google_data_point_id"],
        set_=updates,
    )
    await session.execute(statement, rows)


def _insert(session: AsyncSession, table):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql_insert(table)
    if dialect == "sqlite":
        return sqlite_insert(table)
    raise RuntimeError(f"Unsupported database dialect for UPSERT: {dialect}")
