"""Stable v2 identities: store_records and deletion marking (SQLite).

Each scenario is one seen in the archive or found in review: a device rename (also next to a
second device of the same platform), Apple Health's package losing its ``.<UUID>`` suffix,
civil times added to an interval, a sedentary period whose end grows, a genuine deletion,
and two devices that record at the same instant. Whether two source fingerprints are one
renamed device is decided per source (app/services/source_chains.py), never per record.
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


async def test_rename_writes_onto_the_same_row_as_a_new_version(sessions, clock) -> None:
    first = await store(sessions, HEART, heart(60))
    clock.advance(hours=6)
    fetched_since = clock.now
    second = await store(sessions, HEART, heart(60, name=NEW_NAME))
    assert (first.inserted, second.inserted, second.updated, second.versions_kept) == (1, 0, 1, 1)
    [row] = await raw_rows(sessions, HEART)
    assert row.google_data_point_id.startswith("synthetic:v2:")
    assert row.raw_json["dataSource"]["device"]["displayName"] == NEW_NAME
    assert await versions_of(sessions, row.id) == [heart(60)]
    assert await count(sessions, HeartRate) == 1
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 0
    assert (await raw_rows(sessions, HEART))[0].source_deleted_at is None
    assert await chains(sessions) == [
        (f"FITBIT / {OLD_NAME} / DERIVED", f"FITBIT / {NEW_NAME} / DERIVED", "live")
    ]


async def test_rows_archived_before_v2_are_renamed_only_after_the_backfill(sessions, clock):
    """Before identity-backfill the old name's sightings are unknown: no chain, no merge."""
    raw_id = await legacy(sessions, HEART, heart(60), T0)
    clock.advance(hours=6)
    fetched_since = clock.now
    stats = await store(sessions, HEART, heart(60, name=NEW_NAME))
    assert (stats.inserted, await chains(sessions)) == (1, [])
    # A complete fetch marks the old row (the new row shows the same reading) ...
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 1
    # ... and the backfill links it to the new one.
    await identity_backfill(sessions)
    async with sessions() as session:
        link = await session.get(RawHealthDataSupersession, raw_id)
    assert link is not None and link.reason == "display-name-change"


async def test_rename_after_the_backfill_versions_the_archived_row(sessions, clock) -> None:
    raw_id = await legacy(sessions, HEART, heart(60), T0)
    await identity_backfill(sessions)
    clock.advance(hours=6)
    fetched_since = clock.now
    stats = await store(
        sessions,
        HEART,
        heart(60, name=NEW_NAME),
        heart(70, at=T0 + timedelta(minutes=5), name=NEW_NAME),
    )
    assert (stats.inserted, stats.updated) == (1, 1)
    rows = await raw_rows(sessions, HEART)
    assert len(rows) == 2 and rows[0].id == raw_id
    assert rows[0].google_data_point_id == source_id(HEART, heart(60))  # its key never changes
    assert rows[0].raw_json["dataSource"]["device"]["displayName"] == NEW_NAME
    assert await count(sessions, HeartRate) == 2  # the normalized row was updated, not doubled
    async with sessions() as session:
        aliased = (await session.scalars(select(RawHealthDataIdentity.raw_id))).all()
    assert raw_id in aliased
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 0


async def test_package_suffix_change_keeps_one_row(sessions, clock) -> None:
    await legacy(sessions, STEPS, steps(40), T0)
    await identity_backfill(sessions)
    clock.advance(hours=8)
    await store(sessions, STEPS, steps(40, source=phone(PACKAGE)))
    [row] = await raw_rows(sessions, STEPS)
    assert row.raw_json["dataSource"]["application"]["packageName"] == PACKAGE
    assert await count(sessions, Steps) == 1


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
    # One device's record alone still finds its own row.
    clock.advance(hours=1)
    assert (await store(sessions, HEART, watch)).unchanged == 1


async def test_a_device_seen_moments_ago_is_not_merged(sessions, clock) -> None:
    await store(sessions, HEART, heart(60))  # page 1 of a fetch
    clock.advance(seconds=20)
    await store(sessions, HEART, heart(64, name=WATCH))  # page 2: another device
    rows = await raw_rows(sessions, HEART)
    assert len(rows) == 2 and await count(sessions, RawHealthDataVersion) == 0


async def test_deleted_record_of_one_device_is_marked_next_to_another(sessions, clock) -> None:
    band, watch = heart(60), heart(64, name=WATCH)
    await store(sessions, HEART, band, watch)
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, band)
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 1
    [deleted] = [row for row in await raw_rows(sessions, HEART) if row.source_deleted_at]
    assert deleted.raw_json == watch


# --- review findings ----------------------------------------------------------------------


async def test_renamed_device_next_to_a_second_device_is_versioned_not_duplicated(
    sessions, clock
) -> None:
    """Review 2 blocker 1: a re-fetch after a rename, with a second device at one instant,
    versions the renamed device's rows, creates nothing and marks nothing."""
    later = T0 + timedelta(minutes=1)
    await store(sessions, HEART, heart(60), heart(64, name=WATCH), heart(61, at=later))
    clock.advance(hours=6)
    fetched_since = clock.now
    stats = await store(
        sessions,
        HEART,
        heart(60, name=NEW_NAME),
        heart(64, name=WATCH),
        heart(61, at=later, name=NEW_NAME),
    )
    assert (stats.inserted, stats.updated, stats.unchanged, stats.versions_kept) == (0, 2, 1, 2)
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 0
    rows = await raw_rows(sessions, HEART)
    assert sorted(name_of(row) for row in rows) == [NEW_NAME, NEW_NAME, WATCH]
    [watch] = [row for row in rows if name_of(row) == WATCH]
    assert await versions_of(sessions, watch.id) == []  # never another device's payload
    # A second rename, again next to the watch, extends the chain.
    clock.advance(hours=6)
    stats = await store(sessions, HEART, heart(60, name="Example Band 2"), heart(64, name=WATCH))
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 1, 1)
    assert [label.split(" / ")[1] for _, label, _ in await chains(sessions)] == [
        "Example Band 2",
        NEW_NAME,
    ]


async def test_archived_duplicates_and_a_second_device_after_the_backfill(sessions, clock):
    """Review 2 scenario b: old/new name duplicates of one key plus another device; after the
    backfill a second rename versions the row that holds the record."""
    old = await legacy(sessions, HEART, heart(60), T0, last=T0 + 3 * HOUR)
    new = await legacy(
        sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR, last=T0 + 12 * HOUR
    )
    watch = await legacy(sessions, HEART, heart(64, name=WATCH), T0, last=T0 + 13 * HOUR)
    report = await identity_backfill(sessions)
    async with sessions() as session:
        link = await session.get(RawHealthDataSupersession, old)
    assert link is not None and link.new_raw_id == new
    assert report["heart-rate"].linked == {"display-name-change": 1}
    clock.now = T0 + 20 * HOUR
    fetched_since = clock.now
    stats = await store(sessions, HEART, heart(60, name="Example Band 2"), heart(64, name=WATCH))
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 1, 1)
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 0
    assert await versions_of(sessions, watch) == []
    assert [name_of_json(v) for v in await versions_of(sessions, new)] == [NEW_NAME]


def name_of_json(record: dict) -> str:
    return record["dataSource"]["device"]["displayName"]


async def test_a_new_name_is_never_written_onto_another_devices_row(sessions, clock) -> None:
    """Review 2 blocker 2 (first half): without a chain there is no "newest row" fallback;
    the new name gets a row of its own, the other device's row keeps its payload."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + 3 * HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR, last=T0 + 12 * HOUR)
    watch = await legacy(sessions, HEART, heart(64, name=WATCH), T0, last=T0 + 13 * HOUR)
    clock.now = T0 + 20 * HOUR
    stats = await store(sessions, HEART, heart(60, name="Example Band 2"))
    assert stats.inserted == 1
    assert await versions_of(sessions, watch) == []
    [row] = [row for row in await raw_rows(sessions, HEART) if row.id == watch]
    assert name_of(row) == WATCH


async def test_another_devices_row_never_keeps_a_deleted_record(sessions, clock) -> None:
    """Review 2 blocker 2 / review 1: two devices returned together; one record is deleted
    upstream. A complete fetch marks it, before and after the backfill."""
    for backfilled in (False, True):
        start = T0 + timedelta(days=2) * backfilled
        await legacy(sessions, HEART, heart(60, at=start), start, last=start + HOUR)
        await legacy(sessions, HEART, heart(90, at=start, name=WATCH), start, last=start + HOUR)
        if backfilled:
            await identity_backfill(sessions)
        clock.now = start + 5 * HOUR
        fetched_since = clock.now
        await store(sessions, HEART, heart(60, at=start))  # the watch's record is gone
        assert await mark(sessions, HEART, start, start + HOUR, fetched_since) == 1
        marked = [row for row in await raw_rows(sessions, HEART) if row.source_deleted_at]
        assert [name_of(row) for row in marked][-1] == WATCH


async def test_a_source_seen_earlier_in_the_same_fetch_is_not_renamed(sessions, clock) -> None:
    """Review 1: the live decision compares with the current fetch, not the wall clock. The
    band was returned on page 1; a watch record alone on page 2, at an instant where only
    the band had a row, is a second device."""
    await store(sessions, HEART, heart(60), heart(61, at=T0 + HOUR))
    clock.advance(hours=6)
    started = clock.now
    await store(sessions, HEART, heart(61, at=T0 + HOUR), fetch_started=started)  # page 1
    clock.advance(minutes=20)
    stats = await store(sessions, HEART, heart(64, name=WATCH), fetch_started=started)  # page 2
    assert stats.inserted == 1 and await chains(sessions) == []
    assert await count(sessions, RawHealthDataVersion) == 0


async def test_a_contradicted_chain_is_no_longer_used(sessions, clock) -> None:
    """Review 1 (late second device): a fetch that returns only the watch at an instant the
    band had looks like a rename; when the band is returned again, the sightings contradict
    the chain and both are sources of their own again. No reading is lost (versions) and
    both are visible."""
    await store(sessions, HEART, heart(60))
    clock.advance(minutes=10)
    await store(sessions, HEART, heart(90, name=WATCH))
    assert len(await raw_rows(sessions, HEART)) == 1  # taken as the band renamed
    clock.advance(minutes=10)
    fetched_since = clock.now
    await store(sessions, HEART, heart(60), heart(90, name=WATCH))
    rows = await raw_rows(sessions, HEART)
    assert sorted(name_of(row) for row in rows) == [OLD_NAME, WATCH]
    assert await mark(sessions, HEART, T0, T0 + HOUR, fetched_since) == 0
    from app.services.identity import verify_identities

    async with sessions() as session:
        _, errors, notes = await verify_identities(session)
    assert errors == [] and len(notes) == 1 and "contradicted" in notes[0]


async def test_marking_reads_candidates_in_chunks_and_skips_superseded_rows(
    sessions, clock, monkeypatch
) -> None:
    """Review 1: bounded memory; a superseded row is history and is never marked."""
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
    """Review 1 (low): a row whose source_start_time an older parser wrote differently is
    not found by start when a page is stored (it gets a twin), but deletion marking still
    sees the twin as the same record, and the backfill links them (settled last, with the
    rows of the parsed start, even when they are in other batches)."""
    stale = await legacy(sessions, SWIM, swim(12), T0, start=T0 + timedelta(seconds=30))
    clock.now = T0 + 5 * HOUR
    fetched_since = clock.now
    await store(sessions, SWIM, swim(12, civil=True))
    assert len(await raw_rows(sessions, SWIM)) == 2
    assert await mark(sessions, SWIM, T0, T0 + HOUR, fetched_since) == 0
    report = await identity_backfill(sessions, batch_size=1)
    assert report["swim-lengths-data"].start_mismatches == 1
    async with sessions() as session:
        link = await session.get(RawHealthDataSupersession, stale)
    assert link is not None and link.reason == "interval-shape-change"
