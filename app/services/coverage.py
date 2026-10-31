"""Coverage card of the ``health_archive`` tool (``coverage`` action, schema 1).

Per metric it answers one question: on how many *closed* civil days of the window do the
records every reader counts (app/models/eligibility.py; manual measurements flagged as test
records left out, app/services/manual_flags.py) give a value. That is presence and nothing
more:

* a day with a value is not a fully measured day (``sampling.basis = "presence_only"``): ten
  minutes of steps make a value day. The dashboard's ``status: "complete"`` only says that
  five fields are not null; it is not coverage and is not used here;
* a day without a value is "no value" (``days_without_value``), never zero, and never "Google
  returned nothing": this schema-1 card leaves retrieval receipts to the HTTP report
  (app/services/retrieval_coverage.py), so ``retrieval.state`` is always ``"unknown"``;
* today is still filling up: it is never a closed day and is reported as ``partial_today``.

Distinct source streams (a phone and a band, a Google and a manual weight) are separate
``source_regimes`` rows: never merged, summed into one value or ranked. Exclusion counts are
records, not days; unknown counts are ``null``. Nothing here writes.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.timeutil import as_utc
from app.models.eligibility import normalized_counted, normalized_quarantined, raw_counted
from app.models.health import SleepSession, Steps, Weight
from app.models.manual import ManualMeasurement
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.parsers.text import optional_text
from app.services.coverage_raw import raw_value_days
from app.services.current_sleep import current_sleep, sleep_wake_window
from app.services.current_steps import current_steps
from app.services.current_weight import current_weight
from app.services.dashboard import dashboard_data
from app.services.manual_flags import excluded_measurement_ids

SCHEMA_VERSION = "1"
# UTF-8 JSON, as app/api/codex.py sends it; larger cards are refused, never clipped.
COVERAGE_BUDGET_BYTES = 12 * 1024
# Exclusion classes never overlap: a record counts in the first one it matches.
EXCLUSION_POLICY = "first_match: quarantined > without_raw_record > source_deleted > superseded"
NOTE = (
    "Değeri olan gün tam ölçülmüş gün değildir; değeri olmayan gün sıfır değil, değer yok "
    "demektir. Çekimin eksiksizliği bilinmiyor. Bugün kapalı günlere katılmaz. Kaynak "
    "akışları ayrı satırlardır, toplanmaz. Dışlamalar kayıt sayısıdır, gün değil; her kayıt "
    "yalnız ilk uyan sınıfta sayılır, toplamları örtüşmez. null: bilinmiyor."
)


@dataclass(frozen=True)
class Metric:
    key: str  # the dashboard's day field or profile metric
    data_type: str | None  # Google data type
    read: Literal["steps", "sleep", "raw_daily", "weight", "raw_sample", "manual"]
    fields: tuple[str, ...] = ()  # payload fields of a raw read, as the dashboard reads them
    manual: str | None = None  # manual_measurements.metric


METRICS = (
    Metric("steps", "steps", "steps"),
    Metric("sleep_seconds", "sleep", "sleep"),
    Metric("resting_bpm", "daily-resting-heart-rate", "raw_daily", ("beatsPerMinute",)),
    Metric(
        "hrv_ms",
        "daily-heart-rate-variability",
        "raw_daily",
        (
            "averageHeartRateVariabilityMilliseconds",
            "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds",
            "rmssd",
        ),
    ),
    Metric(
        "spo2",
        "daily-oxygen-saturation",
        "raw_daily",
        ("averagePercentage", "percentage", "average"),
    ),
    Metric("weight", "weight", "weight", manual="weight"),
    Metric("height", "height", "raw_sample", ("heightMillimeters",), manual="height"),
    Metric("body_fat", "body-fat", "raw_sample", ("percentage",), manual="body_fat"),
    Metric("muscle", None, "manual", manual="muscle"),
)
NORMALIZED = {"steps": Steps, "sleep": SleepSession, "weight": Weight}


class Sampling(BaseModel):
    basis: Literal["presence_only"] = "presence_only"
    sample_count: int
    supported_seconds: None = None
    target_seconds: None = None
    longest_gap_seconds: None = None


class Retrieval(BaseModel):
    state: Literal["unknown"] = "unknown"
    completed_with_records_days: None = None
    completed_empty_days: None = None
    incomplete_days: None = None
    not_attempted_days: None = None
    unknown_days: int


class Exclusions(BaseModel):
    policy: str = EXCLUSION_POLICY
    quarantined: int | None
    without_raw_record: int | None
    source_deleted: int | None
    superseded: int | None
    manually_excluded: int | None
    invalid_shape: int | None


class SourceRegime(BaseModel):
    source: str  # opaque: a short hash of the whole dataSource, or "manuel"
    origin: Literal["google", "manual"]
    device: str | None = None
    form_factor: str | None = None
    platform: str | None = None
    first_date: date
    last_date: date
    value_days: int  # closed days with a value from this stream
    partial_today: bool
    record_count: int


class MetricCoverage(BaseModel):
    metric_key: str
    data_type: str | None
    value_days: int
    days_without_value: int
    partial_today: bool
    sampling: Sampling
    retrieval: Retrieval
    exclusions: Exclusions
    source_regimes: list[SourceRegime]
    reason_codes: list[str]


class CoverageCard(BaseModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    start_date: date
    end_date: date
    timezone: str
    computed_at: datetime
    requested_days: int
    closed_days: int
    metrics: list[MetricCoverage]
    reason_codes: list[str]
    note: str = NOTE


# --- eligibility: every count follows the shared definition (app/models/eligibility.py) and
# the manual exclusion flags (app/services/manual_flags.py), as the dashboard does.


# --- reading


@dataclass
class Stream:
    source: str
    origin: Literal["google", "manual"]
    device: str | None
    form_factor: str | None
    platform: str | None
    days: set[date]
    records: int = 0


def _stream_of(data_source: object, streams: dict[str, Stream]) -> Stream:
    """Group original sources while preparing optional labels for both coverage readers.

    Raw JSON is not constrained by the text fields of SourceRegime. Each selected
    label must therefore be checked independently: bad metadata must not discard a
    valid measurement, its day, or any other label on the same source.

    Device selection precedes validation. A truthy non-text displayName wins over
    model and becomes None; it must not expose the fallback or a stringified object.
    Valid strings have no coverage-specific length limit and are kept verbatim.
    Neither these labels nor the guarded containers replace the original source
    used for identity, so two malformed sources can still have separate regimes.
    """
    # Hash before guarding containers or labels: presentation is not source identity.
    fingerprint = payload_hash(data_source)
    if fingerprint not in streams:
        source = data_source if isinstance(data_source, dict) else {}
        device = source.get("device")
        device = device if isinstance(device, dict) else {}
        # Keep the existing truthiness/fallback rule, then validate only its result.
        selected_device = device.get("displayName") or device.get("model")
        streams[fingerprint] = Stream(
            source=f"kaynak-{fingerprint[:8]}",
            origin="google",
            device=optional_text(selected_device),
            form_factor=optional_text(device.get("formFactor")),
            platform=optional_text(source.get("platform")),
            days=set(),
        )
    return streams[fingerprint]


@dataclass
class Window:
    first: date
    end: date
    today: date
    zone: ZoneInfo

    @property
    def start(self) -> datetime:
        return datetime.combine(self.first, time.min, self.zone).astimezone(UTC)

    @property
    def stop(self) -> datetime:
        return datetime.combine(self.end + timedelta(days=1), time.min, self.zone).astimezone(UTC)

    def local(self, instant: datetime) -> date:
        return as_utc(instant).astimezone(self.zone).date()


async def _google_streams(session: AsyncSession, metric: Metric, window: Window):
    """Per source stream, the days with a value; and the counted rows without one."""
    streams: dict[str, Stream] = {}
    invalid = 0
    start, stop = window.start, window.stop
    if metric.read in NORMALIZED:
        model = NORMALIZED[metric.read]
        assert metric.data_type is not None
        raw = aliased(RawHealthData)  # normalized_counted probes the raw table itself
        if metric.read == "steps":
            columns, where = (
                (raw.raw_json,),
                (
                    raw.source_end_time > start,
                    raw.source_start_time < stop,
                ),
            )
        elif metric.read == "sleep":
            columns = (raw.raw_json,)
            where = sleep_wake_window(raw.source_end_time, start, stop)
        else:
            columns, where = (
                (raw.raw_json,),
                (
                    raw.source_start_time >= start,
                    raw.source_start_time < stop,
                ),
            )
        rows = await session.execute(
            select(*columns)
            .select_from(model)
            .join(
                raw,
                and_(
                    raw.data_type == metric.data_type,
                    raw.google_data_point_id == model.google_data_point_id,
                ),
            )
            .where(*where, normalized_counted(model, (metric.data_type,)))
        )
        for (record,) in rows:
            # Extract from decoded JSON; SQL JSON scalar extraction can turn a raw
            # boolean source into 0/1 and thereby change its original fingerprint.
            data_source = record.get("dataSource")
            times = [record]
            if metric.read in {"steps", "sleep"}:
                measurement = (
                    current_steps(times[0]) if metric.read == "steps" else current_sleep(times[0])
                )
                if measurement is None:
                    invalid += 1
                    continue
                times = (measurement[0], measurement[1])
            if metric.read == "weight":
                measurement = current_weight(times[0])
                if measurement is None:
                    invalid += 1
                    continue
                timestamp, _ = measurement
                if start <= timestamp < stop:
                    stream = _stream_of(data_source, streams)
                    stream.records += 1
                    stream.days.add(window.local(timestamp))
                continue
            stream = _stream_of(data_source, streams)
            left, right = as_utc(times[0]), as_utc(times[1])
            if right <= left:  # no value: the dashboard skips these too (steps and sleep)
                continue
            stream.records += 1
            if metric.read == "sleep":  # the wake day
                stream.days.add(window.local(right))
                continue
            day = max(left, start).astimezone(window.zone).date()
            last = (min(right, stop) - timedelta(microseconds=1)).astimezone(window.zone).date()
            while day <= last:
                stream.days.add(day)
                day += timedelta(days=1)
        return streams, invalid
    async for source, day in raw_value_days(session, metric, window):
        if day is None:
            invalid += 1
        else:
            stream = _stream_of(source, streams)
            stream.records += 1
            stream.days.add(day)
    return streams, invalid


async def _manual_stream(
    session: AsyncSession, metric: Metric, window: Window, excluded: set[uuid.UUID]
):
    stream = Stream("manuel", "manual", None, None, None, set())
    dropped = 0
    rows = await session.execute(
        select(ManualMeasurement.id, ManualMeasurement.measured_at).where(
            ManualMeasurement.metric == metric.manual,
            ManualMeasurement.measured_at >= window.start,
            ManualMeasurement.measured_at < window.stop,
        )
    )
    for identifier, measured_at in rows:
        if identifier in excluded:  # flagged as a test record: no value, counted apart
            dropped += 1
            continue
        stream.records += 1
        stream.days.add(window.local(measured_at))
    return stream, dropped


def _counts(*conditions):
    return (func.coalesce(func.sum(case((condition, 1), else_=0)), 0) for condition in conditions)


async def _exclusions(session: AsyncSession, metric: Metric, window: Window) -> dict:
    """The rows of the window the shared definition does not count, broken down into classes
    (``EXCLUSION_POLICY``): each class is part of that complement, and the last one is the
    rest of it, so the classes always add up to every row left out and never overlap."""
    if metric.data_type is None:
        return {"quarantined": 0, "without_raw_record": 0, "source_deleted": 0, "superseded": 0}
    types = (metric.data_type,)
    if metric.read in NORMALIZED:
        model = NORMALIZED[metric.read]
        raw = aliased(RawHealthData)  # normalized_counted probes the raw table itself
        time_column = (
            model.end_time
            if metric.read != "weight"
            else case((raw.id.is_(None), model.timestamp), else_=raw.source_start_time)
        )
        if metric.read == "sleep":
            # An orphan has only archive dates. A joined correction belongs solely to
            # its current source dates, even when its end is missing or moved away.
            time_column = case((raw.id.is_(None), model.end_time), else_=raw.source_end_time)
        if metric.read == "steps":
            # Like sleep: a joined correction belongs to its current source interval,
            # not to the retained normalized row's old one; an orphan keeps its own.
            low = case((raw.id.is_(None), model.start_time), else_=raw.source_start_time)
            high = case((raw.id.is_(None), model.end_time), else_=raw.source_end_time)
            where = (high > window.start, low < window.stop)
        else:
            where = (time_column >= window.start, time_column < window.stop)
        left_out = ~normalized_counted(model, types)
        quarantined = and_(
            left_out, or_(model.is_quarantined.is_(True), raw.is_quarantined.is_(True))
        )
        orphan = and_(left_out, ~quarantined, raw.id.is_(None))
        deleted = and_(left_out, ~quarantined, raw.source_deleted_at.is_not(None))
        # What is left: a live, unquarantined raw row whose record another row holds.
        held = and_(left_out, ~quarantined, raw.id.is_not(None), raw.source_deleted_at.is_(None))
        statement = (
            select(*_counts(quarantined, orphan, deleted, held))
            .select_from(model)
            .outerjoin(
                raw,
                and_(
                    raw.data_type == metric.data_type,
                    raw.google_data_point_id == model.google_data_point_id,
                ),
            )
            .where(*where)
        )
    else:
        start, stop = window.start, window.stop
        if metric.read == "raw_daily":
            start = datetime.combine(window.first, time.min, UTC)
            stop = datetime.combine(window.end + timedelta(days=1), time.min, UTC)
        raw = RawHealthData
        left_out = ~raw_counted(raw, types)
        quarantined = and_(
            left_out, or_(raw.is_quarantined.is_(True), *normalized_quarantined(raw, types))
        )
        deleted = and_(left_out, ~quarantined, raw.source_deleted_at.is_not(None))
        # What is left: a live, unquarantined raw row whose record another row holds.
        held = and_(left_out, ~quarantined, raw.source_deleted_at.is_(None))
        statement = select(*_counts(quarantined, deleted, held)).where(
            raw.data_type == metric.data_type,
            raw.source_start_time >= start,
            raw.source_start_time < stop,
        )
    counts = (await session.execute(statement)).one()
    if metric.read not in NORMALIZED:
        counts = (counts[0], 0, counts[1], counts[2])
    return dict(
        zip(
            ("quarantined", "without_raw_record", "source_deleted", "superseded"),
            counts,
            strict=True,
        )
    )


def _regime(stream: Stream, window: Window) -> SourceRegime | None:
    if not stream.days:
        return None
    return SourceRegime(
        source=stream.source,
        origin=stream.origin,
        device=stream.device,
        form_factor=stream.form_factor,
        platform=stream.platform,
        first_date=min(stream.days),
        last_date=max(stream.days),
        value_days=sum(day < window.today for day in stream.days),
        partial_today=window.today in stream.days,
        record_count=stream.records,
    )


async def coverage_card(
    session: AsyncSession,
    start_date: date,
    end_date: date,
    timezone: str,
    data_type: str | None = None,
) -> CoverageCard:
    zone = ZoneInfo(timezone)
    window = Window(start_date, end_date, datetime.now(zone).date(), zone)
    metrics = [
        metric
        for metric in METRICS
        if data_type is None or data_type in (metric.key, metric.data_type, metric.manual)
    ]
    if not metrics:
        raise ValueError(f"coverage için bilinmeyen data_type: {data_type}")
    requested = (end_date - start_date).days + 1
    closed = sum(start_date + timedelta(days=i) < window.today for i in range(requested))
    dashboard_days: dict[date, dict[str, Any]] = {}
    if any(metric.read in {"steps", "sleep"} for metric in metrics):
        data = await dashboard_data(session, end_date, requested, timezone)
        dashboard_days = {date.fromisoformat(row["date"]): row for row in data["days"]}
    excluded = await excluded_measurement_ids(session)
    cards = []
    for metric in metrics:
        streams: dict[str, Stream] = {}
        invalid: int | None = None
        manual_dropped = 0
        codes = ["presence_only", "retrieval_unknown"]
        if metric.data_type is not None:
            streams, invalid = await _google_streams(session, metric, window)
        if metric.manual is not None:
            stream, dropped = await _manual_stream(session, metric, window, excluded)
            streams["manuel"] = stream
            manual_dropped = dropped
        if metric.read in {"steps", "sleep", "weight"}:
            invalid = None  # rows that never normalized are not looked for yet
            codes.append("invalid_shape_unknown")
        if metric.read in {"steps", "sleep"}:
            days = {day for day, row in dashboard_days.items() if row[metric.key] is not None}
        else:
            # Presence is existential: another invalid row on the same day must
            # not erase a valid source value, regardless of dashboard selection.
            days = set().union(*(stream.days for stream in streams.values()))
        regimes = [
            regime
            for regime in (_regime(stream, window) for stream in streams.values())
            if regime is not None
        ]
        regimes.sort(key=lambda regime: (regime.first_date, regime.source))
        value_days = sum(day < window.today for day in days)
        partial_today = window.today in days
        if not value_days and not partial_today:
            codes.append("no_value_in_window")
        if len(regimes) > 1:
            codes.append("separate_source_streams")
        cards.append(
            MetricCoverage(
                metric_key=metric.key,
                data_type=metric.data_type,
                value_days=value_days,
                days_without_value=closed - value_days,
                partial_today=partial_today,
                sampling=Sampling(sample_count=sum(regime.record_count for regime in regimes)),
                retrieval=Retrieval(unknown_days=closed),
                exclusions=Exclusions(
                    **await _exclusions(session, metric, window),
                    manually_excluded=manual_dropped,
                    invalid_shape=invalid,
                ),
                source_regimes=regimes,
                reason_codes=codes,
            )
        )
    codes = ["retrieval_receipts_unavailable", "daily_status_not_coverage"]
    if closed < requested:
        codes.append("today_partial_excluded")
    return CoverageCard(
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
        computed_at=datetime.now(UTC),
        requested_days=requested,
        closed_days=closed,
        metrics=cards,
        reason_codes=codes,
    )


async def coverage_response(
    session: AsyncSession,
    start_date: date,
    end_date: date,
    timezone: str,
    data_type: str | None = None,
) -> dict:
    """The card as JSON, or a structured error: an unknown ``data_type`` or a card over the
    budget (never a clipped card)."""
    accepted = sorted({name for m in METRICS for name in (m.key, m.data_type, m.manual) if name})
    if data_type is not None and data_type not in accepted:
        return {
            "error": "coverage için data_type bir metrik ya da veri türü olmalı; "
            "verilmezse bütün metrikler döner.",
            "code": "coverage_unknown_data_type",
            "data_type": data_type,
            "accepted_data_types": accepted,
        }
    card = await coverage_card(session, start_date, end_date, timezone, data_type)
    result = card.model_dump(mode="json")
    size = len(json.dumps(result, ensure_ascii=False).encode())
    if size <= COVERAGE_BUDGET_BYTES:
        return result
    days = (end_date - start_date).days + 1
    return {
        "error": "Kapsama yanıtı 12 KiB sınırını aşıyor; daha dar bir tarih aralığı ya da "
        "data_type ile tek bir metrik iste.",
        "code": "coverage_budget_exceeded",
        "budget_bytes": COVERAGE_BUDGET_BYTES,
        "size_bytes": size,
        "suggested_range": {
            "start_date": (end_date - timedelta(days=max(days // 2, 1) - 1)).isoformat(),
            "end_date": end_date.isoformat(),
        },
        "metric_keys": [metric["metric_key"] for metric in result["metrics"]],
    }
