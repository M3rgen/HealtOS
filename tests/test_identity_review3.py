"""Record identity, third review: every finding as a regression test (SQLite).

B1 hidden readings, B2 verify after a later rename, B3 an old name coming back, B4 near
renames left open silently, B5 one stray co-sighting, B6 a wrong chain nobody sees, B7
suspicious deletions counted as genuine, B8 a second device blocking a rename, and the minor
findings that need no PostgreSQL (those that do are in tests/integration/
test_pg_identity_review3.py). Each failed on the code the review looked at.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, timedelta

from identity_records import (
    HEART,
    NEW_NAME,
    OLD_NAME,
    PACKAGE,
    SEDENTARY,
    STEPS,
    T0,
    count,
    counted_rows,
    fitbit,
    heart,
    interval,
    legacy,
    mark,
    name_of,
    phone,
    steps,
    store,
)
from sqlalchemy import event, select
from typer.testing import CliRunner

from app.google_health.capture import CapturedResponse
from app.models.identity import (
    RawHealthDataSupersession,
    RawSourceChain,
    RawSourceDecision,
    RawSourcePair,
    RawSourceSighting,
)
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.parsers.identity import source_fingerprint
from app.services.dashboard import dashboard_data
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import decide_sources
from app.services.source_chains import Chains, Span, decide_chains, load_chains
from app.services.storage import store_records

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
BAND3 = "Example Band 3"
WATCH = "Example Watch"
OLD, NEW = source_fingerprint(fitbit(OLD_NAME)), source_fingerprint(fitbit(NEW_NAME))


async def reconcile(sessions):
    return await identity_backfill(sessions, incremental=True)


async def superseded(sessions) -> set:
    async with sessions() as session:
        return set((await session.scalars(select(RawHealthDataSupersession.old_raw_id))).all())


async def problems(sessions) -> tuple[list, list]:
    async with sessions() as session:
        _, errors, notes = await verify_identities(session)
    return errors, notes


def bpm(rows) -> list:
    return sorted((name_of(row), row.raw_json["heartRate"]["beatsPerMinute"]) for row in rows)


async def day_steps(sessions) -> int | None:
    async with sessions() as session:
        data = await dashboard_data(session, date(2026, 10, 18), 1, "UTC")
    return data["days"][0]["steps"]


# --- B1: never hide a reading Google returns -----------------------------------------------


async def test_old_package_back_after_a_blip_is_counted(sessions, clock) -> None:
    """Review 2 (sh_steps, blip): the suffixed form's row was merged into the plain form's;
    a short blip marks both; then the suffixed form comes back. It counts again (the
    dashboard shows Google's total) and verify stays green."""
    old = await legacy(sessions, STEPS, steps(40), T0, last=T0 + HOUR)
    await legacy(sessions, STEPS, steps(40, source=phone(PACKAGE)), T0 + 9 * HOUR)
    await identity_backfill(sessions)
    assert old in await superseded(sessions)
    assert await day_steps(sessions) == 40
    clock.now = T0 + 30 * HOUR
    assert await mark(sessions, STEPS, T0 - HOUR, T0 + HOUR, clock.now) == 2  # the blip
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, STEPS, steps(40))  # the suffixed form again
    assert await mark(sessions, STEPS, T0 - HOUR, T0 + HOUR, fetched_since) == 0
    assert [row.raw_json["steps"]["count"] for row in await counted_rows(sessions, STEPS)] == ["40"]
    assert await day_steps(sessions) == 40
    await reconcile(sessions)
    assert await day_steps(sessions) == 40
    errors, notes = await problems(sessions)
    # The suffixed row counts again (its holder was marked in the blip): noted, no error.
    assert errors == [] and len(notes) == 1 and "count again" in notes[0]


async def test_a_stray_copy_in_the_new_name_hides_nothing(sessions, clock) -> None:
    """Review 2 (stray): the old name comes back for the whole window, with one stray copy
    of one record in the new name in the same page. Every record is counted once and no
    holder is marked."""
    at = T0 + MINUTE
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(61, at=at), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR)
    await legacy(sessions, HEART, heart(61, at=at, name=NEW_NAME), T0 + 9 * HOUR)
    await identity_backfill(sessions)
    for day in (30, 54):
        clock.now = T0 + day * HOUR
        fetched_since = clock.now
        await store(sessions, HEART, heart(60), heart(61, at=at), heart(60, name=NEW_NAME))
        assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 0
        assert [value for _, value in bpm(await counted_rows(sessions))] == [60, 61]
        await reconcile(sessions)
    assert await problems(sessions) == ([], [])


async def test_old_name_back_with_another_value_is_counted(sessions, clock) -> None:
    """Review 1 #2: A -> B linked (60); A comes back with 65: the 65 is visible at once, and
    the next complete fetch marks the 60 that Google no longer returns."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 30 * HOUR
    await store(sessions, HEART, heart(65))  # a webhook: no deletion check
    assert (OLD_NAME, 65) in bpm(await counted_rows(sessions))
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, heart(65))
    await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since)
    assert bpm(await counted_rows(sessions)) == [(OLD_NAME, 65)]
    assert (await problems(sessions))[0] == []


async def test_a_growing_period_of_the_old_name_counts_at_its_full_length(sessions, clock):
    """Review 1 #2 (identity.py:310-313): a sedentary period in progress at the rename; its
    old form comes back grown. The grown period is what counts: visible at once (a row of its
    own: its own row is hidden behind the shorter end), and alone once the complete fetch
    that returned it marks the end it no longer returned (lane AB: a row with another end is
    another value, never linked to it, so that end coming back is never hidden either)."""

    def period(minutes: int, name: str) -> dict:
        return {
            "dataSource": fitbit(name),
            "sedentaryPeriod": {"interval": interval(T0, minutes, civil=True)},
        }

    await legacy(sessions, SEDENTARY, period(30, OLD_NAME), T0 + 30 * MINUTE, last=T0 + HOUR)
    await legacy(sessions, SEDENTARY, period(45, NEW_NAME), T0 + 9 * HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 30 * HOUR
    since = clock.now
    await store(sessions, SEDENTARY, period(60, OLD_NAME))
    ends = [
        row.raw_json["sedentaryPeriod"]["interval"]["endTime"]
        for row in await counted_rows(sessions, SEDENTARY)
    ]
    assert "2026-10-18T10:00:00Z" in ends
    await mark(sessions, SEDENTARY, T0 - HOUR, T0 + HOUR, since)
    [row] = await counted_rows(sessions, SEDENTARY)
    assert row.raw_json["sedentaryPeriod"]["interval"]["endTime"] == "2026-10-18T10:00:00Z"
    assert (await problems(sessions))[0] == []


# --- B2: verify after a later rename ----------------------------------------------------------


async def test_verify_accepts_links_after_a_later_rename(sessions, clock) -> None:
    """Review 2 (si.py): A -> B decided and linked; then B -> C; the history fetched again in
    C's form gets rows of its own, linked to B's at once. The links stay valid for verify."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 5 * HOUR, last=T0 + 6 * HOUR)
    await identity_backfill(sessions)
    assert len(await superseded(sessions)) == 1
    clock.now = T0 + 9 * HOUR
    await store(sessions, HEART, heart(70, at=T0 + 9 * HOUR, name=BAND3))
    await asyncio.sleep(1.1)  # the links' created_at (CURRENT_TIMESTAMP: whole seconds)
    await reconcile(sessions)
    clock.advance(hours=1)
    stats = await store(sessions, HEART, heart(60, name=BAND3))
    assert (stats.inserted, stats.updated) == (1, 0)
    assert len(await counted_rows(sessions)) == 2
    # C's new row holds B's, which holds A's (readers count C's alone).
    assert await problems(sessions) == (
        [],
        ["1 holder row(s) are superseded themselves (readers follow both links)"],
    )


# --- B4: near renames, open cases, the owner ------------------------------------------------


async def test_sightings_are_stamped_with_the_time_the_response_was_fetched(sessions, clock):
    """B4: a page stored an hour after its response was fetched (it waited for a lock) is
    a sighting at the fetch time."""
    response = CapturedResponse(
        fetched_at=T0 + 2 * HOUR,
        source="sync",
        context_id=None,
        data_type=HEART.name,
        method="GET",
        path="/v4/dataTypes/heart-rate/dataPoints",
        query=None,
        request_body=None,
        attempt=1,
        status_code=200,
        headers={},
        body=b"{}",
    )
    clock.now = T0 + 3 * HOUR
    async with sessions.begin() as session:
        await store_records(session, HEART, [heart(60)], responses=[response])
    async with sessions() as session:
        [sighting] = (await session.scalars(select(RawSourceSighting))).all()
    assert sighting.first_seen_at.replace(tzinfo=UTC) == T0 + 2 * HOUR
    assert sighting.last_seen_at.replace(tzinfo=UTC) == T0 + 2 * HOUR


def test_every_source_left_undecided_is_listed() -> None:
    """B4: a source that appeared while another of its class appeared in between is
    listed with why (it was skipped silently before), as is the one seen alongside."""

    def span(first: float, last: float, label: str) -> Span:
        return Span(T0 + first * HOUR, T0 + last * HOUR, "class", label, True)

    chains = Chains(spans={"x": span(0, 1, "X"), "y": span(3, 10, "Y"), "z": span(9, 12, "Z")})
    decision = decide_chains(chains)
    assert decision.links == []
    whys = {case["source"]: case["why"] for case in decision.ambiguous}
    assert whys["Z"] == "another source of its class appeared in between"
    assert "alongside" in whys["Y"]


def _rename_archive_db(tmp_path, gap: timedelta):
    """A file database with one record of a band renamed ``gap`` after its last sighting."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models  # noqa: F401
    from app.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rename.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await legacy(factory, HEART, heart(60), T0, last=T0 + HOUR)
        await legacy(factory, HEART, heart(60, name=NEW_NAME), T0 + HOUR + gap)

    asyncio.run(prepare())
    return engine, factory


def test_the_nightly_report_and_status_list_what_is_left_open(tmp_path, monkeypatch) -> None:
    """B4: the incremental (nightly) run reports the pairs kept apart and the ambiguous
    sources, and health-mirror status shows them (before: kept_apart only in a full run,
    and status only counted them)."""
    import asyncio

    from app.cli import main as cli

    # A rename in the middle of a fetch: the new name appears 2 min after the old one.
    engine, factory = _rename_archive_db(tmp_path, 2 * MINUTE)
    monkeypatch.setattr(cli, "SessionLocal", factory)
    try:
        assert CliRunner().invoke(cli.app, ["identity-backfill"]).exit_code == 0
        nightly = CliRunner().invoke(cli.app, ["identity-backfill", "--incremental"])
        report = json.loads(nightly.stdout)["data_types"]["heart-rate"]
        [apart] = report["kept_apart"]
        assert apart["fingerprints"] == [OLD[:12], NEW[:12]] and not apart["established"]
        assert "less than five minutes" in report["ambiguous"][0]["why"]
        status = json.loads(CliRunner().invoke(cli.app, ["status"]).stdout)
        [state] = status["identity_reconcile"]
        open_cases = state["last_result"]["open"]
        assert open_cases["ambiguous"][0]["fingerprint"] == NEW[:12]
        assert open_cases["kept_apart"][0]["fingerprints"] == [OLD[:12], NEW[:12]]
    finally:
        asyncio.run(engine.dispose())


async def test_the_owner_links_a_near_rename_and_double_counting_ends(sessions, clock) -> None:
    """B4: identity-decide link, dry run first (it writes nothing and reports the effect),
    then for real: an append-only decision, a chain naming it, the rows linked; verify
    accepts the chain because of the decision."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + HOUR + 2 * MINUTE)
    await identity_backfill(sessions)
    assert len(await counted_rows(sessions)) == 2
    dry = await decide_sources(sessions, "heart-rate", "link", OLD[:12], NEW[:8], dry_run=True)
    assert dry.as_dict()["effect"]["superseded"] == 1
    assert (
        await count(sessions, RawSourceDecision) == 0 and await count(sessions, RawSourceChain) == 0
    )
    outcome = await decide_sources(
        sessions, "heart-rate", "link", OLD[:12], NEW[:12], note="renamed in the app"
    )
    assert outcome.decision_id is not None and outcome.stats.linked
    async with sessions() as session:
        [chain] = (await session.scalars(select(RawSourceChain))).all()
    assert chain.decided_by == "owner" and json.loads(chain.evidence)["owner_decision"] == 1
    assert len(await counted_rows(sessions)) == 1
    assert await problems(sessions) == ([], [])


async def test_rows_seen_minutes_apart_are_not_two_devices(sessions, clock) -> None:
    """B4: the full run no longer takes rows of one key last seen within five minutes for
    two devices (a rename in the middle of a fetch looks exactly like that): no evidence is
    recorded and the old name's row is not marked while the new name returns its record."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + HOUR + 2 * MINUTE)
    await identity_backfill(sessions)
    assert await count(sessions, RawSourcePair) == 0
    clock.now = T0 + 30 * HOUR
    fetched_since = clock.now
    await store(sessions, HEART, heart(60, name=NEW_NAME))
    assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 0


# --- B5: one stray co-sighting --------------------------------------------------------------


async def test_one_stray_co_sighting_does_not_make_two_devices(sessions, clock) -> None:
    """B5: one key returned in one response under both names with two values is evidence,
    not proof; equal values are no evidence (one measurement). Two devices are established
    only by keys from two fetches, the later one returning a record newer than the first
    fetch (review 4: one response is never enough). The owner can set the evidence aside."""
    await store(sessions, HEART, heart(60), heart(61, name=WATCH), heart(70, at=T0 + MINUTE))
    await store(
        sessions, HEART, heart(72, at=T0 + 2 * MINUTE), heart(72, at=T0 + 2 * MINUTE, name=WATCH)
    )
    watch = source_fingerprint(fitbit(WATCH))
    async with sessions() as session:
        chains = await load_chains(session, "heart-rate")
    assert not chains.distinct(OLD, watch)
    assert await count(sessions, RawSourcePair) == 1
    for minute in (3, 4):
        at = T0 + minute * MINUTE
        await store(sessions, HEART, heart(60, at=at), heart(90, at=at, name=WATCH))
    async with sessions() as session:
        chains = await load_chains(session, "heart-rate")
    assert not chains.distinct(OLD, watch)  # three keys, one fetch
    clock.advance(hours=1)
    at = clock.now - MINUTE  # recorded after the first fetch: both still record
    await store(sessions, HEART, heart(60, at=at), heart(91, at=at, name=WATCH))
    async with sessions() as session:
        chains = await load_chains(session, "heart-rate")
    assert chains.distinct(OLD, watch)
    await decide_sources(sessions, "heart-rate", "not-distinct", OLD[:12], watch[:12])
    async with sessions() as session:
        assert not (await load_chains(session, "heart-rate")).distinct(OLD, watch)


async def test_marking_records_no_evidence(sessions, clock) -> None:
    """B5: deletion marking recorded two devices from rows that other writers had stored
    (never returned in one response); only one response may give evidence. A record gone
    whose instant other sources still return with other values is marked."""
    await store(sessions, HEART, heart(60))
    await store(sessions, HEART, heart(90, name=WATCH))
    await store(sessions, HEART, heart(75, name="Old Watch"))
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, heart(60))
    await store(sessions, HEART, heart(90, name=WATCH))
    assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 1
    assert await count(sessions, RawSourcePair) == 0


# --- B6: a chain the reconciler decided is listed for review --------------------------------


async def test_a_decided_chain_is_listed_for_review_and_can_be_rejected(sessions, clock) -> None:
    """B6 (sg2): another device of the class took over without overlap; the reconciler
    chains it. The chain is listed for review; the owner rejects it: it is no longer used,
    the rows it linked stay (they measured the same) and verify stays green."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 9 * HOUR
    await store(sessions, HEART, heart(80, at=T0 + 9 * HOUR, name=WATCH))
    report = (await reconcile(sessions))["heart-rate"]
    watch = source_fingerprint(fitbit(WATCH))
    [review] = report.review
    assert review["fingerprints"] == [OLD[:12], watch[:12]] and review["decided"] == "this run"
    assert report.summary()["open"]["review"][0]["fingerprints"] == [OLD[:12], watch[:12]]
    await decide_sources(sessions, "heart-rate", "reject", OLD[:12], watch[:12])
    clock.advance(hours=1)
    stats = await store(sessions, HEART, heart(60, name=WATCH))  # equal value, other device
    assert stats.inserted == 1
    assert len(await counted_rows(sessions)) == 3
    report = (await reconcile(sessions))["heart-rate"]
    assert report.review == [] and not report.linked
    errors, notes = await problems(sessions)
    assert errors == [] and any("rejected by the owner" in note for note in notes)


async def test_the_nightly_job_logs_what_is_left_to_decide(sessions, clock) -> None:
    """B4/B6: the nightly job's summary (log and status) counts the chains to review and the
    pairs nothing decided, not only the ambiguous sources."""
    from app.services.identity_reconciler import IdentityReconciler

    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 9 * HOUR
    await store(sessions, HEART, heart(80, at=T0 + 9 * HOUR, name=NEW_NAME))
    summary = await IdentityReconciler(sessions).run()
    assert summary is not None
    assert summary["heart-rate"]["review_suggested"] == 1
    assert summary["heart-rate"]["open"]["review"][0]["fingerprints"] == [OLD[:12], NEW[:12]]


# --- B7: suspicious deletions ---------------------------------------------------------------


async def test_deletions_are_split_into_genuine_and_suspicious(sessions) -> None:
    """B7: a deleted record with no other source at its key is a genuine deletion; one
    whose key another, undecided source holds live (a rename nobody decided) is suspicious."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR, deleted=T0 + 2 * HOUR)
    await legacy(
        sessions, HEART, heart(61, at=T0 + MINUTE), T0, last=T0 + HOUR, deleted=T0 + 2 * HOUR
    )
    await legacy(sessions, HEART, heart(61, at=T0 + MINUTE, name=NEW_NAME), T0 + HOUR + MINUTE)
    report = (await identity_backfill(sessions))["heart-rate"]
    assert (report.deletions_kept, report.deletions_suspicious) == (1, 1)
    assert report.summary()["deletions_suspicious"] == 1


# --- B8: a second device does not block a rename --------------------------------------------


async def worn_together(sessions, band: str, minutes, seen, watch_last=None) -> None:
    """One page (stored at ``seen``) with the band and a watch, different values."""
    for minute in minutes:
        at = T0 + minute * MINUTE
        await legacy(sessions, HEART, heart(60 + minute % 30, at=at, name=band), seen)
        await legacy(
            sessions, HEART, heart(90 + minute % 30, at=at, name=WATCH), seen, last=watch_last
        )


async def test_a_watch_worn_alongside_does_not_block_the_bands_rename(sessions) -> None:
    """B8: a watch recorded alongside the band, before and after its rename (pages held
    both, with different values, in two fetches each time, the later one with newer records).
    It is established distinct from both names, so it no longer blocks the band's chain: the
    renamed band is counted once, the watch once."""
    await worn_together(sessions, OLD_NAME, range(3), T0 + 3 * MINUTE)
    await worn_together(sessions, OLD_NAME, range(5, 8), T0 + 8 * MINUTE)
    for minute in (*range(3), *range(5, 8)):  # the old records, renamed
        at = T0 + minute * MINUTE
        await legacy(sessions, HEART, heart(60 + minute, at=at, name=NEW_NAME), T0 + 9 * HOUR)
    await worn_together(sessions, NEW_NAME, range(541, 544), T0 + 9 * HOUR + 4 * MINUTE)
    await worn_together(sessions, NEW_NAME, range(546, 549), T0 + 9 * HOUR + 9 * MINUTE)
    report = (await identity_backfill(sessions))["heart-rate"]
    assert [(chain["from"], chain["to"]) for chain in report.chains] == [
        (f"FITBIT / {OLD_NAME} / DERIVED", f"FITBIT / {NEW_NAME} / DERIVED")
    ]
    assert len(await counted_rows(sessions)) == 6 + 6 + 12  # band 12 records, watch 12
    assert await problems(sessions) == ([], [])


async def test_deciding_a_watch_distinct_lets_the_rename_chain_and_links_at_once(sessions):
    """B8 with a later full run: the watch was seen with the band's new name in no page
    together, so nothing proves it another device and the rename stays open. The owner says
    the watch is another device: the decision records only that (lane AB, AB5: a chain it
    makes possible is listed for the next reconciler run, which decides it with the normal
    review), and the next nightly run chains the rename and links its rows (every row since
    the later source was first seen, not only those written since its checkpoint)."""
    await worn_together(sessions, OLD_NAME, range(3), T0 + 3 * MINUTE)
    await worn_together(sessions, OLD_NAME, range(5, 8), T0 + 8 * MINUTE, T0 + 10 * HOUR)
    for minute in (*range(3), *range(5, 8)):
        at = T0 + minute * MINUTE
        await legacy(sessions, HEART, heart(60 + minute, at=at, name=NEW_NAME), T0 + 9 * HOUR)
    report = (await identity_backfill(sessions))["heart-rate"]
    assert report.chains == [] and len(await counted_rows(sessions)) == 6 * 3
    watch = source_fingerprint(fitbit(WATCH))
    outcome = await decide_sources(sessions, "heart-rate", "distinct", watch[:12], NEW[:12])
    assert outcome.stats.chains == []
    assert [entry["fingerprints"] for entry in outcome.stats.left_to_reconciler] == [
        [OLD[:12], NEW[:12]]
    ]
    assert len(await counted_rows(sessions)) == 6 * 3
    nightly = (await identity_backfill(sessions, incremental=True))["heart-rate"]
    assert [chain["decided"] for chain in nightly.chains] == ["this run"]
    assert [chain["review"] for chain in nightly.review] == ["suggested"]
    assert len(await counted_rows(sessions)) == 6 * 2
    assert await problems(sessions) == ([], [])


# --- minor findings -------------------------------------------------------------------------


async def test_a_source_first_seen_during_the_full_run_is_complete_after_it(
    sessions, clock, monkeypatch
) -> None:
    """A fingerprint first stored while the full run reads the archive was left incomplete
    for good, so its class was never decided again."""
    from app.services import identity_backfill as backfill

    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    original = backfill._timeline

    async def during(*args, **kwargs):
        timeline = await original(*args, **kwargs)
        clock.now = T0 + 9 * HOUR
        await store(sessions, HEART, heart(70, at=T0 + 9 * HOUR, name=NEW_NAME))
        return timeline

    monkeypatch.setattr(backfill, "_timeline", during)
    await identity_backfill(sessions)
    monkeypatch.setattr(backfill, "_timeline", original)
    async with sessions() as session:
        assert all((await session.scalars(select(RawSourceSighting.complete))).all())
    report = (await reconcile(sessions))["heart-rate"]
    assert report.ambiguous == [] and len(report.chains) == 1


async def test_a_linked_row_with_an_id_of_another_scheme_is_no_google_id(sessions) -> None:
    """A row without a Google name whose id is not ``synthetic:`` (restored from an old
    dump under an older id scheme) may be linked; verify judges by its payload, not its id."""
    record = heart(60)
    async with sessions.begin() as session:
        session.add(
            RawHealthData(
                google_data_point_id="legacy-scheme-0001",
                data_type=HEART.name,
                source_start_time=T0,
                raw_json=record,
                payload_hash=payload_hash(record),
                first_seen_at=T0,
                last_seen_at=T0 + HOUR,
            )
        )
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR)
    await identity_backfill(sessions)
    assert len(await superseded(sessions)) == 1
    assert await problems(sessions) == ([], [])


async def test_a_page_records_its_sightings_last_with_few_statements(sessions, clock) -> None:
    """Contention and cost: the sightings row (the one row every writer of a data type
    shares) is written last in the page; an unchanged page needs few statements."""
    records = [heart(60 + minute, at=T0 + minute * MINUTE) for minute in range(5)]
    await store(sessions, HEART, *records)
    statements: list[str] = []
    engine = sessions.kw["bind"].sync_engine

    def seen(_connection, _cursor, statement, *_args) -> None:
        statements.append(statement.split()[0] + " " + statement.split()[2])

    event.listen(engine, "before_cursor_execute", seen)
    try:
        clock.advance(hours=1)
        await store(sessions, HEART, *records)
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert statements[-1] == "INSERT raw_source_sightings"
    assert len(statements) <= 7, statements


def test_status_lists_chains_awaiting_review(tmp_path, monkeypatch) -> None:
    """B6: health-mirror status lists every chain the reconciler decided that the owner
    has neither confirmed nor rejected."""
    import asyncio

    from app.cli import main as cli

    engine, factory = _rename_archive_db(tmp_path, 8 * HOUR)
    monkeypatch.setattr(cli, "SessionLocal", factory)
    try:
        assert CliRunner().invoke(cli.app, ["identity-backfill"]).exit_code == 0
        status = json.loads(CliRunner().invoke(cli.app, ["status"]).stdout)
        [chain] = status["identity_review_suggested"]
        assert chain["fingerprints"] == [OLD[:12], NEW[:12]] and chain["decided_by"] == "backfill"
        decided = CliRunner().invoke(
            cli.app,
            [
                "identity-decide",
                "confirm",
                "--data-type",
                "heart-rate",
                "--from",
                OLD[:12],
                "--to",
                NEW[:12],
            ],
        )
        assert decided.exit_code == 0, decided.output
        assert (
            json.loads(CliRunner().invoke(cli.app, ["status"]).stdout)["identity_review_suggested"]
            == []
        )
        refused = CliRunner().invoke(
            cli.app,
            [
                "identity-decide",
                "reject",
                "--data-type",
                "heart-rate",
                "--from",
                NEW[:12],
                "--to",
                OLD[:12],
            ],
        )
        assert refused.exit_code == 1 and "no chain" in refused.output
    finally:
        asyncio.run(engine.dispose())
