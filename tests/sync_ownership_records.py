"""Synthetic clients and durable state snapshots for run ownership scenarios."""

import asyncio

from retrieval_records import Client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.google_health.data_types import REGISTRY
from app.models.raw_versions import RawHealthDataVersion
from app.models.retrieval import RetrievalCheckpoint, RetrievalEvent, RetrievalReceipt
from app.models.state import BackfillJob, FetchRound, RawHealthData, SyncState
from app.sync.incremental import IncrementalSync


class PausedClient(Client):
    def __init__(self, *pages, pause_at=2):
        super().__init__(*pages)
        self.pause_at = pause_at
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = []

    async def list_data_points(self, spec, start, end, page_token=None):
        self.calls.append((spec.name, start, end, page_token))
        if len(self.tokens) + 1 == self.pause_at:
            self.entered.set()
            await self.release.wait()
        return await super().list_data_points(spec, start, end, page_token)


def service(sessions, client):
    factory = async_sessionmaker(sessions.kw["bind"], expire_on_commit=False)
    return IncrementalSync(Settings(_env_file=None), factory, client, dict(REGISTRY))


async def snapshot(sessions):
    async with sessions() as session:
        result = {}
        for model in (
            SyncState,
            BackfillJob,
            FetchRound,
            RetrievalCheckpoint,
            RetrievalReceipt,
            RetrievalEvent,
            RawHealthData,
            RawHealthDataVersion,
        ):
            table = model.__table__
            rows = await session.execute(select(table).order_by(*table.primary_key.columns))
            result[table.name] = [dict(row) for row in rows.mappings()]
        return result
