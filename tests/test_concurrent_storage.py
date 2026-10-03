import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.google_health.data_types import REGISTRY
from app.models.health import HeartRate
from app.models.state import RawHealthData
from app.services.storage import store_records


def _record(bpm):
    return {
        "name": "users/1/dataTypes/heart-rate/dataPoints/concurrent",
        "heartRate": {
            "sampleTime": {"physicalTime": "2026-08-01T10:00:00Z"},
            "beatsPerMinute": bpm,
        },
    }


async def test_concurrent_writers_keep_one_consistent_mirror_row(tmp_path):
    database = tmp_path / "concurrent.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def write(bpm):
        async with sessions.begin() as session:
            await store_records(session, REGISTRY["heart-rate"], [_record(bpm)])

    await asyncio.gather(write(60), write(65))
    async with sessions() as session:
        raw_count = await session.scalar(select(func.count(RawHealthData.id)))
        normalized_count = await session.scalar(select(func.count(HeartRate.id)))
        raw = await session.scalar(select(RawHealthData))
        normalized = await session.scalar(select(HeartRate))
    await engine.dispose()

    assert raw_count == normalized_count == 1
    assert int(normalized.bpm) == raw.raw_json["heartRate"]["beatsPerMinute"]
