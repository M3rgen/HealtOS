"""Synthetic SQLite coverage of the archive reader shared by both reconciliation paths."""

import uuid
from array import array
from datetime import UTC, datetime, timedelta, timezone

import pytest
from identity_records import HEART, STEPS, T0, heart, legacy, store

from app.core.timeutil import as_utc
from app.models.identity import RawHealthDataSupersession
from app.models.raw_versions import RawHealthDataEvent
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.services import identity_backfill as backfill
from app.services import identity_backfill_reading as reading
from app.services.identity_backfill_stats import BackfillStats


def raw(index, start, **values):
    record = values.pop("raw_json", heart(60, at=start or T0))
    return RawHealthData(
        **{
            "id": uuid.UUID(int=index),
            "google_data_point_id": f"synthetic:reader:{index}",
            "data_type": HEART.name,
            "source_start_time": start,
            "raw_json": record,
            "payload_hash": payload_hash(record),
            "first_seen_at": T0,
            "last_seen_at": T0 + timedelta(hours=1),
            "updated_at": T0,
            **values,
        }
    )


def test_legacy_exports_are_the_production_reader():
    for name in (
        "_COLUMNS",
        "_Row",
        "_read_batch",
        "_rows_at",
        "_rows_of",
        "_EPOCH",
        "_micros",
        "_from_micros",
        "_collect_starts",
    ):
        assert getattr(backfill, name) is getattr(reading, name)
    assert reading._Row.__module__ == reading.__name__
    assert reading._read_batch.__module__ == reading.__name__
    assert reading._collect_starts.__module__ == reading.__name__


@pytest.mark.parametrize("batch_size", [1, 2, 3, 5, 10])
async def test_batches_finish_the_last_start_and_resume_without_duplicates(sessions, batch_size):
    later = T0 + timedelta(minutes=1)
    last = T0 + timedelta(minutes=2)
    async with sessions.begin() as session:
        session.add_all(
            [
                raw(5, T0),
                raw(2, T0),
                raw(4, later),
                raw(1, later),
                raw(3, later),
                raw(6, last),
                raw(7, None),
                raw(8, T0, data_type=STEPS.name),
            ]
        )
    cursor, found = None, []
    async with sessions() as session:
        while batch := await reading._read_batch(session, HEART.name, batch_size, cursor, None):
            # Every start in this batch includes all its rows, including the boundary start.
            for start, expected in ((T0, {2, 5}), (later, {1, 3, 4}), (last, {6})):
                group = {row[0].int for row in batch if as_utc(row[3]) == start}
                assert not group or group == expected
            found.extend(row[0].int for row in batch)
            cursor = (batch[-1][3], batch[-1][0])
        resumed = await reading._read_batch(session, HEART.name, 1, None, later)
        # Once a cursor exists, it takes precedence over the initial --after cutoff.
        following = await reading._read_batch(
            session, HEART.name, 1, (resumed[-1][3], resumed[-1][0]), last + timedelta(days=1)
        )
    assert found == [2, 5, 1, 3, 4, 6]
    assert [row[0].int for row in resumed] == [1, 3, 4]
    assert [row[0].int for row in following] == [6]


async def test_rows_of_separates_stale_starts_and_preserves_parsing_counters(sessions):
    later = T0 + timedelta(minutes=1)
    async with sessions.begin() as session:
        session.add_all(
            [
                raw(1, T0, is_quarantined=True),
                raw(2, T0, raw_json=heart(61, at=later), source_deleted_at=T0),
                raw(3, T0, raw_json={"name": "users/me/dataTypes/heart-rate/dataPoints/example"}),
                raw(4, T0, raw_json={"heartRate": {"beatsPerMinute": 62}}),
                raw(5, None, raw_json=heart(63)),
            ]
        )
    async with sessions() as session:
        batch = await reading._read_batch(session, HEART.name, 10, None, None)
    stats = BackfillStats(rows=7, named=2, unidentified=3)
    rows, stale = reading._rows_of(HEART, batch, stats)
    assert [row.raw_id.int for row in rows] == [1]
    assert [row.raw_id.int for row in stale] == [2]
    assert (stats.rows, stats.named, stats.unidentified, stats.start_mismatches) == (11, 3, 4, 0)
    assert rows[0].first_seen_at == T0
    assert rows[0].last_seen_at == T0 + timedelta(hours=1)
    assert rows[0].quarantined and not rows[0].deleted
    assert rows[0].payload_hash == payload_hash(heart(60))
    assert stale[0].deleted and not stale[0].quarantined
    assert stale[0].identity.start == later
    assert rows[0].order > stale[0].order
    assert reading._rows_of(HEART, batch, None) == (rows, stale)


async def test_rows_at_deduplicates_and_chunks_starts_without_crossing_data_types(sessions):
    starts = [T0 + timedelta(seconds=i) for i in range(2001)]
    async with sessions.begin() as session:
        session.add_all(
            [
                raw(1, starts[0]),
                raw(2, starts[1999]),
                raw(3, starts[2000]),
                raw(4, starts[2000]),
                raw(5, starts[0], data_type=STEPS.name),
                raw(6, None),
            ]
        )
    async with sessions() as session:
        found = await reading._rows_at(session, HEART, [*reversed(starts), starts[0], starts[-1]])
        assert await reading._rows_at(session, HEART, []) == []
    assert sorted(row[0].int for row in found) == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("changed", "touched", "lapsed", "expected"),
    [
        (False, False, False, []),
        (True, False, False, [1, 3]),
        (False, True, False, [3, 4]),
        (False, False, True, [2, 3, 5]),
        (True, True, True, [1, 2, 3, 4, 5]),
    ],
)
async def test_collect_starts_merges_changed_returned_touched_and_lapsed_rows(
    sessions, changed, touched, lapsed, expected
):
    day = timedelta(days=1)
    since = T0 + 10 * day

    def at(offset):
        return T0 + offset * day

    records = [
        raw(1, at(3), updated_at=since, payload_received_at=since, first_seen_at=since),
        raw(2, at(3)),
        raw(3, at(1)),
        raw(4, at(5)),
        raw(5, at(2)),
        raw(6, at(4), first_seen_at=since),
        raw(7, at(4)),
        raw(8, at(6), first_seen_at=since),  # Lone start does not need relinking.
        raw(9, at(7), updated_at=since, payload_received_at=T0),
        raw(10, at(8), updated_at=T0, payload_received_at=since),
        raw(11, at(9), updated_at=since),  # Legacy payload has no received timestamp.
        raw(12, None, updated_at=since, payload_received_at=since),
        raw(13, at(10), data_type=STEPS.name, updated_at=since, payload_received_at=since),
        raw(14, at(11)),
        raw(15, at(12)),
        raw(16, at(13), first_seen_at=since),
        raw(17, at(13), data_type=STEPS.name),  # Another type is not a companion.
    ]
    async with sessions.begin() as session:
        session.add_all(records)
        await session.flush()
        for index, kind, occurred in (
            (3, "source-returned", since),
            (3, "source-returned", since + day),
            (14, "source-returned", since - day),
            (15, "source-deleted", since),
        ):
            row = records[index - 1]
            session.add(
                RawHealthDataEvent(
                    raw_id=row.id,
                    data_type=row.data_type,
                    google_data_point_id=row.google_data_point_id,
                    event=kind,
                    occurred_at=occurred,
                )
            )
        for old, new, pair in ((4, 5, ("b", "a")), (1, 2, ("a", "b")), (14, 15, ("a", "c"))):
            left, right = records[old - 1], records[new - 1]
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=left.id,
                    new_raw_id=right.id,
                    data_type=HEART.name,
                    old_google_data_point_id=left.google_data_point_id,
                    new_google_data_point_id=right.google_data_point_id,
                    reason="source-rename",
                    old_fingerprint=pair[0],
                    new_fingerprint=pair[1],
                    same_reading=True,
                    new_payload_hash=right.payload_hash,
                )
            )
    found = await reading._collect_starts(
        sessions,
        HEART.name,
        since=since if changed else None,
        touched=since if touched else None,
        lapsed={("a", "b")} if lapsed else set(),
    )
    assert isinstance(found, array) and found.typecode == "q"
    assert [reading._from_micros(value) for value in found] == [at(offset) for offset in expected]


@pytest.mark.parametrize(
    "value",
    [
        datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        T0.replace(microsecond=123456),
        T0.replace(tzinfo=None),
        T0.astimezone(timezone(timedelta(hours=3))),
    ],
)
def test_microseconds_round_trip_utc_naive_offset_and_pre_epoch_times(value):
    assert reading._from_micros(reading._micros(value)) == as_utc(value)
    assert reading._micros(reading._EPOCH) == 0


async def test_full_and_incremental_runs_keep_legacy_reader_monkeypatch_points(
    sessions, clock, monkeypatch
):
    calls = []
    for name in ("_read_batch", "_rows_at", "_collect_starts"):
        original = getattr(backfill, name)

        async def tracked(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return await _original(*args, **kwargs)

        monkeypatch.setattr(backfill, name, tracked)
    await legacy(sessions, HEART, heart(60), T0)
    full = await backfill.identity_backfill(sessions, data_types=[HEART.name], batch_size=1)
    assert full[HEART.name].mode == "full" and "_read_batch" in calls
    calls.clear()
    clock.advance(minutes=1)
    await store(sessions, HEART, heart(61))
    incremental = await backfill.identity_backfill(
        sessions, data_types=[HEART.name], batch_size=1, incremental=True
    )
    assert incremental[HEART.name].mode == "incremental"
    assert "_collect_starts" in calls and "_rows_at" in calls
