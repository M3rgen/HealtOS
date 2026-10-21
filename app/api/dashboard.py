import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_job_request
from app.core.timeutil import as_utc
from app.db.session import get_session
from app.models.health import ExerciseSession, SleepSession, Steps
from app.models.identity import held_elsewhere, plan_each_query
from app.models.manual import ManualMeasurement, ManualWorkout
from app.models.state import RawHealthData
from app.services.dashboard import dashboard_data

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class WorkoutInput(BaseModel):
    exercise_type: str
    start_time: datetime
    end_time: datetime
    calories_kcal: Decimal | None = None
    distance_meters: Decimal | None = None

    @field_validator("exercise_type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128:
            raise ValueError("Spor türü 1–128 karakter olmalı.")
        return value

    @model_validator(mode="after")
    def valid_workout(self):
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("Antrenman tarihleri saat dilimi içermeli.")
        if self.end_time <= self.start_time or self.end_time > datetime.now(UTC):
            raise ValueError("Bitiş başlangıçtan sonra olmalı ve gelecekte olmamalı.")
        for value, maximum in ((self.calories_kcal, 100000), (self.distance_meters, 10000000)):
            if value is not None and (not value.is_finite() or not 0 <= value <= maximum):
                raise ValueError("Kalori ve mesafe geçerli, negatif olmayan değerler olmalı.")
        return self


def workout_dict(row, source: str) -> dict:
    return {
        "id": str(row.id),
        "exercise_type": row.exercise_type,
        "start_time": as_utc(row.start_time),
        "end_time": as_utc(row.end_time),
        "duration_seconds": (
            float(row.duration_seconds)
            if source == "Fitbit" and row.duration_seconds is not None
            else (as_utc(row.end_time) - as_utc(row.start_time)).total_seconds()
        ),
        "calories_kcal": float(row.calories_kcal) if row.calories_kcal is not None else None,
        "distance_meters": float(row.distance_meters) if row.distance_meters is not None else None,
        "source": source,
    }


@router.post("/workouts", status_code=201, dependencies=[Depends(require_job_request)])
async def add_workout(body: WorkoutInput, session: AsyncSession = Depends(get_session)) -> dict:
    item = ManualWorkout(**body.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return workout_dict(item, "Manuel")


@router.get("/workouts")
async def workouts(
    month: date | None = None,
    timezone: str = "UTC",
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    zone = check_zone(timezone)
    await plan_each_query(session)
    current = datetime.now(zone).date()
    first = (month or current).replace(day=1)
    if first > current:
        raise HTTPException(422, "Gelecek ay seçilemez.")
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    start = datetime.combine(first, time.min, zone).astimezone(UTC)
    stop = datetime.combine(following, time.min, zone).astimezone(UTC)
    # Source deletion/quarantine must also hide a previously normalized session.
    valid_source = (
        select(RawHealthData.id)
        .where(
            RawHealthData.data_type == "exercise",
            RawHealthData.google_data_point_id == ExerciseSession.google_data_point_id,
            RawHealthData.is_quarantined.is_(False),
            RawHealthData.source_deleted_at.is_(None),
            ~held_elsewhere(RawHealthData.id),
        )
        .exists()
    )
    imported = (
        await session.scalars(
            select(ExerciseSession).where(
                ExerciseSession.start_time >= start,
                ExerciseSession.start_time < stop,
                ExerciseSession.is_quarantined.is_(False),
                valid_source,
            )
        )
    ).all()
    manual = (
        await session.scalars(
            select(ManualWorkout).where(
                ManualWorkout.start_time >= start,
                ManualWorkout.start_time < stop,
            )
        )
    ).all()
    rows = [workout_dict(row, "Fitbit") for row in imported]
    rows.extend(workout_dict(row, "Manuel") for row in manual)
    return sorted(rows, key=lambda row: row["start_time"], reverse=True)


def check_zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(422, "Geçerli bir saat dilimi seçin.") from exc


@router.get("")
async def dashboard(
    days: int = Query(7, ge=1, le=31),
    end_date: date | None = None,
    timezone: str = "UTC",
    session: AsyncSession = Depends(get_session),
) -> dict:
    zone = check_zone(timezone)
    end_date = end_date or datetime.now(zone).date()
    if end_date > datetime.now(zone).date():
        raise HTTPException(422, "Gelecek tarih seçilemez.")
    return await dashboard_data(session, end_date, days, timezone)


@router.get("/history")
async def metric_history(
    metric: Literal["steps", "sleep", "heart"],
    page: int = Query(1, ge=1),
    timezone: str = "UTC",
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Thirty civil days per page, newest first, through the full valid archive."""
    zone = check_zone(timezone)
    await plan_each_query(session)
    today = datetime.now(zone).date()
    source_type, value_key = {
        "steps": ("steps", "steps"),
        "sleep": ("sleep", "sleep_seconds"),
        "heart": ("daily-resting-heart-rate", "resting_bpm"),
    }[metric]
    # Daily heart summaries encode their civil date at UTC midnight. Sleep
    # belongs to the wake-up date, matching the dashboard's daily aggregation.
    date_zone = ZoneInfo("UTC") if metric == "heart" else zone
    source_time = (
        RawHealthData.source_end_time if metric == "sleep" else RawHealthData.source_start_time
    )
    stop = datetime.combine(today + timedelta(days=1), time.min, date_zone).astimezone(UTC)
    bounds = select(func.min(source_time), func.max(source_time)).where(
        RawHealthData.data_type == source_type,
        RawHealthData.is_quarantined.is_(False),
        RawHealthData.source_deleted_at.is_(None),
        ~held_elsewhere(RawHealthData.id),
        source_time < stop,
    )
    if metric != "heart":
        model = Steps if metric == "steps" else SleepSession
        bounds = bounds.where(
            select(model.id)
            .where(
                model.google_data_point_id == RawHealthData.google_data_point_id,
                model.is_quarantined.is_(False),
            )
            .exists()
        )
    earliest, latest = (await session.execute(bounds)).one()
    earliest_date = as_utc(earliest).astimezone(date_zone).date() if earliest else None
    latest_date = as_utc(latest).astimezone(date_zone).date() if latest else None
    total_pages = ((today - earliest_date).days // 30 + 1) if earliest_date else 1
    if page > total_pages:
        raise HTTPException(422, "Bu geçmiş sayfası bulunamadı.")
    end_date = today - timedelta(days=30 * (page - 1))
    data = await dashboard_data(session, end_date, 30, timezone, valid_sources_only=True)
    return {
        "metric": metric,
        "page": page,
        "total_pages": total_pages,
        "start_date": data["start_date"],
        "end_date": end_date,
        "earliest_date": earliest_date,
        "latest_date": latest_date,
        "timezone": timezone,
        "days": [
            {"date": row["date"], "value": row[value_key], "partial": row["partial"]}
            for row in data["days"]
        ],
    }


@router.get("/export.csv")
async def export(
    days: int = Query(7, ge=1, le=31),
    end_date: date | None = None,
    timezone: str = "UTC",
    session: AsyncSession = Depends(get_session),
) -> Response:
    data = await dashboard(days, end_date, timezone, session)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        ["date", "steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2", "status", "timezone"]
    )
    for row in data["days"]:
        writer.writerow(
            [
                row[key] if row[key] is not None else ""
                for key in (
                    "date",
                    "steps",
                    "sleep_seconds",
                    "resting_bpm",
                    "hrv_ms",
                    "spo2",
                    "status",
                )
            ]
            + [timezone]
        )
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="healtos-{data["end_date"]}.csv"',
            "Cache-Control": "no-store",
        },
    )


class MeasurementInput(BaseModel):
    metric: Literal["height", "weight", "body_fat", "muscle"]
    value: Decimal
    measured_at: datetime

    @field_validator("measured_at")
    @classmethod
    def valid_date(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value > datetime.now(UTC):
            raise ValueError("Ölçüm tarihi saat dilimi içermeli ve gelecekte olmamalı.")
        return value

    @model_validator(mode="after")
    def valid_value(self):
        bounds = {"height": (30, 260), "weight": (1, 500), "body_fat": (0, 100), "muscle": (0, 100)}
        low, high = bounds[self.metric]
        if not self.value.is_finite() or not low <= self.value <= high:
            raise ValueError(f"Ölçüm {low}–{high} aralığında olmalı.")
        return self


@router.post("/measurements", status_code=201, dependencies=[Depends(require_job_request)])
async def add_measurement(
    body: MeasurementInput, session: AsyncSession = Depends(get_session)
) -> dict:
    item = ManualMeasurement(**body.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return {
        "id": str(item.id),
        "metric": item.metric,
        "value": item.value,
        "measured_at": item.measured_at,
        "source": "Manuel",
    }


@router.get("/measurements")
async def measurements(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (
        await session.scalars(
            select(ManualMeasurement).order_by(ManualMeasurement.measured_at.desc()).limit(100)
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "metric": row.metric,
            "value": row.value,
            "measured_at": row.measured_at,
            "source": "Manuel",
        }
        for row in rows
    ]
