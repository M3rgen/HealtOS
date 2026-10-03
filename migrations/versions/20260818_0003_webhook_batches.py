"""Add durable per-notification webhook batch processing.

Revision ID: 20260818_0003
Revises: 20260818_0002
"""

import sqlalchemy as sa
from alembic import op

revision = "20260818_0003"
down_revision = "20260818_0002"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("raw_body", sa.LargeBinary(), nullable=True))
    op.add_column(
        "webhook_events",
        sa.Column("signature_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "webhook_events",
        sa.Column("element_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "webhook_events",
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("webhook_events", sa.Column("last_error", sa.Text(), nullable=True))
    op.execute(
        "UPDATE webhook_events SET signature_verified_at = created_at "
        "WHERE signature_verified_at IS NULL"
    )

    op.create_table(
        "webhook_notifications",
        sa.Column("element_hash", sa.String(64), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.Column("data_type", sa.String(128), nullable=True),
        sa.Column("health_user_id", sa.String(128), nullable=True),
        sa.Column("operation", sa.String(32), nullable=True),
        sa.Column("intervals", sa.JSON(), nullable=True),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("sync_attempt_count", sa.Integer(), nullable=False),
        sa.Column("sync_success_count", sa.Integer(), nullable=False),
        sa.Column("validation_error", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("element_hash"),
    )
    op.create_index("ix_webhook_notifications_data_type", "webhook_notifications", ["data_type"])
    op.create_index("ix_webhook_notifications_status", "webhook_notifications", ["status"])

    op.create_table(
        "webhook_batch_items",
        sa.Column("batch_hash", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("element_hash", sa.String(64), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["batch_hash"], ["webhook_events.payload_hash"]),
        sa.ForeignKeyConstraint(["element_hash"], ["webhook_notifications.element_hash"]),
        sa.PrimaryKeyConstraint("batch_hash", "ordinal"),
    )
    op.create_index("ix_webhook_batch_items_element_hash", "webhook_batch_items", ["element_hash"])


def downgrade() -> None:
    op.drop_index("ix_webhook_batch_items_element_hash", table_name="webhook_batch_items")
    op.drop_table("webhook_batch_items")
    op.drop_index("ix_webhook_notifications_status", table_name="webhook_notifications")
    op.drop_index("ix_webhook_notifications_data_type", table_name="webhook_notifications")
    op.drop_table("webhook_notifications")
    op.drop_column("webhook_events", "last_error")
    op.drop_column("webhook_events", "failure_count")
    op.drop_column("webhook_events", "element_count")
    op.drop_column("webhook_events", "signature_verified_at")
    op.drop_column("webhook_events", "raw_body")
