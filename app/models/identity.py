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
``raw_source_chains`` records which source fingerprints are one device seen before and after
a change (a ``displayName`` rename, a dropped package suffix): one row per link ``from -> to``,
decided per data type from the sources' sighting timelines (app/services/source_chains.py).
It is append-only too. ``raw_source_sightings`` is the bookkeeping those decisions read: when
each source fingerprint was first and last seen. It is derived (like
``raw_health_data.last_seen_at``, updated in place on every fetch) and rebuilt by
``health-mirror identity-backfill`` from the raw rows and their versions; it holds no received
data.

The trigger SQL is duplicated, frozen, in the migration; tests/test_identity_keys.py compares
the two.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.state import RawJSON

# Its after_create DDL defines healthos_refuse_change(), which the triggers below call.
import app.models.raw_versions  # noqa: E402, F401  isort: skip

IDENTITY_TABLE = "raw_health_data_identity"
SUPERSESSION_TABLE = "raw_health_data_supersessions"
CHAIN_TABLE = "raw_source_chains"
SIGHTING_TABLE = "raw_source_sightings"
# The append-only tables of this revision (the sightings are bookkeeping, not guarded).
IDENTITY_TABLES = (IDENTITY_TABLE, SUPERSESSION_TABLE, CHAIN_TABLE)
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


class RawSourceChain(Base):
    """One link of a device's chain of source fingerprints, per data type."""

    __tablename__ = CHAIN_TABLE
    __table_args__ = (
        # A fingerprint has at most one successor and one predecessor: a chain, never a tree.
        PrimaryKeyConstraint("data_type", "from_fingerprint"),
        UniqueConstraint("data_type", "to_fingerprint"),
        CheckConstraint("from_fingerprint <> to_fingerprint", name="distinct_sources"),
    )

    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # app.parsers.identity.source_fingerprint of the dataSource before and after the change.
    from_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    to_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    from_label: Mapped[str] = mapped_column(String(255), nullable=False)
    to_label: Mapped[str] = mapped_column(String(255), nullable=False)
    # "backfill" or "live" (a page of a fetch, app.services.identity).
    decided_by: Mapped[str] = mapped_column(String(16), nullable=False)
    # JSON: both sighting spans, the gap, keys shared, the rule's tolerance.
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawSourceSighting(Base):
    """When a source fingerprint of a data type was first and last seen (derived)."""

    __tablename__ = SIGHTING_TABLE
    __table_args__ = (PrimaryKeyConstraint("data_type", "fingerprint"),)

    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Hash of the stable source fields (app.parsers.identity.stable_class): only fingerprints
    # of one class can share a v2 key.
    stable_class: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    data_source: Mapped[dict | None] = mapped_column(RawJSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The span covers the whole archive (identity-backfill built it, or the data type had no
    # rows when the fingerprint was first seen). Chains are only decided between complete
    # spans.
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
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
