"""Bounded, read-only health tools for the site's separate Codex conversation."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models.state import RawHealthData
from app.services.dashboard import dashboard_data


class ArchiveQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["inventory", "daily", "records"]
    start_date: date | None = None
    end_date: date | None = None
    data_type: str | None = Field(default=None, max_length=128)
    timezone: str = "UTC"
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=100, ge=1, le=200)

    @model_validator(mode="after")
    def validate_range(self):
        zone = ZoneInfo(self.timezone)
        if self.action != "inventory":
            if not self.start_date or not self.end_date:
                raise ValueError("start_date ve end_date gerekli.")
            days = (self.end_date - self.start_date).days + 1
            maximum = 366 if self.action == "daily" else 31
            if not 1 <= days <= maximum:
                raise ValueError(f"Bir sorgu en fazla {maximum} gün olabilir.")
            if self.end_date > datetime.now(zone).date():
                raise ValueError("Gelecek tarih seçilemez.")
        if self.action == "records" and not self.data_type:
            raise ValueError("records sorgusunda data_type gerekli.")
        return self


HEALTH_TOOL = {
    "type": "function",
    "name": "health_archive",
    "description": (
        "Read the user's actual health archive. Start with inventory to discover available "
        "Google data types, counts and date ranges. daily returns civil-day steps, sleep, "
        "resting heart rate, HRV, SpO2, latest body measurements and hourly charts for the "
        "end date (at most 366 days). records reads original Google health payloads for ANY "
        "inventoried data_type, including exercise, temperature, respiration and sleep "
        "stages (at most 31 days, 200 records/page). Follow next_offset until null or narrow "
        "the range; never treat a truncated page as all records. No SQL or writes allowed."
    ),
    "inputSchema": ArchiveQuery.model_json_schema(),
}


async def read_health(arguments: dict) -> dict:
    query = ArchiveQuery.model_validate(arguments)
    async with SessionLocal() as session:
        if query.action == "inventory":
            rows = await session.execute(
                select(
                    RawHealthData.data_type,
                    func.count(),
                    func.min(RawHealthData.source_start_time),
                    func.max(RawHealthData.source_start_time),
                    func.count(RawHealthData.source_start_time),
                )
                .where(
                    RawHealthData.is_quarantined.is_(False),
                    RawHealthData.source_deleted_at.is_(None),
                )
                .group_by(RawHealthData.data_type)
                .order_by(RawHealthData.data_type)
            )
            return jsonable_encoder(
                {
                    "today": datetime.now(ZoneInfo(query.timezone)).date(),
                    "timezone": query.timezone,
                    "streams": [
                        {
                            "data_type": kind,
                            "count": count,
                            "first": first,
                            "last": last,
                            "undated_count": count - dated,
                        }
                        for kind, count, first, last, dated in rows
                    ],
                    "note": "Karantinadaki ve kaynakta silinen kayıtlar hariç. "
                    "Tarihler kaynak zamanı.",
                }
            )
        if query.action == "daily":
            result = await dashboard_data(
                session,
                query.end_date,
                (query.end_date - query.start_date).days + 1,
                query.timezone,
            )
            return jsonable_encoder(result)
        zone = ZoneInfo(query.timezone)
        start = datetime.combine(query.start_date, time.min, zone).astimezone(UTC)
        stop = datetime.combine(query.end_date + timedelta(days=1), time.min, zone).astimezone(UTC)
        result = await session.scalars(
            select(RawHealthData)
            .where(
                RawHealthData.data_type == query.data_type,
                RawHealthData.is_quarantined.is_(False),
                RawHealthData.source_deleted_at.is_(None),
                RawHealthData.source_start_time < stop,
                func.coalesce(RawHealthData.source_end_time, RawHealthData.source_start_time)
                >= start,
            )
            .order_by(RawHealthData.source_start_time, RawHealthData.id)
            .offset(query.offset)
            .limit(query.limit + 1)
        )
        rows = list(result)
        return jsonable_encoder(
            {
                "data_type": query.data_type,
                "timezone": query.timezone,
                "start_date": query.start_date,
                "end_date": query.end_date,
                "records": [
                    {
                        "start": row.source_start_time,
                        "end": row.source_end_time,
                        "source_timezone": row.source_timezone,
                        "payload": row.raw_json,
                    }
                    for row in rows[: query.limit]
                ],
                "next_offset": query.offset + query.limit if len(rows) > query.limit else None,
                "note": "Tarihsiz, karantinadaki ve kaynakta silinen kayıtlar hariç.",
            }
        )
