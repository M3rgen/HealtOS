"""Five daily adapters, separated by whole source stream and measurement definition."""

from collections import defaultdict
from datetime import date
from itertools import pairwise

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.timeutil import as_utc
from app.google_health.data_types import REGISTRY
from app.models.eligibility import raw_counted
from app.models.health import SleepSession, Steps
from app.models.manual import ManualMeasurement
from app.models.state import RawHealthData
from app.parsers.common import parse_date, payload_for, payload_hash
from app.services.canonical_sleep import sleep_evidence
from app.services.canonical_types import CanonicalDay
from app.services.current_sleep import current_sleep
from app.services.current_steps import current_steps
from app.services.finite_math import finite_number, finite_scale, finite_sum
from app.services.health_windows import bounds as bounds
from app.services.manual_flags import excluded_measurements

# Interpret date components losslessly. Older cached days stay under their original
# method; rebuilding must revalidate the raw date without rewriting archived evidence.
METHOD = "source-daily/6"
DAILY_FIELDS = {
    "daily-resting-heart-rate": ("resting_bpm", "bpm", {"beatsPerMinute": "daily_resting"}),
    "daily-heart-rate-variability": (
        "hrv_ms",
        "ms",
        {
            "averageHeartRateVariabilityMilliseconds": "daily_average_hrv",
            "rootMeanSquareOfSuccessiveDifferencesMilliseconds": "daily_rmssd",
            "rmssd": "daily_rmssd",
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": "deep_sleep_rmssd",
        },
    ),
    "daily-oxygen-saturation": (
        "spo2",
        "%",
        {
            "averagePercentage": "daily_average",
            "average": "daily_average",
            "percentage": "daily_percentage",
        },
    ),
}


def _base(raw: RawHealthData, metric: str, unit: str, definition: str, basis: str) -> dict:
    source = raw.raw_json.get("dataSource")
    known = isinstance(source, dict) and bool(source)
    # An absent source is not evidence that unrelated records share a stream.
    fingerprint = payload_hash(
        source if known else {"unknown_source_record": raw.google_data_point_id}
    )
    return {
        "metric": metric,
        "source": "kaynak-" + fingerprint,
        "source_metadata": source,
        "definition": definition,
        "unit": unit,
        "time_basis": basis,
        "method_version": METHOD,
        "value": None,
        "uncertainties": [] if known else ["source_unknown"],
        "observations": [],
    }


def _observation(raw: RawHealthData, value, **extra) -> dict:
    return {
        "record": raw.google_data_point_id,
        "value": value,
        "source_start": as_utc(raw.source_start_time).isoformat()
        if raw.source_start_time
        else None,
        "source_end": as_utc(raw.source_end_time).isoformat() if raw.source_end_time else None,
        "source_timezone": raw.source_timezone,
        **extra,
    }


def _finite(value):
    return finite_number(value)


async def _summaries(session, day, groups):
    first, stop = bounds(day, "UTC")  # payload date is a civil date, not a UTC instant
    rows = await session.scalars(
        select(RawHealthData).where(
            RawHealthData.data_type.in_(DAILY_FIELDS),
            RawHealthData.source_start_time >= first,
            RawHealthData.source_start_time < stop,
            raw_counted(data_types=tuple(DAILY_FIELDS)),
        )
    )
    for raw in rows:
        payload = payload_for(REGISTRY[raw.data_type], raw.raw_json)
        if parse_date(payload.get("date")) != day:
            continue
        metric, unit, fields = DAILY_FIELDS[raw.data_type]
        for field, definition in fields.items():
            value = _finite(payload.get(field))
            # Keep an explicitly missing definition visible without inventing a zero.
            if field not in payload:
                continue
            base = _base(raw, metric, unit, definition, "civil_day_summary")
            metadata = payload.get("dailyRestingHeartRateMetadata")
            if metadata:
                base["measurement_metadata"] = metadata
                # Distinct calculation methods must not be silently made equivalent.
                base["definition"] += ":" + payload_hash(metadata)
            key = (metric, base["source"], base["definition"])
            group = groups.setdefault(key, base)
            group["observations"].append(
                _observation(raw, value, source_day=day.isoformat(), field=field)
            )
    for group in groups.values():
        values = {item["value"] for item in group["observations"] if item["value"] is not None}
        if any(item["value"] is None for item in group["observations"]):
            group["uncertainties"].append("missing_observations")
        if len(values) == 1:
            group["value"] = values.pop()
        elif len(values) > 1:
            group["uncertainties"].append("conflicting_values")
        else:
            group["uncertainties"].append("no_value")


async def _intervals(session, day, timezone, groups):
    first, stop = bounds(day, timezone)
    for model, kind, metric, unit, definition in (
        (Steps, "steps", "steps", "count", "interval_count_prorated"),
        (SleepSession, "sleep", "sleep_seconds", "s", "session_asleep_estimate"),
    ):
        raw = aliased(RawHealthData)

        def in_window(start, end, kind=kind):
            return (start < stop, end > first) if kind == "steps" else (end >= first, end < stop)

        source_candidates = select(RawHealthData.google_data_point_id).where(
            RawHealthData.data_type == kind,
            *in_window(RawHealthData.source_start_time, RawHealthData.source_end_time),
        )
        # Sleep belongs only to its current wake day, including raw-only arrivals.
        # Keep the existing step candidate union; bound both sides before joining.
        candidates = (
            source_candidates
            if kind == "sleep"
            else select(model.google_data_point_id)
            .where(*in_window(model.start_time, model.end_time))
            .union(source_candidates)
        ).subquery()
        rows = await session.scalars(
            select(raw)
            .join(candidates, raw.google_data_point_id == candidates.c.google_data_point_id)
            .where(
                raw.data_type == kind,
                raw_counted(raw, (kind,)),
                # Retained normalized evidence cannot supply missing current dates.
                raw.source_start_time.is_not(None),
                raw.source_end_time.is_not(None),
            )
        )
        spans = defaultdict(list)
        for record in rows:
            if kind == "steps":
                # Normalization may retain old evidence or skip the first payload.
                # Only the current raw measurement supplies the value and interval;
                # missing measurements remain gaps on their current source interval.
                measurement = current_steps(record.raw_json)
                if measurement is None:
                    left, right = as_utc(record.source_start_time), as_utc(record.source_end_time)
                    count = None
                else:
                    left, right, count = measurement
            else:
                measurement = current_sleep(record.raw_json)
                if measurement is None:
                    continue
                left, right, payload = measurement
                # Only an in-memory span reaches both estimators. No retained dates
                # or measurements are copied, and it is never added to the session.
                item = SleepSession(
                    google_data_point_id=record.google_data_point_id,
                    start_time=left,
                    end_time=right,
                )
            if not all(in_window(left, right)):
                continue
            base = _base(
                record,
                metric,
                unit,
                definition,
                "interval_allocation" if kind == "steps" else "wake_day",
            )
            key = (metric, base["source"], definition)
            group = groups.setdefault(key, base)
            if kind == "sleep":
                spans[key].append((item, payload))
                estimate, _ = sleep_evidence([(item, payload)])
                group["observations"].append(
                    _observation(
                        record,
                        estimate.seconds,
                        start=left.isoformat(),
                        end=right.isoformat(),
                        assumptions=estimate.assumptions,
                    )
                )
            elif right <= left:
                group["uncertainties"].append("invalid_interval")
                group["observations"].append(
                    _observation(record, None, start=left.isoformat(), end=right.isoformat())
                )
            else:
                if count is None:
                    group["uncertainties"].extend(("no_value", "missing_observations"))
                    group["observations"].append(
                        _observation(record, None, start=left.isoformat(), end=right.isoformat())
                    )
                    continue
                a, b = max(left, first), min(right, stop)
                value = finite_scale(count, (b - a).total_seconds(), (right - left).total_seconds())
                if value is None:
                    group["uncertainties"].append("numeric_overflow:interval_allocation")
                spans[key].append((a, b, value))
                group["observations"].append(
                    _observation(
                        record,
                        value,
                        start=left.isoformat(),
                        end=right.isoformat(),
                        source_value=count,
                    )
                )
        for key, values in spans.items():
            group = groups[key]
            if kind == "sleep":
                values.sort(
                    key=lambda pair: (
                        as_utc(pair[0].start_time),
                        as_utc(pair[0].end_time),
                        pair[0].google_data_point_id,
                    )
                )
                estimate, timeline = sleep_evidence(values)
                group.update(value=estimate.seconds, sleep=timeline)
                group["uncertainties"].extend(estimate.assumptions)
                timed = group["sleep"].get("timeline_stage_seconds")
                timed_asleep = (
                    sum(timed.get(stage, 0) for stage in ("asleep", "light", "deep", "rem"))
                    if timed is not None
                    else None
                )
                group["sleep"].update(
                    estimated_asleep_seconds=estimate.seconds,
                    timed_asleep_seconds=timed_asleep,
                    estimate_minus_timed_asleep_seconds=(
                        estimate.seconds - timed_asleep
                        if estimate.seconds is not None and timed_asleep is not None
                        else None
                    ),
                )
                if group["sleep"].get("session_overlap_seconds", 0):
                    group["uncertainties"].append("overlap_estimated")
                if any(cell["state"] == "conflict" for cell in group["sleep"]["timeline"] or []):
                    group["uncertainties"].append("conflicting_stages")
            else:
                values.sort(key=lambda row: row[:2])
                if any(b > c for (_, b, _), (c, _, _) in pairwise(values)):
                    group["uncertainties"].append("overlapping_intervals")
                elif not any(item["value"] is None for item in group["observations"]):
                    group["value"] = finite_sum([value for _, _, value in values])
                    if group["value"] is None:
                        group["uncertainties"].append("numeric_overflow:daily_total")
                group["uncertainties"].append("uniform_interval_allocation")


async def _manual_profile(session, stop):
    result = []
    for metric in ("weight", "height", "body_fat", "muscle"):
        valid = (
            ManualMeasurement.metric == metric,
            ManualMeasurement.measured_at < stop,
            ManualMeasurement.id.not_in(excluded_measurements()),
        )
        latest = select(func.max(ManualMeasurement.measured_at)).where(*valid).scalar_subquery()
        rows = await session.scalars(
            select(ManualMeasurement).where(*valid, ManualMeasurement.measured_at == latest)
        )
        observations = sorted(
            [
                {
                    "record": str(row.id),
                    "value": float(row.value),
                    "measured_at": as_utc(row.measured_at).isoformat(),
                }
                for row in rows
            ],
            key=lambda item: item["record"],
        )
        if observations:
            values = {item["value"] for item in observations}
            result.append(
                {
                    "metric": metric,
                    "source": "manuel",
                    "unit": {"weight": "kg", "height": "cm", "body_fat": "%", "muscle": "%"}[
                        metric
                    ],
                    "method_version": METHOD,
                    "time_basis": "latest_before_day_stop",
                    "observations": observations,
                    "value": next(iter(values)) if len(values) == 1 else None,
                    "uncertainties": ["conflicting_values"] if len(values) > 1 else [],
                }
            )
    return result


async def canonical_day(session: AsyncSession, day: date, timezone: str) -> dict:
    groups = {}
    await _summaries(session, day, groups)
    await _intervals(session, day, timezone, groups)
    for group in groups.values():
        group["observations"].sort(
            key=lambda item: (item["source_start"] or "", item["record"], item.get("field", ""))
        )
        if group["metric"] == "sleep_seconds":
            totals_by_span = defaultdict(set)
            for item in group["observations"]:
                if item["value"] is not None:
                    totals_by_span[(item["start"], item["end"])].add(item["value"])
            if any(len(values) > 1 for values in totals_by_span.values()):
                group["uncertainties"].append("conflicting_session_totals")
        group["uncertainties"] = sorted(set(group["uncertainties"]))
        codes = group["uncertainties"]
        group["state"] = (
            "conflict"
            if any(
                code.startswith("conflicting_") or code == "overlapping_intervals" for code in codes
            )
            else "no_value"
            if group["value"] is None
            else "estimated"
            if group["time_basis"] != "civil_day_summary"
            else "measured"
        )
    first, stop = bounds(day, timezone)
    result = {
        "date": day.isoformat(),
        "timezone": timezone,
        "method_version": METHOD,
        "day_seconds": (stop - first).total_seconds(),
        "streams": [groups[key] for key in sorted(groups)],
        "manual_profile": await _manual_profile(session, stop),
        "missing_metrics": sorted(
            {"steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2"}
            - {group["metric"] for group in groups.values() if group["value"] is not None}
        ),
        "source_selection": "none",
        "intraday_samples": "not_used_as_daily_summaries",
    }

    return CanonicalDay.model_validate(result).model_dump(mode="json", exclude_unset=True)
