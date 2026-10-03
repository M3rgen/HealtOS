"""Preserve and quarantine pre-fix non-minute rollups.

Revision ID: 20260818_0002
Revises: 20260818_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20260818_0002"
down_revision = "20260818_0001"
branch_labels = None
depends_on = None

REASON = "pre_fix_rollup_non_minute_boundary_v1"
NORMALIZED_TABLES: Sequence[str] = (
    "active_zone_minutes",
    "distance",
    "energy_expenditure",
    "exercise_sessions",
    "heart_rate",
    "heart_rate_variability",
    "oxygen_saturation",
    "respiratory_rate",
    "resting_heart_rate",
    "scalar_metrics",
    "sleep_sessions",
    "steps",
    "temperature",
    "vo2_max",
    "weight",
)


def _add_quarantine_columns(table: str) -> None:
    op.add_column(
        table,
        sa.Column(
            "is_quarantined",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(table, sa.Column("quarantine_reason", sa.String(255), nullable=True))


def upgrade() -> None:
    _add_quarantine_columns("raw_health_data")
    for table in NORMALIZED_TABLES:
        _add_quarantine_columns(table)

    # The affected rows came from the old 60-second rollup implementation, whose
    # request start retained seconds. Genuine fixed rollups are UTC-minute aligned.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE raw_health_data
                SET is_quarantined = true, quarantine_reason = :reason
                WHERE data_type IN (
                    'calories-in-heart-rate-zone', 'total-calories'
                )
                  AND (
                    extract(second from source_start_time) <> 0
                    OR extract(microseconds from source_start_time) <> 0
                  )
                """
            ).bindparams(reason=REASON)
        )
        op.execute(
            sa.text(
                """
                UPDATE energy_expenditure AS normalized
                SET is_quarantined = true, quarantine_reason = :reason
                FROM raw_health_data AS raw
                WHERE raw.data_type = 'total-calories'
                  AND raw.google_data_point_id = normalized.google_data_point_id
                  AND raw.is_quarantined = true
                  AND raw.quarantine_reason = :reason
                """
            ).bindparams(reason=REASON)
        )
        op.execute(
            sa.text(
                """
                UPDATE scalar_metrics AS normalized
                SET is_quarantined = true, quarantine_reason = :reason
                FROM raw_health_data AS raw
                WHERE raw.data_type = 'calories-in-heart-rate-zone'
                  AND raw.data_type = normalized.data_type
                  AND raw.google_data_point_id = normalized.google_data_point_id
                  AND raw.is_quarantined = true
                  AND raw.quarantine_reason = :reason
                """
            ).bindparams(reason=REASON)
        )

    for table in NORMALIZED_TABLES:
        op.execute(
            f"CREATE VIEW analytics_{table} AS SELECT * FROM {table} WHERE is_quarantined = false"
        )


def downgrade() -> None:
    for table in reversed(NORMALIZED_TABLES):
        op.execute(f"DROP VIEW analytics_{table}")
    for table in reversed(NORMALIZED_TABLES):
        op.drop_column(table, "quarantine_reason")
        op.drop_column(table, "is_quarantined")
    op.drop_column("raw_health_data", "quarantine_reason")
    op.drop_column("raw_health_data", "is_quarantined")
