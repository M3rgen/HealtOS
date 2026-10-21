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
        # the watch, another device, was seen together with both.
        "band": await legacy(
            sessions, HEART, heart(70, at=T0 + timedelta(minutes=3)), T0, last=T0 + day / 2
        ),
        "watch": await legacy(
            sessions,
            HEART,
            heart(72, at=T0 + timedelta(minutes=3), name="Example Watch"),
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
    assert dry["heart-rate"].distinct_sources == 1 and dry["heart-rate"].deletions_kept == 1

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
    # One alias per identity (heart-rate: renamed, lone; the two devices by their specific
    # keys plus their shared plain key), steps 2, swim 1, sedentary 1.
    assert await count(sessions, RawHealthDataIdentity) == 5 + 2 + 1 + 1

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
    await legacy(sessions, HEART, heart(80, name=NEW_NAME), T0 + timedelta(days=1))

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

    assert await board() == (80, 70.0, 80, 80)  # double counted, averaged twice
    await identity_backfill(sessions)
    assert await board() == (40, 80.0, 40, 40)


async def test_verify_reports_an_alias_that_does_not_match_its_row(sessions) -> None:
    from app.services.identity import verify_identities

    ids = await archive_with_duplicates(sessions)
    await identity_backfill(sessions)
    async with sessions() as session:
        count, errors, notes = await verify_identities(session)
    assert count == 9 and errors == [] and notes == []
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


async def test_backfill_links_within_a_chain_next_to_an_overlapping_device(sessions) -> None:
    """Review 1 (high) / review 2 blocker 3: old and new name of one device are linked even
    where a second device recorded at the same instant; the second device is never linked."""
    hour = timedelta(hours=1)
    later = T0 + timedelta(minutes=1)
    old = await legacy(sessions, HEART, heart(60), T0, last=T0 + 3 * hour)
    old_later = await legacy(sessions, HEART, heart(61, at=later), T0, last=T0 + 3 * hour)
    new = await legacy(
        sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * hour, last=T0 + 30 * hour
    )
    new_later = await legacy(
        sessions, HEART, heart(61, at=later, name=NEW_NAME), T0 + 9 * hour, last=T0 + 30 * hour
    )
    await legacy(sessions, HEART, heart(90, name="Example Watch"), T0, last=T0 + 30 * hour)
    dry = await identity_backfill(sessions, dry_run=True)
    report = dry["heart-rate"]
    assert [(chain["from"], chain["to"]) for chain in report.chains] == [
        ("FITBIT / Example Band Pro / DERIVED", "FITBIT / Example Band / DERIVED")
    ]
    assert report.chains[0]["gap_seconds"] == 6 * 3600
    assert sorted(tuple(pair["sources"]) for pair in report.kept_apart) == [
        ("FITBIT / Example Band Pro / DERIVED", "FITBIT / Example Watch / DERIVED"),
        ("FITBIT / Example Watch / DERIVED", "FITBIT / Example Band / DERIVED"),
    ]
    assert await links(sessions) == {}
    result = await identity_backfill(sessions)
    assert await links(sessions) == {
        old: (new, "display-name-change"),
        old_later: (new_later, "display-name-change"),
    }
    assert result["heart-rate"].linked == {"display-name-change": 2} == dry["heart-rate"].linked
    assert result["heart-rate"].distinct_sources == 1


async def test_backfill_keeps_two_devices_stored_seconds_apart(sessions) -> None:
    """Review 1: two devices at one instant, stored on two pages five seconds apart and
    never fetched again, are two sources (seen together within the chain gap)."""
    await legacy(sessions, HEART, heart(60), T0)
    await legacy(sessions, HEART, heart(90, name="Other Watch"), T0 + timedelta(seconds=5))
    report = await identity_backfill(sessions)
    assert await links(sessions) == {}
    assert report["heart-rate"].chains == []
    assert report["heart-rate"].kept_apart[0]["why"].startswith("seen together")


async def test_backfill_never_links_across_fingerprints_with_two_candidates(sessions) -> None:
    """Ambiguous: two old sources both end before the new one: nothing is linked."""
    hour = timedelta(hours=1)
    await legacy(sessions, HEART, heart(60), T0, last=T0 + hour)
    await legacy(sessions, HEART, heart(90, name="Example Watch"), T0, last=T0 + 2 * hour)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * hour)
    report = await identity_backfill(sessions)
    assert await links(sessions) == {}
    assert report["heart-rate"].ambiguous == [
        {
            "source": "FITBIT / Example Band / DERIVED",
            "candidates": [
                "FITBIT / Example Band Pro / DERIVED",
                "FITBIT / Example Watch / DERIVED",
            ],
        }
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
