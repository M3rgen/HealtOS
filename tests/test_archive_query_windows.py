"""Archive date limits use the production entry and preserve validation contracts."""

from datetime import date, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from interpretation_records import call, resting
from interpretation_records import production_reader as production_reader
from pydantic import ValidationError

from app.services import codex_health
from app.services.codex_health import read_health
from app.services.interpretation_types import ACTIONS, Interpretation, Refusal


@pytest.fixture
def last_day_clock(monkeypatch):
    class LastDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(9999, 12, 31, tzinfo=tz)

    monkeypatch.setattr(codex_health, "datetime", LastDay)


@pytest.mark.parametrize(
    "action,day,zone",
    [
        (a, d, z)
        for a in ["daily", "coverage", "records", *sorted(ACTIONS)]
        for d, z in [("0001-01-01", "Etc/GMT-14"), ("9999-12-31", "UTC")]
    ]
    + [(a, "9999-12-30", "Etc/GMT+12") for a in sorted(ACTIONS)],
)
async def test_overflow_keeps_archive_validation_contract(
    monkeypatch, last_day_clock, action, day, zone
):
    sessions, build = Mock(), AsyncMock()
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    monkeypatch.setattr(codex_health, "build_range", build)
    arguments = dict(action=action, start_date=day, end_date=day, timezone=zone)
    if action in ACTIONS:
        arguments.update(metric="steps", other_metric="resting_bpm")
        assert Refusal.model_validate(await read_health(arguments)).code == "invalid_query"
    else:
        arguments["data_type"] = "steps"
        with pytest.raises(ValidationError, match="okuma penceresi"):
            await read_health(arguments)
    sessions.assert_not_called()
    build.assert_not_called()


@pytest.mark.parametrize("action", ["daily", "coverage", "records", *sorted(ACTIONS)])
@pytest.mark.parametrize(
    "day,zone", [("0001-01-01", "UTC"), ("9999-12-29", "Etc/GMT+12"), ("9999-12-30", "Etc/GMT-14")]
)
async def test_representable_neighbors_reach_real_archive(
    sessions, last_day_clock, action, day, zone
):
    if day.startswith("0001") and action in ACTIONS:
        day = "0001-02-28" if action in {"baseline", "analyze"} else "0001-01-02"
    arguments = dict(start_date=day, end_date=day, timezone=zone)
    if action in ACTIONS:
        await resting(sessions, [0], start=date.fromisoformat(day))
        result = await call(
            action, **arguments, metric="resting_bpm", other_metric="resting_bpm", lags=[-14, 0, 14]
        )
        assert Interpretation.model_validate(result).action == action
    else:
        result = await read_health(dict(action=action, data_type="steps", **arguments))
        assert result["start_date"] == day and result["end_date"] == day
