"""Mark archived records that a complete re-fetch of their window no longer returns.

Google Health has no tombstones in list responses: a deleted sleep or exercise record
simply stops appearing. After a window has been fetched from its first page to its last,
any archived record that the same filter must have matched, but which was not seen during
that fetch, is marked with ``source_deleted_at``. Nothing is deleted or overwritten; if
the record comes back, ``store_records`` clears the mark again.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.logging import event
from app.google_health.data_types import AccessMethod, DataTypeSpec, RecordKind, query_time
from app.models.state import RawHealthData

logger = logging.getLogger(__name__)

# Session filters compare civil (local wall-clock) start times with the window bounds,
# while the archive stores UTC. UTC offsets range from -12:00 to +14:00, so only records
# this far inside the window are certain to have matched the filter.
_CIVIL_BEFORE = timedelta(hours=12)
_CIVIL_AFTER = timedelta(hours=14)


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
        lower, upper = start + _CIVIL_BEFORE, end - _CIVIL_AFTER
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
    result = await session.execute(
        update(RawHealthData)
        .where(
            RawHealthData.data_type == spec.name,
            RawHealthData.source_deleted_at.is_(None),
            RawHealthData.last_seen_at < fetched_since,
            column >= lower,
            column < upper,
        )
        .values(source_deleted_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    # An UPDATE always yields a CursorResult; AsyncSession.execute is typed as plain Result.
    marked = cast(CursorResult[Any], result).rowcount or 0
    if marked:
        event(logger, "SOURCE_DELETIONS_MARKED", data_type=spec.name, count=marked)
    return marked
