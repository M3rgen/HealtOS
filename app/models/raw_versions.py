"""Every payload Google ever sent for a raw record, kept as numbered versions.

``raw_health_data`` stays the table of *current* versions that every reader uses. When a
re-fetch brings a different payload for the same ``(data_type, google_data_point_id)``,
a ``BEFORE UPDATE`` trigger copies the row being replaced into
``raw_health_data_versions`` inside the same statement, so the old payload can never be
lost: not by a crash mid-batch (the copy and the overwrite commit or roll back together)
and not by concurrent writers (the trigger runs under the row lock and sees the row it
really replaces). "Different" means a different ``payload_hash`` *or* a different stored
text (``raw_json::text``), so an overwrite that keeps the hash still keeps the old payload.
Re-receiving the same payload changes nothing.

Nothing received from Google is deleted: triggers refuse ``DELETE`` and ``TRUNCATE`` on
``raw_health_data`` (also through ``TRUNCATE ... CASCADE``) and on the history, and refuse
changing a record's identity (``id``, ``data_type``, ``google_data_point_id``,
``first_seen_at``, ``created_at``). History rows are append-only; the one permitted change
is renumbering ``version`` while ``health-mirror import-versions`` slots an older version
recovered from an archive dump in front of later ones, and on PostgreSQL only inside a
transaction that set ``healthos.renumber = '1'`` (which only that command does). A record
with no history rows is version 1, so existing archives need no copy: the current row of a
record with ``n`` history rows is version ``n + 1``.

On PostgreSQL every protective trigger is ``ENABLE ALWAYS``, so it also fires under
``session_replication_role = replica``; ``health-mirror verify`` fails when one is
missing, disabled or its function body differs from :data:`FUNCTION_BODIES`.

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
# Set (transaction-local) by health-mirror import-versions; see the module docstring.
RENUMBER_SETTING = "healthos.renumber"

# How a version reached the archive. Rows stored before migration 0011 have no recorded
# source; their versions show up as "legacy".
SOURCES = (
    "sync",
    "webhook",
    "reconciliation",
    "backfill",
    "gap-repair",
    "archive-import",
    "legacy",
)


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
    # The record's current row in raw_health_data (never deleted; triggers forbid it).
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
    # The fetch that brought the next version. For archive imports: the dump's own value
    # when it has one, otherwise the payload's last sighting (it was replaced after that).
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
# Every history column except `version`: these may never change. raw_json is compared as
# text, so a respelled number (1.0 for 1) counts as a change.
_FROZEN = (
    "id",
    "raw_id",
    "data_type",
    "google_data_point_id",
    "raw_json::text",
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
# A raw record's identity: never rewritten (a new key is a new record).
_IDENTITY = ("id", "data_type", "google_data_point_id", "first_seen_at", "created_at")


def _row(prefix: str, columns) -> str:
    return ", ".join(f"{prefix}.{column}" for column in columns)


def _sqlite_changed(columns) -> str:
    return " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in (c.split("::")[0] for c in columns))


FUNCTION_BODIES = {
    "healthos_refuse_change": """
BEGIN
    RAISE EXCEPTION USING MESSAGE =
        TG_TABLE_NAME || ' keeps every record: ' || TG_OP || ' refused';
END
""",
    "raw_health_data_keep_version": f"""
BEGIN
    INSERT INTO raw_health_data_versions ({_COPY})
    VALUES ({_OLD});
    RETURN NEW;
END
""",
    "raw_health_data_versions_append_only": f"""
BEGIN
    IF TG_OP = 'UPDATE'
       AND current_setting('{RENUMBER_SETTING}', true) = '1'
       AND ({_row("NEW", _FROZEN)}) IS NOT DISTINCT FROM ({_row("OLD", _FROZEN)}) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING MESSAGE =
        'raw_health_data_versions is append-only: ' || TG_OP || ' refused';
END
""",
}

# (table, trigger, function) of every protective PostgreSQL trigger; verify checks them.
PROTECTIVE_TRIGGERS = (
    ("raw_health_data", "raw_health_data_keep_version", "raw_health_data_keep_version"),
    ("raw_health_data", "raw_health_data_frozen_identity", "healthos_refuse_change"),
    ("raw_health_data", "raw_health_data_no_delete", "healthos_refuse_change"),
    ("raw_health_data", "raw_health_data_no_truncate", "healthos_refuse_change"),
    (
        "raw_health_data_versions",
        "raw_health_data_versions_append_only",
        "raw_health_data_versions_append_only",
    ),
    ("raw_health_data_versions", "raw_health_data_versions_no_truncate", "healthos_refuse_change"),
)
# (table, trigger) of every protective SQLite trigger (SQLite cannot disable triggers).
SQLITE_TRIGGERS = (
    ("raw_health_data", "raw_health_data_keep_version"),
    ("raw_health_data", "raw_health_data_frozen_identity"),
    ("raw_health_data", "raw_health_data_no_delete"),
    ("raw_health_data_versions", "raw_health_data_versions_no_delete"),
    ("raw_health_data_versions", "raw_health_data_versions_no_update"),
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

_TRIGGER_SQL = {
    "raw_health_data_keep_version": """BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN (OLD.payload_hash IS DISTINCT FROM NEW.payload_hash
      OR OLD.raw_json::text IS DISTINCT FROM NEW.raw_json::text)""",
    "raw_health_data_frozen_identity": f"""BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN (({_row("OLD", _IDENTITY)}) IS DISTINCT FROM ({_row("NEW", _IDENTITY)}))""",
    "raw_health_data_no_delete": "BEFORE DELETE ON raw_health_data FOR EACH ROW",
    "raw_health_data_no_truncate": "BEFORE TRUNCATE ON raw_health_data FOR EACH STATEMENT",
    "raw_health_data_versions_append_only": (
        "BEFORE UPDATE OR DELETE ON raw_health_data_versions FOR EACH ROW"
    ),
    "raw_health_data_versions_no_truncate": (
        "BEFORE TRUNCATE ON raw_health_data_versions FOR EACH STATEMENT"
    ),
}

POSTGRESQL_DDL = (
    *(
        f"CREATE FUNCTION {name}() RETURNS trigger\nLANGUAGE plpgsql AS $${body}$$"
        for name, body in FUNCTION_BODIES.items()
    ),
    *(
        f"CREATE TRIGGER {trigger}\n{_TRIGGER_SQL[trigger]}\nEXECUTE FUNCTION {function}()"
        for _, trigger, function in PROTECTIVE_TRIGGERS
    ),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}"
        for table, trigger, _ in PROTECTIVE_TRIGGERS
    ),
    VIEW_SQL,
)


def _sqlite_refuse(table: str, operation: str) -> str:
    return f"SELECT RAISE(ABORT, '{table} keeps every record: {operation} refused');"


SQLITE_DDL = (
    f"""CREATE TRIGGER raw_health_data_keep_version
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN OLD.payload_hash IS NOT NEW.payload_hash OR OLD.raw_json IS NOT NEW.raw_json
BEGIN
    INSERT INTO raw_health_data_versions ({_COPY})
    VALUES ({_OLD});
END""",
    f"""CREATE TRIGGER raw_health_data_frozen_identity
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN {_sqlite_changed(_IDENTITY)}
BEGIN
    {_sqlite_refuse("raw_health_data", "UPDATE")}
END""",
    f"""CREATE TRIGGER raw_health_data_no_delete
BEFORE DELETE ON raw_health_data
BEGIN
    {_sqlite_refuse("raw_health_data", "DELETE")}
END""",
    """CREATE TRIGGER raw_health_data_versions_no_delete
BEFORE DELETE ON raw_health_data_versions
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: DELETE refused');
END""",
    # SQLite (tests only) has no session settings: renumbering is not gated there.
    f"""CREATE TRIGGER raw_health_data_versions_no_update
BEFORE UPDATE ON raw_health_data_versions FOR EACH ROW
WHEN {_sqlite_changed(_FROZEN)}
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: UPDATE refused');
END""",
    VIEW_SQL,
)

# Built once every table exists (the functions and views span several tables).
for _statement in POSTGRESQL_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
for _statement in SQLITE_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="sqlite"))
event.listen(Base.metadata, "before_drop", DDL(f"DROP VIEW IF EXISTS {HISTORY_VIEW}"))
