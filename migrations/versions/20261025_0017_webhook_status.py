"""Add value-free webhook diagnostics without changing the webhook archive."""

import sqlalchemy as sa
from alembic import op

revision = "20261025_0017"
down_revision = "20261025_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_status_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_status_events")),
    )
    op.create_index(
        "ix_webhook_status_reason_time", "webhook_status_events", ["reason", "occurred_at"]
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("webhook diagnostics must be checked before an online downgrade")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM webhook_status_events")):
        raise RuntimeError("webhook_status_events holds evidence; downgrade refused")
    op.drop_table("webhook_status_events")
