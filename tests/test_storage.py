from datetime import timedelta
from itertools import product

import httpx
import pytest
from derived_records import read, streams
from numeric_records import BAD_NUMBERS, numeric_point
from raw_response_records import body, client, stored
from retrieval_records import BAD_DATES, BAD_TIMES, OVERFLOW_TIMES, START, Runner, point
from sqlalchemy import func, select, text

from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.health import HeartRate, Weight
from app.models.raw_versions import RawHealthDataVersion
from app.models.retrieval import RetrievalEvent
from app.models.state import BackfillJob, JobStatus, RawHealthData
from app.parsers.common import source_id
from app.parsers.normalize import normalize
from app.services.dashboard import dashboard_data
from app.services.raw_history import record_history
from app.services.retrieval_coverage import coverage_report
from app.services.storage import store_page, store_records


def heart_rate(bpm=60):
    return {
        "name": "users/1/dataTypes/heart-rate/dataPoints/abc",
        "heartRate": {
            "sampleTime": {"physicalTime": "2026-08-01T10:00:00Z", "utcOffset": "0s"},
            "beatsPerMinute": bpm,
        },
    }


@pytest.mark.parametrize("bad", BAD_DATES)
@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("lane", ["sync", "backfill"])
async def test_daily_date_corrections_archive_http_and_keep_neighbor(sessions, bad, named, lane):
    kind = "daily-resting-heart-rate"
    spec, first, last = REGISTRY[kind], START.date(), START.date() + timedelta(days=1)
    valid = point(kind=kind, named=named)
    broken = {**valid, spec.json_key: {"date": bad, "beatsPerMinute": 99}}
    neighbor = point(str(last), kind, named=named, beatsPerMinute=70)
    runner = Runner(sessions, lane, kind, end=START + timedelta(days=2))
    for index, record in enumerate((valid, broken, valid)):
        if lane == "backfill" and index:
            async with sessions.begin() as session:
                (await session.get(BackfillJob, runner.job_id)).status = JobStatus.pending
        wire = body(record, neighbor)
        google = client(lambda _, wire=wire: httpx.Response(200, content=wire), sessions)
        async with google.http:
            await runner.run(google)
        archived = await stored(sessions)
        assert len(archived) == index + 1 and archived[-1].body == wire and archived[-1].complete
        # Sync marks an absent unnamed record as source-deleted; backfill keeps it.
        expected = None if index == 1 and (named or lane == "sync") else 60
        card = await read(sessions, first, last)
        values = [r["value"] for r in streams(card, "resting_bpm", 0)]
        assert values == ([] if expected is None else [expected])
        assert [r["value"] for r in streams(card, "resting_bpm", 1)] == [70]
        async with sessions() as session:
            panel = await dashboard_data(session, last, 2, "UTC")
            assert [r["resting_bpm"] for r in panel["days"]] == [70, expected]
            report = await coverage_report(session, REGISTRY, first, last, "UTC", kind)
            days = report["types"][0]["days"]
            assert [d["has_value"] for d in days] == [expected is not None, True]
            event = await session.scalar(
                select(RetrievalEvent)
                .where(RetrievalEvent.kind == "page")
                .order_by(RetrievalEvent.id.desc())
                .limit(1)
            )
            assert event.unplaced_count == int(index == 1)
            assert event.returned_times == ([[START.isoformat(), 1]] if index != 1 else []) + [
                [(START + timedelta(days=1)).isoformat(), 1]
            ]


async def test_dedup_and_changed_payload_upsert(sessions):
    async with sessions.begin() as session:
        first = await store_records(session, REGISTRY["heart-rate"], [heart_rate(60)])
    async with sessions.begin() as session:
        duplicate = await store_records(session, REGISTRY["heart-rate"], [heart_rate(60)])
        changed = await store_records(session, REGISTRY["heart-rate"], [heart_rate(65)])
    async with sessions() as session:
        raw_count = await session.scalar(select(func.count(RawHealthData.id)))
        normalized_count = await session.scalar(select(func.count(HeartRate.id)))
        rate = await session.scalar(select(HeartRate))
    assert first.inserted == 1
    assert duplicate.unchanged == 1
    assert changed.updated == 1
    assert raw_count == normalized_count == 1
    assert int(rate.bpm) == 65


async def test_upsert_does_not_release_quarantined_records(sessions):
    async with sessions.begin() as session:
        await store_records(session, REGISTRY["heart-rate"], [heart_rate(60)])
    async with sessions.begin() as session:
        raw = await session.scalar(select(RawHealthData))
        normalized = await session.scalar(select(HeartRate))
        raw.is_quarantined = True
        raw.quarantine_reason = "test-quarantine"
        normalized.is_quarantined = True
        normalized.quarantine_reason = "test-quarantine"
    async with sessions.begin() as session:
        correction = heart_rate(65)
        correction["dataSource"] = {"device": {"displayName": {"bad": 1}}}
        correction["heartRate"]["sampleTime"]["timeZone"] = "x" * 129
        await store_records(session, REGISTRY["heart-rate"], [correction])
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
        normalized = await session.scalar(select(HeartRate))
    assert raw.is_quarantined is True
    assert raw.quarantine_reason == "test-quarantine"
    assert normalized.is_quarantined is True
    assert normalized.quarantine_reason == "test-quarantine"
    assert int(normalized.bpm) == 65


@pytest.mark.parametrize("kind, bad", [("steps", None), *BAD_TIMES])
@pytest.mark.parametrize("named", [False, True])
async def test_invalid_payload_is_still_archived(sessions, kind, bad, named):
    spec = REGISTRY[kind]
    broken = point(bad, kind, named=named) if bad else "corrupt-but-preserved"
    valid = point(kind=kind, named=named)
    async with sessions.begin() as session:
        stats = await store_page(session, spec, Page([broken, valid], None))
    async with sessions() as session:
        raw = list(await session.scalars(select(RawHealthData)))
        assert len(list(await session.scalars(select(normalize(spec, valid).model)))) == 1
    assert stats.inserted == 2 and stats.normalized == 1
    assert stats.normalization_skipped == 1
    payloads = [row.raw_json for row in raw]
    assert len(payloads) == 2 and broken in payloads and valid in payloads
    missing = next(row for row in raw if row.raw_json == broken)
    assert missing.source_start_time is missing.source_end_time is None


def test_synthetic_identity_keeps_same_interval_zones_distinct():
    base = {
        "activeZoneMinutes": {
            "interval": {
                "startTime": "2026-08-01T10:00:00Z",
                "endTime": "2026-08-01T10:01:00Z",
            },
            "activeZoneMinutes": "1",
        }
    }
    fat_burn = {
        **base,
        "activeZoneMinutes": {**base["activeZoneMinutes"], "heartRateZone": "FAT_BURN"},
    }
    cardio = {**base, "activeZoneMinutes": {**base["activeZoneMinutes"], "heartRateZone": "CARDIO"}}
    spec = REGISTRY["active-zone-minutes"]
    assert source_id(spec, fat_burn) != source_id(spec, cardio)


async def test_duplicate_ids_in_one_batch_are_upserted_once(sessions, monkeypatch):
    """PostgreSQL rejects an ON CONFLICT statement that touches the same row twice."""
    from app.services import storage

    seen_batches = []
    original_raw = storage._upsert_raw_many
    original_normalized = storage._upsert_normalized_many

    async def spy_raw(session, rows):
        seen_batches.append([row["google_data_point_id"] for row in rows])
        await original_raw(session, rows)

    async def spy_normalized(session, model, rows):
        seen_batches.append([row["google_data_point_id"] for row in rows])
        await original_normalized(session, model, rows)

    monkeypatch.setattr(storage, "_upsert_raw_many", spy_raw)
    monkeypatch.setattr(storage, "_upsert_normalized_many", spy_normalized)
    async with sessions.begin() as session:
        stats = await store_records(
            session, REGISTRY["heart-rate"], [heart_rate(60), heart_rate(70)]
        )
    async with sessions() as session:
        raw_count = await session.scalar(select(func.count(RawHealthData.id)))
        rate = await session.scalar(select(HeartRate))
    assert seen_batches and all(len(batch) == len(set(batch)) == 1 for batch in seen_batches)
    assert stats.inserted == 1 and stats.updated == 0
    assert raw_count == 1
    assert int(rate.bpm) == 70


@pytest.mark.parametrize(
    "kind,bad",
    [
        *product(("steps", "sleep", "heart-rate", "weight"), OVERFLOW_TIMES),
        *BAD_TIMES,
    ],
)
async def test_overflow_correction_preserves_history_and_hides_stale_value(sessions, kind, bad):
    spec, day = REGISTRY[kind], START.date()
    valid = point(kind=kind)
    broken = {**point(bad, kind), "name": valid["name"]}
    async with sessions() as session:
        empty = await dashboard_data(session, day, 1, "UTC")
    for record, expected_history in [(valid, []), (broken, [valid]), (valid, [valid, broken])]:
        async with sessions.begin() as session:
            stats = await store_records(session, spec, [record])
        assert stats.normalized == int(record == valid)
        assert stats.normalization_skipped == int(record == broken)
        async with sessions() as session:
            raw = (await session.scalars(select(RawHealthData))).one()
            versions = await session.scalars(
                select(RawHealthDataVersion).order_by(RawHealthDataVersion.version)
            )
            assert raw.raw_json == record and [row.raw_json for row in versions] == expected_history
            assert (await dashboard_data(session, day, 1, "UTC") == empty) == (record == broken)


@pytest.mark.parametrize("value", [*BAD_NUMBERS, "10000000000", "9999999999.995"])
async def test_numeric_corrections_keep_raw_versions_and_valid_neighbors(sessions, value):
    spec = REGISTRY["weight"]
    valid, neighbor = numeric_point(), numeric_point(value="71000")
    broken = {**numeric_point(value=value), "name": valid["name"]}
    for record, history, skipped in [
        (valid, [], 0),
        (broken, [valid], 1),
        (valid, [valid, broken], 0),
    ]:
        async with sessions.begin() as session:
            stats = await store_page(session, spec, Page([record, neighbor], None))
        assert stats.normalization_skipped == skipped
        assert stats.normalized == (2 if not history else 1 - skipped)
        assert stats.versions_kept == bool(history)
        async with sessions() as session:
            raw = list(await session.scalars(select(RawHealthData)))
            versions = list(await session.scalars(select(RawHealthDataVersion)))
            weights = list(await session.scalars(select(Weight).order_by(Weight.kilograms)))
            assert len(raw) == 2 and {row.google_data_point_id for row in raw} == {
                valid["name"],
                neighbor["name"],
            }
            assert (
                next(row.raw_json for row in raw if row.google_data_point_id == valid["name"])
                == record
            )
            assert [
                row.raw_json for row in sorted(versions, key=lambda row: row.version)
            ] == history
            assert [float(row.kilograms) for row in weights] == [71, 72.5]


@pytest.mark.parametrize(
    "records",
    [[numeric_point()], [point(metadata=[1]), point(heartRateMetadata={"motionContext": {}})]],
)
@pytest.mark.parametrize("failure", ["connection", "sql"])
async def test_validation_does_not_hide_database_failures(sessions, monkeypatch, records, failure):
    from sqlalchemy.exc import DBAPIError, OperationalError

    from app.services import storage

    async def disconnected(session, *args):
        if failure == "sql":
            await session.execute(text("SELECT * FROM synthetic_missing_table"))
        raise OperationalError("synthetic disconnect", None, RuntimeError("connection lost"))

    monkeypatch.setattr(storage, "_upsert_normalized_many", disconnected)
    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            kind = "weight" if "weight" in records[0] else "heart-rate"
            await store_page(session, REGISTRY[kind], Page(records, None))
    async with sessions() as session:
        assert await session.scalar(select(func.count(RawHealthData.id))) == 0


@pytest.mark.parametrize("key", ["metadata", "heartRateMetadata"])
@pytest.mark.parametrize("value", [[1], "bad", 42, True, None, {"motionContext": [1]}])
async def test_metadata_corrections_keep_raw_history_and_neighbors(sessions, key, value):
    valid = point(**{key: {"motionContext": "RESTING"}})
    broken = {**point(beatsPerMinute=72, **{key: value}), "name": valid["name"]}
    neighbor = point(heartRateMetadata={"motionContext": "ACTIVE"})
    for record, history in [(broken, []), (valid, [broken]), (broken, [broken, valid])]:
        page = Page([neighbor, record], None)
        async with sessions.begin() as session:
            stats = await store_page(session, REGISTRY["heart-rate"], page)
        assert (stats.normalization_skipped, stats.normalized) == (0, 1 if history else 2)
        assert stats.versions_kept == bool(history)
        async with sessions() as session:
            for current, past in [(record, history), (neighbor, [])]:
                archived = await record_history(session, "heart-rate", google_id=current["name"])
                assert [row["payload"] for row in archived[0]["versions"]] == [*past, current]
            rates = {r.google_data_point_id: r for r in await session.scalars(select(HeartRate))}
        assert len(rates) == 2 and rates[neighbor["name"]].motion_context == "ACTIVE"
        rate = rates[valid["name"]]
        assert rate.motion_context == ("RESTING" if record == valid else None)
        assert rate.bpm == (60 if record == valid else 72)
        assert rate.timestamp.replace(tzinfo=START.tzinfo) == START.replace(hour=12)
