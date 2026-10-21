"""``health-mirror identity-backfill`` and the readers that skip superseded rows (SQLite)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, timedelta

from identity_records import (
    HEART,
    NEW_NAME,
    PACKAGE,
    SEDENTARY,
    STEPS,
    SWIM,
    T0,
    count,
    fitbit,
    heart,
    legacy,
    phone,
    raw_rows,
    sedentary,
    steps,
    store,
    swim,
)
from sqlalchemy import func, select
from typer.testing import CliRunner

from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.models.raw_versions import RawHealthDataEvent
from app.models.state import RawHealthData
from app.parsers.common import temporal
from app.services.dashboard import dashboard_data
from app.services.identity_backfill import identity_backfill


async def archive_with_duplicates(sessions) -> dict[str, uuid.UUID]:
    """Rows as the pre-v2 archive held them (each pair: before / after the change)."""
    day = timedelta(days=1)
    ids = {
        "renamed-old": await legacy(sessions, HEART, heart(60), T0, deleted=T0 + day),
        "renamed-new": await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + day),
        "lone-deleted": await legacy(
            sessions, HEART, heart(80, at=T0 + timedelta(minutes=9)), T0, deleted=T0 + day
        ),
        # The band (old name) was last seen half a day before the new name first appeared;
        # the watch, another device of another class (recording method), was seen with both.
        "band": await legacy(
            sessions, HEART, heart(70, at=T0 + timedelta(minutes=3)), T0, last=T0 + day / 2
        ),
        "watch": await legacy(
            sessions,
            HEART,
            {
                **heart(72, at=T0 + timedelta(minutes=3)),
                "dataSource": fitbit("Example Watch", method="PASSIVELY_MEASURED"),
            },
            T0 + timedelta(hours=2),
            last=T0 + day,
        ),
        "suffixed": await legacy(sessions, STEPS, steps(40), T0),
        "plain": await legacy(sessions, STEPS, steps(40, source=phone(PACKAGE)), T0 + day),
        "other-steps": await legacy(sessions, STEPS, steps(25, start=T0 + timedelta(hours=1)), T0),
        "swim-flat": await legacy(sessions, SWIM, swim(12), T0),
        "swim-civil": await legacy(sessions, SWIM, swim(12, civil=True), T0 + day),
    }
    for minutes in (15, 30, 45):
        ids[f"sedentary-{minutes}"] = await legacy(
            sessions, SEDENTARY, sedentary(minutes), T0 + timedelta(minutes=minutes)
        )
    return ids


async def links(sessions) -> dict:
    async with sessions() as session:
        rows = (await session.scalars(select(RawHealthDataSupersession))).all()
    return {row.old_raw_id: (row.new_raw_id, row.reason) for row in rows}


async def test_backfill_links_duplicates_once_and_dry_run_writes_nothing(sessions) -> None:
    ids = await archive_with_duplicates(sessions)
    dry = await identity_backfill(sessions, dry_run=True)
    assert await count(sessions, RawHealthDataIdentity) == 0
    assert await links(sessions) == {}
    assert dry["heart-rate"].linked == {"display-name-change": 1}
    assert dry["heart-rate"].distinct_sources == 0 and dry["heart-rate"].deletions_kept == 1

    result = await identity_backfill(sessions)
    expected = {
        ids["renamed-old"]: (ids["renamed-new"], "display-name-change"),
        ids["suffixed"]: (ids["plain"], "package-suffix-change"),
        ids["swim-flat"]: (ids["swim-civil"], "interval-shape-change"),
        ids["sedentary-15"]: (ids["sedentary-45"], "growing-interval"),
        ids["sedentary-30"]: (ids["sedentary-45"], "growing-interval"),
    }
    assert await links(sessions) == expected
    assert {
        name: stats.as_dict() == dry[name].as_dict() for name, stats in result.items()
    } == dict.fromkeys(result, True)
    assert result["heart-rate"].superseded_marked == 1
    async with sessions() as session:
        events = (
            (
                await session.execute(
                    select(RawHealthDataEvent.raw_id, RawHealthDataEvent.event).where(
                        RawHealthDataEvent.event == "identity-merged"
                    )
                )
            )
            .tuples()
            .all()
        )
        lone = await session.get(RawHealthData, ids["lone-deleted"])
    assert sorted(raw_id for raw_id, _ in events) == sorted(expected)
    assert lone is not None and lone.source_deleted_at is not None  # a real deletion stays
    # One alias per key and fingerprint of a row archived under a v1 id (heart-rate 5,
    # steps 3, the two swim rows of one source 1, the growing period 1).
    assert await count(sessions, RawHealthDataIdentity) == 5 + 3 + 1 + 1

    again = await identity_backfill(sessions)
    assert sum(sum(stats.linked.values()) + stats.aliases_added for stats in again.values()) == 0
    async with sessions() as session:
        merged = await session.scalar(
            select(func.count()).where(RawHealthDataEvent.event == "identity-merged")
        )
    assert merged == len(expected)


async def test_backfill_batches_never_split_an_identity_and_resume_after(sessions) -> None:
    ids = await archive_with_duplicates(sessions)
    progress = []
    await identity_backfill(
        sessions,
        data_types=["sedentary-period", "heart-rate"],
        batch_size=1,
        progress=lambda name, stats, reached: progress.append((name, reached)),
    )
    found = await links(sessions)
    assert found[ids["sedentary-15"]] == (ids["sedentary-45"], "growing-interval")
    assert found[ids["renamed-old"]] == (ids["renamed-new"], "display-name-change")
    assert ("heart-rate", T0 + timedelta(minutes=9)) in progress
    # Resuming at a later start does not revisit (or count) earlier rows.
    resumed = await identity_backfill(
        sessions, data_types=["heart-rate"], after=T0 + timedelta(minutes=5)
    )
    assert resumed["heart-rate"].rows == 1


async def test_store_after_backfill_follows_the_links(sessions, clock) -> None:
    ids = await archive_with_duplicates(sessions)
    await identity_backfill(sessions)
    clock.now = T0 + timedelta(days=3)
    await store(sessions, HEART, heart(60, name=NEW_NAME))
    await store(sessions, STEPS, steps(41, source=phone(PACKAGE)))
    await store(sessions, SEDENTARY, sedentary(60))
    async with sessions() as session:
        renamed = await session.get(RawHealthData, ids["renamed-new"])
        plain = await session.get(RawHealthData, ids["plain"])
        growing = await session.get(RawHealthData, ids["sedentary-45"])
    assert renamed is not None and renamed.last_seen_at.replace(tzinfo=UTC) == clock.now
    assert plain is not None and plain.raw_json == steps(41, source=phone(PACKAGE))
    assert growing is not None and temporal(SEDENTARY, growing.raw_json)[1] == T0 + timedelta(
        hours=1
    )
    assert len(await raw_rows(sessions, HEART)) == 5  # nothing new


async def test_superseded_rows_are_not_counted_by_the_dashboard(sessions) -> None:
    day = date(2026, 10, 18)
    await legacy(sessions, STEPS, steps(40), T0)
    await legacy(sessions, STEPS, steps(40, source=phone(PACKAGE)), T0 + timedelta(days=1))
    await legacy(sessions, HEART, heart(60), T0)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + timedelta(days=1))
    await legacy(sessions, HEART, heart(80, at=T0 + timedelta(minutes=1)), T0)

    async def board() -> tuple:
        async with sessions() as session:
            data = await dashboard_data(session, day, 1, "UTC")
            history = await dashboard_data(session, day, 1, "UTC", valid_sources_only=True)
            everything = await dashboard_data(session, day, 1, "UTC", include_source_deleted=True)
        return (
            data["days"][0]["steps"],
            data["charts"]["heart_rate"][9],
            history["days"][0]["steps"],
            everything["days"][0]["steps"],
        )

    assert await board() == (80, 66.7, 80, 80)  # double counted, averaged twice
    await identity_backfill(sessions)
    assert await board() == (40, 70.0, 40, 40)


async def test_verify_reports_an_alias_that_does_not_match_its_row(sessions) -> None:
    from app.services.identity import verify_identities

    ids = await archive_with_duplicates(sessions)
    await identity_backfill(sessions)
    async with sessions() as session:
        count, errors, notes = await verify_identities(session)
    assert count == 10 and errors == [] and notes == []
    async with sessions.begin() as session:
        session.add(
            RawHealthDataIdentity(
                data_type="steps",
                identity_key="v2:" + "0" * 64,
                raw_id=ids["band"],
                google_data_point_id="somewhere-else",
            )
        )
    async with sessions() as session:
        _, errors, _ = await verify_identities(session)
    assert errors == [f"identity alias steps/v2:{'0' * 64} does not match its raw row"]


def test_cli_identity_backfill_reports_per_type_and_reason(tmp_path, monkeypatch) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models  # noqa: F401
    from app.cli import main as cli
    from app.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await archive_with_duplicates(factory)

    asyncio.run(prepare())
    monkeypatch.setattr(cli, "SessionLocal", factory)
    try:
        result = CliRunner().invoke(
            cli.app, ["identity-backfill", "--dry-run", "--data-type", "steps"]
        )
        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert report["dry_run"] is True
        assert report["data_types"]["steps"]["linked"] == {"package-suffix-change": 1}
        assert report["total"]["superseded"] == 1
        assert "resume: --data-type steps --after" in result.stderr
        bad = CliRunner().invoke(cli.app, ["identity-backfill", "--statement-timeout", "1h"])
        assert bad.exit_code != 0
    finally:
        asyncio.run(engine.dispose())


# --- review findings ----------------------------------------------------------------------


async def test_a_device_seen_alongside_keeps_a_rename_undecided(sessions) -> None:
    """Rule 4: a second device of the class seen alongside the old or the new name - even a
    6 h gap does not decide the rename. Nothing is linked (both stay visible), the dry run
    lists the case with its reason, and the second device is never linked either."""
    hour = timedelta(hours=1)
    later = T0 + timedelta(minutes=1)
    await legacy(sessions, HEART, heart(60), T0, last=T0 + 3 * hour)
    await legacy(sessions, HEART, heart(61, at=later), T0, last=T0 + 3 * hour)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * hour, last=T0 + 30 * hour)
    await legacy(
        sessions, HEART, heart(61, at=later, name=NEW_NAME), T0 + 9 * hour, last=T0 + 30 * hour
    )
    await legacy(sessions, HEART, heart(90, name="Example Watch"), T0, last=T0 + 30 * hour)
    dry = await identity_backfill(sessions, dry_run=True)
    report = dry["heart-rate"]
    assert report.chains == []
    [case] = report.ambiguous
    assert case["source"] == "FITBIT / Example Band / DERIVED"
    assert "alongside" in case["why"]
    assert [found["source"] for found in case["candidates"]] == ["FITBIT / Example Watch / DERIVED"]
    # One instant each returned with two values: not enough to establish two devices.
    assert [pair["established"] for pair in report.kept_apart] == [False, False, False]
    result = await identity_backfill(sessions)
    assert await links(sessions) == {} and not result["heart-rate"].linked
    assert result["heart-rate"].distinct_sources == 2


async def test_backfill_keeps_two_devices_stored_seconds_apart(sessions) -> None:
    """Review 1: two devices at one instant, stored on two pages five seconds apart and
    never fetched again, are never linked (a rename within five minutes is left open, listed
    as ambiguous for the owner; rows seen seconds apart are no evidence either way)."""
    await legacy(sessions, HEART, heart(60), T0)
    await legacy(sessions, HEART, heart(90, name="Other Watch"), T0 + timedelta(seconds=5))
    report = await identity_backfill(sessions)
    assert await links(sessions) == {}
    assert report["heart-rate"].chains == []
    assert "less than five minutes" in report["heart-rate"].ambiguous[0]["why"]


async def test_backfill_never_links_across_fingerprints_with_two_candidates(sessions) -> None:
    """Ambiguous: two old sources both end before the new one: nothing is linked."""
    hour = timedelta(hours=1)
    await legacy(sessions, HEART, heart(60), T0, last=T0 + hour)
    await legacy(sessions, HEART, heart(90, name="Example Watch"), T0, last=T0 + 2 * hour)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * hour)
    report = await identity_backfill(sessions)
    assert await links(sessions) == {}
    [case] = report["heart-rate"].ambiguous
    assert case["source"] == "FITBIT / Example Band / DERIVED"
    assert case["why"] == "more than one source of its class ended before it"
    assert [found["source"] for found in case["candidates"]] == [
        "FITBIT / Example Band Pro / DERIVED",
        "FITBIT / Example Watch / DERIVED",
    ]


async def test_backfill_counts_only_links_it_inserted_and_genuine_deletions(
    sessions, monkeypatch
) -> None:
    """Review 2: ``linked`` counts the links this run inserted (another run may insert some
    first); ``deletions_kept`` counts deleted records with no live row of their device."""
    from app.services import identity_backfill as backfill

    day = timedelta(days=1)
    renamed_old = await legacy(sessions, HEART, heart(60), T0, deleted=T0 + day)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + day)
    both_old = await legacy(
        sessions, HEART, heart(62, at=T0 + timedelta(minutes=2)), T0, deleted=T0 + day
    )
    both_new = await legacy(
        sessions,
        HEART,
        heart(62, at=T0 + timedelta(minutes=2), name=NEW_NAME),
        T0 + day,
        deleted=T0 + 2 * day,
    )
    await legacy(sessions, HEART, heart(80, at=T0 + timedelta(minutes=9)), T0, deleted=T0 + day)
    original = backfill._write_plan

    async def racing(session, spec, plan, stats, *, dry_run):
        # Another run linked the first row a moment earlier.
        if plan.links and not dry_run:
            row, holder, reason = plan.links[0]
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=row.raw_id,
                    new_raw_id=holder.raw_id,
                    data_type=spec.name,
                    old_google_data_point_id=row.google_data_point_id,
                    new_google_data_point_id=holder.google_data_point_id,
                    reason=reason,
                )
            )
            await session.flush()
        await original(session, spec, plan, stats, dry_run=dry_run)

    monkeypatch.setattr(backfill, "_write_plan", racing)
    result = (await identity_backfill(sessions))["heart-rate"]
    assert sum(result.linked.values()) == 1  # two planned, one inserted by this run
    found = await links(sessions)
    assert set(found) == {renamed_old, both_old} and found[both_old][0] == both_new
    # The lone deletion and the record deleted under both names; not the renamed one.
    assert result.deletions_kept == 2
    async with sessions() as session:
        merged = await session.scalar(
            select(func.count()).where(RawHealthDataEvent.event == "identity-merged")
        )
    assert merged == 1  # only for the link this run inserted


async def test_after_still_settles_stale_start_rows_before_it(sessions, clock) -> None:
    """Re-review 1 (small): resuming with ``--after`` past a stale-start row still links it
    (pass 1 notes it by id; only ids and starts are kept in memory)."""
    stale = await legacy(sessions, SWIM, swim(12), T0, start=T0 + timedelta(seconds=30))
    await legacy(sessions, SWIM, swim(12, civil=True), T0 + timedelta(days=1))
    report = await identity_backfill(sessions, after=T0 + timedelta(hours=2))
    assert report["swim-lengths-data"].rows == 0  # the second pass read nothing
    assert report["swim-lengths-data"].start_mismatches == 1
    assert stale in await links(sessions)


def test_status_shows_the_reconciler_and_incremental_runs_skip_without_a_full_run(
    tmp_path, monkeypatch
) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models  # noqa: F401
    from app.cli import main as cli
    from app.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'status.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await archive_with_duplicates(factory)

    asyncio.run(prepare())
    monkeypatch.setattr(cli, "SessionLocal", factory)
    try:
        nightly = CliRunner().invoke(cli.app, ["identity-backfill", "--incremental"])
        assert nightly.exit_code == 0, nightly.output
        report = json.loads(nightly.stdout)
        assert report["data_types"]["steps"]["skipped"].startswith("no full identity-backfill")
        assert report["total"]["superseded"] == 0
        full = CliRunner().invoke(cli.app, ["identity-backfill", "--data-type", "steps"])
        assert full.exit_code == 0, full.output
        again = CliRunner().invoke(
            cli.app, ["identity-backfill", "--incremental", "--data-type", "steps"]
        )
        assert json.loads(again.stdout)["data_types"]["steps"]["skipped"] is None
        status = json.loads(CliRunner().invoke(cli.app, ["status"]).stdout)
        [steps_state] = status["identity_reconcile"]
        assert steps_state["data_type"] == "steps" and steps_state["full_run_at"]
        assert steps_state["last_mode"] == "incremental" and steps_state["last_error"] is None
        assert steps_state["last_result"]["skipped"] is None
    finally:
        asyncio.run(engine.dispose())
