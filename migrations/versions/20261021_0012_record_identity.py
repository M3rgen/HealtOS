"""Stable identities for records without a Google id; superseded duplicates.

Records that Google sends without a `name` were archived under a synthetic id that hashed the
raw interval object and the whole `dataSource`. A device rename, a dropped `.<UUID>` package
suffix, civil times added to `interval` and a growing in-progress interval each made the same
record a new row (see app/parsers/identity.py for the v2 key that replaces it). Existing rows
cannot be re-keyed (`google_data_point_id` is frozen by 0011), so this revision adds three
append-only tables and one bookkeeping table:

* `raw_health_data_identity`: v2 identity key -> the raw row that holds the record. New
  writes resolve a record through it (app/services/storage.py).
* `raw_health_data_supersessions`: a duplicate row -> the row that holds its record now, with
  a reason. Superseded rows stay (payload, versions, `source_deleted_at`, events); readers and
  the `analytics_*` views skip them.
* `raw_source_chains`: per data type, source fingerprint -> the fingerprint the same device has
  after a rename or a dropped package suffix (decided from sighting timelines; see
  app/services/source_chains.py). Only fingerprints of one chain are ever paired or merged.
* `raw_source_sightings` (not guarded, derived): first and last sighting of each fingerprint.

The three append-only tables refuse `UPDATE`, `DELETE` and `TRUNCATE` (`ENABLE ALWAYS`
triggers calling 0011's `healthos_refuse_change()`); `health-mirror verify` checks them, their
constraints and that no rule sits on them. On PostgreSQL the `analytics_*` views are replaced
(`CREATE OR REPLACE`, same columns) so they show what the dashboard counts: rows of a
quarantined, source-deleted or superseded raw record are left out (before, only quarantined
ones); SQLite (tests only) keeps the old views.

Catalog-only and fast: two empty tables, their indexes and triggers, and view definitions.
Filling them is `health-mirror identity-backfill`, run after the deploy (docs/RUNBOOK.md).
Like 0011 it sets `lock_timeout` (30 s, `-x lock-timeout=...`): the foreign keys to
`raw_health_data` need a SHARE ROW EXCLUSIVE lock on it (writers conflict, readers and
pg_dump do not), and a lock held too long makes the upgrade fail fast, changing nothing,
instead of queueing every writer behind it.

downgrade() refuses while an append-only table has rows (dropping them would lose which
duplicates were linked and which sources were chained); pass `-x allow-identity-loss=1` after
a backup.

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
SIGHTING_TABLE = "raw_source_sightings"
IDENTITY_TABLES = (IDENTITY_TABLE, SUPERSESSION_TABLE, CHAIN_TABLE)

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
POSTGRESQL_DDL = (
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
    # deleted at the source or superseded. The data type keeps the lookup on the raw table's
    # (data_type, google_data_point_id) key.
    types = NORMALIZED_TABLES[table]
    same_type = (
        f"r.data_type = {table}.data_type"
        if not types
        else "r.data_type IN (" + ", ".join(f"'{name}'" for name in types) + ")"
    )
    return (
        f"{sql} AND NOT EXISTS (SELECT 1 FROM raw_health_data r "
        f"WHERE {same_type} AND r.google_data_point_id = {table}.google_data_point_id "
        f"AND (r.source_deleted_at IS NOT NULL OR EXISTS (SELECT 1 FROM {SUPERSESSION_TABLE} s "
        "WHERE s.old_raw_id = r.id)))"
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
        return
    timeout = _lock_timeout()
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
        sa.PrimaryKeyConstraint("old_raw_id", name="pk_raw_health_data_supersessions"),
    )
    op.create_index(
        "ix_raw_health_data_supersessions_new_raw_id", SUPERSESSION_TABLE, ["new_raw_id"]
    )
    op.create_index(
        "ix_raw_health_data_supersessions_old_key",
        SUPERSESSION_TABLE,
        ["old_google_data_point_id", "data_type"],
    )
    op.create_table(
        CHAIN_TABLE,
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
        sa.PrimaryKeyConstraint("data_type", "from_fingerprint", name="pk_raw_source_chains"),
        sa.UniqueConstraint("data_type", "to_fingerprint", name="uq_raw_source_chains_data_type"),
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
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)
    if dialect == "postgresql":
        for table in NORMALIZED_TABLES:
            op.execute(_view(table, current_only=True))


def _refuse_while_kept(context_) -> None:
    if context.get_x_argument(as_dictionary=True).get("allow-identity-loss") == "1":
        return
    hint = "Take a backup, then rerun with -x allow-identity-loss=1"
    if context_.as_sql:
        if context_.dialect.name != "postgresql":
            raise RuntimeError(
                f"offline downgrade cannot check {', '.join(IDENTITY_TABLES)}; {hint}"
            )
        for table in IDENTITY_TABLES:
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
    for table in IDENTITY_TABLES:
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
    op.drop_table(SIGHTING_TABLE)
    op.drop_table(CHAIN_TABLE)
    op.drop_table(SUPERSESSION_TABLE)
    op.drop_table(IDENTITY_TABLE)
