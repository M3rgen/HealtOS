from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise

from sqlalchemy import case, select
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
    # Replaced payloads preserved in raw_health_data_versions.
    versions_kept: int = 0

    def add(self, other: StoreStats) -> None:
        for field in self.__dataclass_fields__:
            setattr(self, field, getattr(self, field) + getattr(other, field))


# The write path that delivered the records being stored (raw_health_data.payload_source,
# raw_health_data_versions.source). Callers that run the incremental sync on behalf of
# something else (webhooks, reconciliation) set it around the call; see recording_source.
_source: ContextVar[str] = ContextVar("raw_payload_source", default="sync")


@contextmanager
def recording_source(source: str) -> Iterator[None]:
    token = _source.set(source)
    try:
        yield
    finally:
        _source.reset(token)


async def store_records(
    session: AsyncSession,
    spec: DataTypeSpec,
    records: Sequence[object],
    *,
    source: str | None = None,
) -> StoreStats:
    """Archive one page of records; a changed payload keeps the previous one as a version.

    ``raw_health_data`` is overwritten with the newest payload, and the trigger from
    migration 20261020_0011 copies the replaced row into ``raw_health_data_versions`` in
    the same statement. A record that appears more than once in ``records`` with
    different payloads is written once per distinct payload, in order, so each becomes
    its own version (consecutive identical copies collapse into one).
    """
    stats = StoreStats()
    now = datetime.now(UTC)
    origin = source or _source.get()
    # Per record id, the distinct consecutive payloads of this page, in page order.
    by_id: dict[str, list[tuple]] = {}
    duplicates = 0
    for record in records:
        google_id = source_id(spec, record)
        digest = payload_hash(record)
        start = end = timezone = None
        if isinstance(record, dict):
            start, end, timezone = temporal(spec, record)
        sequence = by_id.setdefault(google_id, [])
        if sequence:
            duplicates += 1
            if sequence[-1][2] == digest:
                continue
            event(logger, "RECORD_BATCH_DUPLICATE_CONFLICT", data_type=spec.name)
        sequence.append((record, google_id, digest, start, end, timezone))

    existing_hashes: dict[str, str] = {}
    google_ids = list(by_id)
    for offset in range(0, len(google_ids), 5000):
        rows = await session.execute(
            select(RawHealthData.google_data_point_id, RawHealthData.payload_hash).where(
                RawHealthData.data_type == spec.name,
                RawHealthData.google_data_point_id.in_(google_ids[offset : offset + 5000]),
            )
        )
        existing_hashes.update(rows.tuples().all())

    # One INSERT ... ON CONFLICT statement may not touch the same row twice on
    # PostgreSQL, so the n-th payload of every record goes into the n-th statement.
    rounds: list[list[dict]] = []
    normalized_values = defaultdict(list)
    for google_id, sequence in by_id.items():
        for position, item in enumerate(sequence):
            if position == len(rounds):
                rounds.append([])
            rounds[position].append(_raw_row(spec, item, now, origin))
        existing_hash = existing_hashes.get(google_id)
        hashes = [existing_hash, *(item[2] for item in sequence)]
        replaced = sum(old is not None and old != new for old, new in pairwise(hashes))
        stats.versions_kept += replaced
        if existing_hash is None:
            stats.inserted += 1
        elif replaced:
            stats.updated += 1
        else:
            stats.unchanged += 1
        normalized = normalize(spec, sequence[-1][0])
        if normalized is None:
            stats.normalization_skipped += 1
        elif existing_hash is None or replaced:
            normalized_values[normalized.model].append(normalized.values)
            stats.normalized += 1
    for rows in rounds:
        await _upsert_raw_many(session, rows)
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
            versions_kept=stats.versions_kept,
        )
    return stats


def _raw_row(spec: DataTypeSpec, item: tuple, now: datetime, origin: str) -> dict:
    record, google_id, digest, start, end, timezone = item
    return {
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
        "payload_received_at": now,
        "payload_source": origin,
    }


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
    # The payload is overwritten in place; when its hash changes, the trigger of
    # migration 20261020_0011 has already copied the replaced row into
    # raw_health_data_versions. When and how a payload arrived only changes with it.
    unchanged = table.c.payload_hash == statement.excluded.payload_hash
    updates = {
        "source_start_time": statement.excluded.source_start_time,
        "source_end_time": statement.excluded.source_end_time,
        "source_timezone": statement.excluded.source_timezone,
        "raw_json": statement.excluded.raw_json,
        "payload_hash": statement.excluded.payload_hash,
        "last_seen_at": statement.excluded.last_seen_at,
        "source_deleted_at": None,
        "payload_received_at": case(
            (unchanged, table.c.payload_received_at),
            else_=statement.excluded.payload_received_at,
        ),
        "payload_source": case(
            (unchanged, table.c.payload_source), else_=statement.excluded.payload_source
        ),
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
