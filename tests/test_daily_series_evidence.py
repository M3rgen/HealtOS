"""Device collisions through the production reader, without timing thresholds."""

from collections import Counter
from datetime import timedelta
from unittest.mock import Mock

import pytest
from derived_records import BAND, DAY, PHONE, T0, snapshot, store, summary
from interpretation_records import call
from interpretation_records import production_reader as production_reader

from app.models.identity import RawSourceDevice
from app.parsers.common import canonical_json, payload_hash
from app.services import health_interpretation as reader
from app.services import interpretation_baseline_index as indexing
from app.services import interpretation_inputs
from app.services import interpretation_series as daily
from app.services import interpretation_stats as stats
from app.services.interpretation_inputs import MAX_INPUT_BYTES, MAX_INPUT_RECORDS

KIND = "daily-resting-heart-rate"
DEVICE = "d" * 64


async def decide(sessions, sources, device=DEVICE):
    async with sessions.begin() as session:
        session.add_all(
            RawSourceDevice(
                data_type=KIND, fingerprint=payload_hash(source), device=device, decided_at=T0
            )
            for source in sources
        )


async def archive(sessions, count):
    sources = [{**BAND, "device": {"displayName": f"Synthetic {i}"}} for i in range(count)]
    # Two methods within each source exercise definition partitioning as well as
    # collisions across sources. The third day has a single comparable source.
    records = [
        summary(
            KIND,
            f"{i}-{offset}-{method}-{copy}",
            day=DAY - timedelta(days=offset),
            source=source,
            beatsPerMinute=50 + i % 3,
            measurementMethod=method,
            timeZone="UTC",
            dailyRestingHeartRateMetadata={"calculationMethod": "synthetic"},
        )
        for i, source in enumerate(sources)
        for offset in range(3 if i == 0 else 2)
        for method in ("A", "B")
        for copy in range(2)
    ]
    assert len(records) < MAX_INPUT_RECORDS
    assert sum(len(canonical_json(r).encode()) for r in records) < MAX_INPUT_BYTES
    await store(sessions, KIND, *records)
    await decide(sessions, sources)
    return sources, records, dict(start_date=str(DAY - timedelta(days=2)), metric="resting_bpm")


def count_accumulation(monkeypatch, count):
    visits, captured = Counter(), {}
    building = False

    def copied(size):
        if building:
            visits["copied"] += size
            assert visits["copied"] <= 2 * count

    class Evidence(list):
        def __iter__(self):
            copied(len(self))
            return super().__iter__()

        def __add__(self, other):
            copied(len(self) + len(other))
            return Evidence(super().__add__(other))

        def __getitem__(self, index):
            result = super().__getitem__(index)
            if isinstance(index, slice):
                copied(len(result))
            return result

        def copy(self):
            copied(len(self))
            return super().copy()

    original_evidence, original_series = daily.daily_evidence, reader.daily_series

    def evidence(group, day):
        visits["created"] += len(group["observations"])
        return Evidence(original_evidence(group, day))

    def series(*args):
        nonlocal building
        visits.clear()
        building = True
        before = payload_hash(args[0])
        try:
            result = original_series(*args)
            assert payload_hash(args[0]) == before
        finally:
            building = False
        captured.clear()
        captured.update(result)
        assert visits["created"] == count
        assert 0 < visits["copied"] <= 2 * count
        return result

    monkeypatch.setattr(daily, "daily_evidence", evidence)
    monkeypatch.setattr(reader, "daily_series", series)
    return captured, visits


@pytest.mark.parametrize("count", [32, 128, 512])
@pytest.mark.parametrize("action", ["baseline", "analyze", "relationship"])
async def test_linked_stream_copy_visits_are_linear_and_preserve_order(
    sessions, monkeypatch, count, action
):
    sources, records, args = await archive(sessions, count)
    captured, visits = count_accumulation(monkeypatch, len(records))
    async with sessions() as session:
        before = await snapshot(session)
    result = await call(
        action,
        limit=1,
        **args,
        **(dict(other_metric="resting_bpm", lags=[0]) if action == "relationship" else {}),
    )
    assert result["total_summaries"] == (4 if action == "relationship" else 6)
    assert result["next_cursor"]
    assert visits["created"] == len(records)
    assert len(captured) == 2
    assert len({payload_hash(source) for source in sources}) == count
    ordered = sorted(records, key=lambda r: (payload_hash(r["dataSource"]), r["name"]))
    for key, days in captured.items():
        method = days[DAY]["method_details"]["measurementMethod"]
        assert key[1] == "cihaz-" + payload_hash([KIND, DEVICE, None])
        for day, row in days.items():
            expected = [
                record
                for record in ordered
                if record["dailyRestingHeartRate"]["date"] == str(day)
                and record["dailyRestingHeartRate"]["measurementMethod"] == method
            ]
            assert row["evidence"] == [
                dict(
                    record=record["name"],
                    date=str(day),
                    value=record["dailyRestingHeartRate"]["beatsPerMinute"],
                    start=f"{day}T00:00:00+00:00",
                    end=f"{day}T00:00:00+00:00",
                    source_timezone="UTC",
                    field="beatsPerMinute",
                    reason_codes=[],
                )
                for record in expected
            ]
            assert row["method_details"] == dict(
                measurementMethod=method,
                measurementLocation=None,
                recordingMethod=None,
                dailyRestingHeartRateMetadata={"calculationMethod": "synthetic"},
            )
            if day == DAY - timedelta(days=2):
                assert row["value"] == 50 and row["comparable"] and row["reasons"] == []
            else:
                assert row["value"] is None and not row["comparable"]
                assert row["reasons"] == ["same_device_multiple_streams"]
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("action", ["baseline", "analyze", "relationship"])
async def test_collision_pages_budgets_and_append_only_decisions(sessions, monkeypatch, action):
    sources, records, args = await archive(sessions, 12)
    # A separate device and recording method must remain independent even when
    # all fingerprints have an explicit device decision.
    manual = {**PHONE, "recordingMethod": "MANUAL"}
    await decide(sessions, [PHONE], "e" * 64)
    await decide(sessions, [manual])
    await store(
        sessions,
        KIND,
        summary(KIND, "phone", source=PHONE, beatsPerMinute=70),
        summary(KIND, "manual", source=manual, beatsPerMinute=80),
    )
    if action == "relationship":
        args.update(other_metric="resting_bpm", lags=[0, 1])
    whole = await call(action, limit=200, **args)
    expected = whole["summaries"]
    page = await call(action, limit=1, **args)
    assert page["summaries"] == expected[:1] and page["next_cursor"]
    rest = await call(action, limit=200, cursor=page["next_cursor"], **args)
    assert page["summaries"] + rest["summaries"] == expected
    assert page["candidates"] == rest["candidates"] == whole["candidates"]
    if action != "relationship":
        assert len({r["source"] for r in expected}) == 3
        assert sorted(r["value"] for r in expected if r["value"] is not None) == [50, 50, 70, 80]
    else:
        # Collision days are excluded from pairs; the singleton day is retained.
        collision_records = {
            r["name"]
            for r in records
            if r["dailyRestingHeartRate"]["date"] != str(DAY - timedelta(days=2))
        }
        assert not collision_records.intersection(
            ref
            for r in expected
            for pair in r["pairs"]
            for side in ("x_records", "y_records")
            for ref in pair[side]
        )
    budgets = [(reader, "MAX_SUMMARIES", len(expected), "summary_budget_exceeded")]
    if action == "relationship":
        pairs = [p for r in expected for p in r["pairs"]]
        references = sum(len(p[side]) for p in pairs for side in ("x_records", "y_records"))
        budgets += [
            (reader, "MAX_RELATIONSHIP_REGIMES", 4, "relationship_regime_budget_exceeded"),
            (reader, "MAX_RELATIONSHIP_PAIRS", len(pairs), "relationship_pair_budget_exceeded"),
            (
                reader,
                "MAX_RELATIONSHIP_REFERENCES",
                references,
                "relationship_reference_budget_exceeded",
            ),
        ]
    else:
        references = sum(len(r["evidence"]) for r in expected)
        budgets.append(
            (indexing, "MAX_BASELINE_EVIDENCE", references, "baseline_evidence_budget_exceeded")
        )
    async with sessions() as session:
        before = await snapshot(session)
    for module, name, ceiling, code in budgets:
        with monkeypatch.context() as patch:
            patch.setattr(module, name, ceiling)
            assert (await call(action, limit=1, **args))["summaries"] == expected[:1]
            patch.setattr(module, name, ceiling - 1)
            blocked = Mock(side_effect=AssertionError("budget must precede output generation"))
            for output in (
                ("correlation", "Relationship")
                if action == "relationship"
                else ("statistics", "Baseline")
            ):
                patch.setattr(stats, output, blocked)
            for limit in (1, 200):
                for cursor in (None, page["next_cursor"]):
                    result = await call(action, limit=limit, cursor=cursor, **args)
                    assert result["code"] == code and result["reason_codes"] == [code]
                    assert "summaries" not in result and "next_cursor" not in result
            blocked.assert_not_called()
    # Input and serialized response ceilings also remain independent of small pages.
    for name, ceiling in (("MAX_INPUT_RECORDS", 1), ("MAX_INPUT_BYTES", 1)):
        with monkeypatch.context() as patch:
            patch.setattr(interpretation_inputs, name, ceiling)
            assert (await call(action, limit=1, **args))["code"] == "input_budget_exceeded"
    with monkeypatch.context() as patch:
        patch.setattr(reader, "RESPONSE_BUDGET_BYTES", 1)
        assert (await call(action, limit=1, **args))["code"] == "response_budget_exceeded"
    async with sessions() as session:
        assert await snapshot(session) == before
    # Reassign by appending, then append the original device again. Both changes
    # expire the cursor, including when effective membership returns to its start.
    await decide(sessions, [sources[0]], "f" * 64)
    assert (await call(action, cursor=page["next_cursor"], **args))["code"] == "stale_cursor"
    await decide(sessions, [sources[0]])
    assert (await call(action, cursor=page["next_cursor"], **args))["code"] == "stale_cursor"
    assert (await call(action, limit=200, **args))["summaries"] == expected
