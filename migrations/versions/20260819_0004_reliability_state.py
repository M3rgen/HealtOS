"""Add persisted reliability state and freshness indexes.

Revision ID: 20260819_0004
Revises: 20260818_0003
"""

import sqlalchemy as sa
from alembic import op

revision = "20260819_0004"
down_revision = "20260818_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reliability_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_reconciliation_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_reconciliation_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_reconciliation_failure_at", sa.DateTime(timezone=True)),
        sa.Column("last_recovery_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_recovery_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_recovery_failure_at", sa.DateTime(timezone=True)),
        sa.Column("last_recovery_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_raw_health_data_updated_at", "raw_health_data", ["updated_at"])
    op.execute("INSERT INTO reliability_state (id) VALUES (1)")


def downgrade() -> None:
    op.drop_index("ix_raw_health_data_updated_at", table_name="raw_health_data")
    op.drop_table("reliability_state")
