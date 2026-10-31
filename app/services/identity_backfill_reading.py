"""Archive row reads and parsing for full and incremental identity reconciliation."""

from __future__ import annotations

import heapq
import uuid
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.timeutil import as_utc
from app.google_health.data_types import DataTypeSpec
from app.models.identity import RawHealthDataSupersession
from app.models.raw_versions import RawHealthDataEvent
from app.models.state import RawHealthData
from app.parsers.common import google_name
from app.parsers.identity import RecordIdentity, instant, record_identity
from app.services.identity import among
from app.services.identity_backfill_stats import BackfillStats


@dataclass
class _Row:
    raw_id: uuid.UUID
    google_data_point_id: str
    raw_json: Any
    first_seen_at: datetime
    last_seen_at: datetime
    deleted: bool
    identity: RecordIdentity
    payload_hash: str = ""
    quarantined: bool = False

    @property
    def order(self) -> tuple:
        return (not self.deleted, self.last_seen_at, self.first_seen_at, str(self.raw_id))


_COLUMNS = (
    RawHealthData.id,
    RawHealthData.google_data_point_id,
    RawHealthData.raw_json,
    RawHealthData.source_start_time,
    RawHealthData.first_seen_at,
    RawHealthData.last_seen_at,
    RawHealthData.source_deleted_at,
    RawHealthData.payload_received_at,
    RawHealthData.payload_hash,
    RawHealthData.is_quarantined,
)


async def _read_batch(
    session: AsyncSession,
    data_type: str,
    batch_size: int,
    cursor: tuple[datetime, uuid.UUID] | None,
    after: datetime | None,
) -> list:
    query = select(*_COLUMNS).where(
        RawHealthData.data_type == data_type, RawHealthData.source_start_time.is_not(None)
    )
    if cursor is not None:
        query = query.where(tuple_(RawHealthData.source_start_time, RawHealthData.id) > cursor)
    elif after is not None:
        query = query.where(RawHealthData.source_start_time >= after)
    order = (RawHealthData.source_start_time, RawHealthData.id)
    rows = list((await session.execute(query.order_by(*order).limit(batch_size))).tuples())
    if len(rows) < batch_size:
        return rows
    # Finish the last start time: an identity's rows all share it.
    last_start, last_id = rows[-1][3], rows[-1][0]
    rest = await session.execute(
        select(*_COLUMNS)
        .where(
            RawHealthData.data_type == data_type,
            RawHealthData.source_start_time == last_start,
            RawHealthData.id > last_id,
        )
        .order_by(RawHealthData.id)
    )
    rows.extend(rest.tuples())
    return rows


async def _rows_at(session: AsyncSession, spec: DataTypeSpec, starts: Sequence[datetime]) -> list:
    found = []
    ordered = sorted(set(starts))
    for offset in range(0, len(ordered), 2000):
        chunk = ordered[offset : offset + 2000]
        found.extend(
            (
                await session.execute(
                    select(*_COLUMNS).where(
                        RawHealthData.data_type == spec.name,
                        among(session, RawHealthData.source_start_time, chunk),
                    )
                )
            )
            .tuples()
            .all()
        )
    return found


def _rows_of(
    spec: DataTypeSpec, batch: Sequence, stats: BackfillStats | None
) -> tuple[list[_Row], list[_Row]]:
    """(rows to settle, rows whose stored start differs from the parsed one)."""
    rows: list[_Row] = []
    mismatched: list[_Row] = []
    for raw_id, google_id, raw_json, start, first, last, deleted, _, digest, quarantined in batch:
        if stats is not None:
            stats.rows += 1
        if google_name(raw_json) is not None:
            if stats is not None:
                stats.named += 1
            continue
        identity = record_identity(spec, raw_json)
        if identity is None:
            if stats is not None:
                stats.unidentified += 1
            continue
        row = _Row(
            raw_id,
            google_id,
            raw_json,
            as_utc(first),
            as_utc(last),
            deleted is not None,
            identity,
            digest,
            bool(quarantined),
        )
        if instant(identity.start) != instant(start):
            mismatched.append(row)
        else:
            rows.append(row)
    return rows, mismatched


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _micros(value: datetime) -> int:
    return (as_utc(value) - _EPOCH) // timedelta(microseconds=1)


def _from_micros(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


async def _collect_starts(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    since: datetime | None,
    touched: datetime | None,
    lapsed: set[tuple[str, str]],
) -> array:
    """The distinct start times (microseconds since the epoch, sorted) of the rows
    ``_settle_changed`` settles; each query streamed in a short read-only transaction of its
    own, so a long run never holds one snapshot (vacuum is not held back)."""
    sources: list[array] = []
    queries = []
    if since is not None:
        queries.append(
            select(RawHealthData.source_start_time).where(
                RawHealthData.data_type == data_type,
                # ix_raw_health_data_updated_at finds what a fetch touched; only rows created
                # or given a new payload since can need a link.
                RawHealthData.updated_at >= since,
                RawHealthData.payload_received_at >= since,
                RawHealthData.source_start_time.is_not(None),
            )
        )
        # A row whose deletion mark was cleared with its payload unchanged counts again: a
        # link it needs is not left for a full run (raw_health_data_events, by trigger).
        returned = RawHealthDataEvent
        queries.append(
            select(RawHealthData.source_start_time)
            .join(returned, returned.raw_id == RawHealthData.id)
            .where(
                returned.data_type == data_type,
                returned.event == "source-returned",
                returned.occurred_at >= since,
                RawHealthData.source_start_time.is_not(None),
            )
        )
    if touched is not None:
        # first_seen_at: the clock the sightings use (the application's, when the page was
        # stored); a row's source never changes, so every row of the later source was first
        # seen since it was.
        # Only start times with another row can need a link (ix_raw_health_data_type_start).
        other = aliased(RawHealthData)
        queries.append(
            select(RawHealthData.source_start_time).where(
                RawHealthData.data_type == data_type,
                RawHealthData.first_seen_at >= touched,
                RawHealthData.source_start_time.is_not(None),
                select(other.id)
                .where(
                    other.data_type == data_type,
                    other.source_start_time == RawHealthData.source_start_time,
                    other.id != RawHealthData.id,
                )
                .exists(),
            )
        )
    if lapsed:
        link = RawHealthDataSupersession
        pairs = sorted({*lapsed, *((right, left) for left, right in lapsed)})
        for column in (link.old_raw_id, link.new_raw_id):
            queries.append(
                select(RawHealthData.source_start_time)
                .join(link, column == RawHealthData.id)
                .where(
                    link.data_type == data_type,
                    tuple_(link.old_fingerprint, link.new_fingerprint).in_(pairs),
                    RawHealthData.source_start_time.is_not(None),
                )
            )
    for query in queries:
        found = array("q")
        async with sessions() as session:
            if session.get_bind().dialect.name == "postgresql":
                result = await session.stream(
                    query.distinct()
                    .order_by(RawHealthData.source_start_time)
                    .execution_options(yield_per=10_000)
                )
                async for (start,) in result:
                    found.append(_micros(start))
            else:
                result = await session.scalars(
                    query.distinct().order_by(RawHealthData.source_start_time)
                )
                found.extend(_micros(start) for start in result.all())
            await session.rollback()
        sources.append(found)
    merged = array("q")
    for value in heapq.merge(*sources):
        if not merged or merged[-1] != value:
            merged.append(value)
    return merged
