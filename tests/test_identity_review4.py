"""Record identity, fourth review: every finding as a regression test (SQLite).

Z1 spare ids found again, Z2 one response never makes two devices, Z3 a wrong link undone in
what readers count, Z4 a rejected chain frees its sources, and the minor findings that need no
PostgreSQL (those that do are in tests/integration/test_pg_identity_review4.py). Each failed on
the code the review looked at.
"""

from __future__ import annotations

from datetime import date, timedelta

from identity_records import (
    HEART,
    NEW_NAME,
    OLD_NAME,
    T0,
    count,
    counted_rows,
    fitbit,
    heart,
    legacy,
    mark,
    name_of,
    raw_rows,
    store,
)
from sqlalchemy import select, text

from app.models.identity import RawHealthDataSupersession, RawSourcePair
from app.parsers.identity import source_fingerprint
from app.services.dashboard import dashboard_data
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import DecisionRefused, decide_sources
from app.services.source_chains import load_chains

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
WATCH = "Example Watch"
CHARGE = "Example Charge"
OLD, NEW = source_fingerprint(fitbit(OLD_NAME)), source_fingerprint(fitbit(NEW_NAME))
WATCH_FP = source_fingerprint(fitbit(WATCH))
CHARGE_FP = source_fingerprint(fitbit(CHARGE))


async def reconcile(sessions):
    return await identity_backfill(sessions, incremental=True)


async def problems(sessions) -> tuple[list, list]:
    async with sessions() as session:
        _, errors, notes = await verify_identities(session)
    return errors, notes


def bpm(rows) -> list:
    return sorted((name_of(row), row.raw_json["heartRate"]["beatsPerMinute"]) for row in rows)


async def hour_bpm(sessions) -> float | None:
    async with sessions() as session:
        data = await dashboard_data(session, date(2026, 10, 18), 1, "UTC")
    return data["charts"]["heart_rate"][9]


# --- Z1: spare ids are found again ----------------------------------------------------------


async def test_a_record_on_a_spare_id_is_found_again_and_never_piles_up(sessions, clock) -> None:
    """Z1: old name, rename, the old name back with a changed value, the reconciler, then
    the value changes again: the record went onto a spare id (its own v2s id held another
    value, merged). Every later fetch added another counted spare row, and after eight the
    page failed. The spare row is found by id like any other: one row, counted once, and
    any number of such cycles never fails a page."""
    await legacy(sessions, HEART, heart(60), T0)
    await identity_backfill(sessions)
    clock.advance(hours=6)
    await store(sessions, HEART, heart(60, name=NEW_NAME))
    clock.advance(hours=6)
    await identity_backfill(sessions)
    clock.advance(hours=1)
    await store(sessions, HEART, heart(70))
    clock.advance(hours=1)
    await store(sessions, HEART, heart(70, name=NEW_NAME))
    clock.advance(hours=1)
    await identity_backfill(sessions)
    rows = len(await raw_rows(sessions, HEART))
    for _ in range(4):
        clock.advance(hours=1)
        await store(sessions, HEART, heart(75))
        assert bpm(await counted_rows(sessions)) == [(NEW_NAME, 70), (OLD_NAME, 75)]
    assert len(await raw_rows(sessions, HEART)) == rows + 1  # one spare row, written onto
    for value in range(80, 92):  # more cycles than the eight spare ids there used to be
        clock.advance(hours=1)
        await store(sessions, HEART, heart(value, name=NEW_NAME))
        await store(sessions, HEART, heart(value))
        await reconcile(sessions)
        assert [value for _, value in bpm(await counted_rows(sessions))] == [value]
    assert (await problems(sessions))[0] == []


# --- Z2: one response never makes two devices ------------------------------------------------


async def band_renamed_with_a_stray(sessions, clock, keys: int) -> None:
    """The band is renamed; in the fetch that sees the new name first, Google also returns
    ``keys`` old records in the old form with values one off (one response)."""
    for minute in range(keys + 2):
        await store(sessions, HEART, heart(60 + minute, at=T0 + minute * MINUTE))
    clock.advance(hours=6)
    page = [heart(60 + minute, at=T0 + minute * MINUTE, name=NEW_NAME) for minute in range(keys)]
    page += [heart(61 + minute, at=T0 + minute * MINUTE) for minute in range(keys)]
    await store(sessions, HEART, *page)


async def test_one_response_with_many_differing_keys_makes_no_two_devices(sessions, clock):
    """Z2 (f2 a3/c20/e5): one response returned 3 or 20 keys under both names with different
    values; the rename was refuted and the old name's rows got false deletion marks when
    their window was fetched again (only the new name returned). One response is no proof:
    nothing is established, and an old row whose value the new name still returns is
    deferred, never marked (only the rows holding the stray values Google no longer returns
    are)."""
    await band_renamed_with_a_stray(sessions, clock, keys=20)
    assert await count(sessions, RawSourcePair) == 3  # recorded, bounded
    async with sessions() as session:
        assert not (await load_chains(session, "heart-rate")).distinct(OLD, NEW)
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(
        sessions,
        HEART,
        *[heart(60 + minute, at=T0 + minute * MINUTE, name=NEW_NAME) for minute in range(22)],
    )
    assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 20
    assert await stray_marks_only(sessions)


async def stray_marks_only(sessions) -> bool:
    """Every marked row holds a value Google no longer returns (a stray one-off value)."""
    rows = await raw_rows(sessions, HEART)
    returned = {
        (
            row.raw_json["heartRate"]["sampleTime"]["physicalTime"],
            row.raw_json["heartRate"]["beatsPerMinute"],
        )
        for row in rows
        if name_of(row) == NEW_NAME
    }
    marked = [row for row in rows if row.source_deleted_at is not None]
    return bool(marked) and all(
        (
            row.raw_json["heartRate"]["sampleTime"]["physicalTime"],
            row.raw_json["heartRate"]["beatsPerMinute"],
        )
        not in returned
        for row in marked
    )


async def test_a_rename_established_distinct_is_listed_and_marks_nothing(sessions, clock):
    """Z2: even when evidence from two fetches establishes two devices, a pair whose
    sightings only touch looks like a rename: it is listed for review (report and status),
    and no deletion is marked on its strength."""
    for minute in range(4):
        await store(sessions, HEART, heart(60 + minute, at=T0 + minute * MINUTE))
    clock.advance(minutes=10)
    first = [heart(60 + minute, at=T0 + minute * MINUTE, name=NEW_NAME) for minute in range(4)]
    await store(sessions, HEART, *first, *[heart(70 + m, at=T0 + m * MINUTE) for m in range(3)])
    clock.advance(minutes=10)
    at = clock.now - MINUTE  # a record newer than the first fetch, in both forms
    await store(sessions, HEART, heart(80, at=at, name=NEW_NAME), heart(81, at=at))
    async with sessions() as session:
        assert (await load_chains(session, "heart-rate")).distinct(OLD, NEW)
    clock.advance(days=2)
    report = (await reconcile(sessions))["heart-rate"]
    summary = report.summary()
    assert summary["review_distinct"] == 1
    assert summary["open"]["review_distinct"][0]["fingerprints"] == [OLD[:12], NEW[:12]]
    fetched_since = clock.now
    await store(sessions, HEART, *first)
    assert await mark(sessions, HEART, T0 - HOUR, T0 + 5 * MINUTE, fetched_since) == 3
    assert await stray_marks_only(sessions)


# --- Z3/Z4: a wrong link is undone; a rejected chain frees its sources -----------------------


async def two_bands_with_equal_values(sessions, clock) -> None:
    """The band (renamed later) and another band worn for two hours with equal values; the
    owner links the band to the wrong one."""
    for minute in range(4):
        at = T0 + minute * MINUTE
        await store(
            sessions, HEART, heart(60 + minute, at=at), heart(60 + minute, at=at, name=CHARGE)
        )
    await identity_backfill(sessions)


async def test_a_wrong_link_is_undone_in_what_readers_count(sessions, clock) -> None:
    """Z3 (f3 c6): the owner linked two devices with equal readings; ``reject`` then
    ``distinct`` left the links hiding one device's rows (counts halved for good). Links
    are in force only while their sources are one device: undone at once, and verify
    notes it."""
    await two_bands_with_equal_values(sessions, clock)
    assert len(await counted_rows(sessions)) == 8
    await decide_sources(sessions, "heart-rate", "link", OLD[:12], CHARGE_FP[:12])
    assert len(await counted_rows(sessions)) == 4
    await decide_sources(sessions, "heart-rate", "reject", OLD[:12], CHARGE_FP[:12])
    assert len(await counted_rows(sessions)) == 8
    assert await hour_bpm(sessions) == 61.5
    await decide_sources(sessions, "heart-rate", "distinct", OLD[:12], CHARGE_FP[:12])
    assert len(await counted_rows(sessions)) == 8
    errors, notes = await problems(sessions)
    assert errors == []
    assert any("4 supersession(s) are no longer in force" in note for note in notes)
    # Undone again: linked, the rows count once more.
    await decide_sources(sessions, "heart-rate", "not-distinct", OLD[:12], CHARGE_FP[:12])
    await decide_sources(sessions, "heart-rate", "link", OLD[:12], CHARGE_FP[:12])
    assert len(await counted_rows(sessions)) == 4
    assert (await problems(sessions))[0] == []


async def test_a_rejected_chain_frees_its_sources_for_the_right_link(sessions, clock) -> None:
    """Z4: a rejected chain kept its two sources' slots: the right link was refused ("reject
    that chain first", though it was rejected) and the reconciler reported the source
    ambiguous for good. Only live chains are chains: the right link is made, and its rows
    linked."""
    await two_bands_with_equal_values(sessions, clock)
    await decide_sources(sessions, "heart-rate", "link", OLD[:12], CHARGE_FP[:12])
    await decide_sources(sessions, "heart-rate", "reject", OLD[:12], CHARGE_FP[:12])
    clock.advance(hours=9)
    await store(
        sessions,
        HEART,
        *[heart(60 + minute, at=T0 + minute * MINUTE, name=NEW_NAME) for minute in range(4)],
    )
    assert len(await counted_rows(sessions)) == 12
    outcome = await decide_sources(sessions, "heart-rate", "link", OLD[:12], NEW[:12])
    assert outcome.chain == "live"
    assert len(await counted_rows(sessions)) == 8  # the band once, the other band once
    assert await problems(sessions) == (
        [],
        [
            "4 supersession(s) are no longer in force (their two sources are no longer one "
            "device: a chain was rejected or refuted, or the owner decided them distinct); the "
            "rows they linked count on their own",
            f"source chain of heart-rate 'FITBIT / {OLD_NAME} / DERIVED' -> 'FITBIT / {CHARGE} "
            "/ DERIVED' was rejected by the owner: it is no longer used, and the rows it linked "
            "count on their own unless another chain joins their sources",
        ],
    )
    # Making the rejected chain live again would fork: refused, saying which chain blocks.
    try:
        await decide_sources(sessions, "heart-rate", "link", OLD[:12], CHARGE_FP[:12])
    except DecisionRefused as refused:
        assert f"already continues as {NEW[:12]}" in str(refused)
    else:  # pragma: no cover - the assertion above is the test
        raise AssertionError("a fork was accepted")


# --- minor findings -------------------------------------------------------------------------


async def test_a_later_link_of_two_devices_holding_rows_leaves_one_holder(sessions, clock):
    """Two devices that each held a superseded row (a civil-time duplicate) were joined later
    by the owner: both holders stayed live with the same reading (counted twice for good,
    only a ``conflicts`` number). The other holder and what it held are linked to one."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + 10 * HOUR)
    await legacy(sessions, HEART, heart(60, civil=True), T0 + MINUTE, last=T0 + 10 * HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME, civil=True), T0 + 9 * HOUR + MINUTE)
    report = (await identity_backfill(sessions))["heart-rate"]
    assert report.ambiguous and len(await counted_rows(sessions)) == 2  # seen alongside
    outcome = await decide_sources(sessions, "heart-rate", "link", OLD[:12], NEW[:12])
    assert outcome.stats.conflicts == 0
    assert len(await counted_rows(sessions)) == 1
    assert (await problems(sessions))[0] == []


async def chained_band_and_a_watch(sessions, clock, value: int = 60) -> None:
    """The band renamed (its old row superseded by the new one), then a watch with an equal
    value at the same instant (undecided)."""
    await legacy(sessions, HEART, heart(value), T0, last=T0 + HOUR)
    clock.now = T0 + 9 * HOUR
    await store(sessions, HEART, heart(value, name=NEW_NAME))
    await identity_backfill(sessions)
    clock.advance(hours=1)
    await store(sessions, HEART, heart(value, name=WATCH))


async def test_distinct_from_one_form_is_distinct_from_the_device(sessions, clock) -> None:
    """Distinctness was checked per fingerprint pair: a watch decided distinct from the band's
    new name but not its old one; the band deleted the record: its holder was marked, its old
    row deferred - and counted again. Distinct from a form is distinct from the device."""
    await chained_band_and_a_watch(sessions, clock)
    await decide_sources(sessions, "heart-rate", "distinct", WATCH_FP[:12], NEW[:12])
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, heart(60, name=WATCH))  # the band's record is gone
    assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 2
    assert bpm(await counted_rows(sessions)) == [(WATCH, 60)]


async def test_a_revised_then_deleted_record_is_not_counted_again(sessions, clock) -> None:
    """f1 e2: the band revised a record (70 -> 90) while an undecided watch returned 70 at
    that instant; then the band deleted it. The band's old row (70, its holder now 90) was
    deferred because of the watch, and counted again once the holder was marked: two 70s.
    Its device returned another value for the record, so its 70 is no longer the band's: it
    is marked when the holder's new value is seen."""
    await chained_band_and_a_watch(sessions, clock, 70)
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, heart(90, name=NEW_NAME), heart(70, name=WATCH))
    assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 1
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, heart(70, name=WATCH))
    assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 1
    assert bpm(await counted_rows(sessions)) == [(WATCH, 70)]
    await reconcile(sessions)
    assert bpm(await counted_rows(sessions)) == [(WATCH, 70)]


async def test_superseded_rows_held_by_a_seen_holder_are_not_read_again(
    sessions, clock, monkeypatch
) -> None:
    """Every complete fetch read every live superseded row of its window again (raw JSON
    and all) and found it present, forever. A row whose holder this fetch saw, unchanged
    since they were linked with the same reading, is no candidate."""
    from app.sync import deletions

    await chained_band_and_a_watch(sessions, clock)
    seen: list = []
    original = deletions.present_records

    async def spy(session, spec, candidates, *args):
        seen.extend(raw_id for raw_id, _, _ in candidates)
        return await original(session, spec, candidates, *args)

    monkeypatch.setattr(deletions, "present_records", spy)
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, heart(60, name=NEW_NAME), heart(60, name=WATCH))
    assert await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 0
    async with sessions() as session:
        superseded = (await session.scalars(select(RawHealthDataSupersession.old_raw_id))).all()
    assert superseded and not set(superseded) & set(seen)


async def test_a_close_rename_beside_a_watch_is_listed_and_one_link_resolves_it(sessions, clock):
    """f4 r2: the band was renamed inside one fetch (the old name last seen two minutes before
    the new one) while a watch recorded alongside: listed as ambiguous (no rule may guess),
    counted twice meanwhile; the owner's link resolves every record."""
    for minute in range(4):
        at = T0 + minute * MINUTE
        await store(
            sessions, HEART, heart(60 + minute, at=at), heart(90 + minute, at=at, name=WATCH)
        )
    clock.advance(minutes=2)
    page = [heart(60 + minute, at=T0 + minute * MINUTE, name=NEW_NAME) for minute in range(6)]
    page += [heart(90 + minute, at=T0 + minute * MINUTE, name=WATCH) for minute in range(6)]
    await store(sessions, HEART, *page)
    clock.advance(days=1)
    report = (await reconcile(sessions))["heart-rate"]
    assert [entry["fingerprint"] for entry in report.summary()["open"]["ambiguous"]] == [NEW[:12]]
    assert len(await counted_rows(sessions)) == 4 + 6 + 6
    await decide_sources(sessions, "heart-rate", "link", OLD[:12], NEW[:12])
    assert len(await counted_rows(sessions)) == 6 + 6
    assert (await problems(sessions))[0] == []


async def test_an_unknown_data_type_is_named_as_such(sessions) -> None:
    """``identity-decide --data-type`` with a name no source has said "no source ... has a
    fingerprint", as if the fingerprint were wrong."""
    await store(sessions, HEART, heart(60))
    try:
        await decide_sources(sessions, "heart_rate", "distinct", OLD[:12], NEW[:12])
    except DecisionRefused as refused:
        assert "check --data-type" in str(refused)
    else:  # pragma: no cover - the assertion above is the test
        raise AssertionError("an unknown data type was accepted")


async def test_a_decision_says_how_deferred_deletions_are_decided(sessions, clock) -> None:
    """f2 b0: after ``distinct`` the deletions deferred between the two sources are decided
    only when their window is fetched again; the outcome says so."""
    await chained_band_and_a_watch(sessions, clock)
    outcome = await decide_sources(sessions, "heart-rate", "distinct", WATCH_FP[:12], NEW[:12])
    assert "next complete fetch of its window" in outcome.as_dict()["deletions"]


async def test_sightings_keep_their_label_and_source(sessions) -> None:
    """The widen-only guard let an UPDATE change a sighting's label or dataSource."""
    from sqlalchemy.exc import IntegrityError, OperationalError

    await store(sessions, HEART, heart(60))
    for statement in (
        "UPDATE raw_source_sightings SET label = 'x'",
        "UPDATE raw_source_sightings SET data_source = '{}'",
    ):
        try:
            async with sessions.begin() as session:
                await session.execute(text(statement))
        except (IntegrityError, OperationalError) as refused:
            assert "only widens" in str(refused)
        else:  # pragma: no cover - the assertion above is the test
            raise AssertionError(f"{statement} was accepted")
