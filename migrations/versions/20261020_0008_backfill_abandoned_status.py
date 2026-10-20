"""Add the `abandoned` job status for backfill windows that keep failing.

A window that failed ten times is parked as `abandoned` so it stops blocking the rest
of its data type's history. Adding an enum value is non-destructive; PostgreSQL needs it
outside a transaction block. SQLite stores the enum as VARCHAR without a CHECK
constraint, so nothing changes there.
"""

from alembic import op

revision = "20261020_0008"
down_revision = "20261017_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'abandoned'")


def downgrade() -> None:
    # PostgreSQL cannot drop a single enum value; park the windows as failed instead so
    # the previous code, which does not know `abandoned`, can still load them.
    op.execute("UPDATE backfill_jobs SET status = 'failed' WHERE status = 'abandoned'")
    op.execute("UPDATE sync_state SET status = 'failed' WHERE status = 'abandoned'")
