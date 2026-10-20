"""Add the single-row `appearance_settings` table for appearance choices shared across devices.

Only preset ids (and the `upload` marker) are stored; uploaded images stay in each browser.
Creating a table is non-destructive.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261020_0009"
down_revision = "20261020_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "appearance_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_appearance_settings_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appearance_settings")),
    )


def downgrade() -> None:
    op.drop_table("appearance_settings")
