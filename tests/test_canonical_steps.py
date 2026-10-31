"""Current raw step corrections through ingestion and production interpretation."""

from datetime import timedelta

import pytest
from derived_records import DAY, T0, snapshot, steps, store, streams
from interpretation_records import call
from interpretation_records import production_reader as production_reader


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("field", ["count", "countSum", "steps"])
@pytest.mark.parametrize("supplied", ["1e100", "-1e100", "1e-100"])
async def test_current_steps_replace_old_value_and_day(sessions, initial, offset, field, supplied):
    if not initial:
        await store(sessions, "steps", steps())
    last = str(DAY + timedelta(days=1))
    await call("daily", end_date=last)
    for count in (supplied, 0):
        record = steps(count=count, start=T0 + timedelta(days=offset))
        record["steps"][field] = record["steps"].pop("count")
        stats = await store(sessions, "steps", record)
        assert stats.normalization_skipped == int(count != 0 and supplied != "1e-100")
        async with sessions() as session:
            before = await snapshot(session)
        card = (await call("daily", end_date=last))["derived"]
        assert not offset or streams(card, "steps", 0) == []
        row = streams(card, "steps", offset)[0]
        assert row["value"] == row["observations"][0]["source_value"] == float(count)
        assert row["observations"][0]["start"] == record["steps"]["interval"]["startTime"]
        for action in ("baseline", "analyze"):
            result = await call(action, metric="steps", end_date=last)
            assert [r["value"] for r in result["summaries"]] == [float(count)]
        async with sessions() as session:
            assert await snapshot(session) == before
