"""Append-only retrieval evidence; no historical rows are inferred or rewritten.

An attempt describes the caller's request and the actual filter coordinates. Page and
outcome events are appended; a continuation points to its exact durable predecessor page.
Downgrading refuses to lose evidence. Trigger SQL is frozen from the model module.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261025_0015"
down_revision = "20261024_0014"
branch_labels = None
depends_on = None

RECEIPT_TABLES = ("retrieval_receipts", "retrieval_events")
CHECKPOINT_TABLE = "retrieval_checkpoints"

PG_TRIGGER_SQL = {}
PROTECTIVE_TRIGGERS = []
SQLITE_TRIGGER_SQL = {}
for _table in RECEIPT_TABLES:
    for _suffix, _events in (("append_only", "UPDATE OR DELETE"), ("no_truncate", "TRUNCATE")):
        _name = f"{_table}_{_suffix}"
        _level = "STATEMENT" if _suffix == "no_truncate" else "ROW"
        PG_TRIGGER_SQL[_name] = (
            _table,
            f"CREATE TRIGGER {_name}\nBEFORE {_events} ON {_table} FOR EACH {_level}\n"
            "EXECUTE FUNCTION healthos_refuse_change()",
        )
        PROTECTIVE_TRIGGERS.append((_table, _name, "healthos_refuse_change"))
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        SQLITE_TRIGGER_SQL[_name] = (
            _table,
            f"CREATE TRIGGER {_name}\nBEFORE {_operation} ON {_table}\nBEGIN\n"
            f"    SELECT RAISE(ABORT, '{_table} keeps every record: {_operation} refused');\nEND",
        )
FUNCTION_BODIES: dict[str, str] = {}
POSTGRESQL_DDL = (
    *(sql for _, sql in PG_TRIGGER_SQL.values()),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}"
        for table, name, _ in PROTECTIVE_TRIGGERS
    ),
)
SQLITE_DDL = tuple(sql for _, sql in SQLITE_TRIGGER_SQL.values())


def upgrade() -> None:
    op.create_table(
        "retrieval_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_type", sa.String(100), nullable=False),
        sa.Column("lane", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("requested_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queried_from", sa.DateTime(timezone=True)),
        sa.Column("queried_to", sa.DateTime(timezone=True)),
        sa.Column("query_filter", sa.String(1000)),
        sa.Column("basis", sa.String(32), nullable=False),
        sa.Column("access_method", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("starts_first", sa.Boolean(), nullable=False),
        sa.Column("resume_token_hash", sa.String(64)),
        sa.Column("parent_event_id", sa.BigInteger()),
        sa.PrimaryKeyConstraint("id", name="pk_retrieval_receipts"),
    )
    op.create_index(
        "ix_retrieval_receipts_window",
        "retrieval_receipts",
        ["data_type", "queried_from", "queried_to"],
    )
    op.create_index(
        "ix_retrieval_receipts_owner", "retrieval_receipts", ["lane", "owner", "started_at"]
    )
    op.create_table(
        "retrieval_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_token_hash", sa.String(64)),
        sa.Column("next_token_hash", sa.String(64)),
        sa.Column("last_page", sa.Boolean(), nullable=False),
        sa.Column("returned_count", sa.Integer(), nullable=False),
        sa.Column("returned_times", sa.JSON(), nullable=False),
        sa.Column("first_record_at", sa.DateTime(timezone=True)),
        sa.Column("last_record_at", sa.DateTime(timezone=True)),
        sa.Column("unplaced_count", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(100)),
        sa.CheckConstraint(
            "kind IN ('page', 'completed', 'failed', 'cancelled', 'reset')",
            name=op.f("ck_retrieval_events_kind"),
        ),
        sa.CheckConstraint(
            "returned_count >= 0 AND unplaced_count >= 0", name=op.f("ck_retrieval_events_counts")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_retrieval_events"),
    )
    op.create_index("ix_retrieval_events_receipt", "retrieval_events", ["receipt_id", "id"])
    op.create_table(
        CHECKPOINT_TABLE,
        sa.Column("lane", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("next_token_hash", sa.String(64)),
        sa.PrimaryKeyConstraint("lane", "owner", name="pk_retrieval_checkpoints"),
    )
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("retrieval evidence must be checked before an online downgrade")
    for table in RECEIPT_TABLES:
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError(f"{table} holds retrieval evidence; downgrade refused")
    op.drop_table(CHECKPOINT_TABLE)
    for table in reversed(RECEIPT_TABLES):
        op.drop_table(table)
