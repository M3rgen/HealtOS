"""Duration-aware intraday cells and existing, evidence-only sleep sessions."""

from collections import defaultdict
from datetime import timedelta
from fractions import Fraction
from itertools import pairwise
from zoneinfo import ZoneInfo

from app.core.timeutil import as_utc
from app.google_health.data_types import REGISTRY
from app.parsers.common import payload_for
from app.services.canonical_daily import _finite, bounds
from app.services.finite_math import finite_calculation, finite_scale
from app.services.interpretation_cells import CellPlan, IntradayBudget
from app.services.interpretation_inputs import evidence, fallback_source, method_details, method_key
from app.services.interpretation_stats import statistics, statistics_reasons
from app.services.interpretation_types import Intraday, SleepSummary

MAX_HEART_GAP_SECONDS = 300


def sleep_summaries(derived, first, last):
    results = []
    for day in derived["days"]:
        if not str(first) <= day["date"] <= str(last) or not day.get("result"):
            continue
        for stream in day["result"]["streams"]:
            if stream["metric"] != "sleep_seconds" or stream.get("sleep") is None:
                continue
            results.append(
                SleepSummary.model_validate(
                    dict(
                        metric="sleep_seconds",
                        source=stream["source"],
                        definition=stream["definition"],
                        method_details=method_details({}, stream["source_metadata"]),
                        unit="s",
                        time_basis="wake_day",
                        date=day["date"],
                        value=stream["value"],
                        sleep=stream["sleep"],
                        reason_codes=sorted(
                            set(stream["uncertainties"] + stream["sleep"]["reason_codes"])
                        ),
                        evidence=[
                            dict(
                                record=r["record"],
                                date=day["date"],
                                value=r["value"],
                                start=r["source_start"],
                                end=r["source_end"],
                                source_timezone=r["source_timezone"],
                                reason_codes=r.get("assumptions") or [],
                            )
                            for r in stream["observations"]
                        ],
                    )
                )
            )
    return results


def _intraday_plans(inputs, query, budget):
    first, last = query.date_range()
    groups = defaultdict(list)
    for raw in inputs.rows:
        kind = raw.data_type
        if kind not in {"steps", "heart-rate"} or (query.data_type and kind != query.data_type):
            continue
        payload = payload_for(REGISTRY[kind], raw.raw_json)
        metadata = raw.raw_json.get("dataSource")
        source = inputs.source(
            kind,
            metadata,
            fallback_source(raw),
        )
        method = method_key(payload, metadata)
        definition = (
            "linear_sample_interpolation:" if kind == "heart-rate" else "interval_rate:"
        ) + method
        if (query.source and source != query.source) or (
            query.definition and definition != query.definition
        ):
            continue
        key = (kind, source, method)
        groups[key].append(raw)
    plans = []
    for (kind, source, method), rows in sorted(groups.items()):
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            left, stop = bounds(day, query.timezone)
            plan, samples = CellPlan(left, stop, budget), []
            reasons = ["coverage_is_not_retrieval_completeness"]
            for raw in rows:
                a, b = as_utc(raw.source_start_time), as_utc(raw.source_end_time)
                if a is None:
                    continue
                payload = payload_for(REGISTRY[kind], raw.raw_json)
                fields = (
                    ("count", "countSum", "steps")
                    if kind == "steps"
                    else ("beatsPerMinute", "bpm", "value")
                )
                value = next(
                    (
                        _finite(payload[field])
                        for field in fields
                        if _finite(payload.get(field)) is not None
                    ),
                    None,
                )
                if kind == "heart-rate":
                    if (
                        left - timedelta(seconds=MAX_HEART_GAP_SECONDS)
                        <= a
                        < stop + timedelta(seconds=MAX_HEART_GAP_SECONDS)
                    ):
                        plan.observe(
                            raw,
                            a.astimezone(ZoneInfo(query.timezone)).date(),
                            value,
                            "beatsPerMinute",
                            ["unsupported_or_missing_value"] if value is None else [],
                        )
                        samples.append((a, value))
                elif b is not None and b > a and a < stop and b > left:
                    rate = (
                        finite_scale(value, 1, (b - a).total_seconds())
                        if value is not None
                        else None
                    )
                    codes = ["unsupported_or_missing_value"] if value is None else []
                    if value is not None and rate is None:
                        codes.append("numeric_overflow:interval_rate")
                    reasons.extend(codes)
                    plan.observe(raw, day, value, fields[0], codes)
                    plan.add_span(max(a, left), min(b, stop), rate)
            if kind == "heart-rate":
                reasons += [
                    "interpolated_between_samples_only",
                    "no_extrapolation",
                    "max_interpolation_gap_300_seconds",
                ]
                # Conflicting same-time samples block interpolation on both sides.
                by_time = defaultdict(list)
                for stamp, value in samples:
                    by_time[stamp].append(value)
                if any(len(set(values)) > 1 for values in by_time.values()):
                    reasons.append("conflicting_same_time_samples")
                points = [
                    (stamp, values[0] if len(set(values)) == 1 else None)
                    for stamp, values in sorted(by_time.items())
                ]
                for (a, x), (b, y) in pairwise(points):
                    if (
                        0 < (b - a).total_seconds() <= MAX_HEART_GAP_SECONDS
                        and x is not None
                        and y is not None
                        and a < stop
                        and b > left
                    ):
                        p, q = max(a, left), min(b, stop)
                        # Integrate the linear segment on the actual clipped interval.
                        mean = (
                            x
                            + (y - x)
                            * (((p - a).total_seconds() + (q - a).total_seconds()) / 2)
                            / (b - a).total_seconds()
                        )
                        elapsed = ((p - a).total_seconds() + (q - a).total_seconds()) / 2
                        mean = finite_calculation(
                            mean,
                            lambda x=x, y=y, elapsed=elapsed, a=a, b=b: (
                                Fraction(x)
                                + (Fraction(y) - Fraction(x))
                                * Fraction(elapsed)
                                / Fraction((b - a).total_seconds())
                            ),
                        )
                        if mean is None:
                            reasons.append("numeric_overflow:interpolation")
                        plan.add_span(p, q, mean)
            else:
                reasons += [
                    "uniform_interval_allocation_estimate",
                    "exact_movement_bursts_unavailable",
                ]
            if plan.observations:
                plans.append((kind, source, method, rows[0], day, left, stop, plan, reasons))
    return plans


def intraday_summaries(inputs, query, *, max_summaries):
    # Finish the whole-query preflight before producing any cells, evidence dicts
    # or models. A small page never hides work or truncates scientific evidence.
    plans = _intraday_plans(inputs, query, IntradayBudget(max_summaries))
    results = []
    for kind, source, method, raw, day, left, stop, plan, reasons in plans:
        observations = []
        for args in plan.observations:
            observations.append(evidence(*args))
        cells, supported, conflict, weighted = [], 0.0, 0.0, 0.0
        weights = []
        for a, b, state, value in plan.cells():
            seconds = (b - a).total_seconds()
            if value is not None:
                supported += seconds
                weighted += value * seconds
                weights.append((value, seconds))
            if state == "conflict":
                conflict += seconds
            cells.append(dict(start=a.isoformat(), end=b.isoformat(), state=state, value=value))
        duration = (stop - left).total_seconds()
        if conflict:
            reasons.append("overlapping_intervals")
        mean = (
            finite_calculation(
                weighted / supported,
                lambda weights=weights, supported=supported: (
                    sum((Fraction(v) * Fraction(s) for v, s in weights), Fraction())
                    / Fraction(supported)
                ),
            )
            if supported
            else None
        )
        if supported and mean is None:
            reasons.append("numeric_overflow:time_weighted_mean")
        stats = statistics(
            [
                r["value"]
                for r in observations
                if r["value"] is not None and (kind == "steps" or r["date"] == str(day))
            ]
        )
        reasons.extend(statistics_reasons(stats))
        reasons.extend(code for row in observations for code in row["reason_codes"])
        results.append(
            Intraday(
                metric=kind,
                source=source,
                method_details=method_details(
                    payload_for(REGISTRY[kind], raw.raw_json),
                    raw.raw_json.get("dataSource"),
                ),
                definition=(
                    "linear_sample_interpolation:" if kind == "heart-rate" else "interval_rate:"
                )
                + method,
                unit="bpm" if kind == "heart-rate" else "count/s",
                time_basis="physical_seconds",
                date=str(day),
                day_seconds=duration,
                supported_seconds=supported,
                gap_seconds=duration - supported - conflict,
                conflict_seconds=conflict,
                cells=cells,
                time_weighted_mean=mean,
                statistics=stats,
                statistics_unit="bpm" if kind == "heart-rate" else "count",
                statistics_basis="samples_inside_day"
                if kind == "heart-rate"
                else "source_interval_counts",
                evidence=observations,
                reason_codes=sorted(set(reasons)),
            )
        )
    return results
