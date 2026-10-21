"""Review 5 of the record identities: a payload goes only onto a row of its own source.

AA1: same-source duplicates were linked across sources and a re-fetch moved one source's
payload onto another's row, so a ``reject`` (or a refutation) left records counted two or three
times within one device, for good. AA2: a row hidden behind a stale holder was written to.
AA3: a deletion was marked on evidence that could still be overturned. AA4: one rename decided
by hand left every later rename ambiguous. No value here is real health data.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import date, timedelta

from identity_records import (
    HEART,
    T0,
    counted_rows,
    fitbit,
    heart,
    legacy,
    mark,
    name_of,
    raw_rows,
    store,
    versions_of,
)
from sqlalchemy import select, update
from typer.testing import CliRunner

from app.models.identity import IdentityReconcileState, RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.identity import record_identity, source_fingerprint
from app.services.dashboard import dashboard_data
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import decide_sources
from app.services.source_chains import load_chains

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
A1, A2, B1, B2 = "Band A1", "Band A2", "Band B1", "Band B2"


def fp(name: str) -> str:
    return source_fingerprint(fitbit(name))


async def nightly(sessions):
    return (await identity_backfill(sessions, incremental=True))["heart-rate"]


async def per_device(sessions) -> Counter:
    """(instant, device, bpm) -> rows readers count."""
    async with sessions() as session:
        chains = await load_chains(session, "heart-rate")
    found: Counter = Counter()
    for row in await counted_rows(sessions):
        identity = record_identity(HEART, row.raw_json)
        assert identity is not None
        sample = row.raw_json["heartRate"]
        found[
            (
                sample["sampleTime"]["physicalTime"],
                chains.device(identity.source),
                sample["beatsPerMinute"],
            )
        ] += 1
    return found


async def problems(sessions):
    async with sessions() as session:
        _, errors, notes = await verify_identities(session)
    return errors, notes


async def four_names(sessions, clock):
    """A band renamed three times (A1 -> A2 -> B1 -> B2), each name returning the archived
    record again and one new instant; the nightly run chains and links them."""
    await legacy(sessions, HEART, heart(60, name=A1), T0, last=T0 + HOUR)
    await identity_backfill(sessions)  # the full run after the deploy
    for step, name in enumerate((A2, B1, B2), start=1):
        clock.now = T0 + 5 * step * HOUR
        await store(
            sessions, HEART, heart(60, name=name), heart(60 + step, at=clock.now, name=name)
        )
    report = await nightly(sessions)
    assert {(chain["from"], chain["to"]) for chain in report.chains} == {
        (f"FITBIT / {old} / DERIVED", f"FITBIT / {new} / DERIVED")
        for old, new in ((A1, A2), (A2, B1), (B1, B2))
    }


# --- AA1 ---------------------------------------------------------------------------------


async def test_reject_leaves_one_count_per_device_for_same_source_duplicates(sessions, clock):
    """r5a: two archived rows of the old name (before and after civil times), the new name:
    linked root to root, so after ``reject`` each name counts the record once (217257c: three
    rows, the old name's two duplicates counting again), and ``confirm`` restores exactly."""
    await legacy(sessions, HEART, heart(60, name=A1), T0 + 10 * MINUTE)
    await legacy(sessions, HEART, heart(60, name=A1, civil=True), T0 + 20 * MINUTE)
    clock.advance(hours=6)
    await store(sessions, HEART, heart(60, name=A2))
    await identity_backfill(sessions)
    before = {row.id for row in await counted_rows(sessions)}
    assert len(before) == 1
    await decide_sources(sessions, "heart-rate", "reject", fp(A1)[:12], fp(A2)[:12])
    assert sorted(name_of(row) for row in await counted_rows(sessions)) == [A1, A2]
    clock.advance(days=1)
    await nightly(sessions)
    await identity_backfill(sessions)
    assert sorted(name_of(row) for row in await counted_rows(sessions)) == [A1, A2]
    await decide_sources(sessions, "heart-rate", "confirm", fp(A1)[:12], fp(A2)[:12])
    assert {row.id for row in await counted_rows(sessions)} == before
    assert (await problems(sessions))[0] == []


async def test_a_middle_reject_is_settled_at_once_and_undone_exactly(sessions, clock):
    """repro_middle_reject: rejecting A2 -> B1 of A1 -> A2 -> B1 -> B2 left B1 and B2 (one
    device) both counted until a full run. The rows of every lapsed link are settled again by
    the decision itself (also rows stored before 0011), and confirm restores the counts."""
    await four_names(sessions, clock)
    before = {row.id for row in await counted_rows(sessions)}
    assert len(before) == 4
    await decide_sources(sessions, "heart-rate", "reject", fp(A2)[:12], fp(B1)[:12])
    counts = await per_device(sessions)
    assert set(counts.values()) == {1}  # never twice within one device
    assert len(counts) == 5  # the shared record once per device, the others once
    clock.advance(days=1)
    await nightly(sessions)
    assert await per_device(sessions) == counts
    await decide_sources(sessions, "heart-rate", "confirm", fp(A2)[:12], fp(B1)[:12])
    assert {row.id for row in await counted_rows(sessions)} == before
    assert (await problems(sessions))[0] == []


async def test_reject_after_a_re_fetch_never_counts_three_times(sessions, clock):
    """repro_reject_after_refetch: the history fetched again under the newest name went onto
    the older names' rows; a reject then counted records three times, even after a full run.
    Now the newest name's records get rows of their own, linked at once, and no row's history
    holds another source's payload."""
    await four_names(sessions, clock)
    clock.advance(hours=2)
    await store(
        sessions,
        HEART,
        *(heart(60 + step, at=T0 + 5 * step * HOUR, name=B2) for step in range(3)),
        heart(60, name=B2),
    )
    assert len(await counted_rows(sessions)) == 4
    for row in await raw_rows(sessions, HEART):
        assert all(
            name_of_json(version) == name_of(row) for version in await versions_of(sessions, row.id)
        )
    await decide_sources(sessions, "heart-rate", "reject", fp(A2)[:12], fp(B1)[:12])
    assert set((await per_device(sessions)).values()) == {1}
    await identity_backfill(sessions)
    assert set((await per_device(sessions)).values()) == {1}
    await decide_sources(sessions, "heart-rate", "confirm", fp(A2)[:12], fp(B1)[:12])
    assert len(await counted_rows(sessions)) == 4
    assert (await problems(sessions))[0] == []


def name_of_json(payload: dict) -> str | None:
    return payload.get("dataSource", {}).get("device", {}).get("displayName")


async def test_verify_flags_a_payload_of_another_source_in_a_rows_history(sessions) -> None:
    """A payload written onto another source's row (what 217257c's write path did) is an
    error of verify."""
    raw_id = await legacy(sessions, HEART, heart(60, name=A1), T0)
    assert (await problems(sessions))[0] == []
    async with sessions.begin() as session:
        # The 0011 trigger keeps the replaced payload as a version.
        await session.execute(
            update(RawHealthData)
            .where(RawHealthData.id == raw_id)
            .values(raw_json=heart(60, name=A2), payload_hash="forged")
        )
    errors, _ = await problems(sessions)
    assert any("more than one source in their history" in error for error in errors)


async def test_verify_notes_a_record_counted_twice_within_one_device(sessions) -> None:
    """Two archived rows of one source and record, not linked yet (before the full run)."""
    await legacy(sessions, HEART, heart(60, name=A1), T0)
    await legacy(sessions, HEART, heart(60, name=A1, civil=True), T0 + HOUR)
    errors, notes = await problems(sessions)
    assert errors == []
    assert any("1 record(s) of heart-rate are counted more than once" in note for note in notes)
    await identity_backfill(sessions)
    assert await problems(sessions) == ([], [])


# --- AA2 ---------------------------------------------------------------------------------


async def test_a_row_hidden_behind_a_stale_holder_is_never_written_to(sessions, clock) -> None:
    """D -> R (live) -> H (deleted): 217257c followed the links to H, found it deleted and
    wrote Google's new value onto D, which readers still hid behind R's old value. The write
    path asks what readers ask (a link in force to a counted row) and keeps the value counted."""
    for step, name in enumerate((A1, A2, B1)):
        await legacy(sessions, HEART, heart(60, name=name), T0 + 5 * step * HOUR)
    await identity_backfill(sessions)
    rows = {name_of(row): row for row in await raw_rows(sessions, HEART)}
    d, r, h = rows[A1], rows[A2], rows[B1]
    async with sessions.begin() as session:
        links = {
            (link.old_raw_id, link.new_raw_id)
            for link in (await session.scalars(select(RawHealthDataSupersession))).all()
        }
        assert (d.id, h.id) in links and (r.id, h.id) in links
        session.add(
            RawHealthDataSupersession(
                old_raw_id=d.id,
                new_raw_id=r.id,
                data_type="heart-rate",
                old_google_data_point_id=d.google_data_point_id,
                new_google_data_point_id=r.google_data_point_id,
                reason="display-name-change",
                old_fingerprint=fp(A1),
                new_fingerprint=fp(A2),
                same_reading=True,
                new_payload_hash=r.payload_hash,
            )
        )
        await session.execute(
            update(RawHealthData).where(RawHealthData.id == h.id).values(source_deleted_at=T0)
        )
    assert [name_of(row) for row in await counted_rows(sessions)] == [A2]
    clock.now = T0 + 20 * HOUR
    await store(sessions, HEART, heart(70, name=A1))
    assert sorted(
        row.raw_json["heartRate"]["beatsPerMinute"] for row in await counted_rows(sessions)
    ) == [
        60,
        70,
    ]


# --- AA3 ---------------------------------------------------------------------------------


async def test_reprocessed_records_under_a_new_name_mark_nothing_and_are_listed(sessions, clock):
    """reproc_far: at a rename Google returned three old-name records with other values, and
    again 27 h later for newer records, while thousands came back with equal values under the
    new name. 217257c ruled two devices (the spans overlap by more than a day, so not even
    review_distinct) and marked every old-name row. Equal readings dominate: a rename with
    reprocessing, never marked, listed for review."""
    keys = [T0 + index * MINUTE for index in range(12)]
    for at in keys:
        await legacy(sessions, HEART, heart(60, at=at, name=A1), at)
    await identity_backfill(sessions)
    rename = T0 + 2 * HOUR
    clock.now = rename
    fetched_since = clock.now
    stray = [heart(61, at=at, name=A1) for at in keys[:3]]
    await store(sessions, HEART, *(heart(60, at=at, name=A2) for at in keys), *stray)
    later = rename + 27 * HOUR
    clock.now = later
    newer = [later - (index + 1) * MINUTE for index in range(3)]
    await store(
        sessions,
        HEART,
        *(heart(62, at=at, name=A2) for at in newer),
        *(heart(63, at=at, name=A1) for at in newer),
    )
    async with sessions() as session:
        chains = await load_chains(session, "heart-rate")
    assert chains.pair_distinct(fp(A1), fp(A2))  # the evidence alone says two devices
    assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 0
    report = await nightly(sessions)
    assert [sorted(entry["fingerprints"]) for entry in report.review_distinct] == [
        sorted([fp(A1)[:12], fp(A2)[:12]])
    ]
    assert report.deferred_deletions and report.summary()["deferred_deletions"] == 9
    async with sessions() as session:
        chains = await load_chains(session, "heart-rate")
    assert chains.agreeing(fp(A1), fp(A2)) and not chains.pair_distinct(fp(A1), fp(A2))
    assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 0


def test_status_lists_deferred_deletions(tmp_path, monkeypatch) -> None:
    """Deferred deletions were only logged (SOURCE_DELETIONS_DEFERRED): status shows them."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models  # noqa: F401
    from app.cli import main as cli
    from app.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'deferred.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await legacy(factory, HEART, heart(60, name=A1), T0)
        await identity_backfill(factory)
        async with factory.begin() as session:
            from app.services.storage import store_records

            await store_records(session, HEART, [heart(60, name="Example Watch")])
        await mark(factory, HEART, T0 - HOUR, T0 + HOUR, T0 + 2 * HOUR)

    asyncio.run(prepare())
    monkeypatch.setattr(cli, "SessionLocal", factory)
    try:
        status = json.loads(CliRunner().invoke(cli.app, ["status"]).stdout)
        [entry] = status["identity_deferred_deletions"]
        assert entry["data_type"] == "heart-rate" and entry["deferred"] == 1
        assert entry["sources"][0]["fingerprints"] == [fp(A1)[:12], fp("Example Watch")[:12]]
    finally:
        asyncio.run(engine.dispose())


async def test_distinct_warns_that_its_marks_are_permanent(sessions, clock) -> None:
    await legacy(sessions, HEART, heart(60, name=A1), T0)
    clock.advance(hours=1)
    await store(sessions, HEART, heart(61, name="Example Watch"))
    await identity_backfill(sessions)
    for dry_run in (True, False):
        outcome = await decide_sources(
            sessions,
            "heart-rate",
            "distinct",
            fp(A1)[:12],
            fp("Example Watch")[:12],
            dry_run=dry_run,
        )
        assert "MARKED DELETED" in outcome.as_dict()["deletions"]


# --- AA4 ---------------------------------------------------------------------------------


async def test_a_rename_decided_by_hand_lets_the_next_rename_chain(sessions, clock) -> None:
    """repro_cascade: a rename in the middle of a fetch (A2 -> B1, both names in one page) is
    left to the owner; the next clean rename B1 -> B2 stayed ambiguous ("more than one source
    of its class ended before it": A2, B1) even after the owner linked A2 -> B1: a form that
    continues as another source is no predecessor."""
    await legacy(sessions, HEART, heart(60, name=A1), T0, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 5 * HOUR
    await store(sessions, HEART, heart(61, at=clock.now, name=A2))
    report = await nightly(sessions)
    assert [chain["fingerprints"] for chain in report.chains] == [[fp(A1)[:12], fp(A2)[:12]]]
    clock.now = T0 + 10 * HOUR
    await store(
        sessions, HEART, heart(62, at=clock.now, name=A2), heart(63, at=clock.now + MINUTE, name=B1)
    )
    clock.now = T0 + 15 * HOUR
    await store(sessions, HEART, heart(64, at=clock.now, name=B2))
    report = await nightly(sessions)
    assert [chain["fingerprints"] for chain in report.chains] == [[fp(A1)[:12], fp(A2)[:12]]]
    assert {entry["fingerprint"] for entry in report.ambiguous} == {fp(B1)[:12], fp(B2)[:12]}
    await decide_sources(sessions, "heart-rate", "link", fp(A2)[:12], fp(B1)[:12])
    report = await nightly(sessions)
    assert {tuple(chain["fingerprints"]) for chain in report.chains} == {
        (fp(A1)[:12], fp(A2)[:12]),
        (fp(A2)[:12], fp(B1)[:12]),
        (fp(B1)[:12], fp(B2)[:12]),
    }
    assert report.ambiguous == []
    # B1 -> B2 rests on A2 -> B1 (A2 would compete otherwise): a reject undoes both, exactly.
    counted = {row.id for row in await counted_rows(sessions)}
    await decide_sources(sessions, "heart-rate", "reject", fp(A2)[:12], fp(B1)[:12])
    report = await nightly(sessions)
    states = {tuple(chain["fingerprints"]): chain["state"] for chain in report.chains}
    assert states[(fp(B1)[:12], fp(B2)[:12])] == "lapsed"
    await decide_sources(sessions, "heart-rate", "confirm", fp(A2)[:12], fp(B1)[:12])
    assert {row.id for row in await counted_rows(sessions)} == counted


async def test_a_quarantined_holder_hides_nothing(sessions) -> None:
    await legacy(sessions, HEART, heart(60, name=A1), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(60, name=A2), T0 + 5 * HOUR)
    await identity_backfill(sessions)
    [counted] = await counted_rows(sessions)
    async with sessions.begin() as session:
        await session.execute(
            update(RawHealthData).where(RawHealthData.id == counted.id).values(is_quarantined=True)
        )
    shown = [row for row in await counted_rows(sessions) if not row.is_quarantined]
    assert len(shown) == 1 and shown[0].id != counted.id


async def test_with_deletions_included_a_live_row_is_never_hidden_behind_a_deleted_one(
    sessions, clock
) -> None:
    """include_source_deleted: 217257c hid every superseded row, so a live row whose holder
    was marked deleted disappeared and the deleted holder's old value was shown instead."""
    await legacy(sessions, HEART, heart(60, name=A1), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(60, name=A2), T0 + 5 * HOUR)
    await identity_backfill(sessions)
    [holder] = await counted_rows(sessions)
    async with sessions.begin() as session:
        await session.execute(
            update(RawHealthData).where(RawHealthData.id == holder.id).values(source_deleted_at=T0)
        )
    clock.now = T0 + 9 * HOUR
    other = A1 if name_of(holder) == A2 else A2
    await store(sessions, HEART, heart(80, name=other))
    async with sessions() as session:
        day = await dashboard_data(
            session, date(2026, 10, 18), 1, "UTC", include_source_deleted=True
        )
    assert day["charts"]["heart_rate"][9] == 80


async def test_a_failed_incremental_run_says_incremental(sessions, monkeypatch) -> None:
    from app.services import identity_backfill as reconciler

    await legacy(sessions, HEART, heart(60, name=A1), T0)
    await identity_backfill(sessions)

    async def failing(*args, **kwargs):
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(reconciler, "reconcile_data_type", failing)
    report = await identity_backfill(sessions, incremental=True)
    assert report["heart-rate"].error == "statement timeout"
    async with sessions() as session:
        state = await session.get(IdentityReconcileState, "heart-rate")
    assert (state.last_mode, state.last_error) == ("incremental", "statement timeout")


def test_identity_decide_help_describes_reject_as_it_is() -> None:
    from app.cli import main as cli

    output = CliRunner().invoke(cli.app, ["identity-decide", "--help"], terminal_width=200).output
    assert "stay linked" not in output and "count on their own" in " ".join(output.split())


async def test_the_views_query_requires_a_view(sessions) -> None:
    """A materialized view in place of an analytics view passed verify (precedence of OR)."""
    from app.services import identity

    sql = " ".join(str(identity._VIEWS_SQL).split())
    assert "c.relkind = 'v' AND (" in sql


async def test_a_link_settles_rows_by_the_clock_the_sightings_use(sessions, clock) -> None:
    """Decisions settled rows by updated_at (the database clock on insert) against a source's
    first sighting (the application's fetch time): with the two clocks apart, a link linked
    nothing. Rows are selected by first_seen_at, the sightings' own clock, and only at start
    times holding more than one row."""
    await legacy(sessions, HEART, heart(60, name=A1), T0, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + HOUR + 2 * MINUTE  # a rename within a fetch: left to the owner
    await store(sessions, HEART, heart(60, name=A2), heart(61, at=clock.now, name=A2))
    async with sessions.begin() as session:
        await session.execute(update(RawHealthData).values(updated_at=T0 - 30 * HOUR))
    assert len(await counted_rows(sessions)) == 3
    outcome = await decide_sources(sessions, "heart-rate", "link", fp(A1)[:12], fp(A2)[:12])
    assert outcome.stats.linked == {"display-name-change": 1}
    assert len(await counted_rows(sessions)) == 2
