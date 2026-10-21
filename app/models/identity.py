"""Stable identities of raw records and the duplicates they replace (migration 20261021_0012).

``raw_health_data_identity`` maps a record's fingerprint-specific v2 key
(``RecordIdentity.specific_key``, app/parsers/identity.py) to the raw row that holds it, for
rows whose own id does not say so: a raw row's ``google_data_point_id`` can never change (the
frozen identity trigger of 0011), so rows stored under a v1 id are not re-keyed; the reconciler
(``health-mirror identity-backfill``) gives each of them an alias, and ``store_records`` finds
a returning record through it. Rows stored since 0012 need none: their id
(``synthetic:v2s:<hash>``) is computed from the key and the fingerprint.

``raw_health_data_supersessions`` links a duplicate row that was archived under an older id
of the same record to the row that holds the record now. A superseded row is kept with its
payload, versions, ``source_deleted_at`` and events (it is history), and it no longer counts
while its holder is live: dashboards, Umay and the ``analytics_*`` views skip it, as they skip a
source deletion (``held_elsewhere``). When the holder is marked deleted at the source, a live
superseded row counts again: a link never hides a live record behind a deleted one.

``raw_source_chains`` records which source fingerprints are one device seen before and after
a change (a ``displayName`` rename, a dropped package suffix): one row per link ``from -> to``,
decided per data type and stable source class by the reconciler from the sources' sighting
timelines (app/services/source_chains.py), with the spans it was decided on as ``evidence``,
or by the owner (``health-mirror identity-decide link``). A link joins the two forms into one
device in both directions: the old form coming back later does not undo it.
``raw_source_chain_events`` records when a link was *refuted* (the two forms turned out to be
two devices): from then on it is no longer used, while the supersessions made under it stay.

``raw_source_pairs`` records evidence of two distinct sources: a key returned in one response
under two fingerprints with two different readings (one record has one value). Two
fingerprints with at least ``PAIR_KEYS`` such keys are *established distinct*.

``raw_source_decisions`` records the owner's decisions about two fingerprints (link, reject or
confirm a chain; mark a pair distinct or not distinct). The latest decision per question wins.

These six tables are append-only (no ``UPDATE``, ``DELETE`` or ``TRUNCATE``; triggers
``ENABLE ALWAYS``). ``raw_source_sightings`` - when each source fingerprint was first and last
seen, by the database clock - may only widen: no ``DELETE`` or ``TRUNCATE``, and an ``UPDATE``
may only move ``first_seen_at`` earlier, ``last_seen_at`` later and ``complete`` to true.
``health-mirror verify`` checks all of these triggers like the ones of 0011.

``identity_reconcile_state`` is bookkeeping: per data type, how far the reconciler got.

The trigger SQL is duplicated, frozen, in the migration; tests/test_identity_keys.py compares
the two.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, aliased, mapped_column

from app.db.base import Base
from app.models.state import RawHealthData, RawJSON

# Its after_create DDL defines healthos_refuse_change(), which the triggers below call.
import app.models.raw_versions  # noqa: E402, F401  isort: skip

IDENTITY_TABLE = "raw_health_data_identity"
SUPERSESSION_TABLE = "raw_health_data_supersessions"
CHAIN_TABLE = "raw_source_chains"
CHAIN_EVENT_TABLE = "raw_source_chain_events"
PAIR_TABLE = "raw_source_pairs"
DECISION_TABLE = "raw_source_decisions"
SIGHTING_TABLE = "raw_source_sightings"
RECONCILE_TABLE = "identity_reconcile_state"
# The append-only tables of this revision.
IDENTITY_TABLES = (
    IDENTITY_TABLE,
    SUPERSESSION_TABLE,
    CHAIN_TABLE,
    CHAIN_EVENT_TABLE,
    PAIR_TABLE,
    DECISION_TABLE,
)
# Every table whose rows verify protects (the sightings may only widen).
GUARDED_TABLES = (*IDENTITY_TABLES, SIGHTING_TABLE)
# Who decides a chain: a full run of identity-backfill, the incremental reconciler, or the
# owner (identity-decide link, with its decision row).
DECIDERS = ("backfill", "reconciler", "owner")
# A chain whose two forms turned out to be two devices (raw_source_chain_events).
REFUTED = "refuted"
# The owner's decisions (raw_source_decisions): about a chain from -> to, or about a pair.
CHAIN_DECISIONS = ("link", "reject", "confirm")
PAIR_DECISIONS = ("distinct", "not-distinct")
DECISIONS = (*CHAIN_DECISIONS, *PAIR_DECISIONS)
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
    # "v2s:<UTC start, to the second>:<sha256 of key and fingerprint>".
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
    # DECIDERS: "backfill" (a full run) or "reconciler" (an incremental run).
    decided_by: Mapped[str] = mapped_column(String(16), nullable=False)
    # JSON: both sighting spans as they were when the link was decided, the gap, the rule.
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawSourceChainEvent(Base):
    """A chain link was refuted (its two forms are two devices): it is no longer used."""

    __tablename__ = CHAIN_EVENT_TABLE
    __table_args__ = (PrimaryKeyConstraint("data_type", "from_fingerprint", "event"),)

    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    from_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    to_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # REFUTED.
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    # JSON: both spans and the evidence (or decision) when the link was refuted.
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawSourcePair(Base):
    """One key that one Google response returned under two fingerprints with two different
    readings: evidence of two distinct sources.

    One record has one value: a renamed device (or one whose old name comes back) never
    returns two values for one record, two devices that measured one instant do. Written by
    a page that holds both (one response), and by a full identity-backfill (two rows of one
    key stored or last seen by the same page). A pair is *established distinct* with at least
    ``PAIR_KEYS`` keys (app/services/source_chains.py); a few more are never recorded."""

    __tablename__ = PAIR_TABLE
    __table_args__ = (
        PrimaryKeyConstraint(
            "data_type", "first_fingerprint", "second_fingerprint", "identity_key"
        ),
        CheckConstraint("first_fingerprint < second_fingerprint", name="ordered"),
    )

    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    first_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    second_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # The v2 key (RecordIdentity.key) returned under both.
    identity_key: Mapped[str] = mapped_column(String(100), nullable=False)
    seen_together_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawSourceDecision(Base):
    """The owner's decision about two source fingerprints (``health-mirror identity-decide``).

    ``link``, ``reject``, ``confirm``: about the chain ``first -> second`` (record it; stop
    using it; reviewed, keep it). ``distinct``, ``not-distinct``: about the pair (two devices;
    the evidence of two devices is to be ignored). The latest row per question wins."""

    __tablename__ = DECISION_TABLE
    __table_args__ = (
        CheckConstraint("first_fingerprint <> second_fingerprint", name="distinct_sources"),
        CheckConstraint(
            "decision IN ('link', 'reject', 'confirm', 'distinct', 'not-distinct')",
            name="known_decision",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    first_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    second_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    first_label: Mapped[str] = mapped_column(String(255), nullable=False)
    second_label: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdentityReconcileState(Base):
    """How far the reconciler got for a data type (bookkeeping, not guarded)."""

    __tablename__ = RECONCILE_TABLE

    data_type: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Rows changed (updated_at) since this database time, minus the safety overlap, are
    # what the next incremental run reads. Set by every successful run to its start.
    checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When a full run (identity-backfill without --incremental) last completed.
    full_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_mode: Mapped[str | None] = mapped_column(String(16))
    # JSON summary of the last run (links, chains, ambiguous sources, rows read).
    last_result: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    runs: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawSourceSighting(Base):
    """When a source fingerprint of a data type was first and last seen (may only widen)."""

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


def held_elsewhere(raw_id):
    """``EXISTS`` clause: ``raw_id`` was superseded by a row that is not marked deleted at the
    source. Readers skip exactly these rows (the holder counts their record); a superseded row
    whose holder was marked deleted counts again when it is live itself, so a link can never
    hide a live record behind a deleted one."""
    holder = aliased(RawHealthData)
    link = RawHealthDataSupersession
    return (
        select(link.old_raw_id)
        .join(holder, holder.id == link.new_raw_id)
        .where(link.old_raw_id == raw_id, holder.source_deleted_at.is_(None))
        .exists()
    )


# --- guards ------------------------------------------------------------------------------
# The function healthos_refuse_change() comes from migration 20261020_0011.

SIGHTINGS_FUNCTION = "healthos_sightings_widen_only"
FUNCTION_BODIES = {
    SIGHTINGS_FUNCTION: f"""
BEGIN
    IF NEW.data_type IS DISTINCT FROM OLD.data_type
       OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
       OR NEW.stable_class IS DISTINCT FROM OLD.stable_class
       OR NEW.first_seen_at > OLD.first_seen_at
       OR NEW.last_seen_at < OLD.last_seen_at
       OR (OLD.complete AND NOT NEW.complete) THEN
        RAISE EXCEPTION USING MESSAGE =
            '{SIGHTING_TABLE} only widens: ' || TG_OP || ' refused';
    END IF;
    RETURN NEW;
END
""",
}

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
_PG_TRIGGERS[f"{SIGHTING_TABLE}_widen_only"] = (
    SIGHTING_TABLE,
    f"BEFORE UPDATE ON {SIGHTING_TABLE} FOR EACH ROW",
    SIGHTINGS_FUNCTION,
)
_PG_TRIGGERS[f"{SIGHTING_TABLE}_no_delete"] = (
    SIGHTING_TABLE,
    f"BEFORE DELETE ON {SIGHTING_TABLE} FOR EACH ROW",
    "healthos_refuse_change",
)
_PG_TRIGGERS[f"{SIGHTING_TABLE}_no_truncate"] = (
    SIGHTING_TABLE,
    f"BEFORE TRUNCATE ON {SIGHTING_TABLE} FOR EACH STATEMENT",
    "healthos_refuse_change",
)

PROTECTIVE_TRIGGERS = tuple(
    (table, trigger, function) for trigger, (table, _, function) in _PG_TRIGGERS.items()
)
PG_TRIGGER_SQL = {
    trigger: (table, f"CREATE TRIGGER {trigger}\n{definition}\nEXECUTE FUNCTION {function}()")
    for trigger, (table, definition, function) in _PG_TRIGGERS.items()
}


def _sqlite_refusal(name: str, table: str, operation: str, when: str = "") -> str:
    condition = f"\nWHEN {when}" if when else ""
    message = (
        f"{table} only widens: {operation} refused"
        if when
        else f"{table} keeps every record: {operation} refused"
    )
    return f"""CREATE TRIGGER {name}
BEFORE {operation} ON {table}{condition}
BEGIN
    SELECT RAISE(ABORT, '{message}');
END"""


_SQLITE_TRIGGERS = {}
for _table in IDENTITY_TABLES:
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        _SQLITE_TRIGGERS[_name] = (_table, _sqlite_refusal(_name, _table, _operation))
_SQLITE_TRIGGERS[f"{SIGHTING_TABLE}_no_delete"] = (
    SIGHTING_TABLE,
    _sqlite_refusal(f"{SIGHTING_TABLE}_no_delete", SIGHTING_TABLE, "DELETE"),
)
_SQLITE_TRIGGERS[f"{SIGHTING_TABLE}_widen_only"] = (
    SIGHTING_TABLE,
    _sqlite_refusal(
        f"{SIGHTING_TABLE}_widen_only",
        SIGHTING_TABLE,
        "UPDATE",
        "NEW.data_type IS NOT OLD.data_type OR NEW.fingerprint IS NOT OLD.fingerprint "
        "OR NEW.stable_class IS NOT OLD.stable_class OR NEW.first_seen_at > OLD.first_seen_at "
        "OR NEW.last_seen_at < OLD.last_seen_at OR (OLD.complete AND NOT NEW.complete)",
    ),
)
SQLITE_TRIGGER_SQL = dict(_SQLITE_TRIGGERS)

POSTGRESQL_DDL = (
    *(
        f"CREATE FUNCTION {name}() RETURNS trigger\nLANGUAGE plpgsql AS $${body}$$"
        for name, body in FUNCTION_BODIES.items()
    ),
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
