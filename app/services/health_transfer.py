"""Value-free receipts for tool results handed to the local model bridge.

Counts are transmitted representations, never archive totals or unique source records.
Repeated pages/definitions/days may describe the same source. Empty day placeholders do
not count as daily summaries; null-valued derived observations still carry source data.
Inventory/coverage describe metadata, not transmitted raw records. Nothing is persisted.
"""

import json
import logging
from datetime import UTC, date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.google_health.data_types import REGISTRY
from app.services.codex_health import ArchiveQuery
from app.services.interpretation_types import ACTIONS

logger = logging.getLogger(__name__)
METRICS = ("steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2")
COUNT_KEYS = (
    "raw_records",
    "daily_summaries",
    "profile_measurements",
    "chart_points",
    "derived_summaries",
    "derived_observations",
    "metadata_items",
)
# Only fixed schema/catalogue field names may reach logs. Dynamic map keys become '*',
# including source metadata, arbitrary Google payload keys and sleep-stage labels.
SAFE_KEYS = frozenset(
    """
    method_details measurementMethod measurementLocation recordingMethod
    dailyRestingHeartRateMetadata
    statistics_unit statistics_basis
    x_records y_records summary_offset reference_method
    action summaries candidates statistics_scope next_cursor total_summaries kind eligible
    support_days expected_days minimum_support cold_start_support comparison_dates statistics
    median mad minimum maximum p10 p90 percentile robust_z time_weighted_mean supported_seconds
    gap_seconds cells exact_bursts reported_active_seconds exercise_type calories_kcal
    distance_meters
    overlaps continuous_running_seconds age_days freshness change other_metric other_source
    other_definition other_unit lag_days support_pairs expected_pairs minimum_pairs correlation
    split_correlations stable pairs x_date y_date x y limits rank summary_index evidence_dates
    priority_score suggestion evidence lookback_days
    today timezone streams data_type count first last undated_count note start_date end_date
    records start end source_timezone payload next_offset days date steps sleep_seconds
    sleep_assumptions resting_bpm hrv_ms spo2 partial status profile charts heart_rate
    sleep_method weight height body_fat muscle metric value measured_at source derived
    schema_version method_version source_selection state revision result day_seconds
    manual_profile missing_metrics intraday_samples source_metadata definition unit
    time_basis uncertainties observations measurement_metadata sleep record source_start
    source_end source_day field source_value assumptions sessions episodes timeline
    reason_codes interval_seconds stage_totals_seconds supplied_asleep_seconds
    payload_period_seconds stage stages session_count stage_count timeline_stage_seconds
    stage_total_differences_seconds unknown_seconds known_seconds conflict_seconds
    covered_session_seconds session_total_seconds stage_overlap_seconds
    session_overlap_seconds between_sessions_seconds estimated_asleep_seconds
    timed_asleep_seconds estimate_minus_timed_asleep_seconds computed_at requested_days
    closed_days metrics metric_key value_days days_without_value partial_today sampling
    retrieval exclusions source_regimes basis sample_count supported_seconds target_seconds
    longest_gap_seconds completed_with_records_days completed_empty_days incomplete_days
    not_attempted_days unknown_days policy quarantined without_raw_record source_deleted
    superseded manually_excluded invalid_shape origin device form_factor platform
    first_date last_date record_count error code accepted_data_types budget_bytes size_bytes
    suggested_range metric_keys name dataSource displayName formFactor detail observation_count
    heartRate dailyRestingHeartRate dailyHeartRateVariability dailyOxygenSaturation
    bodyFat sampleTime physicalTime interval startTime endTime beatsPerMinute bpm
    averageHeartRateVariabilityMilliseconds rootMeanSquareOfSuccessiveDifferencesMilliseconds
    deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds rmssd averagePercentage
    percentage average heightMillimeters weightGrams kilograms summary minutesAsleep
    duration seconds nanos sleepStages stagesSummary type data
""".split()
) | frozenset(key for spec in REGISTRY.values() for key in (spec.json_key, spec.payload_key))


def field_paths(value: object) -> list[str]:
    """Describe all fields, including nulls/empty containers, without copying map keys.

    [] collapses array indices; * hides unknown names; ** covers deeper arbitrary JSON.
    The depth bound affects only the receipt, never the transmitted tool result.
    """
    paths: set[str] = set()
    pending = [(value, "", 0)]
    while pending:
        item, path, depth = pending.pop()
        if depth >= 20:
            paths.add(path + ".**")
        elif isinstance(item, dict) and item:
            for key, child in item.items():
                safe = key if key in SAFE_KEYS else "*"
                pending.append((child, f"{path}.{safe}" if path else safe, depth + 1))
        elif isinstance(item, list) and item:
            pending.extend((child, path + "[]", depth + 1) for child in item)
        else:
            paths.add(path)
    return sorted(paths)


def _day(value: object, timezone: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(ZoneInfo(timezone)).date().isoformat()
    except (ValueError, KeyError, OverflowError):
        return None


def _range(days: list[str]) -> dict | None:
    return {"first": min(days), "last": max(days)} if days else None


def describe_result(arguments: object, data: dict, success: bool) -> dict:
    """Use the exact serialized result, plus validated requested bounds; no extra reads."""
    try:
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        query = ArchiveQuery.model_validate(arguments)
    except (ValueError, KeyError):
        query = None
    timezone = query.timezone if query else "UTC"
    counts = dict.fromkeys(COUNT_KEYS, 0)
    dates: list[str] = []
    metadata_dates: list[str] = []
    profile_dates: list[str] = []
    source_timezones: set[str] = set()

    def add_dates(row, keys, target=dates):
        for key in keys:
            day = _day(row.get(key), timezone)
            if day:
                target.append(day)

    def observation(row):
        add_dates(row, ("source_start", "source_end", "source_day", "start", "end", "measured_at"))
        zone = row.get("source_timezone")
        if isinstance(zone, str) and len(zone) <= 64:
            try:
                ZoneInfo(zone)
                source_timezones.add(zone)
            except (ValueError, KeyError):
                pass

    action = query.action if query else "invalid"
    if success and action == "records":
        rows = data.get("records", [])
        counts["raw_records"] = len(rows)
        for row in rows:
            add_dates(row, ("start", "end"))
            observation(row)
    elif success and action == "daily":
        for row in data.get("days", []):
            if any(row.get(metric) is not None for metric in METRICS):
                counts["daily_summaries"] += 1
                add_dates(row, ("date",))
        for row in data.get("profile", {}).values():
            counts["profile_measurements"] += 1
            add_dates(row, ("measured_at",), profile_dates)
        counts["chart_points"] = sum(
            value is not None for values in data.get("charts", {}).values() for value in values
        )
        if counts["chart_points"]:
            add_dates(data, ("end_date",))
        for day in data.get("derived", {}).get("days", []):
            result = day.get("result") or {}
            groups = result.get("streams", []) + result.get("manual_profile", [])
            counts["derived_summaries"] += len(groups)
            for group in groups:
                rows = group.get("observations", [])
                counts["derived_observations"] += len(rows)
                if rows or group.get("value") is not None:
                    add_dates(result, ("date",))
                # A budget summary keeps a manual profile's date without its observations.
                add_dates(group, ("measured_at",), profile_dates)
                for row in rows:
                    observation(row)
                    add_dates(row, ("measured_at",), profile_dates)
    elif success and action in ACTIONS:
        for summary in data.get("summaries", []):
            if summary.get("state") == "absent":
                counts["metadata_items"] += 1
                continue
            counts["derived_summaries"] += 1
            add_dates(summary, ("date", "start", "end", "first_date", "last_date"))
            for day in summary.get("comparison_dates", []):
                parsed = _day(day, timezone)
                if parsed:
                    dates.append(parsed)
            rows = summary.get("evidence", [])
            counts["derived_observations"] += len(rows)
            for row in rows:
                observation(row)
                add_dates(row, ("date",))
                if summary.get("source") == "manuel" and summary.get("kind") == "longitudinal":
                    add_dates(row, ("date",), profile_dates)
            pairs = summary.get("pairs", [])
            counts["derived_observations"] += len(pairs)
            for pair in pairs:
                add_dates(pair, ("x_date", "y_date"))
        for candidate in data.get("candidates", []):
            counts["derived_summaries"] += 1
            for day in candidate.get("evidence_dates", []):
                parsed = _day(day, timezone)
                if parsed:
                    dates.append(parsed)
    elif success and action == "inventory":
        counts["metadata_items"] = len(data.get("streams", []))
        for row in data.get("streams", []):
            add_dates(row, ("first", "last"), metadata_dates)
    elif success and action == "coverage":
        counts["metadata_items"] = len(data.get("metrics", []))
        for metric in data.get("metrics", []):
            for row in metric.get("source_regimes", []):
                add_dates(row, ("first_date", "last_date"), metadata_dates)
    dates.extend(profile_dates)
    return {
        "action": action,
        "outcome": "sent" if success else "refused",
        "counts": counts,
        "unique_source_records": None,  # no cross-representation identity proof in these results
        "requested": {"first": str(query.start_date), "last": str(query.end_date)}
        if query and action != "inventory"
        else None,
        "timezone": timezone,
        "source_timezones": sorted(source_timezones),
        "data_dates": _range(dates),
        "profile_dates": _range(profile_dates),
        "metadata_dates": _range(metadata_dates),
        "fields": field_paths(data),
    }


def neutral_receipt(success: bool) -> dict:
    """A value-free stand-in when a result cannot be described; nothing is inferred."""
    return {
        "action": "invalid",
        "outcome": "sent" if success else "refused",
        "counts": dict.fromkeys(COUNT_KEYS, 0),
        "unique_source_records": None,
        "requested": None,
        "timezone": "UTC",
        "source_timezones": [],
        "data_dates": None,
        "profile_dates": None,
        "metadata_dates": None,
        "fields": [],
    }


class HealthTransfer:
    """One accepted turn, owned by its relay; ids never come from questions or the bridge."""

    def __init__(self) -> None:
        self.turn = uuid4().hex
        self.status = "active"
        self.calls: list[dict] = []
        self.failed_sends = 0

    def snapshot(self) -> dict:
        return {
            "turn": self.turn,
            "status": self.status,
            "calls": self.calls.copy(),
            "failed_sends": self.failed_sends,
            "counts": {key: sum(call["counts"][key] for call in self.calls) for key in COUNT_KEYS},
        }

    def log(self) -> dict:
        receipt = self.snapshot()
        logger.info("Umay health transfer %s", json.dumps(receipt, ensure_ascii=False))
        return receipt
