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
``first_seen_at``, ``created_at``). The same guards cover the verbatim responses, the
source-deletion events, the webhook tables (whose received payload is frozen) and the
manual measurements and workouts (append-only). History rows are append-only; the one
permitted change is renumbering ``version`` while ``health-mirror import-versions`` slots
an older version recovered from an archive dump in front of later ones, and on PostgreSQL
only inside a transaction that set ``healthos.renumber = '1'`` (which only that command
does). A record with no history rows is version 1, so existing archives need no copy: the
current row of a record with ``n`` history rows is version ``n + 1``.

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
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
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
    # The Google response this payload was first read from (NULL: before 0011, imports).
    response_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), ForeignKey("raw_api_responses.id")
    )


class RawApiResponse(Base):
    """One HTTP response body from the Google Health API, byte for byte (append-only)."""

    __tablename__ = "raw_api_responses"
    __table_args__ = (
        Index("ix_raw_api_responses_type_fetched", "data_type", "fetched_at"),
        Index("ix_raw_api_responses_context_id", "context_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Write path and run (app.core.provenance): sync, webhook, backfill, probe, ...
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    context_id: Mapped[str | None] = mapped_column(String(160))
    data_type: Mapped[str | None] = mapped_column(String(128))
    # The request, without headers (no token): method, path, query string as sent, and the
    # JSON body of rollUp requests.
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    query: Mapped[str | None] = mapped_column(Text)
    request_body: Mapped[bytes | None] = mapped_column(LargeBinary)
    # 0 for the first exchange of a request; retries and token refreshes count up.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    headers: Mapped[dict] = mapped_column(JSON, nullable=False)
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # SHA-256 (hex) and length of `body`; health-mirror verify recomputes both.
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # False when the connection failed before the body ended; `error` names the failure.
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawHealthDataEvent(Base):
    """A source-deletion mark set or cleared on a raw record (append-only, by trigger)."""

    __tablename__ = "raw_health_data_events"
    __table_args__ = (Index("ix_raw_health_data_events_raw_id", "raw_id"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    raw_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_health_data.id"), nullable=False)
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # source-deleted (marked, also when restored from a dump as deleted) or source-returned.
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    # The mark's time, or for a return the fetch that saw the record again.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The mark being replaced or cleared.
    previous_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# Columns copied from the replaced raw row; the trigger SQL lists them in this order.
_COPY = (
    "raw_id, data_type, google_data_point_id, version, raw_json, payload_hash, "
    "source_start_time, source_end_time, source_timezone, received_at, last_seen_at, "
    "superseded_at, source, response_id"
)
_OLD = (
    "OLD.id, OLD.data_type, OLD.google_data_point_id, "
    "(SELECT coalesce(max(v.version), 0) + 1 FROM raw_health_data_versions v "
    "WHERE v.raw_id = OLD.id), "
    "OLD.raw_json, OLD.payload_hash, OLD.source_start_time, OLD.source_end_time, "
    "OLD.source_timezone, OLD.payload_received_at, OLD.last_seen_at, NEW.last_seen_at, "
    "coalesce(OLD.payload_source, 'legacy'), OLD.response_id"
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
    "response_id",
)
# A raw record's identity: never rewritten (a new key is a new record).
_IDENTITY = ("id", "data_type", "google_data_point_id", "first_seen_at", "created_at")
_EVENT = (
    "INSERT INTO raw_health_data_events "
    "(raw_id, data_type, google_data_point_id, event, occurred_at, previous_deleted_at) "
    "VALUES (NEW.id, NEW.data_type, NEW.google_data_point_id, "
    "CASE WHEN NEW.source_deleted_at IS NULL THEN 'source-returned' "
    "ELSE 'source-deleted' END, "
    "coalesce(NEW.source_deleted_at, NEW.last_seen_at), {previous})"
)
# Tables whose rows never change once written: what Google sent, what was derived from it
# for the timeline, and what the maintainer entered by hand (no edit or delete path exists; a
# correction is a new entry).
APPEND_ONLY_TABLES = (
    "raw_api_responses",
    "raw_health_data_events",
    "webhook_batch_items",
    "manual_measurements",
    "manual_workouts",
)
# Tables whose rows are never deleted and whose received payload never changes, while
# processing state (status, counters, errors, processed_at) is updated in place:
# table -> (frozen columns, extra PostgreSQL / SQLite condition for a refused UPDATE).
PAYLOAD_TABLES = {
    "webhook_events": (
        ("payload_hash", "raw_json::text", "created_at"),
        # The signed body may be filled in once (legacy batches), never replaced.
        "OLD.raw_body IS NOT NULL AND OLD.raw_body IS DISTINCT FROM NEW.raw_body",
        "OLD.raw_body IS NOT NULL AND OLD.raw_body IS NOT NEW.raw_body",
    ),
    "webhook_notifications": (("element_hash", "raw_json::text", "created_at"), None, None),
}


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
    "raw_health_data_record_event": f"""
BEGIN
    {_EVENT.format(previous="CASE WHEN TG_OP = 'UPDATE' THEN OLD.source_deleted_at END")};
    RETURN NULL;
END
""",
}

# trigger -> (table, timing/events/level/WHEN, function) for every protective PostgreSQL
# trigger; health-mirror verify checks each one.
_PG_TRIGGERS = {
    "raw_health_data_keep_version": (
        "raw_health_data",
        """BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN (OLD.payload_hash IS DISTINCT FROM NEW.payload_hash
      OR OLD.raw_json::text IS DISTINCT FROM NEW.raw_json::text)""",
        "raw_health_data_keep_version",
    ),
    "raw_health_data_frozen_identity": (
        "raw_health_data",
        f"""BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN (({_row("OLD", _IDENTITY)}) IS DISTINCT FROM ({_row("NEW", _IDENTITY)}))""",
        "healthos_refuse_change",
    ),
    "raw_health_data_no_delete": (
        "raw_health_data",
        "BEFORE DELETE ON raw_health_data FOR EACH ROW",
        "healthos_refuse_change",
    ),
    "raw_health_data_no_truncate": (
        "raw_health_data",
        "BEFORE TRUNCATE ON raw_health_data FOR EACH STATEMENT",
        "healthos_refuse_change",
    ),
    "raw_health_data_deletion_event": (
        "raw_health_data",
        """AFTER UPDATE ON raw_health_data FOR EACH ROW
WHEN (OLD.source_deleted_at IS DISTINCT FROM NEW.source_deleted_at)""",
        "raw_health_data_record_event",
    ),
    "raw_health_data_restored_event": (
        "raw_health_data",
        """AFTER INSERT ON raw_health_data FOR EACH ROW
WHEN (NEW.source_deleted_at IS NOT NULL)""",
        "raw_health_data_record_event",
    ),
    "raw_health_data_versions_append_only": (
        "raw_health_data_versions",
        "BEFORE UPDATE OR DELETE ON raw_health_data_versions FOR EACH ROW",
        "raw_health_data_versions_append_only",
    ),
    "raw_health_data_versions_no_truncate": (
        "raw_health_data_versions",
        "BEFORE TRUNCATE ON raw_health_data_versions FOR EACH STATEMENT",
        "healthos_refuse_change",
    ),
}
for _table in APPEND_ONLY_TABLES:
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

for _table, (_columns, _pg_extra, _) in PAYLOAD_TABLES.items():
    _when = f"({_row('OLD', _columns)}) IS DISTINCT FROM ({_row('NEW', _columns)})"
    if _pg_extra:
        _when = f"{_when}\n      OR ({_pg_extra})"
    _PG_TRIGGERS[f"{_table}_frozen_payload"] = (
        _table,
        f"BEFORE UPDATE ON {_table} FOR EACH ROW\nWHEN ({_when})",
        "healthos_refuse_change",
    )
    _PG_TRIGGERS[f"{_table}_no_delete"] = (
        _table,
        f"BEFORE DELETE ON {_table} FOR EACH ROW",
        "healthos_refuse_change",
    )
    _PG_TRIGGERS[f"{_table}_no_truncate"] = (
        _table,
        f"BEFORE TRUNCATE ON {_table} FOR EACH STATEMENT",
        "healthos_refuse_change",
    )

# (table, trigger, function) of every protective PostgreSQL trigger.
PROTECTIVE_TRIGGERS = tuple(
    (table, trigger, function) for trigger, (table, _, function) in _PG_TRIGGERS.items()
)

VIEW_SQL = f"""CREATE VIEW {HISTORY_VIEW} AS
SELECT v.data_type, v.google_data_point_id, v.raw_id, v.version, (1 = 0) AS is_current,
       v.raw_json, v.payload_hash, v.source,
       coalesce(v.received_at, CASE WHEN v.version = 1 THEN r.first_seen_at END) AS valid_from,
       v.superseded_at AS valid_to, v.last_seen_at, v.source_start_time, v.source_end_time,
       NULL AS source_deleted_at, v.id AS history_id, v.response_id
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
       NULL, r.last_seen_at, r.source_start_time, r.source_end_time, r.source_deleted_at, NULL,
       r.response_id
FROM raw_health_data r"""

POSTGRESQL_DDL = (
    *(
        f"CREATE FUNCTION {name}() RETURNS trigger\nLANGUAGE plpgsql AS $${body}$$"
        for name, body in FUNCTION_BODIES.items()
    ),
    *(
        f"CREATE TRIGGER {trigger}\n{definition}\nEXECUTE FUNCTION {function}()"
        for trigger, (_, definition, function) in _PG_TRIGGERS.items()
    ),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}"
        for table, trigger, _ in PROTECTIVE_TRIGGERS
    ),
    VIEW_SQL,
)


def _sqlite_refuse(table: str, operation: str) -> str:
    return f"SELECT RAISE(ABORT, '{table} keeps every record: {operation} refused');"


# trigger -> (table, full SQL) of every protective SQLite trigger (SQLite cannot disable
# triggers, so verify only checks that they exist).
_SQLITE_TRIGGERS = {
    "raw_health_data_keep_version": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_keep_version
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN OLD.payload_hash IS NOT NEW.payload_hash OR OLD.raw_json IS NOT NEW.raw_json
BEGIN
    INSERT INTO raw_health_data_versions ({_COPY})
    VALUES ({_OLD});
END""",
    ),
    "raw_health_data_frozen_identity": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_frozen_identity
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN {_sqlite_changed(_IDENTITY)}
BEGIN
    {_sqlite_refuse("raw_health_data", "UPDATE")}
END""",
    ),
    "raw_health_data_no_delete": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_no_delete
BEFORE DELETE ON raw_health_data
BEGIN
    {_sqlite_refuse("raw_health_data", "DELETE")}
END""",
    ),
    "raw_health_data_deletion_event": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_deletion_event
AFTER UPDATE ON raw_health_data FOR EACH ROW
WHEN OLD.source_deleted_at IS NOT NEW.source_deleted_at
BEGIN
    {_EVENT.format(previous="OLD.source_deleted_at")};
END""",
    ),
    "raw_health_data_restored_event": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_restored_event
AFTER INSERT ON raw_health_data FOR EACH ROW
WHEN NEW.source_deleted_at IS NOT NULL
BEGIN
    {_EVENT.format(previous="NULL")};
END""",
    ),
    "raw_health_data_versions_no_delete": (
        "raw_health_data_versions",
        """CREATE TRIGGER raw_health_data_versions_no_delete
BEFORE DELETE ON raw_health_data_versions
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: DELETE refused');
END""",
    ),
    # SQLite (tests only) has no session settings: renumbering is not gated there.
    "raw_health_data_versions_no_update": (
        "raw_health_data_versions",
        f"""CREATE TRIGGER raw_health_data_versions_no_update
BEFORE UPDATE ON raw_health_data_versions FOR EACH ROW
WHEN {_sqlite_changed(_FROZEN)}
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: UPDATE refused');
END""",
    ),
}
for _table in APPEND_ONLY_TABLES:
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        _SQLITE_TRIGGERS[_name] = (
            _table,
            f"""CREATE TRIGGER {_name}
BEFORE {_operation} ON {_table}
BEGIN
    {_sqlite_refuse(_table, _operation)}
END""",
        )

for _table, (_columns, _, _sqlite_extra) in PAYLOAD_TABLES.items():
    _when = _sqlite_changed(_columns)
    if _sqlite_extra:
        _when = f"{_when} OR ({_sqlite_extra})"
    _SQLITE_TRIGGERS[f"{_table}_frozen_payload"] = (
        _table,
        f"""CREATE TRIGGER {_table}_frozen_payload
BEFORE UPDATE ON {_table} FOR EACH ROW
WHEN {_when}
BEGIN
    {_sqlite_refuse(_table, "UPDATE")}
END""",
    )
    _SQLITE_TRIGGERS[f"{_table}_no_delete"] = (
        _table,
        f"""CREATE TRIGGER {_table}_no_delete
BEFORE DELETE ON {_table}
BEGIN
    {_sqlite_refuse(_table, "DELETE")}
END""",
    )

SQLITE_TRIGGERS = tuple((table, trigger) for trigger, (table, _) in _SQLITE_TRIGGERS.items())
SQLITE_DDL = (*(sql for _, sql in _SQLITE_TRIGGERS.values()), VIEW_SQL)

# Built once every table exists (the functions and views span several tables).
for _statement in POSTGRESQL_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
for _statement in SQLITE_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="sqlite"))
event.listen(Base.metadata, "before_drop", DDL(f"DROP VIEW IF EXISTS {HISTORY_VIEW}"))
