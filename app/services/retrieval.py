"""Write retrieval evidence without deriving success from mutable job state."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import delete, select

from app.core.timeutil import as_utc
from app.google_health.client import Page, _minute_aligned_range
from app.google_health.data_types import (
    AccessMethod,
    DataTypeSpec,
    RecordKind,
    query_iso,
    query_time,
)
from app.google_health.pagination import PaginationPaused
from app.models.retrieval import RetrievalCheckpoint, RetrievalEvent, RetrievalReceipt
from app.models.state import BackfillJob, SyncState, fetch_round_start
from app.parsers.common import parse_date, parse_datetime, payload_for, temporal

logger = logging.getLogger(__name__)


def token_hash(token: str | None) -> str | None:
    return hashlib.sha256(token.encode()).hexdigest() if token else None


def query_scope(spec: DataTypeSpec, start: datetime, end: datetime):
    """Actual client filter coordinates, not the caller's subsecond timestamps."""
    start, end = as_utc(start), as_utc(end)
    if not spec.time_filtered:
        return None, None, None, "unbounded"
    if spec.access_method == AccessMethod.rollup:
        start, end = _minute_aligned_range(start, end)
        return query_time(start), query_time(end), None, "rollup_start"
    expression = spec.filter_for(query_iso(start), query_iso(end))
    start, end = query_time(start), query_time(end)
    if spec.kind == RecordKind.daily:
        first, stop = start.date(), end.date()
        if stop <= first or end.time() != time.min:
            stop += timedelta(days=1)
        return (
            datetime.combine(first, time.min, UTC),
            datetime.combine(stop, time.min, UTC),
            expression,
            "date",
        )
    if spec.name == "sleep":
        basis = "physical_end"
    elif spec.name == "electrocardiogram":
        return start, None, expression, "physical_start_lower_bound"
    elif spec.kind == RecordKind.session:
        basis = "civil_start"
    else:
        basis = "physical_start"
    return start, end, expression, basis


def returned_times(spec: DataTypeSpec, page: Page) -> tuple[list, int]:
    counts: Counter[str] = Counter()
    missing = 0
    for point in page.data_points:
        if spec.kind == RecordKind.daily:
            day = (
                parse_date(payload_for(spec, point).get("date"))
                if isinstance(point, dict)
                else None
            )
            stamp = datetime.combine(day, time.min, UTC) if day else None
        else:
            start, end, _ = temporal(spec, point) if isinstance(point, dict) else (None, None, None)
            stamp = end if spec.name == "sleep" else start
        if stamp is None:
            missing += 1
        else:
            counts[as_utc(stamp).isoformat()] += 1
    return sorted([stamp, count] for stamp, count in counts.items()), missing


class ReceiptWriter:
    """One invocation; token reset creates a new first-page attempt, keeping the old one.

    Resuming only links an exact, durable matching page. A legacy or foreign checkpoint
    stays unrooted and cannot establish completeness, even if it reaches the last page.
    """

    def __init__(self, sessions, spec, start, end, lane, owner, token):
        self.sessions = sessions
        self.spec = spec
        self.start, self.end = as_utc(start), as_utc(end)
        self.lane, self.owner = lane, owner
        self.token = token_hash(token)
        self.id = uuid.uuid4()
        self.unstored: Page | None = None

    async def begin(self, session=None, *, started_at: datetime | None = None) -> None:
        if session is None:
            async with self.sessions.begin() as session:
                await self.begin(session, started_at=started_at)
            return
        lower, upper, expression, basis = query_scope(self.spec, self.start, self.end)
        parent = None
        if self.token:
            checkpoint = await session.get(RetrievalCheckpoint, (self.lane, self.owner))
            if checkpoint is not None and checkpoint.next_token_hash == self.token:
                parent = await session.scalar(
                    select(RetrievalEvent.id)
                    .join(RetrievalReceipt, RetrievalEvent.receipt_id == RetrievalReceipt.id)
                    .where(
                        RetrievalEvent.id == checkpoint.event_id,
                        RetrievalReceipt.lane == self.lane,
                        RetrievalReceipt.owner == self.owner,
                        RetrievalReceipt.data_type == self.spec.name,
                        RetrievalReceipt.requested_from == self.start,
                        RetrievalReceipt.requested_to == self.end,
                        RetrievalEvent.kind == "page",
                        RetrievalEvent.next_token_hash == self.token,
                    )
                )
        session.add(
            RetrievalReceipt(
                id=self.id,
                data_type=self.spec.name,
                lane=self.lane,
                owner=self.owner,
                requested_from=self.start,
                requested_to=self.end,
                queried_from=lower,
                queried_to=upper,
                query_filter=expression,
                basis=basis,
                access_method=self.spec.access_method,
                started_at=started_at or datetime.now(UTC),
                starts_first=self.token is None,
                resume_token_hash=self.token,
                parent_event_id=parent,
            )
        )

    def event(self, session, kind: str, **values) -> RetrievalEvent:
        times = values.get("returned_times", [])
        values["first_record_at"] = parse_datetime(times[0][0]) if times else None
        values["last_record_at"] = parse_datetime(times[-1][0]) if times else None
        event = RetrievalEvent(
            receipt_id=self.id, kind=kind, occurred_at=datetime.now(UTC), **values
        )
        session.add(event)
        return event

    async def page(self, session, page: Page) -> None:
        times, missing = returned_times(self.spec, page)
        next_token = token_hash(page.next_page_token)
        event = self.event(
            session,
            "page",
            request_token_hash=self.token,
            next_token_hash=next_token,
            last_page=next_token is None,
            returned_count=len(page.data_points),
            returned_times=times,
            unplaced_count=missing,
        )
        # Flush also locks/writes SyncState or BackfillJob before the pointer, so
        # concurrent page transactions leave the job token and pointer consistent.
        await session.flush()
        from app.services.storage import dialect_insert

        statement = dialect_insert(session, RetrievalCheckpoint.__table__)
        await session.execute(
            statement.values(
                lane=self.lane, owner=self.owner, event_id=event.id, next_token_hash=next_token
            ).on_conflict_do_update(
                index_elements=["lane", "owner"],
                set_={"event_id": event.id, "next_token_hash": next_token},
            )
        )

    def committed(self, page: Page) -> None:
        self.token = token_hash(page.next_page_token)

    async def reset(self) -> datetime:
        """Commit the new first-page boundary, checkpoint and receipt together.

        No request for the replacement attempt may run before this commits. Keep the
        in-memory writer on the old receipt on rollback, including cancellation.
        """
        now = datetime.now(UTC)
        replacement = ReceiptWriter(
            self.sessions, self.spec, self.start, self.end, self.lane, self.owner, None
        )
        async with self.sessions.begin() as session:
            # Same lock/write order as a page: job/state before the receipt pointer.
            if self.lane == "sync":
                state = await session.get(SyncState, self.owner)
                state.last_page_token = None
            else:
                job = await session.get(BackfillJob, uuid.UUID(self.owner))
                job.page_token = None
            started = await fetch_round_start(session, self.lane, self.owner, fresh=True, now=now)
            self.event(session, "reset")
            await replacement.begin(session, started_at=started)
            await session.flush()
            await session.execute(
                delete(RetrievalCheckpoint).where(
                    RetrievalCheckpoint.lane == self.lane,
                    RetrievalCheckpoint.owner == self.owner,
                )
            )
        self.id, self.token = replacement.id, replacement.token
        return as_utc(started)

    async def failure(self, failure: BaseException) -> None:
        """Best effort on cancellation; a missing outcome always stays incomplete."""
        if isinstance(failure, PaginationPaused):
            # No failure or completion occurred. Immutable pages and their checkpoint
            # remain an incomplete chain until a later invocation reaches the last page.
            return

        async def write():
            times, missing = returned_times(self.spec, self.unstored) if self.unstored else ([], 0)
            async with self.sessions.begin() as session:
                self.event(
                    session,
                    "cancelled" if isinstance(failure, asyncio.CancelledError) else "failed",
                    error_type=type(failure).__name__[:100],
                    # A failed store still received these records. Only `page` events
                    # prove persistence; readers never use failure counts as empty proof.
                    returned_count=len(self.unstored.data_points) if self.unstored else 0,
                    returned_times=times,
                    unplaced_count=missing,
                )

        task = asyncio.create_task(write())
        try:
            await asyncio.wait_for(asyncio.shield(task), 10)
        except BaseException:
            task.cancel()
            # Sync receipts may share their owner's connection. Finish rollback before
            # that owner releases it; no outcome task may outlive run ownership.
            await asyncio.gather(task, return_exceptions=True)
            logger.warning("Retrieval outcome was not persisted; receipt remains incomplete")
