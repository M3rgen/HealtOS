"""Stable v2 identities: store_records and deletion marking (SQLite).

The write path never decides that two source fingerprints are one device: an unknown
fingerprint gets rows of its own (counted twice at worst, never hidden), and deletion marks
are deferred while a fingerprint not yet decided distinct holds the record. Only the
reconciler (identity-backfill; nightly incremental) decides chains; the write path then
applies them. Each scenario is one seen in the archive or found in review.
"""

from __future__ import annotations

from datetime import timedelta

from identity_records import (
    HEART,
    NEW_NAME,
    OLD_NAME,
    PACKAGE,
    SEDENTARY,
    STEPS,
    SWIM,
    T0,
    chains,
    count,
    counted_rows,
    heart,
    legacy,
    mark,
    name_of,
    phone,
    raw_rows,
    sedentary,
    steps,
    store,
    swim,
    versions_of,
)
from sqlalchemy import select

from app.models.health import HeartRate, Steps
from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.models.raw_versions import RawHealthDataVersion
from app.parsers.common import source_id, temporal
from app.services.identity_backfill import identity_backfill

WATCH = "Example Watch"
HOUR = timedelta(hours=1)


async def reconcile(sessions):
    """The nightly run: incremental, from each data type's checkpoint."""
    return await identity_backfill(sessions, incremental=True)


def visible(rows) -> list:
    return sorted(
        (name_of(row), row.raw_json["heartRate"]["beatsPerMinute"])
        for row in rows
        if row.source_deleted_at is None
    )


async def superseded_ids(sessions) -> set:
    async with sessions() as session:
        return set((await session.scalars(select(RawHealthDataSupersession.old_raw_id))).all())


async def test_an_unknown_fingerprint_gets_a_row_of_its_own(sessions, clock) -> None:
    """A rename the reconciler has not decided: two rows (counted twice), no chain decided by
    the write path, and the complete fetch that no longer returns the old name defers the
    mark instead of hiding anything."""
    first = await store(sessions, HEART, heart(60))
    clock.advance(hours=6)
    fetched_since = clock.now
    second = await store(sessions, HEART, heart(60, name=NEW_NAME))
    assert (first.inserted, second.inserted, second.versions_kept) == (1, 1, 0)
    rows = await raw_rows(sessions, HEART)
    assert [row.google_data_point_id[:14] for row in rows] == ["synthetic:v2s:"] * 2
    assert await chains(sessions) == []
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 0
    assert visible(await raw_rows(sessions, HEART)) == [(NEW_NAME, 60), (OLD_NAME, 60)]


async def test_the_reconciler_ends_double_counting_and_the_chain_is_applied(sessions, clock):
    """Archived before v2, then renamed: separate rows until the reconciler decides the chain
    and links them; from then on the new name is written onto the old rows (versions)."""
    old = await legacy(sessions, HEART, heart(60), T0)
    other = await legacy(sessions, HEART, heart(61, at=T0 + timedelta(minutes=1)), T0)
    await identity_backfill(sessions)  # the full run after the deploy
    clock.advance(hours=6)
    fetched_since = clock.now
    stats = await store(sessions, HEART, heart(60, name=NEW_NAME))
    assert stats.inserted == 1
    window = (T0, T0 + timedelta(seconds=30))
    assert await mark(sessions, HEART, *window, fetched_since) == 0  # deferred
    assert len(visible(await raw_rows(sessions, HEART))) == 3  # counted twice for now
    report = await reconcile(sessions)
    assert report["heart-rate"].linked == {"display-name-change": 1}
    assert [chain["decided"] for chain in report["heart-rate"].chains] == ["this run"]
    assert await chains(sessions) == [
        (f"FITBIT / {OLD_NAME} / DERIVED", f"FITBIT / {NEW_NAME} / DERIVED", "reconciler")
    ]
    assert old in await superseded_ids(sessions)
    # The decided chain is applied: the other record's row gets the new name as a version.
    clock.advance(hours=1)
    stats = await store(sessions, HEART, heart(61, at=T0 + timedelta(minutes=1), name=NEW_NAME))
    assert (stats.inserted, stats.updated, stats.versions_kept) == (0, 1, 1)
    assert [name_of_json(v) for v in await versions_of(sessions, other)] == [OLD_NAME]
    assert await count(sessions, HeartRate) == 3  # normalized rows updated, not doubled


async def test_a_decided_chain_versions_the_archived_row_and_aliases_it(sessions, clock):
    raw_id = await legacy(sessions, HEART, heart(60), T0)
    await identity_backfill(sessions)
    clock.advance(hours=6)
    await store(sessions, HEART, heart(70, at=T0 + 6 * HOUR, name=NEW_NAME))  # new instant
    await reconcile(sessions)
    clock.advance(hours=1)
    stats = await store(sessions, HEART, heart(60, name=NEW_NAME))
    assert (stats.inserted, stats.updated) == (0, 1)
    rows = await raw_rows(sessions, HEART)
    assert rows[0].id == raw_id
    assert rows[0].google_data_point_id == source_id(HEART, heart(60))  # its key never changes
    assert name_of(rows[0]) == NEW_NAME
    async with sessions() as session:
        aliased = (await session.scalars(select(RawHealthDataIdentity.raw_id))).all()
    assert raw_id in aliased


async def test_package_suffix_change_keeps_one_row_once_decided(sessions, clock) -> None:
    await legacy(sessions, STEPS, steps(40), T0)
    await identity_backfill(sessions)
    clock.advance(hours=8)
    await store(sessions, STEPS, steps(41, start=T0 + 8 * HOUR, source=phone(PACKAGE)))
    await reconcile(sessions)
    await store(sessions, STEPS, steps(40, source=phone(PACKAGE)))
    rows = await raw_rows(sessions, STEPS)
    assert len(rows) == 2
    assert rows[0].raw_json["dataSource"]["application"]["packageName"] == PACKAGE
    assert await count(sessions, Steps) == 2


async def test_interval_civil_fields_keep_one_row(sessions, clock) -> None:
    await store(sessions, SWIM, swim(12))
    clock.advance(days=1)
    stats = await store(sessions, SWIM, swim(12, civil=True))
    assert (stats.inserted, stats.updated) == (0, 1)
    assert len(await raw_rows(sessions, SWIM)) == 1


async def test_growing_sedentary_period_is_one_record_with_versions(sessions, clock) -> None:
    for minutes in (15, 30, 45):
        await store(sessions, SEDENTARY, sedentary(minutes))
        clock.advance(minutes=15)
    [row] = await raw_rows(sessions, SEDENTARY)
    assert temporal(SEDENTARY, row.raw_json)[1] == T0 + timedelta(minutes=45)
    assert await count(sessions, RawHealthDataVersion) == 2


async def test_genuine_deletion_is_still_marked(sessions, clock) -> None:
    kept, gone = heart(60), heart(61, at=T0 + timedelta(minutes=1))
    await store(sessions, HEART, kept, gone)
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, kept)
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 1
    rows = await raw_rows(sessions, HEART)
    assert [
        row.raw_json["heartRate"]["beatsPerMinute"] for row in rows if row.source_deleted_at
    ] == [61]


async def test_two_devices_in_one_page_stay_apart(sessions, clock) -> None:
    band, watch = heart(60), heart(64, name=WATCH)
    await store(sessions, HEART, band, watch)
    clock.advance(hours=1)
    stats = await store(sessions, HEART, watch, band)
    assert (stats.unchanged, stats.versions_kept) == (2, 0)
    rows = await raw_rows(sessions, HEART)
    assert len(rows) == 2 and all(
        row.google_data_point_id.startswith("synthetic:v2s:") for row in rows
    )
    clock.advance(hours=1)
    assert (await store(sessions, HEART, watch)).unchanged == 1


async def test_deleted_record_of_one_device_is_marked_next_to_another(sessions, clock) -> None:
    """Seen together (overlapping sightings): decided distinct without any reconciler run, so
    the band's row never keeps the watch's deleted record."""
    band, watch = heart(60), heart(64, name=WATCH)
    await store(sessions, HEART, band, watch)
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, band)
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 1
    [deleted] = [row for row in await raw_rows(sessions, HEART) if row.source_deleted_at]
    assert deleted.raw_json == watch


async def test_another_devices_row_never_keeps_a_deleted_record(sessions, clock) -> None:
    """Two devices returned together (before and after the full run); one record is deleted
    upstream: a complete fetch marks it."""
    for backfilled in (False, True):
        start = T0 + timedelta(days=2) * backfilled
        clock.now = start
        await store(sessions, HEART, heart(60, at=start), heart(90, at=start, name=WATCH))
        if backfilled:
            await identity_backfill(sessions)
        clock.now = start + 5 * HOUR
        fetched_since = clock.now
        await store(sessions, HEART, heart(60, at=start))  # the watch's record is gone
        assert await mark(sessions, HEART, start, start + HOUR, fetched_since) == 1
        marked = [row for row in await raw_rows(sessions, HEART) if row.source_deleted_at]
        assert [name_of(row) for row in marked][-1] == WATCH


async def test_a_new_name_is_never_written_onto_another_devices_row(sessions, clock) -> None:
    """Review 2 blocker 2: no "newest row" fallback; the other device's row keeps its
    payload, the unknown name gets a row of its own."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + 3 * HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR, last=T0 + 12 * HOUR)
    watch = await legacy(sessions, HEART, heart(64, name=WATCH), T0, last=T0 + 13 * HOUR)
    clock.now = T0 + 20 * HOUR
    stats = await store(sessions, HEART, heart(60, name="Example Band 2"))
    assert stats.inserted == 1
    assert await versions_of(sessions, watch) == []
    [row] = [row for row in await raw_rows(sessions, HEART) if row.id == watch]
    assert name_of(row) == WATCH


async def test_a_decided_chain_never_writes_a_different_reading(sessions, clock) -> None:
    """Even along a decided chain, a record that measured something else than the old row
    gets a row of its own: a source wrongly taken for a renamed device hides nothing."""
    await legacy(sessions, HEART, heart(60), T0)
    await identity_backfill(sessions)
    clock.advance(hours=6)
    await store(sessions, HEART, heart(70, at=T0 + 6 * HOUR, name=NEW_NAME))
    await reconcile(sessions)
    stats = await store(sessions, HEART, heart(95, name=NEW_NAME))  # same instant, other value
    assert stats.inserted == 1
    assert visible(await raw_rows(sessions, HEART)) == [
        (NEW_NAME, 70),
        (NEW_NAME, 95),
        (OLD_NAME, 60),
    ]
    report = await reconcile(sessions)
    assert report["heart-rate"].kept_differing == 1 and not report["heart-rate"].linked


async def test_a_resumed_fetch_never_renames_a_device_seen_before_the_crash(sessions, clock):
    """Re-review 1 #1: page 1 before a crash returned the band; the resumed page 2 returns a
    second watch at the band's instant. It is a source of its own: no chain, no version."""
    started = clock.now
    await store(sessions, HEART, heart(60), heart(61, at=T0 + timedelta(minutes=1)))
    clock.advance(minutes=30)
    await store(
        sessions,
        HEART,
        heart(95, name="Second Watch"),
        heart(96, name="Second Watch", at=T0 + timedelta(minutes=2)),
        fetch_started=started,
    )
    rows = await raw_rows(sessions, HEART)
    assert len(rows) == 4 and await count(sessions, RawHealthDataVersion) == 0
    assert await chains(sessions) == []


async def test_an_old_name_coming_back_keeps_the_chain_and_counts_once(sessions, clock):
    """Review B3: the old name returns after the chain was decided. That is no contradiction
    (a name can change back): the chain stays in use, each record Google returns in either
    form with the same value is counted once, and verify stays green without notes."""
    from app.services.identity import verify_identities

    old = await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 7 * HOUR
    await store(
        sessions, HEART, heart(60, name=NEW_NAME), heart(65, at=T0 + 7 * HOUR, name=NEW_NAME)
    )
    await reconcile(sessions)
    assert old in await superseded_ids(sessions)
    clock.now = T0 + 30 * HOUR
    fetched_since = clock.now
    # The whole window again, in the old name: both records.
    stats = await store(sessions, HEART, heart(60), heart(65, at=T0 + 7 * HOUR))
    assert (stats.inserted, stats.updated) == (0, 2)
    assert await mark(sessions, HEART, T0 - HOUR, T0 + 8 * HOUR, fetched_since) == 0
    await reconcile(sessions)
    assert visible(await counted_rows(sessions)) == [(OLD_NAME, 60), (OLD_NAME, 65)]
    # And back again: still one row per record.
    clock.advance(hours=1)
    stats = await store(sessions, HEART, heart(60, name=NEW_NAME))
    assert (stats.inserted, stats.updated) == (0, 1)
    assert len(visible(await counted_rows(sessions))) == 2
    async with sessions() as session:
        _, errors, notes = await verify_identities(session)
    assert errors == [] and notes == []


async def test_marking_defers_while_a_rename_is_undecided(sessions, clock) -> None:
    """Re-review 2 (within five minutes, inside a long fetch): the new name appears pages
    after the old one in the same fetch. Nothing is marked; the reconciler cannot decide
    (gap under five minutes) and lists it; both rows stay visible."""
    await store(sessions, HEART, heart(60), heart(61, at=T0 + timedelta(minutes=1)))
    await identity_backfill(sessions)
    clock.advance(hours=3)
    fetched_since = clock.now
    await store(sessions, HEART, heart(61, at=T0 + timedelta(minutes=1)))  # page 1: old name
    clock.advance(minutes=2)
    await store(sessions, HEART, heart(60, name=NEW_NAME))  # page 3: renamed meanwhile
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 0
    report = await reconcile(sessions)
    [case] = report["heart-rate"].ambiguous
    assert "less than five minutes" in case["why"]
    assert len(visible(await raw_rows(sessions, HEART))) == 3


async def test_three_step_rename_chains_step_by_step(sessions, clock) -> None:
    """A -> B -> C, where B only ever returned new instants (it never met A's rows): each
    step satisfies the rule, so both links are decided; C's records land on A's rows."""
    old = await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 5 * HOUR
    await store(sessions, HEART, heart(70, at=T0 + 5 * HOUR, name=NEW_NAME))
    clock.now = T0 + 9 * HOUR
    await store(sessions, HEART, heart(80, at=T0 + 9 * HOUR, name="Example Band 3"))
    report = await reconcile(sessions)
    assert {(chain["from"], chain["to"]) for chain in report["heart-rate"].chains} == {
        (f"FITBIT / {OLD_NAME} / DERIVED", f"FITBIT / {NEW_NAME} / DERIVED"),
        (f"FITBIT / {NEW_NAME} / DERIVED", "FITBIT / Example Band 3 / DERIVED"),
    }
    clock.advance(hours=1)
    stats = await store(sessions, HEART, heart(60, name="Example Band 3"))
    assert (stats.inserted, stats.updated) == (0, 1)
    rows = await raw_rows(sessions, HEART)
    assert [name_of(row) for row in rows if row.id == old] == ["Example Band 3"]


async def test_a_second_device_alongside_blocks_a_chain(sessions, clock) -> None:
    """Rule 4: another source of the class seen alongside either form - the rename stays
    undecided (listed), both rows stay visible, nothing is marked."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(90, name=WATCH), T0, last=T0 + 20 * HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 8 * HOUR
    fetched_since = clock.now
    await store(sessions, HEART, heart(60, name=NEW_NAME), heart(90, name=WATCH))
    report = await reconcile(sessions)
    assert report["heart-rate"].chains == [] and not report["heart-rate"].linked
    assert "alongside" in report["heart-rate"].ambiguous[0]["why"]
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 0
    assert len(visible(await raw_rows(sessions, HEART))) == 3


async def test_marking_reads_candidates_in_chunks_and_skips_superseded_rows(
    sessions, clock, monkeypatch
) -> None:
    """Bounded memory; a superseded row is history and is never marked."""
    from app.sync import deletions

    monkeypatch.setattr(deletions, "CHUNK", 2)
    old = await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR, last=T0 + 10 * HOUR)
    for minute in range(1, 5):
        await legacy(sessions, HEART, heart(70, at=T0 + timedelta(minutes=minute)), T0)
    await identity_backfill(sessions)
    clock.now = T0 + 20 * HOUR
    fetched_since = clock.now
    await store(sessions, HEART, heart(60, name=NEW_NAME))
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 4
    async with sessions() as session:
        from app.models.state import RawHealthData

        superseded_row = await session.get(RawHealthData, old)
    assert superseded_row is not None and superseded_row.source_deleted_at is None


async def test_a_stale_stored_start_is_kept_by_marking_and_linked_by_the_backfill(
    sessions, clock
) -> None:
    """A row whose source_start_time an older parser wrote differently: deletion marking sees
    its twin as the same record, and the backfill links them (settled last by id)."""
    stale = await legacy(sessions, SWIM, swim(12), T0, start=T0 + timedelta(seconds=30))
    clock.now = T0 + 5 * HOUR
    fetched_since = clock.now
    await store(sessions, SWIM, swim(12, civil=True))
    assert len(await raw_rows(sessions, SWIM)) == 2
    assert await mark(sessions, SWIM, T0, T0 + HOUR, fetched_since) == 0
    report = await identity_backfill(sessions, batch_size=1)
    assert report["swim-lengths-data"].start_mismatches == 1
    async with sessions() as session:
        link = await session.scalar(
            select(RawHealthDataSupersession).where(RawHealthDataSupersession.old_raw_id == stale)
        )
    assert link is not None and link.reason == "interval-shape-change"


def name_of_json(record: dict) -> str:
    return record["dataSource"]["device"]["displayName"]


# --- write path timing (re-review 2 blocker 3) ----------------------------------------------


async def test_last_seen_is_stamped_after_the_lock_and_never_moves_back(
    sessions, clock, monkeypatch
) -> None:
    """A writer that waited for the data type's lock stamps the time it got it, and a writer
    that stamps an older time never moves ``last_seen_at`` backwards: a fetch that started
    meanwhile does not see the record as unseen (no false deletion mark)."""
    from app.services import identity

    original = identity.lock_data_type

    async def waiting(session, data_type, **options):
        clock.advance(minutes=10)  # the time spent waiting behind a reconciler batch
        await original(session, data_type, **options)

    monkeypatch.setattr(identity, "lock_data_type", waiting)
    before = clock.now
    await store(sessions, HEART, heart(60))
    [row] = await raw_rows(sessions, HEART)
    assert row.last_seen_at.replace(tzinfo=None) == (before + timedelta(minutes=10)).replace(
        tzinfo=None
    )
    monkeypatch.setattr(identity, "lock_data_type", original)
    seen = clock.advance(hours=1)
    await store(sessions, HEART, heart(60))  # the fetch that started at `seen`
    clock.now = seen - timedelta(minutes=30)  # a delayed writer with an older time
    await store(sessions, HEART, heart(60))
    [row] = await raw_rows(sessions, HEART)
    assert row.last_seen_at.replace(tzinfo=None) == seen.replace(tzinfo=None)
    assert await mark(sessions, HEART, T0, T0 + HOUR, seen) == 0


async def test_an_old_name_coming_back_never_marks_the_new_names_rows(sessions, clock) -> None:
    """Validation of the redesign (review 2 s2): the device's old name is returned again for
    an old window. Both forms' spans now overlap, but they were never returned together for
    one key, so they are not decided distinct: the new name's rows are not marked."""
    await store(sessions, HEART, heart(60))
    clock.advance(hours=6)
    await store(sessions, HEART, heart(60, name=NEW_NAME))
    clock.advance(hours=6)
    fetched_since = clock.now
    await store(sessions, HEART, heart(60))  # the old name comes back
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 0
    assert visible(await raw_rows(sessions, HEART)) == [(NEW_NAME, 60), (OLD_NAME, 60)]


async def test_two_devices_archived_together_are_distinct_after_the_backfill(sessions, clock):
    """Re-review 1 probe: two devices' rows of one instant, archived before v2 and last seen
    by the same fetch, are two sources once the full run has read them: a deletion of one is
    marked next to the other."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(90, name=WATCH), T0, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 5 * HOUR
    fetched_since = clock.now
    await store(sessions, HEART, heart(60))  # the watch's record is gone
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 1
    [marked] = [row for row in await raw_rows(sessions, HEART) if row.source_deleted_at]
    assert name_of(marked) == WATCH
