"""Durable reconciliation runs and committed differences; no historical inference."""

import sqlalchemy as sa
from alembic import op

revision = "20261025_0016"
down_revision = "20261025_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("requested_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_type", sa.String(100)),
        sa.CheckConstraint(
            "requested_from < requested_to", name=op.f("ck_reconciliation_runs_range")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed', 'cancelled')",
            name=op.f("ck_reconciliation_runs_status"),
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status <> 'running' AND finished_at IS NOT NULL)",
            name=op.f("ck_reconciliation_runs_finished"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_runs")),
    )
    op.create_index("ix_reconciliation_runs_started", "reconciliation_runs", ["started_at", "id"])
    op.create_table(
        "reconciliation_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("data_type", sa.String(100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        *(
            sa.Column(name, sa.Integer(), nullable=False)
            for name in ("inserted", "updated", "unchanged", "source_deleted", "pages_committed")
        ),
        sa.Column("error_type", sa.String(100)),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed', 'cancelled')",
            name=op.f("ck_reconciliation_results_status"),
        ),
        sa.CheckConstraint(
            "inserted >= 0 AND updated >= 0 AND unchanged >= 0 AND source_deleted >= 0 "
            "AND pages_committed >= 0",
            name=op.f("ck_reconciliation_results_counts"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["reconciliation_runs.id"],
            name=op.f("fk_reconciliation_results_run_id_reconciliation_runs"),
        ),
        sa.PrimaryKeyConstraint("run_id", "data_type", name=op.f("pk_reconciliation_results")),
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("reconciliation reports must be checked before an online downgrade")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM reconciliation_runs")):
        raise RuntimeError("reconciliation_runs holds reports; downgrade refused")
    op.drop_table("reconciliation_results")
    op.drop_table("reconciliation_runs")
