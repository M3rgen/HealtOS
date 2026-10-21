"""Mark archived records that a complete re-fetch of their window no longer returns.

Google Health has no tombstones in list responses: a deleted sleep or exercise record
simply stops appearing. After a window has been fetched from its first page to its last,
any archived record that the same filter must have matched, but which was not seen during
that fetch, is marked with ``source_deleted_at``. Nothing is deleted or overwritten; if
the record comes back, ``store_records`` clears the mark again.

"Not seen" is decided per record identity, not per row (app.services.identity.
present_records). A row is not marked while, in the same fetch, a row with its v2 key was
seen

* of the *same device* - the same source fingerprint, or one of its decided chain
  (app/services/source_chains.py): a duplicate of the record under another id; or
* of a fingerprint not yet *decided distinct* from the row's (the two were never seen
  together): possibly the same device renamed, which only the reconciler decides. The mark is
  deferred, never guessed; once the reconciler has linked the two, the old row is superseded
  instead.

A row of a fingerprint decided distinct (seen alongside the row's own) never keeps a
candidate: of two devices that recorded at one instant, one can be deleted alone.

Superseded rows are skipped (they no longer count, and their record is decided through the
row that holds it). Candidates are read and decided in chunks, so a long window never sits
in memory at once.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.logging import event
from app.google_health.data_types import (
    CIVIL_AFTER,
    CIVIL_BEFORE,
    AccessMethod,
    DataTypeSpec,
    RecordKind,
    query_time,
)
from app.models.identity import superseded
from app.models.state import RawHealthData
from app.services.identity import among, present_records
from app.services.source_chains import lock_data_type

logger = logging.getLogger(__name__)

# Candidates read and decided at a time.
CHUNK = 2000


def deletion_scope(
    spec: DataTypeSpec, start: datetime, end: datetime
) -> tuple[InstrumentedAttribute, datetime, datetime] | None:
    """Return (stored column, lower, upper) that the list filter for the window covers.

    Mirrors ``DataTypeSpec.filter_for`` on the bounds the client actually sends
    (``query_time``: UTC, whole seconds), never on finer caller datetimes. ``None``
    means deletions cannot be inferred: unfiltered catalogs, and rollup/reconcile
    responses, which are derived aggregates rather than individually deletable source
    records.
    """
    if not spec.time_filtered or spec.access_method != AccessMethod.list:
        return None
    start, end = query_time(start), query_time(end)
    if spec.kind == RecordKind.daily:
        start_date = date.fromisoformat(start.isoformat()[:10])
        end_date = date.fromisoformat(end.isoformat()[:10])
        if end_date <= start_date or end.isoformat()[11:19] != "00:00:00":
            end_date += timedelta(days=1)
        lower = datetime.combine(start_date, time.min, UTC)
        upper = datetime.combine(end_date, time.min, UTC)
        return RawHealthData.source_start_time, lower, upper
    if spec.name == "sleep":
        return RawHealthData.source_end_time, start, end
    if spec.kind == RecordKind.session and spec.name != "electrocardiogram":
        # Civil start times: only records this far inside are certain to have matched.
        lower, upper = start + CIVIL_BEFORE, end - CIVIL_AFTER
        if lower >= upper:
            return None
        return RawHealthData.source_start_time, lower, upper
    # Samples, intervals, and electrocardiograms (whose filter has no upper bound,
    # so the returned set is a superset of this window).
    return RawHealthData.source_start_time, start, end


async def mark_source_deletions(
    session: AsyncSession,
    spec: DataTypeSpec,
    start: datetime,
    end: datetime,
    fetched_since: datetime,
) -> int:
    """Mark records in the window that were not seen since ``fetched_since``.

    ``store_records`` bumps ``last_seen_at`` for every returned record (changed or not),
    so a record in scope that is older than the start of the complete fetch was not
    returned by Google.
    """
    scope = deletion_scope(spec, start, end)
    if scope is None:
        return 0
    column, lower, upper = scope
    await lock_data_type(session, spec.name, exclusive=False)
    unseen = (
        RawHealthData.data_type == spec.name,
        RawHealthData.source_deleted_at.is_(None),
        RawHealthData.last_seen_at < fetched_since,
        column >= lower,
        column < upper,
    )
    marked = kept = deferred = 0
    now = datetime.now(UTC)
    cursor: tuple | None = None
    while True:
        query = select(
            RawHealthData.id,
            RawHealthData.raw_json,
            RawHealthData.source_start_time,
            column,
        ).where(*unseen, ~superseded(RawHealthData.id))
        if cursor is not None:
            query = query.where(tuple_(column, RawHealthData.id) > cursor)
        chunk = (
            (await session.execute(query.order_by(column, RawHealthData.id).limit(CHUNK)))
            .tuples()
            .all()
        )
        if not chunk:
            break
        cursor = (chunk[-1][3], chunk[-1][0])
        presence = await present_records(
            session,
            spec,
            [(raw_id, raw_json, start) for raw_id, raw_json, start, _ in chunk],
            fetched_since,
        )
        kept += len(presence.present)
        deferred += len(presence.deferred)
        spared = presence.present | presence.deferred
        missing = sorted(raw_id for raw_id, *_ in chunk if raw_id not in spared)
        if missing:
            result = await session.execute(
                update(RawHealthData)
                .where(*unseen, among(session, RawHealthData.id, missing))
                .values(source_deleted_at=now)
                .execution_options(synchronize_session=False)
            )
            # An UPDATE always yields a CursorResult; AsyncSession.execute is typed as Result.
            marked += cast(CursorResult[Any], result).rowcount or 0
        if len(chunk) < CHUNK:
            break
    if kept:
        event(logger, "SOURCE_DELETIONS_KEPT_BY_IDENTITY", data_type=spec.name, count=kept)
    if deferred:
        # Possibly renamed: the reconciler decides (identity-backfill, nightly).
        event(logger, "SOURCE_DELETIONS_DEFERRED", data_type=spec.name, count=deferred)
    if marked:
        event(logger, "SOURCE_DELETIONS_MARKED", data_type=spec.name, count=marked)
    return marked
