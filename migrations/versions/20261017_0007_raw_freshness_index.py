"""Index raw_health_data by stream and effective data time for cheap freshness probes.

The /health freshness summary asks, per frequent stream, for the latest
coalesce(source_end_time, source_start_time). With this index each probe is a single
backward index scan (ORDER BY ... DESC LIMIT 1) instead of a full-table GROUP BY.

On PostgreSQL the index is built CONCURRENTLY so webhook ingestion is not blocked; it
also drops the ad-hoc CASE-expression index created by hand on 2026-10-17 (VM 130).
"""

import sqlalchemy as sa
from alembic import op

revision = "20261017_0007"
down_revision = "20261001_0006"
branch_labels = None
depends_on = None

INDEX = "ix_raw_health_data_type_freshness"
AD_HOC_INDEX = "ix_raw_health_data_type_effective_time"


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON raw_health_data "
                "(data_type, coalesce(source_end_time, source_start_time))"
            )
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {AD_HOC_INDEX}")
        return
    op.create_index(
        INDEX,
        "raw_health_data",
        ["data_type", sa.text("coalesce(source_end_time, source_start_time)")],
    )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        return
    op.drop_index(INDEX, table_name="raw_health_data")
