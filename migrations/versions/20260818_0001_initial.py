"""Initial raw-first health mirror schema.

Revision ID: 20260818_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20260818_0001"
down_revision = None
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


def _source_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("google_data_point_id", sa.String(512), nullable=False),
        sa.Column("device", sa.String(255), nullable=True),
        sa.Column("source_timezone", sa.String(128), nullable=True),
    ]


def _create_table(name: str, columns: Sequence[sa.Column], *constraints: sa.Constraint) -> None:
    op.create_table(name, *columns, *_timestamps(), *constraints)


def _create_source_table(
    name: str, columns: Sequence[sa.Column], *constraints: sa.Constraint
) -> None:
    _create_table(
        name,
        [*_source_columns(), *columns],
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("google_data_point_id"),
        *constraints,
    )


def _create_sample_metric(
    table: str,
    value_name: str,
    value_type: sa.types.TypeEngine,
    extra_columns: Sequence[sa.Column] = (),
) -> None:
    _create_source_table(
        table,
        [
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column(value_name, value_type, nullable=False),
            *extra_columns,
        ],
    )
    op.create_index(f"ix_{table}_timestamp", table, ["timestamp"])


def _create_interval_metric(
    table: str,
    value_name: str,
    value_type: sa.types.TypeEngine,
    extra_columns: Sequence[sa.Column] = (),
    *,
    range_index: bool = False,
) -> None:
    _create_source_table(
        table,
        [
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column(value_name, value_type, nullable=False),
            *extra_columns,
        ],
    )
    if range_index:
        op.create_index(f"ix_{table}_range", table, ["start_time", "end_time"])
    else:
        op.create_index(f"ix_{table}_start_time", table, ["start_time"])


def upgrade() -> None:
    job_status = sa.Enum("pending", "running", "completed", "failed", name="jobstatus")

    _create_interval_metric(
        "active_zone_minutes",
        "minutes",
        sa.Numeric(12, 3),
        [sa.Column("heart_rate_zone", sa.String(64), nullable=True)],
    )

    _create_table(
        "backfill_jobs",
        [
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("data_type", sa.String(128), nullable=False),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", job_status, nullable=False),
            sa.Column("page_token", sa.Text(), nullable=True),
            sa.Column("records_downloaded", sa.BigInteger(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("retry_count", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
        ],
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("data_type", "window_start", "window_end"),
    )
    op.create_index("ix_backfill_jobs_data_type", "backfill_jobs", ["data_type"])
    op.create_index("ix_backfill_jobs_status", "backfill_jobs", ["status"])

    _create_interval_metric("distance", "meters", sa.Numeric(18, 5))
    _create_interval_metric("energy_expenditure", "kcal", sa.Numeric(18, 5))

    _create_source_table(
        "exercise_sessions",
        [
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("exercise_type", sa.String(128), nullable=True),
            sa.Column("duration_seconds", sa.Numeric(14, 3), nullable=True),
            sa.Column("calories_kcal", sa.Numeric(14, 4), nullable=True),
            sa.Column("distance_meters", sa.Numeric(16, 4), nullable=True),
        ],
    )
    op.create_index("ix_exercise_sessions_start_time", "exercise_sessions", ["start_time"])

    _create_sample_metric(
        "heart_rate",
        "bpm",
        sa.Numeric(10, 3),
        [sa.Column("motion_context", sa.String(128), nullable=True)],
    )
    _create_source_table(
        "heart_rate_variability",
        [
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("rmssd", sa.Numeric(12, 4), nullable=True),
            sa.Column("metrics", sa.JSON(), nullable=False),
        ],
    )
    op.create_index("ix_heart_rate_variability_timestamp", "heart_rate_variability", ["timestamp"])

    _create_table(
        "oauth_tokens",
        [
            sa.Column("subject", sa.String(128), nullable=False),
            sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
            sa.Column("scopes", sa.Text(), nullable=True),
            sa.Column("health_user_id", sa.String(128), nullable=True),
            sa.Column("legacy_user_id", sa.String(128), nullable=True),
        ],
        sa.PrimaryKeyConstraint("subject"),
        sa.UniqueConstraint("health_user_id"),
    )

    _create_sample_metric("oxygen_saturation", "spo2", sa.Numeric(8, 4))

    _create_table(
        "raw_health_data",
        [
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("google_data_point_id", sa.String(512), nullable=False),
            sa.Column("data_type", sa.String(128), nullable=False),
            sa.Column("source_start_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_end_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_timezone", sa.String(128), nullable=True),
            sa.Column("raw_json", sa.JSON(), nullable=False),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_deleted_at", sa.DateTime(timezone=True), nullable=True),
        ],
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("data_type", "google_data_point_id"),
    )
    op.create_index(
        "ix_raw_health_data_type_start", "raw_health_data", ["data_type", "source_start_time"]
    )

    _create_sample_metric(
        "respiratory_rate",
        "breaths_per_minute",
        sa.Numeric(10, 4),
        [sa.Column("metric_type", sa.String(128), nullable=False)],
    )
    _create_sample_metric(
        "resting_heart_rate",
        "bpm",
        sa.Numeric(10, 3),
        [sa.Column("calculation_method", sa.String(128), nullable=True)],
    )

    _create_table(
        "scalar_metrics",
        [
            *_source_columns(),
            sa.Column("data_type", sa.String(128), nullable=False),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("value", sa.Numeric(24, 8), nullable=True),
            sa.Column("unit", sa.String(64), nullable=True),
            sa.Column("attributes", sa.JSON(), nullable=False),
        ],
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("data_type", "google_data_point_id"),
    )
    op.create_index("ix_scalar_metric_type_time", "scalar_metrics", ["data_type", "start_time"])

    _create_source_table(
        "sleep_sessions",
        [
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("duration_seconds", sa.Numeric(14, 3), nullable=True),
            sa.Column("deep_sleep_seconds", sa.Numeric(14, 3), nullable=True),
            sa.Column("light_sleep_seconds", sa.Numeric(14, 3), nullable=True),
            sa.Column("rem_sleep_seconds", sa.Numeric(14, 3), nullable=True),
            sa.Column("awake_seconds", sa.Numeric(14, 3), nullable=True),
            sa.Column("efficiency", sa.Numeric(8, 3), nullable=True),
        ],
    )
    op.create_index("ix_sleep_sessions_range", "sleep_sessions", ["start_time", "end_time"])
    _create_interval_metric("steps", "count", sa.Numeric(20, 3), range_index=True)

    _create_table(
        "sync_state",
        [
            sa.Column("data_type", sa.String(128), nullable=False),
            sa.Column("last_requested_from", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_requested_to", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_successful_sync", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_page_token", sa.Text(), nullable=True),
            sa.Column("status", job_status, nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("records_downloaded", sa.BigInteger(), nullable=False),
        ],
        sa.PrimaryKeyConstraint("data_type"),
    )

    _create_sample_metric(
        "temperature",
        "celsius",
        sa.Numeric(10, 4),
        [
            sa.Column("metric_type", sa.String(128), nullable=False),
            sa.Column("baseline_celsius", sa.Numeric(10, 4), nullable=True),
        ],
    )
    _create_source_table(
        "vo2_max",
        [
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("metric_type", sa.String(64), nullable=False),
            sa.Column("ml_per_kg_min", sa.Numeric(12, 4), nullable=False),
            sa.Column("fitness_level", sa.String(64), nullable=True),
        ],
    )
    op.create_index("ix_vo2_max_timestamp", "vo2_max", ["timestamp"])

    _create_table(
        "webhook_events",
        [
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("data_type", sa.String(128), nullable=True),
            sa.Column("health_user_id", sa.String(128), nullable=True),
            sa.Column("operation", sa.String(32), nullable=True),
            sa.Column("raw_json", sa.JSON(), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        ],
        sa.PrimaryKeyConstraint("payload_hash"),
    )
    op.create_index("ix_webhook_events_data_type", "webhook_events", ["data_type"])
    _create_sample_metric("weight", "kilograms", sa.Numeric(12, 5))


def downgrade() -> None:
    for table in (
        "weight",
        "webhook_events",
        "vo2_max",
        "temperature",
        "sync_state",
        "steps",
        "sleep_sessions",
        "scalar_metrics",
        "resting_heart_rate",
        "respiratory_rate",
        "raw_health_data",
        "oxygen_saturation",
        "oauth_tokens",
        "heart_rate_variability",
        "heart_rate",
        "exercise_sessions",
        "energy_expenditure",
        "distance",
        "backfill_jobs",
        "active_zone_minutes",
    ):
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        sa.Enum(name="jobstatus").drop(op.get_bind(), checkfirst=True)
