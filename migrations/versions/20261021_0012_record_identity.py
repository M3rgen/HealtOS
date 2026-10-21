"""Stable identities for records without a Google id; superseded duplicates.

Records that Google sends without a `name` were archived under a synthetic id that hashed the
raw interval object and the whole `dataSource`. A device rename, a dropped `.<UUID>` package
suffix, civil times added to `interval` and a growing in-progress interval each made the same
record a new row (see app/parsers/identity.py for the v2 key that replaces it). Existing rows
cannot be re-keyed (`google_data_point_id` is frozen by 0011), so this revision adds seven
append-only tables, a widen-only one and a bookkeeping one:

* `raw_health_data_identity`: fingerprint-specific v2 key -> the raw row that holds the
  record, for rows whose own id does not say so (rows stored before 0012). New writes find a
  record through it (app/services/storage.py); rows stored since get `synthetic:v2s:` ids.
* `raw_health_data_supersessions`: a duplicate row -> a row that holds its record, with a
  reason and both rows' source fingerprints. Superseded rows stay (payload, versions,
  `source_deleted_at`, events); readers and the `analytics_*` views skip them while the link is
  in force (its fingerprints are one, or one device in `raw_source_devices`).
* `raw_source_chains`: per data type, source fingerprint -> the fingerprint the same device has
  after a rename or a dropped package suffix, decided by the reconciler (identity-backfill;
  app/services/source_chains.py) or by the owner (identity-decide). Only fingerprints of one
  chain are ever paired or merged.
* `raw_source_chain_events`: a chain link was refuted (its two forms are two devices).
* `raw_source_devices`: the device each chained fingerprint belongs to, logged on every change
  (the latest row per fingerprint is current).
* `raw_source_pairs`: a key returned in one response under two fingerprints with two
  different readings (evidence of two distinct sources).
* `raw_source_decisions`: the owner's decisions about two fingerprints.
* `raw_source_sightings` (widen-only): first and last sighting of each fingerprint.
* `identity_reconcile_state` (bookkeeping): the reconciler's checkpoint per data type.

It also adds `fetch_rounds` (bookkeeping): when a sync round or a backfill window fetch began
at its first page, kept when the fetch resumes from its page token.

The seven append-only tables refuse `UPDATE`, `DELETE` and `TRUNCATE` (`ENABLE ALWAYS`
triggers calling 0011's `healthos_refuse_change()`); the sightings refuse `DELETE`,
`TRUNCATE` and any `UPDATE` that narrows a span or changes anything but the span and
`complete` (`healthos_sightings_widen_only()`).
`health-mirror verify` checks them, their constraints, that no rule or row-level security
sits on them and that the `analytics_*` views are these. On PostgreSQL the `analytics_*`
views are replaced (`CREATE OR REPLACE`, same columns) so they show what the dashboard
counts: rows of a quarantined or source-deleted raw record, and rows superseded by a live row
through a link in force, are left out (before, only quarantined ones); SQLite (tests only)
keeps the old views.

Fast: empty tables, their indexes and triggers, view definitions, and one partial index on
`raw_health_data` (its source-deleted rows, so the views never scan the whole table), built
`CONCURRENTLY` first, in a step of its own outside the transaction (writers never wait for it;
when it cannot finish within the lock timeout, the upgrade goes on without it and verify
names it); no ALTER of an existing table, so a running pg_dump never blocks it.
Filling them is `health-mirror identity-backfill`, run after the deploy (docs/RUNBOOK.md).
Like 0011 it sets `lock_timeout` (30 s, `-x lock-timeout=...`): the foreign keys to
`raw_health_data` need a SHARE ROW EXCLUSIVE lock on it (writers conflict, readers and
pg_dump do not), and a lock held too long makes the upgrade fail fast, changing nothing,
instead of queueing every writer behind it.

downgrade() refuses while an append-only table or the sightings have rows (dropping them
would lose which duplicates were linked, which sources were chained, what the owner decided
and when each source was seen); pass `-x allow-identity-loss=1` after a backup.

The trigger SQL below is a frozen copy of app/models/identity.py; tests/test_identity_keys.py
compares them.
"""

import re

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "20261021_0012"
down_revision = "20261020_0011"
branch_labels = None
depends_on = None

IDENTITY_TABLE = "raw_health_data_identity"
SUPERSESSION_TABLE = "raw_health_data_supersessions"
CHAIN_TABLE = "raw_source_chains"
CHAIN_EVENT_TABLE = "raw_source_chain_events"
PAIR_TABLE = "raw_source_pairs"
DECISION_TABLE = "raw_source_decisions"
DEVICE_TABLE = "raw_source_devices"
SIGHTING_TABLE = "raw_source_sightings"
RECONCILE_TABLE = "identity_reconcile_state"
FETCH_ROUND_TABLE = "fetch_rounds"
IDENTITY_TABLES = (
    IDENTITY_TABLE,
    SUPERSESSION_TABLE,
    CHAIN_TABLE,
    CHAIN_EVENT_TABLE,
    PAIR_TABLE,
    DECISION_TABLE,
    DEVICE_TABLE,
)
# Source-deleted raw rows, for the analytics views (a partial index; the only object this
# revision adds to an existing table).
DELETED_INDEX = "ix_raw_health_data_source_deleted"
# Tables whose rows downgrade() refuses to drop without -x allow-identity-loss=1.
KEPT_TABLES = (*IDENTITY_TABLES, SIGHTING_TABLE)

SIGHTINGS_FUNCTION = "healthos_sightings_widen_only"
FUNCTION_BODIES = {
    SIGHTINGS_FUNCTION: f"""
BEGIN
    IF NEW.data_type IS DISTINCT FROM OLD.data_type
       OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
       OR NEW.stable_class IS DISTINCT FROM OLD.stable_class
       OR NEW.label IS DISTINCT FROM OLD.label
       OR NEW.data_source IS DISTINCT FROM OLD.data_source
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
        "OR NEW.stable_class IS NOT OLD.stable_class OR NEW.label IS NOT OLD.label "
        "OR NEW.data_source IS NOT OLD.data_source OR NEW.first_seen_at > OLD.first_seen_at "
        "OR NEW.last_seen_at < OLD.last_seen_at OR (OLD.complete AND NOT NEW.complete)",
    ),
)
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

# The analytics views of migration 20260818_0002, and the raw data types each table is
# normalized from (app/parsers/normalize.py; scalar_metrics carries its own data_type).
NORMALIZED_TABLES = {
    "active_zone_minutes": ("active-zone-minutes",),
    "distance": ("distance",),
    "energy_expenditure": ("active-energy-burned", "total-calories"),
    "exercise_sessions": ("exercise",),
    "heart_rate": ("heart-rate",),
    "heart_rate_variability": ("heart-rate-variability", "daily-heart-rate-variability"),
    "oxygen_saturation": ("oxygen-saturation", "daily-oxygen-saturation"),
    "respiratory_rate": ("daily-respiratory-rate", "respiratory-rate-sleep-summary"),
    "resting_heart_rate": ("daily-resting-heart-rate",),
    "scalar_metrics": (),
    "sleep_sessions": ("sleep",),
    "steps": ("steps",),
    "temperature": ("core-body-temperature", "daily-sleep-temperature-derivations"),
    "vo2_max": ("vo2-max", "daily-vo2-max", "run-vo2-max"),
    "weight": ("weight",),
}


def _view(table: str, *, current_only: bool) -> str:
    sql = (
        f"CREATE OR REPLACE VIEW analytics_{table} AS "
        f"SELECT * FROM {table} WHERE is_quarantined = false"
    )
    if not current_only:
        return sql
    # Like the dashboard: a normalized row counts unless its raw record (same key) is marked
    # deleted at the source or superseded, by a link in force, by a row that is not. The
    # lookups are index probes: the raw table's (data_type, google_data_point_id) key, the
    # supersessions' (old_google_data_point_id, data_type) index, and the devices joined now,
    # computed once per query.
    types = NORMALIZED_TABLES[table]
    if types:
        names = ", ".join(f"'{name}'" for name in types)
        raw_type, link_type = f"r.data_type IN ({names})", f"s.data_type IN ({names})"
    else:
        raw_type, link_type = (f"{alias}.data_type = {table}.data_type" for alias in "rs")
    return (
        f"{sql} AND NOT EXISTS (SELECT 1 FROM raw_health_data r "
        f"WHERE {raw_type} AND r.google_data_point_id = {table}.google_data_point_id "
        "AND r.source_deleted_at IS NOT NULL) "
        f"AND NOT EXISTS (SELECT 1 FROM {SUPERSESSION_TABLE} s "
        "JOIN raw_health_data h ON h.id = s.new_raw_id "
        f"WHERE {link_type} AND s.old_google_data_point_id = {table}.google_data_point_id "
        f"AND h.source_deleted_at IS NULL AND h.is_quarantined = false AND {_IN_FORCE})"
    )


_CURRENT = (
    f"(SELECT d.data_type, d.fingerprint, d.device FROM {DEVICE_TABLE} d WHERE NOT EXISTS "
    f"(SELECT 1 FROM {DEVICE_TABLE} e WHERE e.data_type = d.data_type "
    "AND e.fingerprint = d.fingerprint AND e.id > d.id))"
)
# A link is in force while its two fingerprints are one, or one device now (the latest
# raw_source_devices row of each alike).
_IN_FORCE = (
    "(s.old_fingerprint = s.new_fingerprint OR (s.data_type, s.old_fingerprint, "
    "s.new_fingerprint) IN (SELECT a.data_type, a.fingerprint, b.fingerprint FROM "
    f"{_CURRENT} a JOIN {_CURRENT} b ON b.data_type = a.data_type AND b.device = a.device))"
)


LOCK_TIMEOUT = "30s"
_LOCK_NOT_AVAILABLE = "55P03"


def _lock_timeout() -> str:
    value = context.get_x_argument(as_dictionary=True).get("lock-timeout", LOCK_TIMEOUT)
    if not re.fullmatch(r"[1-9][0-9]*(ms|s|min)?", value):
        raise ValueError(f"lock-timeout must look like 30s, 500ms or 2min, not {value!r}")
    return value


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        _upgrade("sqlite")
        op.create_index(
            DELETED_INDEX,
            "raw_health_data",
            ["data_type", "google_data_point_id"],
            sqlite_where=sa.text("source_deleted_at IS NOT NULL"),
        )
        return
    timeout = _lock_timeout()
    # First, outside any transaction: nothing of this revision exists if it fails.
    _deleted_index(timeout)
    op.execute(f"SET LOCAL lock_timeout = '{timeout}'")
    try:
        _upgrade("postgresql")
    except sa.exc.DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if code != _LOCK_NOT_AVAILABLE:
            raise
        raise RuntimeError(
            f"20261021_0012 could not lock its tables within {timeout}: another session holds "
            "them (a long sync, backfill or import transaction writing raw_health_data). "
            "Nothing was changed and the database is still on 20261020_0011; stop the app or "
            "retry when that session has finished (see docs/RUNBOOK.md, 20261021_0012)."
        ) from exc
    op.execute("SET LOCAL lock_timeout TO DEFAULT")


def _upgrade(dialect: str) -> None:
    op.create_table(
        IDENTITY_TABLE,
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("identity_key", sa.String(100), nullable=False),
        sa.Column("raw_id", sa.Uuid(), nullable=False),
        sa.Column("google_data_point_id", sa.String(512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["raw_id"],
            ["raw_health_data.id"],
            name="fk_raw_health_data_identity_raw_id_raw_health_data",
        ),
        sa.PrimaryKeyConstraint("data_type", "identity_key", name="pk_raw_health_data_identity"),
    )
    op.create_table(
        SUPERSESSION_TABLE,
        sa.Column("old_raw_id", sa.Uuid(), nullable=False),
        sa.Column("new_raw_id", sa.Uuid(), nullable=False),
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("old_google_data_point_id", sa.String(512), nullable=False),
        sa.Column("new_google_data_point_id", sa.String(512), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("old_fingerprint", sa.String(64), nullable=False),
        sa.Column("new_fingerprint", sa.String(64), nullable=False),
        sa.Column("same_reading", sa.Boolean(), nullable=False),
        sa.Column("new_payload_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "old_raw_id <> new_raw_id", name=op.f("ck_raw_health_data_supersessions_distinct_rows")
        ),
        sa.ForeignKeyConstraint(
            ["old_raw_id"],
            ["raw_health_data.id"],
            name="fk_raw_health_data_supersessions_old_raw_id_raw_health_data",
        ),
        sa.ForeignKeyConstraint(
            ["new_raw_id"],
            ["raw_health_data.id"],
            name="fk_raw_health_data_supersessions_new_raw_id_raw_health_data",
        ),
        sa.PrimaryKeyConstraint(
            "old_raw_id", "new_raw_id", name="pk_raw_health_data_supersessions"
        ),
    )
    op.create_index(
        "ix_raw_health_data_supersessions_new_raw_id", SUPERSESSION_TABLE, ["new_raw_id"]
    )
    op.create_index(
        "ix_raw_health_data_supersessions_old_key",
        SUPERSESSION_TABLE,
        ["old_google_data_point_id", "data_type"],
    )
    # The links between two sources, found when they are no longer one device.
    op.create_index(
        "ix_raw_health_data_supersessions_sources",
        SUPERSESSION_TABLE,
        ["data_type", "old_fingerprint", "new_fingerprint"],
    )
    op.create_table(
        CHAIN_TABLE,
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("from_fingerprint", sa.String(64), nullable=False),
        sa.Column("to_fingerprint", sa.String(64), nullable=False),
        sa.Column("from_label", sa.String(255), nullable=False),
        sa.Column("to_label", sa.String(255), nullable=False),
        sa.Column("decided_by", sa.String(16), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_fingerprint <> to_fingerprint",
            name=op.f("ck_raw_source_chains_distinct_sources"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_source_chains"),
        sa.UniqueConstraint(
            "data_type", "from_fingerprint", "to_fingerprint", name="uq_raw_source_chains_link"
        ),
    )
    raw_json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        SIGHTING_TABLE,
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("stable_class", sa.String(64), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("data_source", raw_json, nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("data_type", "fingerprint", name="pk_raw_source_sightings"),
    )
    op.create_table(
        CHAIN_EVENT_TABLE,
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("from_fingerprint", sa.String(64), nullable=False),
        sa.Column("to_fingerprint", sa.String(64), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "data_type",
            "from_fingerprint",
            "to_fingerprint",
            "event",
            name="pk_raw_source_chain_events",
        ),
    )
    op.create_table(
        DEVICE_TABLE,
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("device", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_source_devices"),
    )
    op.create_index(
        "ix_raw_source_devices_current", DEVICE_TABLE, ["data_type", "fingerprint", "id"]
    )
    op.create_table(
        PAIR_TABLE,
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("first_fingerprint", sa.String(64), nullable=False),
        sa.Column("second_fingerprint", sa.String(64), nullable=False),
        sa.Column("identity_key", sa.String(100), nullable=False),
        sa.Column("seen_together_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "first_fingerprint < second_fingerprint", name=op.f("ck_raw_source_pairs_ordered")
        ),
        sa.PrimaryKeyConstraint(
            "data_type",
            "first_fingerprint",
            "second_fingerprint",
            "identity_key",
            name="pk_raw_source_pairs",
        ),
    )
    op.create_table(
        DECISION_TABLE,
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("first_fingerprint", sa.String(64), nullable=False),
        sa.Column("second_fingerprint", sa.String(64), nullable=False),
        sa.Column("first_label", sa.String(255), nullable=False),
        sa.Column("second_label", sa.String(255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "first_fingerprint <> second_fingerprint",
            name=op.f("ck_raw_source_decisions_distinct_sources"),
        ),
        sa.CheckConstraint(
            "decision IN ('link', 'reject', 'confirm', 'distinct', 'not-distinct')",
            name=op.f("ck_raw_source_decisions_known_decision"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_source_decisions"),
    )
    op.create_table(
        RECONCILE_TABLE,
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("checkpoint_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("full_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_mode", sa.String(16), nullable=True),
        sa.Column("last_result", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("pair_agreement", sa.Text(), nullable=True),
        sa.Column("deferred_deletions", sa.Text(), nullable=True),
        sa.Column("runs", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("data_type", name="pk_identity_reconcile_state"),
    )
    # Not columns of sync_state / backfill_jobs: ALTER TABLE would need an exclusive lock that
    # a running pg_dump blocks.
    op.create_table(
        FETCH_ROUND_TABLE,
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("kind", "key", name="pk_fetch_rounds"),
    )
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)
    if dialect == "postgresql":
        for table in NORMALIZED_TABLES:
            op.execute(_view(table, current_only=True))


def _deleted_index(timeout: str) -> None:
    """The partial index of source-deleted rows, built ``CONCURRENTLY`` (writers are never
    blocked, however large the table) in its own non-transactional step. It waits for
    transactions that write the table, or hold an older snapshot (a running ``pg_dump``), at
    most ``timeout``: then it is left for later (``health-mirror verify`` names it and the
    statement that builds it), since it only makes the ``analytics_*`` views faster. An index
    left invalid by an interrupted build is built again (``REINDEX ... CONCURRENTLY``)."""
    build = (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {DELETED_INDEX} ON raw_health_data "
        "(data_type, google_data_point_id) WHERE source_deleted_at IS NOT NULL"
    )
    with op.get_context().autocommit_block():
        if context.is_offline_mode():
            op.execute(build)
            return
        bind = op.get_bind()
        bind.execute(sa.text(f"SET lock_timeout = '{timeout}'"))
        try:
            bind.execute(sa.text(build))
            valid = bind.scalar(
                sa.text(
                    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.relname = :name"
                ),
                {"name": DELETED_INDEX},
            )
            if valid is False:
                bind.execute(sa.text(f"REINDEX INDEX CONCURRENTLY {DELETED_INDEX}"))
        except sa.exc.DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if code != _LOCK_NOT_AVAILABLE:
                raise
            print(
                f"20261021_0012: {DELETED_INDEX} could not be built within {timeout} (a writer "
                "or a dump was running); the upgrade goes on without it. Build it later with "
                f"REINDEX INDEX CONCURRENTLY {DELETED_INDEX} (docs/RUNBOOK.md)."
            )
        finally:
            bind.execute(sa.text("RESET lock_timeout"))


def _refuse_while_kept(context_) -> None:
    if context.get_x_argument(as_dictionary=True).get("allow-identity-loss") == "1":
        return
    hint = "Take a backup, then rerun with -x allow-identity-loss=1"
    if context_.as_sql:
        if context_.dialect.name != "postgresql":
            raise RuntimeError(f"offline downgrade cannot check {', '.join(KEPT_TABLES)}; {hint}")
        for table in KEPT_TABLES:
            op.execute(
                f"""DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM {table}) THEN
        RAISE EXCEPTION '{table} holds rows that downgrading would drop; {hint}';
    END IF;
END
$$"""
            )
        return
    for table in KEPT_TABLES:
        kept = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}"))
        if kept:
            raise RuntimeError(f"{table} holds {kept} row(s); downgrading would drop them. {hint}")


def downgrade() -> None:
    context_ = op.get_context()
    _refuse_while_kept(context_)
    if context_.dialect.name == "postgresql":
        # The views depend on the supersession table: restore them first.
        for table in NORMALIZED_TABLES:
            op.execute(_view(table, current_only=False))
    # Dropping a table drops its triggers; healthos_refuse_change() belongs to 0011.
    op.drop_table(FETCH_ROUND_TABLE)
    op.drop_table(RECONCILE_TABLE)
    op.drop_table(DEVICE_TABLE)
    op.drop_table(DECISION_TABLE)
    op.drop_table(PAIR_TABLE)
    op.drop_table(CHAIN_EVENT_TABLE)
    op.drop_table(SIGHTING_TABLE)
    op.drop_table(CHAIN_TABLE)
    op.drop_table(SUPERSESSION_TABLE)
    op.drop_table(IDENTITY_TABLE)
    op.execute(f"DROP INDEX IF EXISTS {DELETED_INDEX}")
    if context_.dialect.name == "postgresql":
        op.execute(f"DROP FUNCTION {SIGHTINGS_FUNCTION}()")
