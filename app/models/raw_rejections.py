"""Google records that PostgreSQL jsonb cannot hold, kept apart instead of blocking a type.

Migration 20261102_0023. Three append-only tables (UPDATE/DELETE and PostgreSQL TRUNCATE are
refused, including replica sessions):

* ``raw_health_data_rejections``: one row per record left out of ``raw_health_data`` in one
  stored page. The verbatim bytes stay in the page's ``raw_api_responses`` row
  (``response_id``, one row per HTTP exchange, never merged); ``element_json`` is the parsed
  record with ASCII escapes (U+0000, lone surrogates and NaN/Infinity survive as escapes,
  never sanitized), the only copy when a caller had no HTTP response.
* ``raw_rejection_acks``: the maintainer's ``health-mirror rejections --ack <id>``: every
  rejection up to that id has been looked at. Health is green again until a new record is
  rejected.
* ``retrieval_event_rejections``: how many records of a retrieval page were rejected; kept
  apart from ``retrieval_events.unplaced_count`` (records without a usable time).

No foreign keys: a key onto ``raw_api_responses`` or ``retrieval_events`` would make the
migration lock those tables. The ids are plain references.
"""

from datetime import datetime

from sqlalchemy import (
    DDL,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Register the shared refusal function before this module's trigger listeners.
import app.models.raw_versions  # noqa: E402, F401  isort: skip

REJECTIONS = "raw_health_data_rejections"
ACKS = "raw_rejection_acks"
EVENT_COUNTS = "retrieval_event_rejections"
TABLES = (REJECTIONS, ACKS, EVENT_COUNTS)
REASON = "jsonb_unrepresentable"
_ID = BigInteger().with_variant(Integer(), "sqlite")


class RawHealthDataRejection(Base):
    __tablename__ = REJECTIONS
    __table_args__ = (
        # One row per record of one stored exchange; a retried store adds nothing.
        UniqueConstraint("response_id", "ordinal", name="uq_raw_health_data_rejections_response"),
        # Without an exchange (NULL never conflicts above): same record, path and place.
        Index(
            "uq_raw_health_data_rejections_unanchored",
            "data_type",
            "source",
            "ordinal",
            "element_sha256",
            unique=True,
            sqlite_where=text("response_id IS NULL"),
            postgresql_where=text("response_id IS NULL"),
        ),
        Index("ix_raw_health_data_rejections_element", "data_type", "element_sha256"),
        CheckConstraint("ordinal >= 0", name="ordinal"),
    )

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    data_type: Mapped[str] = mapped_column(String(128))
    # Write path and run (app.core.provenance), as on raw_api_responses.
    source: Mapped[str] = mapped_column(String(32))
    context_id: Mapped[str | None] = mapped_column(String(160))
    # raw_api_responses.id of the exchange the page was read from (its last one).
    response_id: Mapped[int | None] = mapped_column(_ID)
    # Zero-based position among the records the caller passed, before anything was left out.
    ordinal: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64), default=REASON)
    # SHA-256 (hex) of element_json: the same record seen again has the same digest.
    element_sha256: Mapped[str] = mapped_column(String(64))
    element_json: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RawRejectionAck(Base):
    __tablename__ = ACKS
    __table_args__ = (CheckConstraint("through_id >= 1", name="through_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # raw_health_data_rejections.id the maintainer looked at, and everything before it.
    through_id: Mapped[int] = mapped_column(_ID)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RetrievalEventRejection(Base):
    __tablename__ = EVENT_COUNTS
    __table_args__ = (CheckConstraint("rejected_count > 0", name="rejected_count"),)

    # retrieval_events.id of a page (or failed/cancelled outcome) with rejected records.
    event_id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=False)
    rejected_count: Mapped[int] = mapped_column(Integer)


PG_TRIGGER_SQL = {}
PROTECTIVE_TRIGGERS = []
SQLITE_TRIGGER_SQL = {}
for _table in TABLES:
    for _suffix, _events, _level in (
        ("append_only", "UPDATE OR DELETE", "ROW"),
        ("no_truncate", "TRUNCATE", "STATEMENT"),
    ):
        _name = f"{_table}_{_suffix}"
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
# healthos_refuse_change() belongs to 0011 and is verified with its other guards.
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
