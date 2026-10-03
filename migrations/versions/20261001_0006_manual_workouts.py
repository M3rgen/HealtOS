"""Store manual workouts separately from Fitbit records."""

import sqlalchemy as sa
from alembic import op

revision = "20261001_0006"
down_revision = "20260930_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manual_workouts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exercise_type", sa.String(128), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calories_kcal", sa.Numeric(14, 4)),
        sa.Column("distance_meters", sa.Numeric(16, 4)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_manual_workouts_start_time", "manual_workouts", ["start_time"])


def downgrade() -> None:
    op.drop_index("ix_manual_workouts_start_time", table_name="manual_workouts")
    op.drop_table("manual_workouts")
