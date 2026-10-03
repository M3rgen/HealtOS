"""Add separate manual body measurements for HealtOS."""

import sqlalchemy as sa
from alembic import op

revision = "20260930_0005"
down_revision = "20260819_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manual_measurements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("value", sa.Numeric(12, 4), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_manual_measurements_metric_time", "manual_measurements", ["metric", "measured_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_manual_measurements_metric_time", table_name="manual_measurements")
    op.drop_table("manual_measurements")
