"""ORM definitions for raw-record identities, source evidence and reconciliation state."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.naming import conv

from app.db.base import Base
from app.models.identity_constants import (
    AGREEMENT_TABLE,
    CHAIN_EVENT_TABLE,
    CHAIN_TABLE,
    DECISION_TABLE,
    DEFERRAL_TABLE,
    DEVICE_TABLE,
    IDENTITY_TABLE,
    PAIR_TABLE,
    RECONCILE_TABLE,
    SIGHTING_TABLE,
    SUPERSESSION_TABLE,
)
from app.models.state import RawJSON

# Load the archive tables and their healthos_refuse_change() DDL before identity guards.
import app.models.raw_versions  # noqa: E402, F401  isort: skip


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
        PrimaryKeyConstraint("old_raw_id", "new_raw_id"),
        CheckConstraint("old_raw_id <> new_raw_id", name="distinct_rows"),
        Index("ix_raw_health_data_supersessions_new_raw_id", "new_raw_id"),
        Index("ix_raw_health_data_supersessions_old_key", "old_google_data_point_id", "data_type"),
        # The links between two sources, found when they are no longer one device.
        Index(
            "ix_raw_health_data_supersessions_sources",
            "data_type",
            "old_fingerprint",
            "new_fingerprint",
        ),
    )

    # A row may be linked again when its links are no longer in force (identity_queries.in_force);
    # its holder is the target of its newest in-force link.
    old_raw_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_health_data.id"), nullable=False)
    new_raw_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_health_data.id"), nullable=False)
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    old_google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False)
    new_google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    # The two rows' source fingerprints when they were linked: the link is in force while
    # they are one, or one device.
    old_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    new_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Both rows held the same reading when they were linked, and the holder's payload hash
    # then: while the holder still has it, the superseded row's record is the holder's.
    same_reading: Mapped[bool] = mapped_column(Boolean, nullable=False)
    new_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawSourceChain(Base):
    """One link of a device's chain of source fingerprints, per data type."""

    __tablename__ = CHAIN_TABLE
    __table_args__ = (
        PrimaryKeyConstraint("id"),
        # A pair is recorded once (rejected, it can be linked again by a decision). That a
        # fingerprint has at most one *live* successor and predecessor (a chain, never a
        # tree) is checked when a link is recorded or revived, and by verify.
        UniqueConstraint(
            "data_type",
            "from_fingerprint",
            "to_fingerprint",
            name=conv("uq_raw_source_chains_link"),
        ),
        CheckConstraint("from_fingerprint <> to_fingerprint", name="distinct_sources"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # app.parsers.identity.source_fingerprint of the dataSource before and after the change.
    from_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    to_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    from_label: Mapped[str] = mapped_column(String(255), nullable=False)
    to_label: Mapped[str] = mapped_column(String(255), nullable=False)
    # DECIDERS: "backfill" (a full run), "reconciler" (an incremental run) or "owner".
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
    __table_args__ = (
        PrimaryKeyConstraint("data_type", "from_fingerprint", "to_fingerprint", "event"),
    )

    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    from_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    to_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # REFUTED.
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    # JSON: the evidence (or decision) when the link was refuted.
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawSourceDevice(Base):
    """The device a fingerprint belongs to from now on (its smallest fingerprint joined by
    live chain links), appended whenever that changes; the latest row per fingerprint is
    current. Readers decide from it whether a supersession is in force."""

    __tablename__ = DEVICE_TABLE
    __table_args__ = (
        PrimaryKeyConstraint("id"),
        Index("ix_raw_source_devices_current", "data_type", "fingerprint", "id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    device: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawSourcePair(Base):
    """One key that one Google response returned under two fingerprints with two different
    readings: evidence of two distinct sources.

    One record has one value: a renamed device (or one whose old name comes back) never
    returns two values for one record, two devices that measured one instant do. Written by
    a page that holds both (one response), and by a full identity-backfill (two rows of one
    key stored or last seen by the same page). ``seen_together_at`` is when the fetch began
    (or the page was stored): the rule of *established distinct* needs two fetches
    (app/services/source_chains.py). Recording stops once a pair is established, and a pair
    that cannot be established by a key adds no more than ``PAIR_KEYS`` keys."""

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
    # JSON: per fingerprint pair of evidence, how many of the keys both hold agree (some row
    # of each holds the same reading) and how many hold two readings, and whether every key
    # both hold was counted (app/services/source_chains.py, ``agreeing``): the latest rows of
    # raw_source_agreements summed, updated in the transaction that appends them.
    pair_agreement: Mapped[str | None] = mapped_column(Text)
    # JSON: rows a run or a decision still has to settle again because devices changed (the
    # pairs no longer one device, the earliest first sighting of pairs newly one), kept until
    # that re-settlement finished, so an interrupted run resumes it.
    pending_settle: Mapped[str | None] = mapped_column(Text)
    runs: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawSourceAgreement(Base):
    """Whether the rows of two fingerprints (with evidence of two devices) of one key hold the
    same reading, appended when that changes (the latest row per pair and key is current)."""

    __tablename__ = AGREEMENT_TABLE
    __table_args__ = (
        PrimaryKeyConstraint("id"),
        CheckConstraint("first_fingerprint < second_fingerprint", name="ordered"),
        Index(
            "ix_raw_source_agreements_key",
            "data_type",
            "identity_key",
            "first_fingerprint",
            "second_fingerprint",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    first_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    second_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(100), nullable=False)
    same_reading: Mapped[bool] = mapped_column(Boolean, nullable=False)
    counted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdentityDeferredDeletion(Base):
    """A raw row whose deletion a complete fetch deferred (its reading came back under a source
    that may be its device renamed), until a later complete fetch of its window decides it
    (bookkeeping, not guarded; never deleted: ``resolved_at`` is set)."""

    __tablename__ = DEFERRAL_TABLE
    __table_args__ = (
        Index(
            "ix_identity_deferred_deletions_open",
            "data_type",
            postgresql_where=text("resolved_at IS NULL"),
            sqlite_where=text("resolved_at IS NULL"),
        ),
    )

    raw_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_health_data.id"), primary_key=True)
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # The row's fingerprint, and the one that returned its reading.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    returned_by: Mapped[str] = mapped_column(String(64), nullable=False)
    first_deferred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_deferred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # The physical time of the newest record returned under it (a re-fetch of old records
    # sights a source again without making it newer: a retired device stays retired).
    newest_record_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The span covers the whole archive (identity-backfill built it, or the data type had no
    # rows when the fingerprint was first seen). Chains are only decided between complete
    # spans.
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
