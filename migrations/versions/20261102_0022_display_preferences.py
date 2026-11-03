"""Add the single-row `display_preferences` table shared by every device.

It holds the display preferences (time zone choice, body model rotation) that were kept
only in each browser's localStorage. `schema_version` records the shape of the JSON in
`preferences`, so a later shape can upgrade an older row instead of misreading it.
Creating a table is non-destructive; no existing row is read or changed.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261102_0022"
down_revision = "20261031_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "display_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("preferences", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_display_preferences_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_display_preferences")),
    )


def downgrade() -> None:
    op.drop_table("display_preferences")
