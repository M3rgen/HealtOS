from __future__ import annotations

from sqlalchemy import JSON, CheckConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AppearanceSettings(TimestampMixin, Base):
    """Single row (id=1) holding the appearance preset choices shared by every device."""

    __tablename__ = "appearance_settings"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    selection: Mapped[dict] = mapped_column(JSON, nullable=False)
