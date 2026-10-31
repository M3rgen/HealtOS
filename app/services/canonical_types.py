"""Version-one source daily contract, independent of the dashboard's legacy scalars."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue

Metric = Literal["steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2"]
# The day and every nested value share the same interpretation rules.
DailyMethod = Literal["source-daily/6"]


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record: str
    value: FiniteFloat | None
    source_start: str | None
    source_end: str | None
    source_timezone: str | None
    source_day: str | None = None
    field: str | None = None
    start: str | None = None
    end: str | None = None
    source_value: FiniteFloat | None = None
    assumptions: list[str] | None = None


class Span(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str


class TimedStage(Span):
    stage: str


class SleepSessionEvidence(Span):
    interval_seconds: FiniteFloat = Field(gt=0)
    stage_totals_seconds: dict[str, FiniteFloat | None]
    supplied_asleep_seconds: FiniteFloat | None
    payload_period_seconds: FiniteFloat | None
    timeline: list[TimedStage] | None


class TimelineCell(Span):
    state: Literal["known", "unknown", "conflict", "between_sessions"]
    stages: list[str]
    session_count: int = Field(ge=0)
    stage_count: int = Field(ge=0)


class SleepEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessions: list[SleepSessionEvidence]
    episodes: list[Span]
    timeline: list[TimelineCell] | None
    reason_codes: list[str]
    timeline_stage_seconds: dict[str, FiniteFloat] | None
    stage_total_differences_seconds: dict[str, FiniteFloat | None] | None
    unknown_seconds: FiniteFloat = Field(ge=0)
    known_seconds: FiniteFloat = Field(ge=0)
    conflict_seconds: FiniteFloat = Field(ge=0)
    covered_session_seconds: FiniteFloat = Field(ge=0)
    session_total_seconds: FiniteFloat = Field(ge=0)
    stage_overlap_seconds: FiniteFloat | None
    session_overlap_seconds: FiniteFloat = Field(ge=0)
    between_sessions_seconds: FiniteFloat = Field(ge=0)
    estimated_asleep_seconds: FiniteFloat | None
    timed_asleep_seconds: FiniteFloat | None
    estimate_minus_timed_asleep_seconds: FiniteFloat | None


class SourceValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: Metric
    source: str
    source_metadata: JsonValue
    definition: str
    unit: Literal["count", "s", "bpm", "ms", "%"]
    time_basis: Literal["civil_day_summary", "interval_allocation", "wake_day"]
    method_version: DailyMethod
    value: FiniteFloat | None
    state: Literal["measured", "estimated", "conflict", "no_value"]
    uncertainties: list[str]
    observations: list[Observation]
    measurement_metadata: JsonValue | None = None
    sleep: SleepEvidence | None = None


class ManualObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record: str
    value: FiniteFloat
    measured_at: str


class ManualProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    source: Literal["manuel"]
    method_version: DailyMethod
    time_basis: Literal["latest_before_day_stop"]
    observations: list[ManualObservation]
    unit: Literal["kg", "cm", "%"]
    value: FiniteFloat | None
    uncertainties: list[str]


class CanonicalDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str
    timezone: str
    method_version: DailyMethod
    day_seconds: FiniteFloat = Field(ge=0)
    streams: list[SourceValue]
    manual_profile: list[ManualProfile]
    missing_metrics: list[Metric]
    source_selection: Literal["none"]
    intraday_samples: Literal["not_used_as_daily_summaries"]
