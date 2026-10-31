"""Commit/rollback, identity undo, flags and persistent bounded refresh contracts."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta

import pytest
from derived_records import (
    ADAPTER_KINDS,
    DAY,
    RAW_MUTATIONS,
    T0,
    cache,
    manual,
    read,
    sleep,
    snapshot,
    steps,
    store,
    streams,
    summary,
)
from sqlalchemy import select, update
from sqlalchemy.exc import DataError, InterfaceError, OperationalError

from app.google_health.data_types import REGISTRY
from app.models.derived import DerivedDay
from app.models.health import SleepSession, Steps
from app.models.state import RawHealthData
from app.services import derived
from app.services.canonical_daily import METHOD
from app.services.derived import DerivedRefresh, register_range
from app.services.manual_flags import record_flag
from app.services.storage import store_records
from app.sync.deletions import mark_source_deletions


async def test_refetch_only_changes_seen_time_and_never_recalculates(sessions):
    record = steps()
    await store(sessions, "steps", record)
    original = await read(sessions)
    before = await cache(sessions)
    for _ in range(3):
        await store(sessions, "steps", deepcopy(record))
        assert await read(sessions) == original
    after = await cache(sessions)
    assert (after.build_count, after.revision) == (before.build_count, before.revision)
    assert await DerivedRefresh(sessions).run() == 0


async def test_date_and_value_corrections_invalidate_both_days_and_registered_zones(sessions):
    await store(sessions, "steps", steps())
    for zone in ("UTC", "Europe/Paris"):
        await read(sessions, DAY, DAY + timedelta(days=1), zone)
    await store(sessions, "steps", steps(count=240, start=T0 + timedelta(days=1)))
    for zone in ("UTC", "Europe/Paris"):
        for day in (DAY, DAY + timedelta(days=1)):
            row = await cache(sessions, day, zone)
            assert row.revision > row.built_revision
        card = await read(sessions, DAY, DAY + timedelta(days=1), zone)
        assert streams(card, "steps", 0) == []
        assert streams(card, "steps", 1)[0]["value"] == 240


async def test_summary_date_correction_invalidates_old_and_new_civil_dates(sessions):
    kind = "daily-resting-heart-rate"
    await store(sessions, kind, summary(kind, beatsPerMinute=50))
    await read(sessions, DAY, DAY + timedelta(days=1), "Pacific/Honolulu")
    await store(sessions, kind, summary(kind, day=DAY + timedelta(days=1), beatsPerMinute=60))
    card = await read(sessions, DAY, DAY + timedelta(days=1), "Pacific/Honolulu")
    assert streams(card, "resting_bpm", 0) == []
    assert streams(card, "resting_bpm", 1)[0]["value"] == 60


async def test_deletion_service_and_revival_refresh_values_without_deleting_archive(sessions):
    await store(sessions, "steps", steps())
    await read(sessions)
    async with sessions() as session, session.begin():
        count = await mark_source_deletions(
            session, REGISTRY["steps"], T0, T0 + timedelta(days=1), T0 + timedelta(days=365)
        )
        assert count == 1
    assert streams(await read(sessions)) == []
    await store(sessions, "steps", steps())
    assert streams(await read(sessions))[0]["value"] == 120
    async with sessions() as session:
        assert len((await session.scalars(select(RawHealthData))).all()) == 1


@pytest.mark.parametrize("normalized", [False, True])
@pytest.mark.parametrize("kind", ["steps", "sleep"])
async def test_quarantine_and_release_rebuild_eligibility(sessions, normalized, kind):
    await store(sessions, kind, steps() if kind == "steps" else sleep())
    if kind == "sleep":
        await store(sessions, kind, sleep(duration="1e20s"))
    await read(sessions)
    model = {"steps": Steps, "sleep": SleepSession}[kind] if normalized else RawHealthData
    for flag in (True, False):
        async with sessions() as session, session.begin():
            await session.execute(update(model).values(is_quarantined=flag))
        assert bool(streams(await read(sessions))) is not flag


async def test_normalized_correction_invalidates_but_raw_supplies_value_and_time(sessions):
    await store(sessions, "steps", steps())
    await read(sessions, DAY, DAY + timedelta(days=1))
    async with sessions() as session, session.begin():
        await session.execute(
            update(Steps).values(
                start_time=T0 + timedelta(days=1),
                end_time=T0 + timedelta(days=1, hours=1),
                count=25,
            )
        )
    card = await read(sessions, DAY, DAY + timedelta(days=1))
    assert streams(card, day=0)[0]["value"] == 120 and streams(card, day=1) == []


async def test_manual_flags_invalidate_future_latest_profile_and_are_reversible(sessions):
    older = await manual(sessions, 70, T0 - timedelta(days=30))
    newest = await manual(sessions, 80)
    await read(sessions, DAY, DAY + timedelta(days=2))
    for mark, expected in ((True, 70), (False, 80)):
        async with sessions() as session, session.begin():
            assert await record_flag(session, newest, "test", mark)
        for offset in range(3):
            row = await cache(sessions, DAY + timedelta(days=offset))
            assert row.revision > row.built_revision
        card = await read(sessions, DAY, DAY + timedelta(days=2))
        assert [item["result"]["manual_profile"][0]["value"] for item in card["days"]] == [
            expected
        ] * 3
    assert older != newest
    # A newly inserted latest measurement also invalidates a day computed beforehand.
    await manual(sessions, 85, T0 + timedelta(hours=1))
    assert (await read(sessions))["days"][0]["result"]["manual_profile"][0]["value"] == 85


async def test_rollback_reverts_source_revision_flags_and_partial_build(sessions):
    await store(sessions, "steps", steps())
    identifier = await manual(sessions, 80)
    original = await read(sessions)
    before = await cache(sessions)
    with pytest.raises(RuntimeError):
        async with sessions() as session, session.begin():
            await store_records(session, REGISTRY["steps"], [steps(count=999)])
            await record_flag(session, identifier, "test", True)
            assert streams(await derived.derived_range(session, DAY, DAY, "UTC"))[0]["value"] == 999
            raise RuntimeError("synthetic rollback")
    assert await read(sessions) == original
    after = await cache(sessions)
    assert (after.revision, after.build_count) == (before.revision, before.build_count)


async def test_change_during_build_stays_pending_and_is_not_returned_current(sessions, monkeypatch):
    await store(sessions, "steps", steps())
    original = derived.canonical_day
    changed = False

    async def changing(session, day, timezone):
        nonlocal changed
        result = await original(session, day, timezone)
        if not changed:
            changed = True
            await store_records(session, REGISTRY["steps"], [steps(count=999)])
        return result

    monkeypatch.setattr(derived, "canonical_day", changing)
    result = await read(sessions)
    assert result["days"][0]["state"] == "stale" and result["days"][0]["result"] is None
    assert (await cache(sessions)).revision != (await cache(sessions)).built_revision
    # A new worker instance after restart sees the outstanding revision.
    assert await DerivedRefresh(sessions).run() == 1
    assert streams(await read(sessions))[0]["value"] == 999


async def test_bounded_refresh_persists_progress_and_never_discovers_full_archive(
    sessions, monkeypatch
):
    await store(sessions, "steps", steps(), steps(count=240))
    await manual(sessions, 80)
    first, last = DAY - timedelta(days=20), DAY
    async with sessions() as session, session.begin():
        await register_range(session, first, last, "UTC")
        before = await snapshot(session)
    assert await DerivedRefresh(sessions).run() == 16
    async with sessions() as session:
        rows = list((await session.scalars(select(DerivedDay).order_by(DerivedDay.day))).all())
        assert [row.build_count for row in rows] == [1] * 16 + [0] * 5
    original = derived.canonical_day

    # An interruption (cancelled worker, shutdown) is not a failed build: it rolls back
    # and the day stays pending. Ordinary build errors are isolated, tested below.
    async def fail(session, day, timezone):
        raise asyncio.CancelledError

    monkeypatch.setattr(derived, "canonical_day", fail)
    with pytest.raises(asyncio.CancelledError):
        await DerivedRefresh(sessions).run()
    monkeypatch.setattr(derived, "canonical_day", original)
    assert await DerivedRefresh(sessions).run() == 5
    assert await DerivedRefresh(sessions).run() == 0
    await read(sessions, first, last)
    async with sessions() as session:
        assert await snapshot(session) == before
        assert len((await session.scalars(select(DerivedDay))).all()) == 21


async def test_failing_day_is_isolated_recorded_skipped_and_retried_after_a_change(
    sessions, monkeypatch, caplog
):
    first, last = DAY - timedelta(days=2), DAY
    for offset in range(3):
        await store(sessions, "steps", steps(f"day{offset}", start=T0 - timedelta(days=offset)))
    async with sessions() as session, session.begin():
        await register_range(session, first, last, "UTC")
    original = derived.canonical_day
    broken = {DAY - timedelta(days=1)}

    async def fail_one(session, day, timezone):
        if day in broken:
            raise ValueError("synthetic-private-value 998877")
        return await original(session, day, timezone)

    monkeypatch.setattr(derived, "canonical_day", fail_one)
    # The queue builds the other days and records the failure instead of stopping on it.
    assert await DerivedRefresh(sessions).run() == 3
    assert await DerivedRefresh(sessions).run() == 0
    row = await cache(sessions, DAY - timedelta(days=1))
    assert row.revision == row.built_revision and row.result["build_error"]["reason_codes"] == [
        derived.BUILD_FAILED
    ]
    assert "998877" not in caplog.text and "ValueError" in caplog.text
    # A range read returns the healthy days and an explicit error day; no exception.
    card = await read(sessions, first, last)
    assert [item["state"] for item in card["days"]] == ["current", "error", "current"]
    assert card["days"][1]["result"] is None
    assert card["days"][1]["reason_codes"] == [derived.BUILD_FAILED]
    # The record is not lost: a source change on that day makes it pending again.
    monkeypatch.setattr(derived, "canonical_day", original)
    await store(sessions, "steps", steps("day1", count=7, start=T0 - timedelta(days=1)))
    assert await DerivedRefresh(sessions).run() == 1
    assert streams(await read(sessions, first, last), day=1)[0]["value"] == 7


@pytest.mark.parametrize("error_type", [DataError, OperationalError, InterfaceError])
@pytest.mark.parametrize("between", ["success", "source", "alone"])
@pytest.mark.parametrize("initial", ["cold", "dirty", "empty"])
async def test_database_error_after_rollback_only_marks_pending_revision(
    sessions, monkeypatch, caplog, error_type, between, initial
):
    first = DAY - timedelta(days=1)
    for offset in range(2):
        await store(sessions, "steps", steps(f"day{offset}", start=T0 - timedelta(days=offset)))
    await manual(sessions, 80)
    await store(sessions, "steps", steps("day1", count=121, start=T0 - timedelta(days=1)))
    async with sessions() as session, session.begin():
        await register_range(session, first, DAY, "UTC")
    if initial == "dirty":
        await derived.build_day(sessions, first, "UTC")
        await store(sessions, "steps", steps("day1", count=122, start=T0 - timedelta(days=1)))
    elif initial == "empty":
        async with sessions() as session, session.begin():
            await session.execute(update(DerivedDay).values(built_revision=DerivedDay.revision))
    async with sessions() as session:
        before = await snapshot(session)
    original = derived.canonical_day
    attempted = False

    async def fail(session, day, timezone):
        nonlocal attempted
        if day == first and not attempted:
            attempted = True
            await original(session, day, timezone)
            await session.execute(
                update(DerivedDay).where(DerivedDay.day == day).values(build_count=99)
            )
            raise error_type("998877", {"health": 998877}, Exception("998877"))
        return await original(session, day, timezone)

    @asynccontextmanager
    async def interleaved_sessions():
        nonlocal before, completed
        try:
            async with sessions() as session:
                yield session
        except error_type:
            # session.begin() has rolled back and the failed session is closed.
            if between == "success":
                await derived.build_day(sessions, first, "UTC")
            elif between == "source":
                await store(
                    sessions, "steps", steps("day1", count=123, start=T0 - timedelta(days=1))
                )
            completed = await cache(sessions, first)
            async with sessions() as session:
                if between == "source":
                    before = await snapshot(session)  # exclude the intentional source write
                else:
                    assert await snapshot(session) == before
            raise

    completed = None
    monkeypatch.setattr(derived, "canonical_day", fail)
    assert await DerivedRefresh(interleaved_sessions).run() == 2
    row = await cache(sessions, first)
    assert completed is not None
    if between == "alone" and error_type is DataError:
        assert row.revision == row.built_revision and derived.build_failed(row.result)
        assert row.build_count == completed.build_count + 1 and "998877" not in repr(row.result)
    else:
        fields = ("result", "built_revision", "computed_at", "build_count")
        assert [getattr(row, key) for key in fields] == [getattr(completed, key) for key in fields]
    pending = between == "source" or (between == "alone" and error_type is not DataError)
    assert (row.revision != row.built_revision or row.result is None) is pending
    assert (await cache(sessions)).revision == (await cache(sessions)).built_revision
    assert "998877" not in caplog.text
    assert await DerivedRefresh(sessions).run() == int(pending)
    card = await read(sessions, first, DAY)
    expected = "error" if between == "alone" and error_type is DataError else "current"
    assert [item["state"] for item in card["days"]] == [expected, "current"]
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_range_build_commits_each_day_before_the_next(sessions, monkeypatch):
    first = DAY - timedelta(days=2)
    for offset in range(3):
        await store(sessions, "steps", steps(f"day{offset}", start=T0 - timedelta(days=offset)))
    original = derived.canonical_day

    async def interrupt_last(session, day, timezone):
        if day == DAY:
            raise asyncio.CancelledError
        return await original(session, day, timezone)

    monkeypatch.setattr(derived, "canonical_day", interrupt_last)
    with pytest.raises(asyncio.CancelledError):
        await derived.build_range(sessions, first, DAY, "UTC")
    async with sessions() as session:
        rows = (await session.scalars(select(DerivedDay).order_by(DerivedDay.day))).all()
        # Every day was registered up front; the two built days were already committed.
        assert [row.build_count for row in rows] == [1, 1, 0]
    monkeypatch.setattr(derived, "canonical_day", original)
    assert await derived.build_range(sessions, first, DAY, "UTC") == 1
    assert await derived.build_range(sessions, first, DAY, "UTC") == 0


async def test_cold_day_failure_is_recorded_and_a_daily_read_still_answers(sessions, monkeypatch):
    from sqlalchemy.exc import DataError

    from app.services import codex_health

    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    first = DAY - timedelta(days=1)
    for offset in range(2):
        await store(sessions, "steps", steps(f"day{offset}", start=T0 - timedelta(days=offset)))
    original = derived.canonical_day

    async def fail_first(session, day, timezone):
        if day == first:
            raise DataError("synthetic", {}, Exception())
        return await original(session, day, timezone)

    monkeypatch.setattr(derived, "canonical_day", fail_first)
    args = {"action": "daily", "start_date": str(first), "end_date": str(DAY), "timezone": "UTC"}
    result = await codex_health.read_health(args)
    assert [item["state"] for item in result["derived"]["days"]] == ["error", "current"]
    assert result["days"][0]["steps"] == 120  # legacy scalars are unaffected
    # Interpretation actions refuse explicitly instead of failing the whole tool call.
    refused = await codex_health.read_health(dict(args, action="sleep"))
    assert refused["code"] == derived.BUILD_FAILED


@pytest.mark.parametrize("days", [0, 367])
async def test_rebuild_range_is_bounded(sessions, days):
    with pytest.raises(ValueError, match="366"):
        await read(sessions, DAY, DAY + timedelta(days=days - 1))


async def test_method_version_has_a_separate_cache_key(sessions):
    await store(sessions, "steps", steps())
    await read(sessions)
    async with sessions() as session, session.begin():
        await session.execute(update(DerivedDay).values(method="source-daily/3"))
    await store(sessions, "steps", steps(count="1e100"))
    await read(sessions)
    async with sessions() as session:
        rows = (await session.scalars(select(DerivedDay))).all()
        assert {row.method: row.result["streams"][0]["value"] for row in rows} == {
            "source-daily/3": 120,
            METHOD: 1e100,
        }


async def test_identity_link_and_owner_undo_invalidate_existing_results(sessions, clock):
    from identity_records import STEPS, fitbit
    from identity_records import steps as unnamed_steps
    from identity_records import store as identity_store

    from app.parsers.identity import source_fingerprint
    from app.services.identity_backfill import identity_backfill
    from app.services.identity_decisions import decide_sources

    first, second = fitbit("Synthetic A"), fitbit("Synthetic B")
    for minute in range(2):
        at = T0 + timedelta(minutes=minute)
        await identity_store(
            sessions, STEPS, unnamed_steps(10, at, first), unnamed_steps(10, at, second)
        )
    await identity_backfill(sessions)
    assert len(streams(await read(sessions))) == 2
    a, b = source_fingerprint(first), source_fingerprint(second)
    await decide_sources(sessions, "steps", "link", a[:12], b[:12])
    assert (await cache(sessions)).revision > (await cache(sessions)).built_revision
    assert sum(row["value"] for row in streams(await read(sessions))) == 20
    await decide_sources(sessions, "steps", "reject", a[:12], b[:12])
    assert (await cache(sessions)).revision > (await cache(sessions)).built_revision
    assert sorted(row["value"] for row in streams(await read(sessions))) == [20, 20]


async def test_deleted_holder_invalidates_a_held_record_on_another_day(sessions):
    from app.models.identity import RawHealthDataSupersession

    await store(sessions, "steps", steps("old"), steps("new", start=T0 + timedelta(days=1)))
    async with sessions() as session, session.begin():
        old, new = (
            await session.scalars(select(RawHealthData).order_by(RawHealthData.source_start_time))
        ).all()
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old.id,
                new_raw_id=new.id,
                data_type="steps",
                old_google_data_point_id=old.google_data_point_id,
                new_google_data_point_id=new.google_data_point_id,
                reason="duplicate",
                old_fingerprint="a" * 64,
                new_fingerprint="a" * 64,
                same_reading=True,
                new_payload_hash=new.payload_hash,
            )
        )
    assert streams(await read(sessions)) == []
    async with sessions() as session, session.begin():
        await session.execute(
            update(RawHealthData).where(RawHealthData.id == new.id).values(source_deleted_at=T0)
        )
    assert streams(await read(sessions))[0]["value"] == 120


@pytest.mark.parametrize("kind", ["heart-rate", *ADAPTER_KINDS])
@pytest.mark.parametrize("operation", RAW_MUTATIONS)
async def test_raw_and_identity_mutations_only_dirty_adapter_days(sessions, kind, operation):
    from derived_records import raw_mutation

    mutation = await raw_mutation(sessions, kind, operation)
    last = DAY + timedelta(days=1)
    async with sessions.begin() as session:
        await register_range(session, DAY, last, "UTC")
    before = [(await cache(sessions, day)).revision for day in (DAY, last)]
    async with sessions.begin() as session:
        await session.execute(mutation)
    after = [(await cache(sessions, day)).revision for day in (DAY, last)]
    affected = [] if kind == "heart-rate" or operation == "unchanged" else [0]
    if affected and operation in ("move", "supersession"):
        affected.append(1)
    assert [right - left for left, right in zip(before, after, strict=True)] == [
        int(index in affected) for index in range(2)
    ]
