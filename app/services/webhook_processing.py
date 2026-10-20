from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import event
from app.core.timeutil import as_utc
from app.models.state import (
    OAuthToken,
    WebhookBatchItem,
    WebhookEvent,
    WebhookNotification,
)
from app.parsers.common import parse_datetime, payload_hash
from app.sync.incremental import IncrementalSync

logger = logging.getLogger(__name__)

# A captured signed body stays cryptographically valid forever. A live re-delivery of a
# batch first verified longer ago than this cannot restart its processing; retries of
# old batches go through process_pending (the replay CLI) instead.
SIGNED_REPLAY_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class IngestResult:
    batch_hash: str
    element_count: int
    inserted: bool
    should_process: bool


@dataclass
class ProcessingStats:
    batches: int = 0
    completed_elements: int = 0
    failed_elements: int = 0
    rejected_elements: int = 0


class _ElementLock:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0  # coroutines holding or waiting for the lock


class WebhookBatchProcessor:
    """Durably ingest, deduplicate, process, and retry signed webhook batches."""

    def __init__(
        self,
        sessions: async_sessionmaker,
        incremental: IncrementalSync,
        supported_data_types: set[str],
    ) -> None:
        self.sessions = sessions
        self.incremental = incremental
        self.supported_data_types = supported_data_types
        # Only elements being processed right now have an entry; see _element_lock.
        self._element_locks: dict[str, _ElementLock] = {}

    async def ingest_verified(self, raw: bytes, payload: object) -> IngestResult:
        """Persist an authenticated request without altering its signed raw bytes."""

        batch_hash = hashlib.sha256(raw).hexdigest()
        return await self._prepare_batch(
            batch_hash,
            payload,
            raw_body=raw,
            signature_verified_at=datetime.now(UTC),
        )

    async def prepare_existing_batch(self, batch_hash: str) -> IngestResult:
        """Materialize per-element state for a previously verified legacy batch."""

        async with self.sessions() as session:
            stored = await session.get(WebhookEvent, batch_hash)
            if stored is None:
                raise ValueError(f"Unknown webhook batch: {batch_hash}")
            payload = stored.raw_json
        return await self._prepare_batch(
            batch_hash,
            payload,
            raw_body=None,
            signature_verified_at=None,
        )

    async def _prepare_batch(
        self,
        batch_hash: str,
        payload: object,
        *,
        raw_body: bytes | None,
        signature_verified_at: datetime | None,
    ) -> IngestResult:
        elements = payload if isinstance(payload, list) else [payload]
        now = datetime.now(UTC)
        inserted = False
        prepared = False
        async with self.sessions.begin() as session:
            dialect_insert = (
                postgresql_insert
                if session.get_bind().dialect.name == "postgresql"
                else sqlite_insert
            )
            first_data = _notification_data(elements[0]) if len(elements) == 1 else None
            statement = (
                dialect_insert(WebhookEvent)
                .values(
                    payload_hash=batch_hash,
                    data_type=first_data.get("dataType") if first_data else None,
                    health_user_id=first_data.get("healthUserId") if first_data else None,
                    operation=first_data.get("operation") if first_data else None,
                    raw_json=payload,
                    raw_body=raw_body,
                    signature_verified_at=signature_verified_at or now,
                    element_count=len(elements),
                    failure_count=0,
                )
                .on_conflict_do_nothing(index_elements=["payload_hash"])
                .returning(WebhookEvent.payload_hash)
            )
            inserted = (await session.scalar(statement)) is not None
            batch = await session.get(WebhookEvent, batch_hash)
            if batch is None:
                raise RuntimeError("Webhook batch could not be persisted")
            if not inserted and raw_body is not None and _stale(batch.signature_verified_at, now):
                event(logger, "WEBHOOK_REPLAY_IGNORED", batch_hash=batch_hash)
                return IngestResult(
                    batch_hash=batch_hash,
                    element_count=len(elements),
                    inserted=False,
                    should_process=False,
                )
            existing_items = await session.scalar(
                select(func.count())
                .select_from(WebhookBatchItem)
                .where(WebhookBatchItem.batch_hash == batch_hash)
            )
            if not existing_items:
                expected_user_id = await session.scalar(
                    select(OAuthToken.health_user_id).where(OAuthToken.subject == "me")
                )
                for ordinal, item in enumerate(elements, start=1):
                    element_digest = payload_hash(item)
                    extracted, validation_error = self._extract(item, expected_user_id)
                    is_valid = validation_error is None
                    values = {
                        "element_hash": element_digest,
                        "raw_json": item,
                        "data_type": extracted.get("dataType"),
                        "health_user_id": extracted.get("healthUserId"),
                        "operation": extracted.get("operation"),
                        "intervals": extracted.get("intervals"),
                        "is_valid": is_valid,
                        "status": "pending" if is_valid else "rejected",
                        "attempt_count": 0,
                        "sync_attempt_count": 0,
                        "sync_success_count": 0,
                        "validation_error": validation_error,
                    }
                    await session.execute(
                        dialect_insert(WebhookNotification)
                        .values(**values)
                        .on_conflict_do_nothing(index_elements=["element_hash"])
                    )
                    await session.execute(
                        dialect_insert(WebhookBatchItem)
                        .values(
                            batch_hash=batch_hash,
                            ordinal=ordinal,
                            element_hash=element_digest,
                        )
                        .on_conflict_do_nothing(index_elements=["batch_hash", "ordinal"])
                    )
                batch.element_count = len(elements)
                if raw_body is not None and batch.raw_body is None:
                    batch.raw_body = raw_body
                if signature_verified_at is not None and batch.signature_verified_at is None:
                    batch.signature_verified_at = signature_verified_at
                prepared = True

            incomplete = await session.scalar(
                select(func.count())
                .select_from(WebhookBatchItem)
                .join(
                    WebhookNotification,
                    WebhookNotification.element_hash == WebhookBatchItem.element_hash,
                )
                .where(
                    WebhookBatchItem.batch_hash == batch_hash,
                    WebhookNotification.is_valid.is_(True),
                    WebhookNotification.status != "completed",
                )
            )
        return IngestResult(
            batch_hash=batch_hash,
            element_count=len(elements),
            inserted=inserted,
            should_process=prepared or bool(incomplete),
        )

    def _extract(self, item: object, expected_user_id: str | None) -> tuple[dict, str | None]:
        if not isinstance(item, dict):
            return {}, "notification element must be an object"
        data = item.get("data")
        if not isinstance(data, dict):
            return {}, "notification element.data must be an object"
        extracted = {
            "dataType": data.get("dataType"),
            "healthUserId": data.get("healthUserId"),
            "operation": data.get("operation"),
            "intervals": data.get("intervals"),
        }
        for field in ("dataType", "healthUserId", "operation"):
            if not isinstance(extracted[field], str) or not extracted[field]:
                return extracted, f"notification {field} must be a non-empty string"
        if not isinstance(extracted["intervals"], list):
            return extracted, "notification intervals must be an array"
        if expected_user_id is None:
            return extracted, "authorized Google Health user ID is unavailable"
        if extracted["healthUserId"] != expected_user_id:
            return extracted, "notification healthUserId does not match the authorized account"
        if extracted["dataType"] not in self.supported_data_types:
            return extracted, "notification dataType is not enabled by this mirror"
        return extracted, None

    async def process_batch(self, batch_hash: str) -> ProcessingStats:
        async with self.sessions() as session:
            hashes = (
                await session.scalars(
                    select(WebhookBatchItem.element_hash)
                    .where(WebhookBatchItem.batch_hash == batch_hash)
                    .order_by(WebhookBatchItem.ordinal)
                )
            ).all()
        stats = ProcessingStats(batches=1)
        for element_hash in dict.fromkeys(hashes):
            result = await self._process_element(element_hash)
            if result == "completed":
                stats.completed_elements += 1
            elif result == "failed":
                stats.failed_elements += 1
            elif result == "rejected":
                stats.rejected_elements += 1
        await self._finalize_batch(batch_hash)
        return stats

    @asynccontextmanager
    async def _element_lock(self, element_hash: str) -> AsyncIterator[None]:
        """Serialize work on one element without keeping a lock per element forever.

        The entry is counted before awaiting the lock and dropped only when no coroutine
        holds or waits for it. There is no await between the lookup and the count, so a
        newcomer can never pick up an entry that is about to be removed.
        """
        entry = self._element_locks.get(element_hash)
        if entry is None:
            entry = self._element_locks[element_hash] = _ElementLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._element_locks.get(element_hash) is entry:
                del self._element_locks[element_hash]

    async def _process_element(self, element_hash: str) -> str:
        async with self._element_lock(element_hash):
            async with self.sessions.begin() as session:
                notification = await session.get(WebhookNotification, element_hash)
                if notification is None:
                    return "failed"
                if not notification.is_valid:
                    return "rejected"
                if notification.status == "completed":
                    return "completed"
                claim = await session.execute(
                    update(WebhookNotification)
                    .where(
                        WebhookNotification.element_hash == element_hash,
                        WebhookNotification.status.in_(("pending", "failed")),
                    )
                    .values(
                        status="running",
                        attempt_count=WebhookNotification.attempt_count + 1,
                        last_error=None,
                    )
                    .returning(
                        WebhookNotification.data_type,
                        WebhookNotification.intervals,
                    )
                )
                claimed = claim.one_or_none()
            if claimed is None:
                return "running"
            data_type, intervals = claimed
            try:
                await self._run_incremental(element_hash, data_type, intervals)
                async with self.sessions.begin() as session:
                    notification = await session.get(WebhookNotification, element_hash)
                    notification.status = "completed"
                    notification.processed_at = datetime.now(UTC)
                    notification.last_error = None
                event(logger, "WEBHOOK_ELEMENT_PROCESSED", data_type=data_type)
                result = "completed"
            except Exception as exc:
                async with self.sessions.begin() as session:
                    notification = await session.get(WebhookNotification, element_hash)
                    notification.status = "failed"
                    notification.last_error = str(exc)[:2000]
                logger.exception(
                    "Webhook notification processing failed",
                    extra={"event": "WEBHOOK_ELEMENT_FAILED", "data_type": data_type},
                )
                result = "failed"
            await self._finalize_related_batches(element_hash)
            return result

    async def _run_incremental(
        self, element_hash: str, data_type: str, intervals: list | None
    ) -> None:
        start, end = _physical_range(intervals)
        spec = self.incremental.specs[data_type]
        windows: list[tuple[datetime | None, datetime | None]] = []
        if start and end:
            cursor = start
            while cursor < end:
                window_end = (
                    min(cursor + timedelta(days=spec.max_window_days), end)
                    if spec.max_window_days
                    else end
                )
                windows.append((cursor, window_end))
                cursor = window_end
        else:
            windows.append((None, None))
        for window_start, window_end in windows:
            async with self.sessions.begin() as session:
                await session.execute(
                    update(WebhookNotification)
                    .where(WebhookNotification.element_hash == element_hash)
                    .values(sync_attempt_count=WebhookNotification.sync_attempt_count + 1)
                )
            await self.incremental.run(data_type, window_start, window_end)
            async with self.sessions.begin() as session:
                await session.execute(
                    update(WebhookNotification)
                    .where(WebhookNotification.element_hash == element_hash)
                    .values(sync_success_count=WebhookNotification.sync_success_count + 1)
                )

    async def _finalize_related_batches(self, element_hash: str) -> None:
        async with self.sessions() as session:
            batch_hashes = (
                await session.scalars(
                    select(WebhookBatchItem.batch_hash).where(
                        WebhookBatchItem.element_hash == element_hash
                    )
                )
            ).all()
        for batch_hash in set(batch_hashes):
            await self._finalize_batch(batch_hash)

    async def _finalize_batch(self, batch_hash: str) -> None:
        async with self.sessions.begin() as session:
            rows = (
                await session.execute(
                    select(
                        WebhookNotification.is_valid,
                        WebhookNotification.status,
                        WebhookNotification.validation_error,
                        WebhookNotification.last_error,
                    )
                    .select_from(WebhookBatchItem)
                    .join(
                        WebhookNotification,
                        WebhookNotification.element_hash == WebhookBatchItem.element_hash,
                    )
                    .where(WebhookBatchItem.batch_hash == batch_hash)
                )
            ).all()
            incomplete = [row for row in rows if row.is_valid and row.status != "completed"]
            rejected = [row for row in rows if not row.is_valid]
            failed = [row for row in rows if row.is_valid and row.status == "failed"]
            batch = await session.get(WebhookEvent, batch_hash)
            if batch is None:
                return
            batch.failure_count = len(rejected) + len(failed)
            errors = [row.validation_error for row in rejected] + [row.last_error for row in failed]
            batch.last_error = "; ".join(error for error in errors if error)[:2000] or None
            batch.processed_at = None if incomplete else datetime.now(UTC)

    async def process_pending(self, *, recover_running: bool = True) -> ProcessingStats:
        """Hydrate legacy batches, retry failed work, and preserve completed elements."""

        async with self.sessions() as session:
            all_batches = (await session.scalars(select(WebhookEvent.payload_hash))).all()
            prepared_batches = set(
                (await session.scalars(select(WebhookBatchItem.batch_hash).distinct())).all()
            )
        for batch_hash in all_batches:
            if batch_hash not in prepared_batches:
                await self.prepare_existing_batch(batch_hash)
        if recover_running:
            async with self.sessions.begin() as session:
                await session.execute(
                    update(WebhookNotification)
                    .where(WebhookNotification.status == "running")
                    .values(
                        status="failed",
                        last_error="Recovered interrupted webhook processing attempt",
                    )
                )
        async with self.sessions() as session:
            pending = (
                await session.scalars(
                    select(WebhookEvent.payload_hash)
                    .where(WebhookEvent.processed_at.is_(None))
                    .order_by(WebhookEvent.created_at)
                )
            ).all()
        total = ProcessingStats()
        for batch_hash in pending:
            result = await self.process_batch(batch_hash)
            total.batches += result.batches
            total.completed_elements += result.completed_elements
            total.failed_elements += result.failed_elements
            total.rejected_elements += result.rejected_elements
        return total


def _stale(verified_at: datetime | None, now: datetime) -> bool:
    if verified_at is None:
        return True
    return now - as_utc(verified_at) > SIGNED_REPLAY_MAX_AGE


def _notification_data(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    data = item.get("data")
    return data if isinstance(data, dict) else None


def _physical_range(intervals: object) -> tuple[datetime | None, datetime | None]:
    if not isinstance(intervals, list):
        return None, None
    ranges = []
    for item in intervals:
        physical = item.get("physicalTimeInterval", {}) if isinstance(item, dict) else {}
        if not isinstance(physical, dict):
            continue
        start = parse_datetime(physical.get("startTime"))
        end = parse_datetime(physical.get("endTime"))
        if start and end and start < end:
            ranges.append((start, end))
    if not ranges:
        return None, None
    return min(start for start, _ in ranges), max(end for _, end in ranges)
