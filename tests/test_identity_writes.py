"""Stable v2 identities: store_records and deletion marking (SQLite).

Each scenario is one seen in the archive: a device rename, Apple Health's package losing its
``.<UUID>`` suffix, civil times added to an interval, a sedentary period whose end grows, a
genuine deletion, and two devices that record at the same instant.
"""

from __future__ import annotations

from datetime import timedelta

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
    mark,
    phone,
    raw_rows,
    sedentary,
    steps,
    store,
    swim,
)
from sqlalchemy import select

from app.models.health import HeartRate, Steps
from app.models.identity import RawHealthDataIdentity
from app.models.raw_versions import RawHealthDataVersion
from app.parsers.common import source_id, temporal


async def test_rename_writes_onto_the_same_row_as_a_new_version(sessions, clock) -> None:
    first = await store(sessions, HEART, heart(60))
    clock.advance(hours=6)
    fetched_since = clock.now
    second = await store(sessions, HEART, heart(60, name=NEW_NAME))
    assert (first.inserted, second.inserted, second.updated, second.versions_kept) == (1, 0, 1, 1)
    [row] = await raw_rows(sessions, HEART)
    assert row.google_data_point_id.startswith("synthetic:v2:")
    assert row.raw_json["dataSource"]["device"]["displayName"] == NEW_NAME
    async with sessions() as session:
        [version] = (await session.scalars(select(RawHealthDataVersion))).all()
    assert version.raw_json == heart(60) and version.raw_id == row.id
    assert await count(sessions, HeartRate) == 1
    assert await mark(sessions, HEART, T0, T0 + timedelta(hours=1), fetched_since) == 0
    assert (await raw_rows(sessions, HEART))[0].source_deleted_at is None


async def test_rename_of_a_row_archived_before_v2(sessions, clock) -> None:
    raw_id = await legacy(sessions, HEART, heart(60), T0)
    clock.advance(hours=6)
    fetched_since = clock.now
    await store(sessions, HEART, heart(60, name=NEW_NAME), heart(70, at=T0 + timedelta(minutes=5)))
    rows = await raw_rows(sessions, HEART)
    assert len(rows) == 2 and rows[0].id == raw_id
    assert rows[0].google_data_point_id == source_id(HEART, heart(60))  # its key never changes
    assert rows[0].raw_json["dataSource"]["device"]["displayName"] == NEW_NAME
    assert await count(sessions, HeartRate) == 2  # the normalized row was updated, not doubled
    async with sessions() as session:
        aliased = (await session.scalars(select(RawHealthDataIdentity.raw_id))).all()
    assert raw_id in aliased
    assert await mark(sessions, HEART, T0, T0 + timedelta(hours=1), fetched_since) == 0


async def test_package_suffix_change_keeps_one_row(sessions, clock) -> None:
    await legacy(sessions, STEPS, steps(40), T0)
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
    assert await mark(sessions, HEART, T0, T0 + timedelta(hours=1), fetched_since) == 1
    rows = await raw_rows(sessions, HEART)
    assert [
        row.raw_json["heartRate"]["beatsPerMinute"] for row in rows if row.source_deleted_at
    ] == [61]


async def test_two_devices_in_one_page_stay_apart(sessions, clock) -> None:
    band, watch = heart(60), heart(64, name="Example Watch")
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
    await store(sessions, HEART, heart(64, name="Example Watch"))  # page 2: another device
    rows = await raw_rows(sessions, HEART)
    assert len(rows) == 2 and await count(sessions, RawHealthDataVersion) == 0


async def test_deleted_record_of_one_device_is_marked_next_to_another(sessions, clock) -> None:
    band, watch = heart(60), heart(64, name="Example Watch")
    await store(sessions, HEART, band, watch)
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(sessions, HEART, band)
    assert await mark(sessions, HEART, T0, T0 + timedelta(hours=1), fetched_since) == 1
    [deleted] = [row for row in await raw_rows(sessions, HEART) if row.source_deleted_at]
    assert deleted.raw_json == watch
