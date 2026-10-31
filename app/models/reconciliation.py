"""Durable reconciliation outcomes; values and upstream error messages stay in the archive."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        Index("ix_reconciliation_runs_started", "started_at", "id"),
        CheckConstraint("requested_from < requested_to", name="range"),
        CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed', 'cancelled')", name="status"
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status <> 'running' AND finished_at IS NOT NULL)",
            name="finished",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
    requested_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    error_type: Mapped[str | None] = mapped_column(String(100))


class ReconciliationResult(Base):
    __tablename__ = "reconciliation_results"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed', 'cancelled')",
            name="status",
        ),
        CheckConstraint(
            "inserted >= 0 AND updated >= 0 AND unchanged >= 0 AND source_deleted >= 0 "
            "AND pages_committed >= 0",
            name="counts",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(ReconciliationRun.id), primary_key=True)
    data_type: Mapped[str] = mapped_column(String(100), primary_key=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    source_deleted: Mapped[int] = mapped_column(Integer, default=0)
    pages_committed: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(100))
