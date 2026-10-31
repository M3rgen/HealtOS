"""Version two interpretation contracts; version one archive responses stay unchanged."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue

from app.services.canonical_types import SleepEvidence, Span

ACTIONS = frozenset(
    {
        "baseline",
        "analyze",
        "sleep",
        "intraday",
        "sessions",
        "physiology",
        "longitudinal",
        "relationship",
    }
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Contract):
    record: str
    date: str
    value: FiniteFloat | None = None
    start: str | None = None
    end: str | None = None
    source_timezone: str | None = None
    field: str | None = None
    reason_codes: list[str] = Field(default_factory=list)


class Statistics(Contract):
    count: int
    median: FiniteFloat | None = None
    mad: FiniteFloat | None = None
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None
    p10: FiniteFloat | None = None
    p90: FiniteFloat | None = None


class Frame(Contract):
    metric: str
    source: str
    definition: str
    unit: str
    method_version: str = "interpretation/2"
    method_details: dict[str, JsonValue] = Field(default_factory=dict)
    time_basis: str
    reason_codes: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class Baseline(Frame):
    reference_method: Literal["median-mad-linear-quantile-midrank/2"] = (
        "median-mad-linear-quantile-midrank/2"
    )
    kind: Literal["baseline"] = "baseline"
    date: str
    value: FiniteFloat | None
    eligible: bool
    support_days: int
    expected_days: int
    minimum_support: int
    cold_start_support: int
    comparison_dates: list[str]
    statistics: Statistics
    percentile: FiniteFloat | None
    robust_z: FiniteFloat | None


class SleepSummary(Frame):
    kind: Literal["sleep"] = "sleep"
    date: str
    value: FiniteFloat | None
    sleep: SleepEvidence


class DistributionCell(Span):
    value: FiniteFloat | None
    state: Literal["supported", "gap", "conflict"]


class Intraday(Frame):
    kind: Literal["intraday"] = "intraday"
    date: str
    day_seconds: FiniteFloat
    supported_seconds: FiniteFloat
    gap_seconds: FiniteFloat
    conflict_seconds: FiniteFloat
    cells: list[DistributionCell]
    statistics: Statistics
    statistics_unit: Literal["bpm", "count"]
    statistics_basis: Literal["samples_inside_day", "source_interval_counts"]
    time_weighted_mean: FiniteFloat | None
    exact_bursts: None = None


class SessionSummary(Frame):
    kind: Literal["session"] = "session"
    start: str
    end: str
    interval_seconds: FiniteFloat
    reported_active_seconds: FiniteFloat | None
    exercise_type: str | None
    calories_kcal: FiniteFloat | None
    distance_meters: FiniteFloat | None
    overlaps: list[str]
    continuous_running_seconds: None = None


class Longitudinal(Frame):
    interval_seconds: FiniteFloat | None = None
    kind: Literal["longitudinal", "physiology"]
    state: Literal["available", "absent", "unsupported"]
    statistics: Statistics
    first_date: str | None
    last_date: str | None
    age_days: int | None
    freshness: Literal["recent", "stale", "absent"]
    change: FiniteFloat | None


class Pair(Contract):
    x_date: str
    y_date: str
    x: FiniteFloat
    y: FiniteFloat
    x_records: list[str]
    y_records: list[str]


class Relationship(Contract):
    kind: Literal["relationship"] = "relationship"
    metric: str
    other_metric: str
    source: str
    other_source: str
    definition: str
    other_definition: str
    unit: str
    other_unit: str
    lag_days: int
    method_version: Literal["spearman-calendar-split/2"] = "spearman-calendar-split/2"
    eligible: bool
    support_pairs: int
    expected_pairs: int
    minimum_pairs: int
    correlation: FiniteFloat | None
    split_correlations: list[FiniteFloat | None]
    stable: bool
    pairs: list[Pair]
    reason_codes: list[str]
    limits: list[str]


class Candidate(Contract):
    rank: int
    code: str
    summary_index: int
    evidence_dates: list[str]
    priority_score: FiniteFloat
    suggestion: str


Summary = Baseline | SleepSummary | Intraday | SessionSummary | Longitudinal | Relationship


class Interpretation(Contract):
    schema_version: Literal["2"] = "2"
    action: str
    timezone: str
    start_date: str
    end_date: str
    source_selection: Literal["none"] = "none"
    statistics_scope: Literal["whole_requested_window"] = "whole_requested_window"
    summaries: list[Summary]
    candidates: list[Candidate] = Field(default_factory=list)
    next_cursor: str | None = None
    total_summaries: int
    summary_offset: int
    reason_codes: list[str] = Field(default_factory=list)


class Refusal(Contract):
    schema_version: Literal["2"] = "2"
    error: str = "Yorum sorgusu tamamlanamadı; aralığı veya sayfa boyutunu daralt."
    code: str
    reason_codes: list[str]
