"""Sleep evidence: missing measurements and invalid numbers remain distinct."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, DecimalException, InvalidOperation, Underflow, localcontext
from math import isfinite

from app.parsers import numeric
from app.parsers.common import parse_datetime

SLEEP_NUMERIC_ERROR = "numeric_overflow:sleep_estimate"


def _finite_seconds(value: Decimal) -> Decimal:
    """Validate before clamping; a nonzero value must not become a float zero."""
    if not value.is_finite():
        raise InvalidOperation("Nonfinite sleep evidence")
    converted = float(value)
    if not isfinite(converted) or (converted == 0 and value != 0):
        raise InvalidOperation("Unrepresentable sleep evidence")
    return value


def sleep_minutes_seconds(value: object) -> Decimal | None:
    """Scale in a bounded context, rejecting underflow as well as overflow."""
    with localcontext(Context()) as context:
        context.traps[Underflow] = True
        minutes = numeric.number(value)
        return _finite_seconds(minutes * 60) if minutes is not None else None


def _sleep_stages(payload: dict, *, strict: bool = False) -> dict[str, Decimal]:
    """Keep invalid read totals as NaN evidence; writes reject them instead.

    Readers must validate the totals they use. This preserves independent sound
    stage totals for the timeline even when another stage's number is unusable.
    """
    totals: dict[str, Decimal] = {}

    def add(entry: dict, *, minutes: bool = False) -> None:
        label = entry.get("type") if minutes else entry.get("stage") or entry.get("type")
        name = str(label or "").lower()
        if not name:
            return
        try:
            with localcontext(Context()) as context:
                context.traps[Underflow] = True
                if minutes:
                    value = numeric.number(entry.get("minutes"))
                    duration = value * 60 if value is not None else None
                else:
                    supplied = entry.get("duration")
                    if supplied is None:
                        supplied = entry.get("durationSeconds")
                    duration = numeric.duration_seconds(supplied)
                    if duration is None:
                        start = parse_datetime(entry.get("startTime"))
                        end = parse_datetime(entry.get("endTime"))
                        if start and end:
                            duration = Decimal(str((end - start).total_seconds()))
                if duration is not None:
                    _finite_seconds(duration)
                    total = totals.get(name, Decimal(0)) + duration
                    totals[name] = _finite_seconds(total)
        except DecimalException:
            if strict:
                raise
            totals[name] = Decimal("NaN")

    summary = payload.get("summary", {})
    if isinstance(summary, dict) and isinstance(summary.get("stagesSummary"), list):
        for entry in summary["stagesSummary"]:
            if isinstance(entry, dict):
                add(entry, minutes=True)
        if totals:
            return totals
    entries = payload.get("stages") or payload.get("sleepStages") or []
    if isinstance(entries, dict):
        entries = entries.get("stages") or entries.get("data") or []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                add(entry)
    return totals


def sleep_period_seconds(payload: dict, start: datetime, end: datetime) -> tuple[Decimal, bool]:
    """The one duration rule for writes and reads; True when the payload supplied it.

    Top-level duration has no Google schema meaning, so readers mark its use.
    """
    duration = numeric.duration_seconds(payload.get("duration"))
    if duration is not None:
        return duration, True
    return Decimal(str((end - start).total_seconds())), False


@dataclass(frozen=True)
class SleepAsleep:
    """None seconds with a period means unknown awake; no period means invalid evidence."""

    seconds: Decimal | None
    period: Decimal | None
    assumptions: tuple[str, ...] = ()


def sleep_asleep_seconds(payload: dict, start: datetime, end: datetime) -> SleepAsleep:
    """Prefer Google's asleep summary, else subtract AWAKE and RESTLESS from the period.

    A missing awake measurement keeps the period as an explicitly marked upper bound.
    Invalid used numeric evidence never supports that fallback. A sound summary, like
    the subtraction, is independent of stage totals it does not use.
    """
    try:
        with localcontext(Context()) as context:
            context.traps[Underflow] = True
            period, supplied = sleep_period_seconds(payload, start, end)
            _finite_seconds(period)
            summary = payload.get("summary")
            asleep = (
                sleep_minutes_seconds(summary.get("minutesAsleep"))
                if isinstance(summary, dict)
                else None
            )
            if asleep is not None:
                return SleepAsleep(asleep, period)
            assumptions = ("duration_from_payload",) if supplied else ()
            stages = _sleep_stages(payload)
            if not stages:
                return SleepAsleep(None, period, (*assumptions, "awake_unknown"))
            awake = stages["awake"] if "awake" in stages else stages.get("wake")
            if awake is None:
                assumptions = (*assumptions, "awake_unknown")
            restless = _finite_seconds(stages.get("restless", Decimal(0)))
            awake = _finite_seconds(awake or Decimal(0))
            asleep = _finite_seconds(period - awake - restless)
            return SleepAsleep(asleep, period, assumptions)
    except DecimalException:
        # Only source parsing/arithmetic is contained here; database failures propagate.
        return SleepAsleep(None, None, (SLEEP_NUMERIC_ERROR,))
