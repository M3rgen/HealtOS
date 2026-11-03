"""Bounded health tools: source reads and a disposable local derived cache."""

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text

from app.db.session import SessionLocal
from app.models.eligibility import raw_counted
from app.models.identity import plan_each_query
from app.models.state import RawHealthData
from app.services.coverage import coverage_response
from app.services.dashboard import dashboard_data
from app.services.derived import build_range, derived_range
from app.services.health_interpretation import derived_window, interpret, refusal
from app.services.health_windows import bounds, input_bounds, interpretation_window
from app.services.interpretation_types import ACTIONS

DERIVED_BUDGET_BYTES = 64 * 1024
# The card's own fields (timezone, detail, reason codes, sizes, suggested range).
ENVELOPE_RESERVE_BYTES = 1024
# Per-record detail; the summary keeps each stream's value, state and uncertainties.
DETAIL_KEYS = ("observations", "source_metadata", "measurement_metadata")
SLEEP_DETAIL_KEYS = ("sessions", "episodes", "timeline")


class ArchiveQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "inventory",
        "daily",
        "records",
        "coverage",
        "baseline",
        "analyze",
        "sleep",
        "intraday",
        "sessions",
        "physiology",
        "longitudinal",
        "relationship",
    ]
    start_date: date | None = None
    end_date: date | None = None
    data_type: str | None = Field(default=None, max_length=128)
    timezone: str = "UTC"
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=100, ge=1, le=200)

    cursor: str | None = Field(default=None, max_length=2048)
    lookback_days: int = Field(default=56, ge=14, le=90)
    metric: str | None = Field(default=None, max_length=128)
    other_metric: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=128)
    other_source: str | None = Field(default=None, max_length=128)
    definition: str | None = Field(default=None, max_length=512)
    other_definition: str | None = Field(default=None, max_length=512)
    lags: list[int] = Field(default_factory=lambda: [0], min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_range(self):
        zone = ZoneInfo(self.timezone)
        if self.action != "inventory":
            if not self.start_date or not self.end_date:
                raise ValueError("start_date ve end_date gerekli.")
            days = (self.end_date - self.start_date).days + 1
            maximum = (
                366
                if self.action in {"daily", "coverage"}
                else 180
                if self.action == "relationship"
                else 90
                if self.action in ACTIONS
                else 31
            )
            if not 1 <= days <= maximum:
                raise ValueError(f"Bir sorgu en fazla {maximum} gün olabilir.")
            if self.end_date > datetime.now(zone).date():
                raise ValueError("Gelecek tarih seçilemez.")
            try:
                if self.action in ACTIONS:
                    first, last = interpretation_window(
                        self.action, self.start_date, self.end_date, self.lookback_days
                    )
                    input_bounds(first, last, self.timezone)
                else:
                    bounds(self.start_date, self.timezone)
                    bounds(self.end_date, self.timezone)
            except OverflowError as exc:
                raise ValueError(
                    "Sorgunun okuma penceresi temsil edilebilir tarihleri aşıyor."
                ) from exc
        if self.action == "records" and not self.data_type:
            raise ValueError("records sorgusunda data_type gerekli.")
        if self.action in ACTIONS:
            metrics = {"steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2"}
            if self.offset or (self.metric and self.metric not in metrics):
                raise ValueError("Yorum sorgusunda sürüm 2 imleci ve desteklenen metric kullan.")
            if self.action == "relationship" and (
                not self.metric or self.other_metric not in metrics
            ):
                raise ValueError("relationship iki günlük metric gerektirir.")
            if len(set(self.lags)) != len(self.lags) or any(abs(lag) > 14 for lag in self.lags):
                raise ValueError("Gecikmeler önceden belirlenmiş, tekil ve -14–14 gün olmalı.")
        return self

    def date_range(self) -> tuple[date, date]:
        """The validated (start_date, end_date) of a daily or records query."""
        if self.start_date is None or self.end_date is None:
            raise ValueError("start_date ve end_date gerekli.")
        return self.start_date, self.end_date


HEALTH_TOOL = {
    "type": "function",
    "name": "health_archive",
    "description": (
        "Read the user's actual health archive. Start with inventory to discover available "
        "Google data types, counts and date ranges. daily returns civil-day steps, sleep, "
        "resting heart rate, HRV, SpO2, latest body measurements and hourly charts for the "
        "end date (at most 366 days), plus derived schema 1: separate source streams and "
        "HRV definitions, conflicts, evidence-only sleep timelines; source_selection=none. "
        "derived is at most 64 KiB: derived.detail=full includes observations and sleep "
        "timelines; summary keeps per-stream value, state, uncertainties and "
        "observation_count (narrow the range, or use sleep/records, for the detail); omitted "
        "means even that does not fit, see suggested_range. "
        "Legacy scalar fields remain for compatibility. records reads original Google health "
        "payloads for ANY "
        "inventoried data_type, including exercise, temperature, respiration and sleep "
        "stages (at most 31 days, 200 records/page). Follow next_offset until null or narrow "
        "the range; never treat a truncated page as all records. coverage tells, per metric "
        "(the five daily values and body measurements), on how many closed days of the range "
        "a value exists, from which separately listed source streams, and how many records "
        "were excluded; a value day is not a fully measured day and today is reported apart "
        "(at most 366 days). From retrieval receipts it says why a closed day has no value: "
        "empty_response (Google returned nothing), not_retrieved (never requested), "
        "incomplete, returned_without_value, or unknown (before receipts); gap periods are "
        "listed when one metric is requested. Its optional data_type narrows "
        "it to one of steps, sleep, daily-resting-heart-rate, daily-heart-rate-variability, "
        "daily-oxygen-saturation, weight, height, body-fat or muscle. New interpretation schema 2 "
        "actions: baseline/analyze use only preceding comparable days (default lookback_days=56, "
        "minimum 14 days, cold start below 7, support at least 50%); metric optionally selects "
        "steps, sleep_seconds, resting_bpm, hrv_ms or spo2. analyze adds evidence-bound ranked "
        "observation candidates, never diagnoses. sleep preserves sessions, stages, overlap and "
        "unknown intervals. intraday supports heart-rate/steps; sessions returns Google "
        "exercise/manual workouts without inferring uninterrupted running. physiology supports "
        "respiration, temperature and VO2 definitions; longitudinal supports energy/distance/body "
        "measurements. relationship requires metric, other_metric and preselected lags in days "
        "(-14..14, up to five); positive lag means y follows x, calendar gaps never slide. "
        "source/definition and other_source/other_definition select exact regimes from results. "
        "New actions: at most 90 days (relationship 180), 50000 inputs, 16 MiB input, 64 KiB "
        "response. Statistics use the whole requested window, independent of limit; follow "
        "next_cursor using cursor with identical query parameters (limit may differ); "
        "stale_cursor requires restarting. Structured refusals are complete JSON. No SQL or "
        "writes allowed. Reads refresh only a disposable derived cache; raw and manual data "
        "are never written. "
    ),
    "inputSchema": ArchiveQuery.model_json_schema(),
}


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())


def _summary_group(group: dict) -> dict:
    row = {key: value for key, value in group.items() if key not in DETAIL_KEYS}
    row["observation_count"] = len(group.get("observations", []))
    if group.get("sleep") is not None:
        row["sleep"] = {
            key: value for key, value in group["sleep"].items() if key not in SLEEP_DETAIL_KEYS
        }
    dates = {item.get("measured_at") for item in group.get("observations", [])}
    if group.get("source") == "manuel" and len(dates) == 1:
        row["measured_at"] = dates.pop()
    return row


def _summary_day(item: dict) -> dict:
    result = item.get("result")
    if result is None:
        return item
    return item | {
        "result": result
        | {
            "streams": [_summary_group(group) for group in result["streams"]],
            "manual_profile": [_summary_group(group) for group in result["manual_profile"]],
        }
    }


def _suggested(days: list[dict]) -> dict | None:
    """The latest days whose items, in this representation, fit the budget on their own."""
    room, count = DERIVED_BUDGET_BYTES - ENVELOPE_RESERVE_BYTES, 0
    for item in reversed(days):
        room -= _size(item) + 2  # the ", " separator
        if room < 0:
            break
        count += 1
    if not count or count == len(days):
        return None
    return {"start_date": days[-count]["date"], "end_date": days[-1]["date"]}


def bounded_derived(card: dict) -> dict:
    """The daily derived card within its byte budget, never a clipped JSON document.

    Detail is moved, not discarded: a narrower daily range, sleep and records still
    return observations, sleep timelines and the original payloads.
    """
    full = card | {"detail": "full"}
    full_size = _size(full)
    if full_size <= DERIVED_BUDGET_BYTES:
        return full
    days = [_summary_day(item) for item in card["days"]]
    summary = card | {
        "days": days,
        "detail": "summary",
        "reason_codes": ["derived_detail_budget_exceeded"],
        "budget_bytes": DERIVED_BUDGET_BYTES,
        "size_bytes": full_size,
        "suggested_range": _suggested(card["days"]),
    }
    summary_size = _size(summary)
    if summary_size <= DERIVED_BUDGET_BYTES:
        return summary
    return card | {
        "days": [],
        "detail": "omitted",
        "reason_codes": ["derived_budget_exceeded"],
        "budget_bytes": DERIVED_BUDGET_BYTES,
        "size_bytes": summary_size,
        "suggested_range": _suggested(days),
    }


async def read_health(arguments: dict) -> dict:
    try:
        query = ArchiveQuery.model_validate(arguments)
    except (ValueError, KeyError):
        if isinstance(arguments, dict) and arguments.get("action") in ACTIONS:
            return refusal("invalid_query")
        raise
    window = (
        query.date_range()
        if query.action == "daily"
        else derived_window(query)
        if query.action in ACTIONS
        else None
    )
    if window is not None:
        # Cold/pending days are built first, one short transaction per day: the build lock
        # never spans a whole range, and no day is built from a REPEATABLE READ snapshot.
        await build_range(SessionLocal, *window, query.timezone)
    async with SessionLocal() as session:
        if query.action in ACTIONS and session.get_bind().dialect.name == "postgresql":
            # One source/cache/eligibility snapshot across every scientific read.
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        await plan_each_query(session)
        if query.action == "inventory":
            rows = await session.execute(
                select(
                    RawHealthData.data_type,
                    func.count(),
                    func.min(RawHealthData.source_start_time),
                    func.max(RawHealthData.source_start_time),
                    func.count(RawHealthData.source_start_time),
                )
                .where(raw_counted())
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
        start_date, end_date = query.date_range()
        if query.action in ACTIONS:
            result = await interpret(session, query)
            await session.commit()  # disposable derived cache only
            return result
        if query.action == "coverage":
            return await coverage_response(
                session, start_date, end_date, query.timezone, query.data_type
            )
        if query.action == "daily":
            result = await dashboard_data(
                session,
                end_date,
                (end_date - start_date).days + 1,
                query.timezone,
            )
            result["derived"] = bounded_derived(
                await derived_range(session, start_date, end_date, query.timezone)
            )
            await session.commit()  # only the local, disposable read model is written
            return jsonable_encoder(result)
        zone = ZoneInfo(query.timezone)
        start = datetime.combine(start_date, time.min, zone).astimezone(UTC)
        stop = datetime.combine(end_date + timedelta(days=1), time.min, zone).astimezone(UTC)
        result = await session.scalars(
            select(RawHealthData)
            .where(
                RawHealthData.data_type == query.data_type,
                raw_counted(data_types=(query.data_type,)),
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
