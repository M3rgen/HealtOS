from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class JobStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class RawHealthData(TimestampMixin, Base):
    __tablename__ = "raw_health_data"
    __table_args__ = (
        UniqueConstraint("data_type", "google_data_point_id"),
        Index("ix_raw_health_data_type_start", "data_type", "source_start_time"),
        Index("ix_raw_health_data_updated_at", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False)
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    source_start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_timezone: Mapped[str | None] = mapped_column(String(128))
    raw_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_quarantined: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    quarantine_reason: Mapped[str | None] = mapped_column(String(255))


class SyncState(TimestampMixin, Base):
    __tablename__ = "sync_state"

    data_type: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_requested_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_requested_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_page_token: Mapped[str | None] = mapped_column(Text)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.pending)
    last_error: Mapped[str | None] = mapped_column(Text)
    records_downloaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class BackfillJob(TimestampMixin, Base):
    __tablename__ = "backfill_jobs"
    __table_args__ = (UniqueConstraint("data_type", "window_start", "window_end"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.pending, index=True
    )
    page_token: Mapped[str | None] = mapped_column(Text)
    records_downloaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class OAuthToken(TimestampMixin, Base):
    __tablename__ = "oauth_tokens"

    subject: Mapped[str] = mapped_column(String(128), primary_key=True, default="me")
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str | None] = mapped_column(Text)
    health_user_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    legacy_user_id: Mapped[str | None] = mapped_column(String(128))


class WebhookEvent(TimestampMixin, Base):
    __tablename__ = "webhook_events"

    payload_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    data_type: Mapped[str | None] = mapped_column(String(128), index=True)
    health_user_id: Mapped[str | None] = mapped_column(String(128))
    operation: Mapped[str | None] = mapped_column(String(32))
    raw_json: Mapped[dict | list] = mapped_column(JSON, nullable=False)
    raw_body: Mapped[bytes | None] = mapped_column(LargeBinary)
    signature_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    element_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookNotification(TimestampMixin, Base):
    """A globally deduplicated notification element from one or more signed batches."""

    __tablename__ = "webhook_notifications"

    element_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    raw_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSON, nullable=False
    )
    data_type: Mapped[str | None] = mapped_column(String(128), index=True)
    health_user_id: Mapped[str | None] = mapped_column(String(128))
    operation: Mapped[str | None] = mapped_column(String(32))
    intervals: Mapped[list | None] = mapped_column(JSON)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sync_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sync_success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    validation_error: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookBatchItem(TimestampMixin, Base):
    """Membership of a notification element in an immutable signed request batch."""

    __tablename__ = "webhook_batch_items"

    batch_hash: Mapped[str] = mapped_column(
        ForeignKey("webhook_events.payload_hash"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    element_hash: Mapped[str] = mapped_column(
        ForeignKey("webhook_notifications.element_hash"), nullable=False, index=True
    )


class ReliabilityState(TimestampMixin, Base):
    """Singleton operational timestamps not represented by source synchronization state."""

    __tablename__ = "reliability_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_reconciliation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciliation_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciliation_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_recovery_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_recovery_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_recovery_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_recovery_error: Mapped[str | None] = mapped_column(Text)
