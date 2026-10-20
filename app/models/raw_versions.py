"""Every payload Google ever sent for a raw record, kept as numbered versions.

``raw_health_data`` stays the table of *current* versions that every reader uses. When a
re-fetch brings a different payload for the same ``(data_type, google_data_point_id)``,
a ``BEFORE UPDATE`` trigger copies the row being replaced into
``raw_health_data_versions`` inside the same statement, so the old payload can never be
lost: not by a crash mid-batch (the copy and the overwrite commit or roll back together)
and not by concurrent writers (the trigger runs under the row lock and sees the row it
really replaces). Re-receiving the same payload changes nothing.

History rows are append-only (enforced by triggers); only ``version`` may be rewritten,
when ``health-mirror import-versions`` slots an older version recovered from an archive
dump in front of later ones. A record with no history rows is version 1, so existing
archives need no copy: the current row of a record with ``n`` history rows is version
``n + 1``.

The trigger and view SQL is duplicated, frozen, in migration ``20261020_0011``; the copies
below build the same objects for ``Base.metadata.create_all`` (SQLite tests).
tests/test_raw_versions.py compares the two.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DDL,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.state import RawJSON

HISTORY_VIEW = "raw_health_data_history"

# How a version reached the archive. Rows stored before migration 0011 have no recorded
# source; their versions show up as "legacy".
SOURCES = ("sync", "webhook", "reconciliation", "backfill", "archive-import", "legacy")


class RawHealthDataVersion(Base):
    __tablename__ = "raw_health_data_versions"
    __table_args__ = (
        UniqueConstraint("raw_id", "version"),
        Index("ix_raw_health_data_versions_key", "data_type", "google_data_point_id"),
        Index("ix_raw_health_data_versions_superseded_at", "superseded_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    # The record's current row in raw_health_data (never deleted; the FK forbids it).
    raw_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_health_data.id"), nullable=False)
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[dict] = mapped_column(RawJSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_timezone: Mapped[str | None] = mapped_column(String(128))
    # When this payload was first received; NULL if it arrived before migration 0011 (or
    # in an archive dump) and the time is unknown.
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The last fetch that still returned this payload.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The fetch that brought the next version; NULL for archive imports (unknown).
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# Columns copied from the replaced raw row; the trigger SQL lists them in this order.
_COPY = (
    "raw_id, data_type, google_data_point_id, version, raw_json, payload_hash, "
    "source_start_time, source_end_time, source_timezone, received_at, last_seen_at, "
    "superseded_at, source"
)
_OLD = (
    "OLD.id, OLD.data_type, OLD.google_data_point_id, "
    "(SELECT coalesce(max(v.version), 0) + 1 FROM raw_health_data_versions v "
    "WHERE v.raw_id = OLD.id), "
    "OLD.raw_json, OLD.payload_hash, OLD.source_start_time, OLD.source_end_time, "
    "OLD.source_timezone, OLD.payload_received_at, OLD.last_seen_at, NEW.last_seen_at, "
    "coalesce(OLD.payload_source, 'legacy')"
)
# Every history column except `version`: these may never change.
_FROZEN = (
    "id",
    "raw_id",
    "data_type",
    "google_data_point_id",
    "raw_json",
    "payload_hash",
    "source_start_time",
    "source_end_time",
    "source_timezone",
    "received_at",
    "last_seen_at",
    "superseded_at",
    "source",
    "created_at",
)

VIEW_SQL = f"""CREATE VIEW {HISTORY_VIEW} AS
SELECT v.data_type, v.google_data_point_id, v.raw_id, v.version, (1 = 0) AS is_current,
       v.raw_json, v.payload_hash, v.source,
       coalesce(v.received_at, CASE WHEN v.version = 1 THEN r.first_seen_at END) AS valid_from,
       v.superseded_at AS valid_to, v.last_seen_at, v.source_start_time, v.source_end_time,
       NULL AS source_deleted_at, v.id AS history_id
FROM raw_health_data_versions v
JOIN raw_health_data r ON r.id = v.raw_id
UNION ALL
SELECT r.data_type, r.google_data_point_id, r.id,
       coalesce((SELECT max(h.version) FROM raw_health_data_versions h WHERE h.raw_id = r.id), 0)
       + 1,
       (1 = 1), r.raw_json, r.payload_hash, coalesce(r.payload_source, 'legacy'),
       coalesce(
           r.payload_received_at,
           (SELECT max(h.superseded_at) FROM raw_health_data_versions h WHERE h.raw_id = r.id),
           CASE WHEN NOT EXISTS (SELECT 1 FROM raw_health_data_versions h WHERE h.raw_id = r.id)
                THEN r.first_seen_at END),
       NULL, r.last_seen_at, r.source_start_time, r.source_end_time, r.source_deleted_at, NULL
FROM raw_health_data r"""

POSTGRESQL_DDL = (
    f"""CREATE FUNCTION raw_health_data_keep_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO raw_health_data_versions ({_COPY})
    VALUES ({_OLD});
    RETURN NEW;
END
$$""",
    """CREATE TRIGGER raw_health_data_keep_version
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN (OLD.payload_hash IS DISTINCT FROM NEW.payload_hash)
EXECUTE FUNCTION raw_health_data_keep_version()""",
    f"""CREATE FUNCTION raw_health_data_versions_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF ({", ".join(f"NEW.{c}" for c in _FROZEN)})
           IS NOT DISTINCT FROM ({", ".join(f"OLD.{c}" for c in _FROZEN)}) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION USING MESSAGE =
        'raw_health_data_versions is append-only: ' || TG_OP || ' refused';
END
$$""",
    """CREATE TRIGGER raw_health_data_versions_append_only
BEFORE UPDATE OR DELETE ON raw_health_data_versions FOR EACH ROW
EXECUTE FUNCTION raw_health_data_versions_append_only()""",
    """CREATE TRIGGER raw_health_data_versions_no_truncate
BEFORE TRUNCATE ON raw_health_data_versions FOR EACH STATEMENT
EXECUTE FUNCTION raw_health_data_versions_append_only()""",
    VIEW_SQL,
)

SQLITE_DDL = (
    f"""CREATE TRIGGER raw_health_data_keep_version
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN OLD.payload_hash IS NOT NEW.payload_hash
BEGIN
    INSERT INTO raw_health_data_versions ({_COPY})
    VALUES ({_OLD});
END""",
    """CREATE TRIGGER raw_health_data_versions_no_delete
BEFORE DELETE ON raw_health_data_versions
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: DELETE refused');
END""",
    f"""CREATE TRIGGER raw_health_data_versions_no_update
BEFORE UPDATE ON raw_health_data_versions FOR EACH ROW
WHEN {" OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _FROZEN)}
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: UPDATE refused');
END""",
    VIEW_SQL,
)

_table = RawHealthDataVersion.__table__
for _statement in POSTGRESQL_DDL:
    event.listen(_table, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
for _statement in SQLITE_DDL:
    event.listen(_table, "after_create", DDL(_statement).execute_if(dialect="sqlite"))
event.listen(_table, "before_drop", DDL(f"DROP VIEW IF EXISTS {HISTORY_VIEW}"))
