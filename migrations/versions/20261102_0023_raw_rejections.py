"""Keep Google records jsonb cannot hold apart instead of failing their whole data type.

Three new, append-only tables (app/models/raw_rejections.py): ``raw_health_data_rejections``
(the record left out of ``raw_health_data``, linked to its verbatim ``raw_api_responses``
row), ``raw_rejection_acks`` (the maintainer's acknowledgement for /health) and
``retrieval_event_rejections`` (rejected records per retrieval page, apart from
``unplaced_count``).

Only new tables, their indexes and guards: no ALTER of an existing table and no foreign key
onto one, so no lock is taken on anything that exists and a running pg_dump never blocks or
fails it. No existing row is read or changed. Frozen SQL, independent of application modules.

downgrade() refuses while any of the tables holds a row (the rejected records and the
acknowledgements would be lost).
"""

import sqlalchemy as sa
from alembic import op

revision = "20261102_0023"
down_revision = "20261102_0022"
branch_labels = None
depends_on = None

TABLES = ("raw_health_data_rejections", "raw_rejection_acks", "retrieval_event_rejections")
POSTGRESQL_DDL = []
SQLITE_DDL = []
for _table in TABLES:
    POSTGRESQL_DDL += [
        f"CREATE TRIGGER {_table}_append_only\nBEFORE UPDATE OR DELETE ON {_table} "
        "FOR EACH ROW\nEXECUTE FUNCTION healthos_refuse_change()",
        f"CREATE TRIGGER {_table}_no_truncate\nBEFORE TRUNCATE ON {_table} "
        "FOR EACH STATEMENT\nEXECUTE FUNCTION healthos_refuse_change()",
    ]
    SQLITE_DDL += [
        f"CREATE TRIGGER {_table}_no_{operation.lower()}\nBEFORE {operation} ON {_table}\n"
        f"BEGIN\n    SELECT RAISE(ABORT, '{_table} keeps every record: {operation} refused');"
        "\nEND"
        for operation in ("UPDATE", "DELETE")
    ]
POSTGRESQL_DDL += [
    f"ALTER TABLE {_table} ENABLE ALWAYS TRIGGER {_table}_{suffix}"
    for _table in TABLES
    for suffix in ("append_only", "no_truncate")
]


def _id() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "raw_health_data_rejections",
        sa.Column("id", _id(), autoincrement=True, nullable=False),
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("context_id", sa.String(160), nullable=True),
        sa.Column("response_id", _id(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("element_sha256", sa.String(64), nullable=False),
        sa.Column("element_json", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_raw_health_data_rejections_ordinal")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_health_data_rejections")),
        sa.UniqueConstraint(
            "response_id", "ordinal", name="uq_raw_health_data_rejections_response"
        ),
    )
    op.create_index(
        "uq_raw_health_data_rejections_unanchored",
        "raw_health_data_rejections",
        ["data_type", "source", "ordinal", "element_sha256"],
        unique=True,
        sqlite_where=sa.text("response_id IS NULL"),
        postgresql_where=sa.text("response_id IS NULL"),
    )
    op.create_index(
        "ix_raw_health_data_rejections_element",
        "raw_health_data_rejections",
        ["data_type", "element_sha256"],
    )
    op.create_table(
        "raw_rejection_acks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("through_id", _id(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("through_id >= 1", name=op.f("ck_raw_rejection_acks_through_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_rejection_acks")),
    )
    op.create_table(
        "retrieval_event_rejections",
        sa.Column("event_id", _id(), autoincrement=False, nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "rejected_count > 0", name=op.f("ck_retrieval_event_rejections_rejected_count")
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_retrieval_event_rejections")),
    )
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("rejected Google records must be checked before an online downgrade")
    for table in TABLES:
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError(f"{table} holds evidence; downgrade refused")
    for table in reversed(TABLES):
        op.drop_table(table)
