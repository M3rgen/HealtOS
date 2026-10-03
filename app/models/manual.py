import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ManualMeasurement(TimestampMixin, Base):
    """User measurements, kept separate from the immutable Google archive."""

    __tablename__ = "manual_measurements"
    __table_args__ = (Index("ix_manual_measurements_metric_time", "metric", "measured_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ManualWorkout(TimestampMixin, Base):
    __tablename__ = "manual_workouts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    exercise_type: Mapped[str] = mapped_column(String(128), nullable=False)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    calories_kcal: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    distance_meters: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
