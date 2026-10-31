"""Read-only status projection of durable, value-free diagnostic events.

The signature format has no expiry field. `expired_replay` means only that the
local SIGNED_REPLAY_MAX_AGE processing policy ignored an already archived body.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.models.state import WebhookBatchItem, WebhookEvent, WebhookNotification
from app.models.webhook_status import WebhookStatusEvent

Reason = Literal[
    "authorization_rejected",
    "receiver_unconfigured",
    "missing_signature",
    "malformed_signature",
    "invalid_signature",
    "key_download_failed",
    "signature_valid",
    "challenge",
    "invalid_json",
    "body_rejected",
    "notification_received",
    "duplicate",
    "expired_replay",
    "ingestion_failed",
    "notification_rejected",
    "processing_failed",
    "batch_processing_failed",
    "processing_interrupted",
]
REASONS: tuple[Reason, ...] = (
    "authorization_rejected",
    "receiver_unconfigured",
    "missing_signature",
    "malformed_signature",
    "invalid_signature",
    "key_download_failed",
    "signature_valid",
    "challenge",
    "invalid_json",
    "body_rejected",
    "notification_received",
    "duplicate",
    "expired_replay",
    "ingestion_failed",
    "notification_rejected",
    "processing_failed",
    "batch_processing_failed",
    "processing_interrupted",
)
PROCESSING_ERRORS = {
    "ingestion_failed",
    "notification_rejected",
    "processing_failed",
    "batch_processing_failed",
    "processing_interrupted",
}
VALIDATION_ERRORS = {
    "authorization_rejected",
    "receiver_unconfigured",
    "missing_signature",
    "malformed_signature",
    "invalid_signature",
    "key_download_failed",
    "invalid_json",
    "body_rejected",
}
# Rejected before authorization: anyone can trigger these, and diagnostic rows are never
# deleted. At most one row per reason per interval is persisted; the rest are counted in
# memory (`process_local`). Authorized deliveries are always persisted in full.
SAMPLED_REASONS: tuple[Reason, ...] = ("authorization_rejected", "receiver_unconfigured")
SAMPLE_INTERVAL = timedelta(minutes=1)
SILENCE_THRESHOLD = timedelta(days=7)
logger = logging.getLogger(__name__)


class WebhookStatusService:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.clock = clock
        self.started_at = clock()
        self._sampled_at: dict[str, datetime] = {}
        self._unpersisted = dict.fromkeys(SAMPLED_REASONS, 0)

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.google_health_webhook_endpoint_uri
            and self.settings.google_health_webhook_authorization
            and self.settings.google_health_webhook_authorization != "CHANGE_ME"
        )

    async def start(self) -> None:
        # Called at application startup, never by GET. The first configured startup
        # starts the no-notification grace period; unconfigured time does not count.
        await self._append(
            ["monitoring_started"] + (["receiver_started"] if self.configured else [])
        )

    async def record(self, *reasons: Reason) -> None:
        if any(reason not in REASONS for reason in reasons):
            raise ValueError("Unknown webhook diagnostic reason")
        now = self.clock()
        kept = []
        for reason in reasons:
            if reason in self._unpersisted:
                last = self._sampled_at.get(reason)
                # Checked and set without an await in between, so concurrent requests
                # on this event loop cannot both persist inside one interval.
                if last is not None and timedelta(0) <= now - last < SAMPLE_INTERVAL:
                    self._unpersisted[reason] += 1
                    continue
                self._sampled_at[reason] = now
            kept.append(reason)
        if kept:
            await self._append(kept)

    def observe(self, session: AsyncSession, *reasons: Reason) -> None:
        """Append with the processing transaction, so rollback cannot count a result."""
        session.add_all(
            WebhookStatusEvent(reason=reason, occurred_at=self.clock()) for reason in reasons
        )

    async def _append(self, reasons: list[str]) -> None:
        now = self.clock()
        try:
            async with self.sessions.begin() as session:
                session.add_all(
                    WebhookStatusEvent(reason=reason, occurred_at=now) for reason in reasons
                )
        except SQLAlchemyError:
            # Diagnostics must not turn a 201/204/401 into a failed delivery.
            # No exception text: drivers can include connection credentials or SQL.
            logger.error(
                "Webhook diagnostics unavailable", extra={"event": "WEBHOOK_STATUS_WRITE_FAILED"}
            )

    async def snapshot(self) -> dict:
        now = self.clock()
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        WebhookStatusEvent.reason,
                        func.count(),
                        func.min(WebhookStatusEvent.occurred_at),
                        func.max(WebhookStatusEvent.occurred_at),
                    ).group_by(WebhookStatusEvent.reason)
                )
            ).all()
            # Historical archive is read only. Its timestamps are original verification
            # evidence, never inferred from a data write or a processing retry.
            archived = await session.scalar(
                select(func.max(WebhookEvent.signature_verified_at)).where(
                    WebhookEvent.raw_body.is_not(None),
                    select(WebhookBatchItem.element_hash)
                    .join(
                        WebhookNotification,
                        WebhookNotification.element_hash == WebhookBatchItem.element_hash,
                    )
                    .where(
                        WebhookBatchItem.batch_hash == WebhookEvent.payload_hash,
                        WebhookNotification.is_valid.is_(True),
                    )
                    .exists(),
                )
            )
            error = (
                await session.execute(
                    select(WebhookStatusEvent.reason, WebhookStatusEvent.occurred_at)
                    .where(WebhookStatusEvent.reason.in_(VALIDATION_ERRORS | PROCESSING_ERRORS))
                    .order_by(WebhookStatusEvent.occurred_at.desc(), WebhookStatusEvent.id.desc())
                    .limit(1)
                )
            ).first()
        counts = {reason: 0 for reason in REASONS}
        first, last = {}, {}
        for reason, count, earliest, latest in rows:
            if reason in counts:
                counts[reason] = count
            first[reason], last[reason] = as_utc(earliest), as_utc(latest)
        evidence = [
            value for value in (as_utc(archived), last.get("notification_received")) if value
        ]
        received = max(evidence) if evidence else None
        monitoring_since = min(first.values(), default=self.started_at)
        waiting_since = first.get("receiver_started", self.started_at)
        silence_since = received or waiting_since
        silent = self.configured and now - silence_since >= SILENCE_THRESHOLD
        state = (
            "unconfigured"
            if not self.configured
            else "silent"
            if silent
            else "never_received"
            if received is None
            else "receiving"
        )
        return {
            "checked_at": now,
            "receiver": {"configured": self.configured},
            "last_notification_at": received,
            "notification_basis": (
                "verified_signature_with_valid_notification_element; "
                "excludes_challenge_and_expired_replays"
            ),
            "silence": {
                "state": state,
                "warning": silent,
                "since": silence_since if self.configured else None,
                "never_received": received is None,
                "threshold_seconds": int(SILENCE_THRESHOLD.total_seconds()),
            },
            "counters": {
                "scope": "persistent_diagnostic_events",
                "since": monitoring_since,
                "survives_restart": True,
                "reasons": counts,
                "semantics": (
                    "events_not_exclusive; no historical counter backfill; "
                    "database outages may omit observations"
                ),
                "valid": counts["signature_valid"],
                "invalid": sum(
                    counts[key]
                    for key in ("missing_signature", "malformed_signature", "invalid_signature")
                ),
                "expired": counts["expired_replay"],
                "expired_basis": "local_signed_replay_max_age; signature has no expiry field",
                "units": {
                    "validation": "requests; valid includes duplicates and expired replays",
                    "notification_received": "non_expired_verified_deliveries_with_valid_elements",
                    "notification_rejected": "rejected_batch_elements",
                    "processing_failed": "failed_element_attempts",
                    "batch_processing_failed": "unhandled_batch_attempt_errors",
                    "processing_interrupted": "recovery_runs_with_interrupted_elements",
                },
            },
            "process_local": {
                "scope": "this_process_since_start",
                "since": self.started_at,
                "survives_restart": False,
                "unpersisted": dict(self._unpersisted),
                "semantics": (
                    "pre_authorization_rejections_persist_at_most_one_row_per_reason_per_minute; "
                    "unpersisted_counts_only_the_skipped_rows"
                ),
            },
            "subscription": {
                "status": "unknown",
                "renewal_status": "unknown",
                "last_renewed_at": None,
                "basis": "no_subscription_client_or_evidence",
            },
            "last_error": {
                "code": error.reason,
                "stage": "processing" if error.reason in PROCESSING_ERRORS else "validation",
                "at": as_utc(error.occurred_at),
            }
            if error
            else None,
        }
