from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import cast as cast_type

from sqlalchemy import Table, Text, and_, case, cast, func, insert, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import event
from app.core.provenance import current_source, provenance
from app.google_health.capture import CapturedResponse
from app.google_health.data_types import DataTypeSpec
from app.models.raw_versions import RawApiResponse
from app.models.state import RawHealthData
from app.parsers import normalize
from app.parsers.common import payload_hash, temporal
from app.services.identity import add_aliases, resolve_ids

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


@contextmanager
def recording_source(source: str) -> Iterator[None]:
    """The write path for records stored (and responses fetched) in the enclosed work."""
    with provenance(source=source):
        yield


async def insert_responses(
    session: AsyncSession, responses: Sequence[CapturedResponse]
) -> list[int]:
    """Append Google responses to raw_api_responses; returns their ids in order."""
    if not responses:
        return []
    table = cast_type(Table, RawApiResponse.__table__)
    statement = insert(table).returning(table.c.id, sort_by_parameter_order=True)
    result = await session.execute(statement, [response.row() for response in responses])
    return list(result.scalars().all())


async def store_page(
    session: AsyncSession,
    spec: DataTypeSpec,
    page,
    *,
    source: str | None = None,
    fetch_started: datetime | None = None,
) -> StoreStats:
    """Store a fetched page: its Google responses and its records, in one transaction.

    ``fetch_started``: when the fetch (the run of pages) this page belongs to began."""
    return await store_records(
        session,
        spec,
        page.data_points,
        source=source,
        responses=page.responses,
        fetch_started=fetch_started,
    )


class _Collision(Exception):
    """A row this page writes onto changed, or a row it creates was created, meanwhile."""


# A page re-resolves this often when a concurrent writer of the same records got there first.
WRITE_ATTEMPTS = 4


async def store_records(
    session: AsyncSession,
    spec: DataTypeSpec,
    records: Sequence[object],
    *,
    source: str | None = None,
    responses: Sequence[CapturedResponse] = (),
    fetch_started: datetime | None = None,
) -> StoreStats:
    """Archive one page of records; a changed payload keeps the previous one as a version.

    ``raw_health_data`` is overwritten with the newest payload, and the trigger from
    migration 20261020_0011 copies the replaced row into ``raw_health_data_versions`` in
    the same statement. A record that appears more than once in ``records`` with
    different payloads is written once per distinct payload, in order, so each becomes
    its own version (consecutive identical copies collapse into one).

    ``responses`` are the HTTP exchanges the records were read from: they are appended to
    ``raw_api_responses`` first, in this same transaction, and each new payload is linked
    to the last of them (``response_id``), so a crash keeps both or neither.

    Records without a Google id are written onto the row that holds their identity
    (app/services/identity.py). The rows written onto are locked and checked against what
    the resolution read; new rows are created with ``ON CONFLICT DO NOTHING``. When another
    writer changed or created one of them meanwhile, the page is resolved again (inside a
    savepoint), so a payload is never written onto a row that holds another source.

    The time written to ``last_seen_at`` is read after the data type's lock was granted, and
    ``last_seen_at`` never moves backwards (``GREATEST``): a writer that waited for the lock
    cannot make a record look unseen to a fetch that started meanwhile.

    ``fetch_started``: when the fetch that returned these records began (its first page; a
    resumed fetch passes the original start). Kept for the caller's bookkeeping; deletion
    marking reads it from the fetch itself.
    """
    origin = source or current_source()
    response_ids = await insert_responses(session, responses)
    response_id = response_ids[-1] if response_ids else None
    digests = [payload_hash(record) for record in records]
    if session.get_bind().dialect.name != "postgresql":
        # One writer at a time (tests): nothing to collide with.
        return await _store(session, spec, records, digests, origin, response_id)
    for _ in range(WRITE_ATTEMPTS):
        try:
            async with session.begin_nested():
                return await _store(session, spec, records, digests, origin, response_id)
        except _Collision:
            event(logger, "RECORD_BATCH_RESOLVED_AGAIN", data_type=spec.name)
    raise RuntimeError(
        f"{spec.name}: a concurrent writer kept changing the rows of this page; "
        "nothing of it was stored (the next sync fetches it again)"
    )


async def _lock_targets(session: AsyncSession, spec: DataTypeSpec, resolution) -> None:
    """Lock the rows the page writes onto (key order, like the upserts) and check that each
    still holds the payload it was resolved against."""
    if not resolution.expected or session.get_bind().dialect.name != "postgresql":
        return
    from app.services.identity import among

    table = RawHealthData
    current = dict(
        (
            await session.execute(
                select(table.id, table.payload_hash)
                .where(among(session, table.id, sorted(resolution.expected)))
                .order_by(table.google_data_point_id)
                .with_for_update()
            )
        )
        .tuples()
        .all()
    )
    if any(current.get(raw_id) != digest for raw_id, digest in resolution.expected.items()):
        raise _Collision


async def _store(
    session: AsyncSession,
    spec: DataTypeSpec,
    records: Sequence[object],
    digests: Sequence[str],
    origin: str,
    response_id: int | None,
) -> StoreStats:
    stats = StoreStats()
    resolution = await resolve_ids(session, spec, records, digests)
    await _lock_targets(session, spec, resolution)
    # After the data type's lock and the row locks: a writer that waited never stamps an
    # older time than a fetch that began while it waited.
    now = datetime.now(UTC)
    # Per record id, the distinct consecutive payloads of this page, in page order.
    by_id: dict[str, list[tuple]] = {}
    duplicates = 0
    for record, google_id, digest in zip(records, resolution.ids, digests, strict=True):
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
    created = sorted(google_id for google_id in by_id if google_id in resolution.created)
    if any(google_id in existing_hashes for google_id in created):
        raise _Collision
    if created:
        rows = [
            _raw_row(spec, by_id[google_id][0], now, origin, response_id) for google_id in created
        ]
        inserted = await _insert_new(session, rows)
        if inserted != set(created):
            raise _Collision

    # One INSERT ... ON CONFLICT statement may not touch the same row twice on
    # PostgreSQL, so the n-th payload of every record goes into the n-th statement (the
    # first payload of a new row is in already).
    rounds: list[list[dict]] = []
    normalized_values = defaultdict(list)
    new_rows = set(created)
    for google_id, sequence in by_id.items():
        for position, item in enumerate(sequence):
            if position == 0 and google_id in new_rows:
                continue
            while len(rounds) <= position:
                rounds.append([])
            rounds[position].append(_raw_row(spec, item, now, origin, response_id))
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
            # Keyed like its raw row, which may be an older id of the same record.
            values = {**normalized.values, "google_data_point_id": google_id}
            normalized_values[normalized.model].append(values)
            stats.normalized += 1
    # Concurrent writers (a CLI sync next to the server, backfill next to a webhook) lock
    # rows in key order, so two overlapping pages wait for each other instead of
    # deadlocking.
    for rows in rounds:
        if rows:
            rows.sort(key=lambda row: row["google_data_point_id"])
            await _upsert_raw_many(session, rows)
    await add_aliases(session, spec.name, resolution.new_aliases)
    for model, values in normalized_values.items():
        values.sort(key=lambda row: (row.get("data_type", ""), row["google_data_point_id"]))
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
            chained=resolution.chained,
        )
    return stats


async def _insert_new(session: AsyncSession, rows: list[dict]) -> set[str]:
    """Insert new rows; the ids that were inserted (one another writer created first is not)."""
    table = RawHealthData.__table__
    inserted: set[str] = set()
    for offset in range(0, len(rows), 1000):
        statement = (
            dialect_insert(session, table)
            .values(rows[offset : offset + 1000])
            .on_conflict_do_nothing(index_elements=["data_type", "google_data_point_id"])
            .returning(table.c.google_data_point_id)
        )
        inserted.update((await session.execute(statement)).scalars().all())
    return inserted


def _raw_row(
    spec: DataTypeSpec, item: tuple, now: datetime, origin: str, response_id: int | None
) -> dict:
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
        "response_id": response_id,
    }


async def _upsert_normalized_many(session: AsyncSession, model, rows: list[dict]) -> None:
    if not rows:
        return
    table = model.__table__
    statement = dialect_insert(session, table)
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
    statement = dialect_insert(session, table)
    highest = func.greatest if session.get_bind().dialect.name == "postgresql" else func.max
    # The payload is overwritten in place; when its hash or stored text changes, the
    # trigger of migration 20261020_0011 has already copied the replaced row into
    # raw_health_data_versions. When and how a payload arrived only changes with it.
    unchanged = and_(
        table.c.payload_hash == statement.excluded.payload_hash,
        cast(table.c.raw_json, Text) == cast(statement.excluded.raw_json, Text),
    )
    updates = {
        "source_start_time": statement.excluded.source_start_time,
        "source_end_time": statement.excluded.source_end_time,
        "source_timezone": statement.excluded.source_timezone,
        "raw_json": statement.excluded.raw_json,
        "payload_hash": statement.excluded.payload_hash,
        # Never backwards: a writer that waited must not make the record look older.
        "last_seen_at": highest(table.c.last_seen_at, statement.excluded.last_seen_at),
        "source_deleted_at": None,
        "payload_received_at": case(
            (unchanged, table.c.payload_received_at),
            else_=statement.excluded.payload_received_at,
        ),
        "payload_source": case(
            (unchanged, table.c.payload_source), else_=statement.excluded.payload_source
        ),
        "response_id": case((unchanged, table.c.response_id), else_=statement.excluded.response_id),
        "updated_at": datetime.now(UTC),
    }
    statement = statement.on_conflict_do_update(
        index_elements=["data_type", "google_data_point_id"],
        set_=updates,
    )
    await session.execute(statement, rows)


def dialect_insert(session: AsyncSession, table):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql_insert(table)
    if dialect == "sqlite":
        return sqlite_insert(table)
    raise RuntimeError(f"Unsupported database dialect for UPSERT: {dialect}")
