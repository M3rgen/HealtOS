"""Explicit supported fields for physiology and longitudinal quantities; no guessed fields."""

from collections import defaultdict
from zoneinfo import ZoneInfo

from app.core.timeutil import as_utc
from app.google_health.data_types import REGISTRY, WAKE_DATED_TYPES, RecordKind
from app.parsers.common import parse_date, parse_datetime, payload_for
from app.services.canonical_daily import bounds
from app.services.finite_math import finite_difference
from app.services.interpretation_inputs import (
    BudgetError,
    evidence,
    fallback_source,
    method_details,
    method_key,
    scaled_measurement,
)
from app.services.interpretation_sessions import session_summaries as session_summaries
from app.services.interpretation_stats import statistics, statistics_reasons
from app.services.interpretation_types import Longitudinal

# These are the fields already consumed by normalize.py/dashboard.py. Definitions remain
# separate even when two fields happen to have the same value or unit.
PHYSIOLOGY = {
    "daily-respiratory-rate": (
        "breaths/min",
        {"breathsPerMinute": 1, "fullSleepStats.breathsPerMinute": 1},
    ),
    "respiratory-rate-sleep-summary": (
        "breaths/min",
        {"breathsPerMinute": 1, "fullSleepStats.breathsPerMinute": 1},
    ),
    "core-body-temperature": ("°C", {"temperatureCelsius": 1}),
    "daily-sleep-temperature-derivations": (
        "°C",
        {"temperatureCelsius": 1, "nightlyTemperatureCelsius": 1, "baselineTemperatureCelsius": 1},
    ),
    "vo2-max": ("ml/kg/min", {"vo2Max": 1}),
    "daily-vo2-max": ("ml/kg/min", {"vo2Max": 1}),
    "run-vo2-max": ("ml/kg/min", {"runVo2Max": 1}),
}
QUANTITIES = {
    "active-energy-burned": ("kcal", {"kcal": 1, "kcalSum": 1}),
    "total-calories": ("kcal", {"kcal": 1, "kcalSum": 1}),
    "distance": ("m", {"millimeters": 0.001}),
    "weight": ("kg", {"weightGrams": 0.001}),
    "height": ("cm", {"heightMillimeters": 0.1}),
    "body-fat": ("%", {"percentage": 1}),
}
FRESH_DAYS = 7
VO2_FRESH_DAYS = 30
MANUAL_UNITS = {"weight": "kg", "height": "cm", "body_fat": "%", "muscle": "%"}


def nested(payload, field):
    value = payload
    for key in field.split("."):
        if not isinstance(value, dict) or key not in value:
            return None, False
        value = value[key]
    return value, True


def _overlapping_intervals(rows):
    """Detect any strict overlap in O(n log n) time without allocating pairs.

    Track the furthest end among earlier starts so nested intervals work too.
    Equal endpoints expire before the next interval and never count as overlaps.
    Input order is left intact for statistics, changes and evidence ordering.
    """
    furthest_end = None
    for row in sorted(rows, key=lambda row: row["start"]):
        if furthest_end is not None and row["start"] < furthest_end:
            return True
        if furthest_end is None or row["end"] > furthest_end:
            furthest_end = row["end"]
    return False


def _measurement_metric(data_type):
    """Use the manual metric spelling when comparing measurement presence.

    Google's body-fat type and the manual body_fat metric describe the same
    requested measurement. This identity is only for selection and absence;
    output groups retain their original metric, source and definition.
    """
    return data_type.replace("-", "_")


def _measurement_plan(groups, query, catalog, max_summaries):
    """Count the complete filtered output before statistics or model allocation.

    Real groups suppress absent summaries even if filtered out, matching the
    reader's existing source/definition semantics. Missing summaries use their
    own unknown source and bare metric definition when applying those filters.
    Pagination never changes the whole-query ceiling supplied by the reader.
    """
    requested = [query.data_type] if query.data_type else list(catalog)
    if query.action == "longitudinal" and query.data_type == "muscle":
        catalog = {**catalog, "muscle": ("%", {})}

    def matches(source, definition):
        return (not query.source or source == query.source) and (
            not query.definition or definition == query.definition
        )

    # Decide absence before source/definition filters, including manual aliases.
    present = {_measurement_metric(key[0]) for key in groups}
    missing = [kind for kind in requested if _measurement_metric(kind) not in present]
    selected = {key: rows for key, rows in groups.items() if matches(key[1], key[2])}
    absent = [
        (kind, catalog[kind][0]) for kind in missing if kind in catalog and matches("unknown", kind)
    ]
    if len(selected) + len(absent) > max_summaries:
        raise BudgetError("summary_budget_exceeded")
    return selected, absent


def measurement_summaries(inputs, query, *, max_summaries):
    """Summarize eligible measurements within the reader's whole-query budget."""
    first, last = query.date_range()
    catalog = PHYSIOLOGY if query.action == "physiology" else QUANTITIES
    groups = defaultdict(list)
    methods = {}
    for row in inputs.rows:
        kind = row.data_type
        if kind not in catalog or (query.data_type and query.data_type != kind):
            continue
        spec = REGISTRY[kind]
        payload = payload_for(spec, row.raw_json)
        stamp = as_utc(row.source_start_time)
        if stamp is None:
            continue
        # A sleep summary counts on the day its sleep ended (WAKE_DATED_TYPES), like sleep.
        dated = (as_utc(row.source_end_time) or stamp) if kind in WAKE_DATED_TYPES else stamp
        day = (
            parse_date(payload.get("date"))
            if spec.kind == RecordKind.daily
            else dated.astimezone(ZoneInfo(query.timezone)).date()
        )
        if day is None:
            continue
        if spec.kind == RecordKind.interval:
            left, _ = bounds(first, query.timezone)
            _, stop = bounds(last, query.timezone)
            end = as_utc(row.source_end_time)
            if end is None or end <= stamp or stamp >= stop or end <= left:
                continue
        elif not first <= day <= last:
            continue
        unit, fields = catalog[kind]
        source = inputs.source(
            kind,
            row.raw_json.get("dataSource"),
            fallback_source(row),
        )
        method = method_key(payload, row.raw_json.get("dataSource"))
        supported = False
        for field, factor in fields.items():
            supplied, exists = nested(payload, field)
            if not exists:
                continue
            supported = True
            value, reasons = scaled_measurement(supplied, factor)
            if payload.get("unit") not in (None, unit):
                value = None
                reasons.append("unsupported_unit")
            scope = (
                "interval_amount"
                if spec.kind == RecordKind.interval
                else "civil_day_summary"
                if spec.kind == RecordKind.daily
                else "sample"
            )
            definition = kind + ":" + field + ":" + method
            if spec.kind == RecordKind.interval:
                definition += ":interval_seconds=" + str((end - stamp).total_seconds())
            key = (kind, source, definition, unit, scope)
            groups[key].append(evidence(row, day, value, field, reasons))
            methods[key] = method_details(payload, row.raw_json.get("dataSource"))
        if not supported:
            groups[(kind, source, kind + ":unsupported:" + method, unit, "unsupported")].append(
                evidence(row, day, reasons=["unsupported_payload_shape"])
            )
    if query.action == "longitudinal":
        requested_metric = _measurement_metric(query.data_type) if query.data_type else None
        for row in inputs.measurements:
            stamp = as_utc(row.measured_at)
            if (
                row.metric in MANUAL_UNITS
                and (requested_metric is None or requested_metric == row.metric)
                and first <= stamp.astimezone(ZoneInfo(query.timezone)).date() <= last
            ):
                key = (
                    row.metric,
                    "manuel",
                    "manual_measurement",
                    MANUAL_UNITS[row.metric],
                    "sample",
                )
                groups[key].append(
                    dict(
                        record=str(row.id),
                        date=str(stamp.astimezone(ZoneInfo(query.timezone)).date()),
                        value=float(row.value),
                        start=stamp.isoformat(),
                        end=stamp.isoformat(),
                        field=row.metric,
                        reason_codes=[],
                    )
                )
    groups, absent = _measurement_plan(groups, query, catalog, max_summaries)
    results = []
    for (metric, source, definition, unit, scope), rows in sorted(groups.items()):
        rows.sort(key=lambda r: (r["date"], r["start"] or "", r["record"]))
        reasons = {code for r in rows for code in r["reason_codes"]}
        instants = defaultdict(list)
        for row in rows:
            instants[(row["start"], row["end"])].append(row)
        valid = []
        for readings in instants.values():
            values = {r["value"] for r in readings if r["value"] is not None}
            if len(values) > 1:
                reasons.add("conflicting_values_at_same_time")
            elif len(values) == 1:
                valid.append(next(r for r in readings if r["value"] is not None))
        if scope == "interval_amount":
            reasons.add("variable_interval_scope_no_daily_total")
            if _overlapping_intervals(valid):
                reasons.add("overlapping_intervals")
            reasons.add("source_intervals_not_prorated_to_window")
        if "vo2" in metric:
            reasons.add("estimate_method_specific_not_direct_oxygen_measurement")
        details = methods.get((metric, source, definition, unit, scope), {})
        reasons.add("method_specific" if details.get("measurementMethod") else "method_unspecified")
        latest = max((r["date"] for r in valid), default=None)
        age = (last - parse_date(latest)).days if latest else None
        threshold = VO2_FRESH_DAYS if "vo2" in metric else FRESH_DAYS
        state = "available" if valid else "unsupported"
        interval_seconds = None
        if scope == "interval_amount":
            a, b = parse_datetime(rows[0]["start"]), parse_datetime(rows[0]["end"])
            if a is not None and b is not None:
                interval_seconds = (b - a).total_seconds()
        stats = statistics([r["value"] for r in valid])
        reasons.update(statistics_reasons(stats))
        change = None
        if len(valid) > 1 and scope != "interval_amount":
            change = finite_difference(valid[-1]["value"], valid[0]["value"])
            if change is None:
                reasons.add("numeric_overflow:change")
        results.append(
            Longitudinal(
                metric=metric,
                source=source,
                method_details=methods.get((metric, source, definition, unit, scope), {}),
                definition=definition,
                unit=unit,
                time_basis=scope,
                kind=query.action,
                interval_seconds=interval_seconds,
                state=state,
                statistics=stats,
                first_date=min((r["date"] for r in valid), default=None),
                last_date=latest,
                age_days=age,
                freshness="absent" if age is None else "recent" if age <= threshold else "stale",
                change=change,
                evidence=rows,
                reason_codes=sorted(reasons),
            )
        )
    for kind, unit in absent:
        results.append(
            Longitudinal(
                metric=kind,
                source="unknown",
                definition=kind,
                unit=unit,
                time_basis="unknown",
                kind=query.action,
                state="absent",
                statistics=statistics([]),
                first_date=None,
                last_date=None,
                age_days=None,
                freshness="absent",
                change=None,
                reason_codes=["no_eligible_records_in_window"],
            )
        )
    return results
