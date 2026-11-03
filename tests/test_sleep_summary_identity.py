"""Sleep summaries under a hollow Google name: one row per sleep, dated by its wake day.

Google returns every ``respiratory-rate-sleep-summary`` record under one name whose data point
id is empty (``.../dataPoints/``). Identified by that name alone, each sleep overwrote the one
before it. A record is now identified by the name and its own time (the end of the sleep it
describes), never by a calendar day: sleep by day (night shifts), sleep across midnight and two
sleeps of one day are separate records, each counted on the local day the sleep ended.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from identity_records import counted_rows, mark, raw_rows, store, versions_of
from interpretation_records import call
from interpretation_records import production_reader as production_reader
from sqlalchemy import select, update

from app.google_health.data_types import REGISTRY
from app.models.health import RespiratoryRate
from app.models.identity import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.common import hollow_name, payload_hash, source_id, temporal
from app.services.identity import verify_identities
from app.services.retrieval_coverage import coverage_report

SPEC = REGISTRY["respiratory-rate-sleep-summary"]
NAME = "users/0/dataTypes/respiratory-rate-sleep-summary/dataPoints/"
BAND = {"platform": "FITBIT", "device": {"displayName": "Synthetic band"}}
ZONE = "Europe/Brussels"  # UTC+2 in September
# Sleep by day after a night shift: 08:00-14:00 local, woke 14:00 local on the 20th.
DAY_SLEEP = datetime(2026, 9, 20, 12, tzinfo=UTC)
# Sleep across midnight: 23:00 on the 21st to 07:00 local on the 22nd.
ACROSS_MIDNIGHT = datetime(2026, 9, 22, 5, tzinfo=UTC)
# Two sleeps of the 24th: woke 06:00 and 18:00 local.
SPLIT = (datetime(2026, 9, 24, 4, tzinfo=UTC), datetime(2026, 9, 24, 16, tzinfo=UTC))
# Woke 00:30 local on the 26th: still the 25th in UTC.
AFTER_MIDNIGHT = datetime(2026, 9, 25, 22, 30, tzinfo=UTC)


def summary(at: datetime, bpm: float = 15.0, name: str = NAME) -> dict:
    stats = {"breathsPerMinute": bpm, "standardDeviation": 1.0, "signalToNoise": 9.0}
    return {
        "name": name,
        "dataSource": BAND,
        "respiratoryRateSleepSummary": {
            "sampleTime": {
                "physicalTime": at.isoformat().replace("+00:00", "Z"),
                "utcOffset": "7200s",
            },
            "fullSleepStats": stats,
            "deepSleepStats": stats,
            "lightSleepStats": stats,
            "remSleepStats": stats,
        },
    }


async def legacy_row(sessions, *payloads: dict) -> uuid.UUID:
    """The one row of the bare name as archived before: the first payload, then each later
    one written onto it (the trigger keeps the replaced ones as versions)."""
    first = payloads[0]
    start, end, zone = temporal(SPEC, first)
    seen = datetime(2026, 9, 1, tzinfo=UTC)
    row = RawHealthData(
        id=uuid.uuid4(),
        google_data_point_id=NAME,
        data_type=SPEC.name,
        source_start_time=start,
        source_end_time=end,
        source_timezone=zone,
        raw_json=first,
        payload_hash=payload_hash(first),
        first_seen_at=seen,
        last_seen_at=seen,
    )
    async with sessions.begin() as session:
        session.add(row)
    for payload in payloads[1:]:
        start, end, zone = temporal(SPEC, payload)
        async with sessions.begin() as session:
            await session.execute(
                update(RawHealthData)
                .where(RawHealthData.id == row.id)
                .values(
                    raw_json=payload,
                    payload_hash=payload_hash(payload),
                    source_start_time=start,
                    source_end_time=end,
                )
            )
    return row.id


async def wake_days(sessions, first: date, last: date) -> tuple[list[date], int]:
    async with sessions() as session:
        report = await coverage_report(session, REGISTRY, first, last, ZONE, SPEC.name)
    found = report["types"][0]
    days = [date.fromisoformat(d["date"]) for d in found["days"] if d["has_value"]]
    return days, sum(regime["record_count"] for regime in found["source_regimes"])


def test_a_hollow_name_is_anchored_on_the_records_own_time():
    one, other = summary(DAY_SLEEP), summary(ACROSS_MIDNIGHT)
    assert hollow_name(NAME) and not hollow_name(NAME + "abc") and not hollow_name(None)
    assert source_id(SPEC, one) == f"{NAME}@2026-09-20T12:00:00+00:00"
    assert source_id(SPEC, one) == source_id(SPEC, summary(DAY_SLEEP, bpm=17))
    assert source_id(SPEC, one) != source_id(SPEC, other)
    # A real Google id stays as it is; a hollow one without any time keeps the bare name.
    assert source_id(SPEC, summary(DAY_SLEEP, name=NAME + "abc")) == NAME + "abc"
    timeless = summary(DAY_SLEEP)
    timeless["respiratoryRateSleepSummary"].pop("sampleTime")
    assert source_id(SPEC, timeless) == NAME


async def test_each_sleep_is_its_own_row_and_a_revision_is_a_version(sessions):
    await store(sessions, SPEC, summary(DAY_SLEEP), summary(ACROSS_MIDNIGHT))
    await store(sessions, SPEC, summary(DAY_SLEEP), summary(ACROSS_MIDNIGHT))
    rows = await raw_rows(sessions, SPEC)
    assert len(rows) == 2
    assert {row.google_data_point_id for row in rows} == {
        source_id(SPEC, summary(DAY_SLEEP)),
        source_id(SPEC, summary(ACROSS_MIDNIGHT)),
    }
    # Google revises one sleep's summary: the same row, the earlier payload kept as a version.
    stats = await store(sessions, SPEC, summary(DAY_SLEEP, bpm=16.5))
    assert (stats.inserted, stats.updated, stats.versions_kept) == (0, 1, 1)
    rows = {
        row.source_start_time.replace(tzinfo=UTC): row for row in await raw_rows(sessions, SPEC)
    }
    assert len(rows) == 2
    revised = rows[DAY_SLEEP]
    assert revised.raw_json == summary(DAY_SLEEP, bpm=16.5)
    assert await versions_of(sessions, revised.id) == [summary(DAY_SLEEP)]
    assert await versions_of(sessions, rows[ACROSS_MIDNIGHT].id) == []
    async with sessions() as session:
        normalized = (await session.scalars(select(RespiratoryRate))).all()
    assert len(normalized) == 2


async def test_one_response_with_every_sleep_keeps_them_all(sessions):
    """One page returns many summaries under the one name: none overwrites another."""
    page = [summary(DAY_SLEEP + timedelta(days=offset), 14 + offset / 10) for offset in range(8)]
    stats = await store(sessions, SPEC, *page)
    assert (stats.inserted, stats.updated, stats.versions_kept) == (8, 0, 0)
    assert len(await raw_rows(sessions, SPEC)) == 8


async def test_coverage_counts_each_sleep_on_the_local_day_it_ended(sessions):
    await store(
        sessions,
        SPEC,
        summary(DAY_SLEEP),
        summary(ACROSS_MIDNIGHT),
        *(summary(at) for at in SPLIT),
        summary(AFTER_MIDNIGHT),
    )
    days, records = await wake_days(sessions, date(2026, 9, 19), date(2026, 9, 26))
    # The day sleep counts on its own day, the night across midnight on the day it ended
    # (not the 21st), the two sleeps of the 24th once as a day and twice as records, and a
    # wake just after local midnight on the local day (not the UTC one).
    assert days == [date(2026, 9, 20), date(2026, 9, 22), date(2026, 9, 24), date(2026, 9, 26)]
    assert records == 5


async def test_umay_dates_sleep_summaries_by_their_wake_day(sessions):
    await store(sessions, SPEC, summary(ACROSS_MIDNIGHT, 14), *(summary(at, 16) for at in SPLIT))
    result = await call(
        "physiology",
        data_type=SPEC.name,
        start_date="2026-09-21",
        end_date="2026-09-24",
        timezone=ZONE,
    )
    (row,) = result["summaries"]
    assert [e["date"] for e in row["evidence"]] == ["2026-09-22", "2026-09-24", "2026-09-24"]
    assert row["statistics"]["count"] == 3


async def test_the_old_row_stays_as_it_was_and_counts_once(sessions):
    """The bare-name row holds the last sleep it got (the 22nd), the sleeps before it are its
    versions. It is never written again: the row of its own sleep holds it."""
    old_id = await legacy_row(sessions, summary(DAY_SLEEP, 14), summary(ACROSS_MIDNIGHT, 15))
    before = (await raw_rows(sessions, SPEC))[0]
    kept = await versions_of(sessions, old_id)
    assert kept == [summary(DAY_SLEEP, 14)]
    fetched = datetime.now(UTC)
    await store(sessions, SPEC, summary(DAY_SLEEP, 14), summary(ACROSS_MIDNIGHT, 15))
    await store(sessions, SPEC, summary(DAY_SLEEP, 14), summary(ACROSS_MIDNIGHT, 15))

    rows = {row.google_data_point_id: row for row in await raw_rows(sessions, SPEC)}
    assert len(rows) == 3
    old = rows[NAME]
    for column in ("raw_json", "payload_hash", "last_seen_at", "source_start_time", "updated_at"):
        assert getattr(old, column) == getattr(before, column)
    assert old.source_deleted_at is None
    assert await versions_of(sessions, old_id) == kept
    holder = rows[source_id(SPEC, summary(ACROSS_MIDNIGHT))]
    async with sessions() as session:
        links = (await session.scalars(select(RawHealthDataSupersession))).all()
    assert [(link.old_raw_id, link.new_raw_id, link.reason) for link in links] == [
        (old_id, holder.id, "hollow-name")
    ]
    # Readers count each sleep once; the old row is hidden behind the row of its sleep.
    assert {row.id for row in await counted_rows(sessions, SPEC)} == {
        holder.id,
        rows[source_id(SPEC, summary(DAY_SLEEP))].id,
    }
    days, records = await wake_days(sessions, date(2026, 9, 19), date(2026, 9, 23))
    assert (days, records) == ([date(2026, 9, 20), date(2026, 9, 22)], 2)
    # A complete fetch that returned the sleep does not mark the old row deleted.
    assert (
        await mark(sessions, SPEC, DAY_SLEEP - timedelta(days=1), datetime.now(UTC), fetched) == 0
    )
    async with sessions() as session:
        _, errors, _ = await verify_identities(session)
    assert errors == []


@pytest.mark.parametrize("returned", ["other_value", "not_returned"])
async def test_the_old_row_counts_on_its_own_without_a_row_of_the_same_payload(sessions, returned):
    old_id = await legacy_row(sessions, summary(ACROSS_MIDNIGHT, 15))
    page = [summary(DAY_SLEEP)]
    if returned == "other_value":
        page.append(summary(ACROSS_MIDNIGHT, 18))
    await store(sessions, SPEC, *page)
    async with sessions() as session:
        assert (await session.scalars(select(RawHealthDataSupersession))).all() == []
    assert old_id in {row.id for row in await counted_rows(sessions, SPEC)}


async def test_verify_still_refuses_a_link_that_hides_a_real_google_id(sessions):
    named = summary(DAY_SLEEP, name=NAME + "abc")
    await store(sessions, SPEC, named, summary(DAY_SLEEP, name=NAME + "def"))
    rows = {row.google_data_point_id: row for row in await raw_rows(sessions, SPEC)}
    old, new = rows[NAME + "abc"], rows[NAME + "def"]
    async with sessions.begin() as session:
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old.id,
                new_raw_id=new.id,
                data_type=SPEC.name,
                old_google_data_point_id=old.google_data_point_id,
                new_google_data_point_id=new.google_data_point_id,
                reason="hollow-name",
                old_fingerprint="f",
                new_fingerprint="f",
                same_reading=True,
                new_payload_hash=new.payload_hash,
            )
        )
    async with sessions() as session:
        _, errors, _ = await verify_identities(session)
    assert any("hides a record with a Google id" in error for error in errors)
