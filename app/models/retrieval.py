"""Immutable retrieval attempts and their append-only page/outcome evidence (0015).

fetch_rounds remains an operational checkpoint. Tokens here are SHA-256 digests, never
credentials. Page evidence commits in the same transaction as the records and checkpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Register the shared refusal function before this module's trigger listeners.
import app.models.raw_versions  # noqa: E402, F401  isort: skip

RECEIPT_TABLES = ("retrieval_receipts", "retrieval_events")
CHECKPOINT_TABLE = "retrieval_checkpoints"


class RetrievalReceipt(Base):
    __tablename__ = RECEIPT_TABLES[0]
    __table_args__ = (
        Index("ix_retrieval_receipts_window", "data_type", "queried_from", "queried_to"),
        Index("ix_retrieval_receipts_owner", "lane", "owner", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    data_type: Mapped[str] = mapped_column(String(100))
    lane: Mapped[str] = mapped_column(String(16))
    owner: Mapped[str] = mapped_column(String(100))
    requested_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    queried_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queried_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    query_filter: Mapped[str | None] = mapped_column(String(1000))
    basis: Mapped[str] = mapped_column(String(32))
    access_method: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    starts_first: Mapped[bool] = mapped_column(Boolean())
    resume_token_hash: Mapped[str | None] = mapped_column(String(64))
    # Exact durable page continued from; absent for checkpoints predating receipts.
    parent_event_id: Mapped[int | None] = mapped_column(BigInteger())


class RetrievalEvent(Base):
    __tablename__ = RECEIPT_TABLES[1]
    __table_args__ = (
        CheckConstraint(
            "kind IN ('page', 'completed', 'failed', 'cancelled', 'reset')", name="kind"
        ),
        CheckConstraint("returned_count >= 0 AND unplaced_count >= 0", name="counts"),
        Index("ix_retrieval_events_receipt", "receipt_id", "id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    receipt_id: Mapped[uuid.UUID] = mapped_column(Uuid())
    kind: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_token_hash: Mapped[str | None] = mapped_column(String(64))
    next_token_hash: Mapped[str | None] = mapped_column(String(64))
    last_page: Mapped[bool] = mapped_column(Boolean(), default=False)
    returned_count: Mapped[int] = mapped_column(Integer(), default=0)
    # Temporal keys and multiplicities of *returned* records, including excluded ones.
    # No values, names, devices or source preference. Not a window total spread over days.
    returned_times: Mapped[list] = mapped_column(JSON(), default=list)
    # Allow a day report to read page metadata for the full chain, and only load
    # timestamp histograms of pages overlapping that day (dense backfill windows).
    first_record_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_record_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unplaced_count: Mapped[int] = mapped_column(Integer(), default=0)
    error_type: Mapped[str | None] = mapped_column(String(100))


class RetrievalCheckpoint(Base):
    """Mutable pointer, committed with the existing job checkpoint and page records.

    Never historical completeness evidence: it only binds an opaque saved token to an
    exact immutable page, avoiding guesses when Google reuses a token in another attempt.
    """

    __tablename__ = CHECKPOINT_TABLE

    lane: Mapped[str] = mapped_column(String(16), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger())
    next_token_hash: Mapped[str | None] = mapped_column(String(64))


PG_TRIGGER_SQL = {}
PROTECTIVE_TRIGGERS = []
SQLITE_TRIGGER_SQL = {}
for _table in RECEIPT_TABLES:
    for _suffix, _events in (("append_only", "UPDATE OR DELETE"), ("no_truncate", "TRUNCATE")):
        _name = f"{_table}_{_suffix}"
        _level = "STATEMENT" if _suffix == "no_truncate" else "ROW"
        PG_TRIGGER_SQL[_name] = (
            _table,
            f"CREATE TRIGGER {_name}\nBEFORE {_events} ON {_table} FOR EACH {_level}\n"
            "EXECUTE FUNCTION healthos_refuse_change()",
        )
        PROTECTIVE_TRIGGERS.append((_table, _name, "healthos_refuse_change"))
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        SQLITE_TRIGGER_SQL[_name] = (
            _table,
            f"CREATE TRIGGER {_name}\nBEFORE {_operation} ON {_table}\nBEGIN\n"
            f"    SELECT RAISE(ABORT, '{_table} keeps every record: {_operation} refused');\nEND",
        )
FUNCTION_BODIES: dict[str, str] = {}
POSTGRESQL_DDL = (
    *(sql for _, sql in PG_TRIGGER_SQL.values()),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}"
        for table, name, _ in PROTECTIVE_TRIGGERS
    ),
)
SQLITE_DDL = tuple(sql for _, sql in SQLITE_TRIGGER_SQL.values())
for _dialect, _statements in (("postgresql", POSTGRESQL_DDL), ("sqlite", SQLITE_DDL)):
    for _statement in _statements:
        event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect=_dialect))
