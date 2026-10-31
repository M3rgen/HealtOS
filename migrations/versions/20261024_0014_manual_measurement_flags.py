"""Flags on manual measurements as append-only events (a test entry, left out of the profile).

`manual_measurements` refuses `UPDATE` (20261020_0011), so a flag cannot be a column of it.
This revision adds `manual_measurement_flag_events`: one row per decision, `mark` or `unmark`
of a flag (`test`) on one measurement, with an optional reason; the latest row per measurement
and flag (`created_at`, then `id`) is its state. The measurement row is never touched.

The table refuses `UPDATE`, `DELETE` and `TRUNCATE` (`ENABLE ALWAYS` triggers calling 0011's
`healthos_refuse_change()`); `health-mirror verify` checks them. It has no foreign key to
`manual_measurements` (whose rows are never deleted; the API checks that a measurement exists),
so the upgrade locks no existing table: an empty table, its index and its triggers.

downgrade() refuses while the table has rows (dropping it would lose which entries were marked
as tests); pass `-x allow-flag-loss=1` after a backup.

The trigger SQL below is a frozen copy of app/models/manual_flags.py; tests/test_manual_flags.py
compares them.
"""

import sqlalchemy as sa
from alembic import context, op

revision = "20261024_0014"
down_revision = "20261024_0013"
branch_labels = None
depends_on = None

FLAG_TABLE = "manual_measurement_flag_events"
FLAG_TABLES = (FLAG_TABLE,)
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_PG_TRIGGERS = {}
for _table in FLAG_TABLES:
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
for _table in FLAG_TABLES:
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


def upgrade() -> None:
    op.create_table(
        FLAG_TABLE,
        sa.Column("id", BIGINT, autoincrement=True, nullable=False),
        sa.Column("measurement_id", sa.Uuid(), nullable=False),
        sa.Column("flag", sa.String(32), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('mark', 'unmark')", name=op.f("ck_manual_measurement_flag_events_action")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_manual_measurement_flag_events"),
    )
    op.create_index(
        "ix_manual_measurement_flag_events_current",
        FLAG_TABLE,
        ["measurement_id", "created_at", "id"],
    )
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def _refuse_while_kept(context_) -> None:
    if context.get_x_argument(as_dictionary=True).get("allow-flag-loss") == "1":
        return
    hint = "Take a backup, then rerun with -x allow-flag-loss=1"
    if context_.as_sql:
        if context_.dialect.name != "postgresql":
            raise RuntimeError(f"offline downgrade cannot check {FLAG_TABLE}; {hint}")
        op.execute(
            f"""DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM {FLAG_TABLE}) THEN
        RAISE EXCEPTION '{FLAG_TABLE} holds rows that downgrading would drop; {hint}';
    END IF;
END
$$"""
        )
        return
    kept = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {FLAG_TABLE}"))
    if kept:
        raise RuntimeError(f"{FLAG_TABLE} holds {kept} row(s); downgrading would drop them. {hint}")


def downgrade() -> None:
    _refuse_while_kept(op.get_context())
    # Dropping the table drops its index and triggers; healthos_refuse_change() belongs to 0011.
    op.drop_table(FLAG_TABLE)
