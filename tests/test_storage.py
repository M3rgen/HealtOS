from sqlalchemy import func, select

from app.google_health.data_types import REGISTRY
from app.models.health import HeartRate
from app.models.state import RawHealthData
from app.parsers.common import source_id
from app.services.storage import store_records


def heart_rate(bpm=60):
    return {
        "name": "users/1/dataTypes/heart-rate/dataPoints/abc",
        "heartRate": {
            "sampleTime": {"physicalTime": "2026-08-01T10:00:00Z", "utcOffset": "0s"},
            "beatsPerMinute": bpm,
        },
    }


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
        await store_records(session, REGISTRY["heart-rate"], [heart_rate(65)])
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
        normalized = await session.scalar(select(HeartRate))
    assert raw.is_quarantined is True
    assert raw.quarantine_reason == "test-quarantine"
    assert normalized.is_quarantined is True
    assert normalized.quarantine_reason == "test-quarantine"
    assert int(normalized.bpm) == 65


async def test_invalid_payload_is_still_archived(sessions):
    async with sessions.begin() as session:
        stats = await store_records(session, REGISTRY["steps"], ["corrupt-but-preserved"])
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
    assert stats.inserted == 1
    assert stats.normalization_skipped == 1
    assert raw.raw_json == "corrupt-but-preserved"


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
