from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Index, Numeric, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SourceMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    device: Mapped[str | None] = mapped_column(String(255))
    source_timezone: Mapped[str | None] = mapped_column(String(128))
    is_quarantined: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    quarantine_reason: Mapped[str | None] = mapped_column(String(255))


class HeartRate(SourceMixin, TimestampMixin, Base):
    __tablename__ = "heart_rate"
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    bpm: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    motion_context: Mapped[str | None] = mapped_column(String(128))


class HeartRateVariability(SourceMixin, TimestampMixin, Base):
    __tablename__ = "heart_rate_variability"
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    rmssd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class OxygenSaturation(SourceMixin, TimestampMixin, Base):
    __tablename__ = "oxygen_saturation"
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    spo2: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)


class SleepSession(SourceMixin, TimestampMixin, Base):
    __tablename__ = "sleep_sessions"
    __table_args__ = (Index("ix_sleep_sessions_range", "start_time", "end_time"),)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    deep_sleep_seconds: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    light_sleep_seconds: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    rem_sleep_seconds: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    awake_seconds: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    efficiency: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))


class Steps(SourceMixin, TimestampMixin, Base):
    __tablename__ = "steps"
    __table_args__ = (Index("ix_steps_range", "start_time", "end_time"),)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[Decimal] = mapped_column(Numeric(20, 3), nullable=False)


class ExerciseSession(SourceMixin, TimestampMixin, Base):
    __tablename__ = "exercise_sessions"
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exercise_type: Mapped[str | None] = mapped_column(String(128))
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    calories_kcal: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    distance_meters: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))


class RestingHeartRate(SourceMixin, TimestampMixin, Base):
    __tablename__ = "resting_heart_rate"
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    bpm: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    calculation_method: Mapped[str | None] = mapped_column(String(128))


class Vo2Max(SourceMixin, TimestampMixin, Base):
    __tablename__ = "vo2_max"
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    metric_type: Mapped[str] = mapped_column(String(64), nullable=False)
    ml_per_kg_min: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    fitness_level: Mapped[str | None] = mapped_column(String(64))


class Temperature(SourceMixin, TimestampMixin, Base):
    __tablename__ = "temperature"
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    metric_type: Mapped[str] = mapped_column(String(128), nullable=False)
    celsius: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    baseline_celsius: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))


class RespiratoryRate(SourceMixin, TimestampMixin, Base):
    __tablename__ = "respiratory_rate"
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    metric_type: Mapped[str] = mapped_column(String(128), nullable=False)
    breaths_per_minute: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)


class ActiveZoneMinute(SourceMixin, TimestampMixin, Base):
    __tablename__ = "active_zone_minutes"
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    minutes: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    heart_rate_zone: Mapped[str | None] = mapped_column(String(64))


class Distance(SourceMixin, TimestampMixin, Base):
    __tablename__ = "distance"
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    meters: Mapped[Decimal] = mapped_column(Numeric(18, 5), nullable=False)


class EnergyExpenditure(SourceMixin, TimestampMixin, Base):
    __tablename__ = "energy_expenditure"
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kcal: Mapped[Decimal] = mapped_column(Numeric(18, 5), nullable=False)


class Weight(SourceMixin, TimestampMixin, Base):
    __tablename__ = "weight"
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    kilograms: Mapped[Decimal] = mapped_column(Numeric(12, 5), nullable=False)


class ScalarMetric(SourceMixin, TimestampMixin, Base):
    """Long-tail numeric metrics; core analytics streams retain dedicated tables."""

    __tablename__ = "scalar_metrics"
    __table_args__ = (
        UniqueConstraint("data_type", "google_data_point_id"),
        Index("ix_scalar_metric_type_time", "data_type", "start_time"),
    )
    # Override SourceMixin global uniqueness: IDs can only be assumed unique within a data type.
    google_data_point_id: Mapped[str] = mapped_column(String(512), nullable=False)
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    unit: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
