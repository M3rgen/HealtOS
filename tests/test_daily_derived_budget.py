"""The daily tool's derived card is byte-bounded and moves detail instead of dropping it."""

import json
import time
from datetime import timedelta

from derived_records import DAY, PHONE, T0, manual, sleep, stage, steps, store

from app.services import codex_health
from app.services.codex_health import DERIVED_BUDGET_BYTES, read_health
from app.services.health_transfer import describe_result


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


def daily(first, last=DAY):
    return {"action": "daily", "start_date": str(first), "end_date": str(last), "timezone": "UTC"}


async def five_minute_steps(sessions, days):
    for offset in range(days):
        start = T0 - timedelta(days=offset)
        await store(
            sessions,
            "steps",
            *(
                steps(
                    f"d{offset}m{i}",
                    10 + i,
                    start + timedelta(minutes=5 * i),
                    start + timedelta(minutes=5 * i + 5),
                    PHONE,
                )
                for i in range(288)
            ),
        )


async def test_detail_is_full_when_it_fits_and_a_summary_when_it_does_not(sessions, monkeypatch):
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    await store(sessions, "steps", steps())
    await store(sessions, "sleep", sleep(stages=[stage("LIGHT", 1, 3)]))
    await manual(sessions, 70)
    sparse = (await read_health(daily(DAY)))["derived"]
    assert sparse["detail"] == "full" and size(sparse) <= DERIVED_BUDGET_BYTES
    full = {row["metric"]: row for row in sparse["days"][0]["result"]["streams"]}
    assert full["steps"]["observations"] and full["steps"]["source_metadata"]
    assert full["sleep_seconds"]["sleep"]["timeline"] is not None
    # Five-minute step intervals: a single day's observations alone exceed 64 KiB.
    await five_minute_steps(sessions, 3)
    one = await read_health(daily(DAY))
    assert one["derived"]["detail"] == "summary" and size(one["derived"]) <= DERIVED_BUDGET_BYTES
    assert one["derived"]["suggested_range"] is None  # one day: detail is in records/sleep
    wide = await read_health(daily(DAY - timedelta(days=2)))
    card = wide["derived"]
    assert card["detail"] == "summary" and size(card) <= DERIVED_BUDGET_BYTES
    assert card["reason_codes"] == ["derived_detail_budget_exceeded"]
    assert card["size_bytes"] > DERIVED_BUDGET_BYTES and card["budget_bytes"] == 64 * 1024
    assert [item["state"] for item in card["days"]] == ["current"] * 3
    last = card["days"][-1]["result"]
    summary = {(row["metric"], row["observation_count"]): row for row in last["streams"]}
    dense = summary["steps", 288]
    assert dense["value"] == sum(10 + i for i in range(288)) and dense["state"] == "estimated"
    assert dense["uncertainties"] == ["uniform_interval_allocation"]
    summary = {row["metric"]: row for row in last["streams"] if row is not dense}
    assert "observations" not in dense and "source_metadata" not in dense
    assert "timeline" not in summary["sleep_seconds"]["sleep"]
    assert summary["sleep_seconds"]["sleep"]["estimated_asleep_seconds"] is not None
    for key in ("value", "state", "uncertainties", "source", "definition"):
        assert summary["sleep_seconds"][key] == full["sleep_seconds"][key]
    assert last["manual_profile"][0]["value"] == 70
    assert last["manual_profile"][0]["measured_at"] == T0.isoformat()
    # Detail moved, not removed: every source record is still readable through records.
    pages, offset = [], 0
    while offset is not None:
        page = await read_health(
            daily(DAY) | {"action": "records", "data_type": "steps", "offset": offset, "limit": 200}
        )
        pages += page["records"]
        offset = page["next_offset"]
    assert sum(row["start"].startswith(str(DAY)) for row in pages) == 288 + 1
    receipt = describe_result(daily(DAY - timedelta(days=2)), wide, True)
    assert receipt["counts"]["derived_observations"] == 0
    assert receipt["counts"]["derived_summaries"] == 1 + 1 + 4  # phone; phone; both, sleep, kg
    assert receipt["profile_dates"] == {"first": str(DAY), "last": str(DAY)}
    assert "derived.days[].result.streams[].observation_count" in receipt["fields"]
    assert {"derived.detail", "derived.budget_bytes", "derived.size_bytes"} <= set(
        receipt["fields"]
    )
    # Only sleep-stage labels stay hidden as dynamic map keys.
    assert all(field.endswith("_seconds.*") for field in receipt["fields"] if "*" in field)


async def test_a_full_year_is_bounded_and_its_suggested_range_fits(sessions, monkeypatch):
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    first = DAY - timedelta(days=365)
    for offset in range(366):
        start = T0 - timedelta(days=offset)
        await store(
            sessions,
            "steps",
            steps(f"band{offset}", 100, start),
            steps(f"phone{offset}", 300, start, source=PHONE),
        )
    started = time.monotonic()
    result = await read_health(daily(first))
    card = result["derived"]
    assert time.monotonic() - started < 120
    assert card["detail"] == "omitted" and card["days"] == []
    assert card["reason_codes"] == ["derived_budget_exceeded"]
    assert size(card) <= DERIVED_BUDGET_BYTES and card["size_bytes"] > DERIVED_BUDGET_BYTES
    assert len(result["days"]) == 366  # legacy scalars still span the whole year
    narrower = await read_health(
        daily(card["suggested_range"]["start_date"], card["suggested_range"]["end_date"])
    )
    assert narrower["derived"]["detail"] in {"full", "summary"}
    assert size(narrower["derived"]) <= DERIVED_BUDGET_BYTES
    assert describe_result(daily(first), result, True)["counts"]["derived_summaries"] == 0


def test_tool_description_states_the_only_write():
    description = codex_health.HEALTH_TOOL["description"]
    assert "No SQL or writes allowed. Reads refresh only a disposable derived cache" in description
    assert "raw and manual data are never written" in description
