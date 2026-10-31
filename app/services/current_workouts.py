"""Read Google workouts from the current payload, preserving the normalized archive."""

from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.google_health.data_types import REGISTRY
from app.models.eligibility import normalized_counted
from app.models.health import ExerciseSession
from app.models.state import RawHealthData
from app.parsers import normalize


def current_workout(identifier: object, record: object) -> dict | None:
    """Apply ingestion's field priorities, numeric limits and optional text contract.

    A failed correction may leave an older ExerciseSession intact. Its id is still
    the public id, but none of its dates or measurements can be used as fallbacks.
    Normalization here only constructs values in memory; it never writes a row.
    """
    parsed = normalize(REGISTRY["exercise"], record)
    if parsed is None or parsed.model is not ExerciseSession:
        return None
    values = parsed.values
    start, end = values["start_time"], values["end_time"]
    if end <= start:
        return None
    return {
        "id": str(identifier),
        "exercise_type": values["exercise_type"],
        "start_time": start,
        "end_time": end,
        # normalize prepares finite, SQL-bounded Decimals, including measured zero; an
        # unstorable optional duration is None and falls back to the interval, as stored.
        "duration_seconds": (
            float(values["duration_seconds"])
            if values["duration_seconds"] is not None
            else (end - start).total_seconds()
        ),
        "calories_kcal": (
            float(values["calories_kcal"]) if values["calories_kcal"] is not None else None
        ),
        "distance_meters": (
            float(values["distance_meters"]) if values["distance_meters"] is not None else None
        ),
        "source": "Fitbit",
    }


async def current_workouts(session: AsyncSession, start: datetime, stop: datetime) -> list[dict]:
    """Select by current source time, retaining all existing eligibility filters.

    store_records refreshes source times even when normalization fails. Using them
    for the month bounds prevents preserved sessions from retaining their old month.
    The alias keeps normalized_counted's raw-table probes independently correlated.
    Stream payloads in bounded batches so the full raw month need not be held in memory.
    """
    raw = aliased(RawHealthData)
    candidates = await session.stream(
        select(ExerciseSession.id, raw.raw_json)
        .join(
            raw,
            and_(
                raw.data_type == "exercise",
                raw.google_data_point_id == ExerciseSession.google_data_point_id,
            ),
        )
        .where(
            raw.source_start_time >= start,
            raw.source_start_time < stop,
            raw.source_end_time > raw.source_start_time,
            normalized_counted(ExerciseSession),
        )
        .execution_options(yield_per=100)
    )
    rows = []
    try:
        async for identifier, record in candidates:
            row = current_workout(identifier, record)
            if row is not None and start <= row["start_time"] < stop:
                rows.append(row)
    finally:
        await candidates.close()
    return rows
