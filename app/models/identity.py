"""Stable identities of raw records and the duplicates they replace (migration 20261021_0012).

``raw_health_data_identity`` maps a record's v2 identity key (app/parsers/identity.py) to
the raw row that holds it. A raw row's ``google_data_point_id`` can never change (the frozen
identity trigger of 0011), so rows stored under a v1 id are not re-keyed: an alias row
points the v2 key at them, and ``store_records`` writes a returning record onto that row.

``raw_health_data_supersessions`` links a duplicate row that was archived under an older id
of the same record to the row that holds the record now. A superseded row is kept with its
payload, versions, ``source_deleted_at`` and events (it is history), but it no longer counts:
dashboards, Umay and the ``analytics_*`` views skip it, as they skip a source deletion.

Both tables are append-only (no ``UPDATE``, ``DELETE`` or ``TRUNCATE``; triggers
``ENABLE ALWAYS``) and ``health-mirror verify`` checks those triggers like the ones of 0011.
The trigger SQL is duplicated, frozen, in the migration; tests/test_identity_keys.py compares
the two.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Its after_create DDL defines healthos_refuse_change(), which the triggers below call.
import app.models.raw_versions  # noqa: E402, F401  isort: skip

IDENTITY_TABLE = "raw_health_data_identity"
SUPERSESSION_TABLE = "raw_health_data_supersessions"
IDENTITY_TABLES = (IDENTITY_TABLE, SUPERSESSION_TABLE)
# Why a row was superseded (app.parsers.identity.supersession_reason).
REASONS = (
    "display-name-change",
    "package-suffix-change",
    "source-change",
    "interval-shape-change",
    "growing-interval",
    "duplicate",
)
MERGED_EVENT = "identity-merged"


class RawHealthDataIdentity(Base):
    __tablename__ = IDENTITY_TABLE
    __table_args__ = (
        # Lookups are by key only (the time-led key keeps them local; no raw_id index:
        # raw rows are never deleted, so the foreign key never needs one).
        PrimaryKeyConstraint("data_type", "identity_key"),
    )

    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # "v2:<UTC start, to the second>:<sha256>"; "v2s:..." for a source kept apart.
    identity_key: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_health_data.id"), nullable=False)
    # The raw row's key when the alias was written (it never changes).
    google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawHealthDataSupersession(Base):
    __tablename__ = SUPERSESSION_TABLE
    __table_args__ = (
        PrimaryKeyConstraint("old_raw_id"),
        CheckConstraint("old_raw_id <> new_raw_id", name="distinct_rows"),
        Index("ix_raw_health_data_supersessions_new_raw_id", "new_raw_id"),
        Index("ix_raw_health_data_supersessions_old_key", "old_google_data_point_id", "data_type"),
    )

    # A row is superseded at most once; its successor was not superseded when linked.
    old_raw_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_health_data.id"), nullable=False)
    new_raw_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_health_data.id"), nullable=False)
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    old_google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False)
    new_google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def superseded(raw_id):
    """``EXISTS`` clause: the raw row ``raw_id`` was superseded by another row."""
    return (
        select(RawHealthDataSupersession.old_raw_id)
        .where(RawHealthDataSupersession.old_raw_id == raw_id)
        .exists()
    )


# --- guards ------------------------------------------------------------------------------
# The function healthos_refuse_change() comes from migration 20261020_0011.

# trigger -> (table, timing/events/level, function)
_PG_TRIGGERS = {}
for _table in IDENTITY_TABLES:
    _PG_TRIGGERS[f"{_table}_append_only"] = (
        _table,
        f"BEFORE UPDATE OR DELETE ON {_table} FOR EACH ROW",
        "healthos_refuse_change",
    )
    _PG_TRIGGERS[f"{_table}_no_truncate"] = (
        _table,
        f"BEFORE TRUNCATE ON {_table} FOR EACH STATEMENT",
        "healthos_refuse_change",
    )

PROTECTIVE_TRIGGERS = tuple(
    (table, trigger, function) for trigger, (table, _, function) in _PG_TRIGGERS.items()
)
PG_TRIGGER_SQL = {
    trigger: (table, f"CREATE TRIGGER {trigger}\n{definition}\nEXECUTE FUNCTION {function}()")
    for trigger, (table, definition, function) in _PG_TRIGGERS.items()
}

_SQLITE_TRIGGERS = {}
for _table in IDENTITY_TABLES:
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        _SQLITE_TRIGGERS[_name] = (
            _table,
            f"""CREATE TRIGGER {_name}
BEFORE {_operation} ON {_table}
BEGIN
    SELECT RAISE(ABORT, '{_table} keeps every record: {_operation} refused');
END""",
        )
SQLITE_TRIGGER_SQL = dict(_SQLITE_TRIGGERS)

POSTGRESQL_DDL = (
    *(sql for _, sql in PG_TRIGGER_SQL.values()),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}"
        for table, trigger, _ in PROTECTIVE_TRIGGERS
    ),
)
SQLITE_DDL = tuple(sql for _, sql in _SQLITE_TRIGGERS.values())

for _statement in POSTGRESQL_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
for _statement in SQLITE_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="sqlite"))
