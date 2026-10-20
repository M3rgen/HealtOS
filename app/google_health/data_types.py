from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from app.core.timeutil import as_utc


def query_time(value: datetime) -> datetime:
    """The instant a request bound actually names: UTC, truncated to whole seconds.

    Request bounds are sent with second precision, so ``10:00:00.5`` asks Google for
    ``10:00:00``. Anything that reasons about what a request covered (deletion marking)
    must use these bounds, not the finer datetimes the caller passed in.
    """
    return as_utc(value).replace(microsecond=0)


def query_iso(value: datetime) -> str:
    """RFC 3339 form of ``query_time`` as written into filters and rollup ranges."""
    return query_time(value).isoformat().replace("+00:00", "Z")


class RecordKind(StrEnum):
    interval = "interval"
    sample = "sample"
    session = "session"
    daily = "daily"
    reference = "reference"


class AccessMethod(StrEnum):
    list = "list"
    reconcile = "reconcile"
    rollup = "rollup"


ACTIVITY_SCOPE = "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"
METRICS_SCOPE = (
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
)
SLEEP_SCOPE = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
NUTRITION_SCOPE = "https://www.googleapis.com/auth/googlehealth.nutrition.readonly"
ECG_SCOPE = "https://www.googleapis.com/auth/googlehealth.ecg.readonly"
IRN_SCOPE = "https://www.googleapis.com/auth/googlehealth.irn.readonly"
SETTINGS_SCOPE = "https://www.googleapis.com/auth/googlehealth.settings.readonly"


@dataclass(frozen=True)
class DataTypeSpec:
    name: str
    kind: RecordKind
    scope: str
    webhook: bool = False
    access_method: AccessMethod = AccessMethod.list
    max_window_days: int | None = None
    time_filtered: bool = True
    personal_health_data: bool = True

    @property
    def payload_key(self) -> str:
        return self.name.replace("-", "_")

    @property
    def json_key(self) -> str:
        first, *rest = self.name.split("-")
        return first + "".join(word.title() for word in rest)

    def filter_for(self, start_iso: str, end_iso: str) -> str | None:
        if not self.time_filtered:
            return None
        prefix = self.payload_key
        if self.kind == RecordKind.daily:
            field = f"{prefix}.date"
            start_date = date.fromisoformat(start_iso[:10])
            end_date = date.fromisoformat(end_iso[:10])
            # Daily records use civil dates. Ceil a partial timestamp to the next
            # date so a same-day incremental/backfill window is non-empty and valid.
            if end_date <= start_date or end_iso[11:19] != "00:00:00":
                end_date += timedelta(days=1)
            return f'{field} >= "{start_date}" AND {field} < "{end_date}"'
        if self.kind == RecordKind.sample:
            field = f"{prefix}.sample_time.physical_time"
        elif self.name == "electrocardiogram":
            return f'electrocardiogram.interval.start_time >= "{start_iso}"'
        elif self.name == "sleep":
            field = "sleep.interval.end_time"
        elif self.kind == RecordKind.session:
            field = f"{prefix}.interval.civil_start_time"
            start_iso = start_iso.replace("Z", "")
            end_iso = end_iso.replace("Z", "")
        else:
            field = f"{prefix}.interval.start_time"
        return f'{field} >= "{start_iso}" AND {field} < "{end_iso}"'


_SPECS = [
    DataTypeSpec("active-energy-burned", RecordKind.interval, ACTIVITY_SCOPE),
    DataTypeSpec("active-minutes", RecordKind.interval, ACTIVITY_SCOPE),
    DataTypeSpec("active-zone-minutes", RecordKind.interval, ACTIVITY_SCOPE, True),
    DataTypeSpec("activity-level", RecordKind.interval, ACTIVITY_SCOPE, True),
    DataTypeSpec("altitude", RecordKind.interval, ACTIVITY_SCOPE, True),
    DataTypeSpec("blood-glucose", RecordKind.sample, METRICS_SCOPE, True),
    DataTypeSpec("body-fat", RecordKind.sample, METRICS_SCOPE, True),
    DataTypeSpec("core-body-temperature", RecordKind.sample, METRICS_SCOPE),
    DataTypeSpec(
        "calories-in-heart-rate-zone",
        RecordKind.interval,
        ACTIVITY_SCOPE,
        True,
        access_method=AccessMethod.rollup,
        max_window_days=14,
    ),
    DataTypeSpec("daily-heart-rate-variability", RecordKind.daily, METRICS_SCOPE, True),
    DataTypeSpec("daily-heart-rate-zones", RecordKind.daily, METRICS_SCOPE, True),
    DataTypeSpec("daily-oxygen-saturation", RecordKind.daily, METRICS_SCOPE, True),
    DataTypeSpec("daily-respiratory-rate", RecordKind.daily, METRICS_SCOPE, True),
    DataTypeSpec("daily-resting-heart-rate", RecordKind.daily, METRICS_SCOPE, True),
    DataTypeSpec("daily-sleep-temperature-derivations", RecordKind.daily, METRICS_SCOPE, True),
    DataTypeSpec("daily-vo2-max", RecordKind.daily, ACTIVITY_SCOPE),
    DataTypeSpec("distance", RecordKind.interval, ACTIVITY_SCOPE, True),
    DataTypeSpec("electrocardiogram", RecordKind.session, ECG_SCOPE),
    DataTypeSpec("exercise", RecordKind.session, ACTIVITY_SCOPE, True),
    DataTypeSpec("floors", RecordKind.interval, ACTIVITY_SCOPE, True, AccessMethod.reconcile),
    DataTypeSpec(
        "food",
        RecordKind.reference,
        NUTRITION_SCOPE,
        time_filtered=False,
        personal_health_data=False,
    ),
    DataTypeSpec(
        "food-measurement-unit",
        RecordKind.reference,
        NUTRITION_SCOPE,
        time_filtered=False,
        personal_health_data=False,
    ),
    DataTypeSpec("heart-rate", RecordKind.sample, METRICS_SCOPE, True),
    DataTypeSpec("heart-rate-variability", RecordKind.sample, METRICS_SCOPE, True),
    DataTypeSpec("height", RecordKind.sample, METRICS_SCOPE, True),
    DataTypeSpec("hydration-log", RecordKind.session, NUTRITION_SCOPE, True),
    DataTypeSpec("irregular-rhythm-notification", RecordKind.session, IRN_SCOPE),
    DataTypeSpec("nutrition-log", RecordKind.session, NUTRITION_SCOPE, True),
    DataTypeSpec("oxygen-saturation", RecordKind.sample, METRICS_SCOPE),
    DataTypeSpec("respiratory-rate-sleep-summary", RecordKind.sample, METRICS_SCOPE, True),
    DataTypeSpec("run-vo2-max", RecordKind.sample, ACTIVITY_SCOPE, True),
    DataTypeSpec("sedentary-period", RecordKind.interval, ACTIVITY_SCOPE, True),
    DataTypeSpec("sleep", RecordKind.session, SLEEP_SCOPE, True),
    DataTypeSpec("steps", RecordKind.interval, ACTIVITY_SCOPE, True),
    DataTypeSpec("swim-lengths-data", RecordKind.interval, ACTIVITY_SCOPE),
    DataTypeSpec("time-in-heart-rate-zone", RecordKind.interval, ACTIVITY_SCOPE),
    DataTypeSpec(
        "total-calories",
        RecordKind.interval,
        ACTIVITY_SCOPE,
        access_method=AccessMethod.rollup,
        max_window_days=14,
    ),
    DataTypeSpec("vo2-max", RecordKind.sample, ACTIVITY_SCOPE),
    DataTypeSpec("weight", RecordKind.sample, METRICS_SCOPE, True),
]
REGISTRY = {spec.name: spec for spec in _SPECS}


def supported_data_types(
    extra: list[str] | None = None, enabled: list[str] | None = None
) -> dict[str, DataTypeSpec]:
    specs = dict(REGISTRY)
    for name in extra or []:
        specs.setdefault(name, DataTypeSpec(name, RecordKind.interval, METRICS_SCOPE))
    if enabled:
        unknown = set(enabled) - set(specs)
        if unknown:
            raise ValueError(f"Unknown enabled data types: {', '.join(sorted(unknown))}")
        specs = {name: specs[name] for name in enabled}
    return specs


def mirror_data_types(
    extra: list[str] | None = None, enabled: list[str] | None = None
) -> dict[str, DataTypeSpec]:
    return {
        name: spec
        for name, spec in supported_data_types(extra, enabled).items()
        if spec.personal_health_data
    }


def readonly_scopes(extra: list[str] | None = None, enabled: list[str] | None = None) -> list[str]:
    scopes = {spec.scope for spec in supported_data_types(extra, enabled).values()}
    # Required by users.pairedDevices.list. It is read-only and lets the mirror
    # retain device provenance without requesting profile or write access.
    scopes.add(SETTINGS_SCOPE)
    return sorted(scopes)
