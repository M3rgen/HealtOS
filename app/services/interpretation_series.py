"""Definition-separated daily series with ordered, append-only evidence accumulation."""

from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.google_health.data_types import REGISTRY
from app.parsers.common import parse_datetime, payload_for, payload_hash
from app.services.canonical_daily import bounds
from app.services.interpretation_inputs import method_details, method_key

COMPARABILITY_BLOCKERS = {
    "awake_unknown",
    "duration_from_payload",
    "missing_observations",
    "overlapping_intervals",
    "conflicting_values",
    "conflicting_session_totals",
    "source_unknown",
}
DAILY_KINDS = {
    "steps": "steps",
    "sleep_seconds": "sleep",
    "resting_bpm": "daily-resting-heart-rate",
    "hrv_ms": "daily-heart-rate-variability",
    "spo2": "daily-oxygen-saturation",
}


def daily_evidence(group, day):
    """Materialize each stream's source references once, in observation order."""
    return [
        dict(
            record=obs["record"],
            date=str(day),
            value=obs["value"],
            start=obs["source_start"],
            end=obs["source_end"],
            source_timezone=obs["source_timezone"],
            field=obs.get("field"),
            reason_codes=obs.get("assumptions") or [],
        )
        for obs in group["observations"]
    ]


def definition_groups(group, raw):
    kind = DAILY_KINDS[group["metric"]]
    partitions = defaultdict(list)
    for obs in group["observations"]:
        record = raw.get((kind, obs["record"]))
        method = (
            method_key(
                payload_for(REGISTRY[kind], record.raw_json), record.raw_json.get("dataSource")
            )
            if record
            else "unknown"
        )
        partitions[method].append(obs)
    if len(partitions) <= 1:
        return [group]
    results = []
    for observations in partitions.values():
        reasons = [r for r in group["uncertainties"] if r != "conflicting_values"]
        values = {obs["value"] for obs in observations if obs["value"] is not None}
        value = next(iter(values)) if len(values) == 1 else None
        if len(values) > 1:
            reasons.append("conflicting_values")
        if kind in {"steps", "sleep"}:
            value = None
            reasons.append("mixed_interval_methods_not_aggregated")
        results.append(
            {**group, "observations": observations, "value": value, "uncertainties": reasons}
        )
    return results


def daily_series(derived, inputs):
    """Keep regimes separate and accumulate collision evidence in linear work.

    Device membership does not select or aggregate a stream. On collisions the
    value remains unavailable, and metadata still comes from the last stream.
    """
    series = defaultdict(dict)
    raw = {(row.data_type, row.google_data_point_id): row for row in inputs.rows}
    for item in derived["days"]:
        result = item.get("result")
        if not result:
            continue
        for group in (
            part for original in result["streams"] for part in definition_groups(original, raw)
        ):
            kind = DAILY_KINDS[group["metric"]]
            source = inputs.source(kind, group["source_metadata"], group["source"])
            methods = set()
            details = {}
            for obs in group["observations"]:
                row = raw.get((kind, obs["record"]))
                if row:
                    payload = payload_for(REGISTRY[kind], row.raw_json)
                    methods.add(method_key(payload, row.raw_json.get("dataSource")))
                    details = method_details(payload, row.raw_json.get("dataSource"))
                    if group.get("measurement_metadata") is not None:
                        details["dailyRestingHeartRateMetadata"] = group["measurement_metadata"]
            definition = group["definition"] + ":" + payload_hash(sorted(methods))
            key = (
                group["metric"],
                source,
                definition,
                group["unit"],
                group["method_version"],
                group["time_basis"],
            )
            day = date.fromisoformat(item["date"])
            reasons = list(group["uncertainties"])
            if day >= datetime.now(ZoneInfo(derived["timezone"])).date():
                reasons.append("partial_day")
            if group["metric"] == "steps":
                left, stop = bounds(day, derived["timezone"])
                covered = 0.0
                for obs in group["observations"]:
                    a, b = parse_datetime(obs.get("start")), parse_datetime(obs.get("end"))
                    if a is not None and b is not None:
                        covered += max(0, (min(b, stop) - max(a, left)).total_seconds())
                if covered < (stop - left).total_seconds():
                    reasons.append("incomplete_interval_coverage")
            comparable = not set(reasons) & (
                COMPARABILITY_BLOCKERS | {"partial_day", "incomplete_interval_coverage"}
            )
            entry: dict = dict(
                value=group["value"],
                reasons=reasons,
                comparable=comparable,
                method_details=details,
                evidence=daily_evidence(group, day),
            )
            if day in series[key]:
                previous = series[key][day]
                # Each key/day owns one accumulator. Only the new stream is
                # copied; revisiting the accumulated prefix would be quadratic.
                previous["evidence"].extend(entry["evidence"])
                entry.update(
                    value=None,
                    comparable=False,
                    reasons=["same_device_multiple_streams"],
                    evidence=previous["evidence"],
                )
            series[key][day] = entry
    return series
