"""Synthetic reconciliation inputs shared with the PostgreSQL behavior tests."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from retrieval_records import END, START, Client, point

from app.core.config import Settings
from app.google_health.data_types import REGISTRY
from app.services.reconciliation_reports import list_runs
from app.sync.incremental import IncrementalSync
from app.sync.reconciliation import ReconciliationService


class Frozen(datetime):
    @classmethod
    def now(cls, tz=UTC):
        return END


def service(sessions, client=None, specs=None):
    return ReconciliationService(
        IncrementalSync(
            Settings(_env_file=None),
            sessions,
            client or Client(),
            specs or {"heart-rate": REGISTRY["heart-rate"]},
        ),
        sessions,
        sleep=AsyncMock(),
    )


async def report(sessions, run_id):
    async with sessions() as session:
        history = await list_runs(session, limit=100)
    return next(row for row in history["runs"] if row["id"] == run_id)


async def execute(sessions, *pages, days=1, specs=None):
    runner = service(sessions, Client(*pages), specs)
    run_id = uuid.uuid4()
    results = await runner.run(days, run_id=run_id)
    return await report(sessions, run_id), results, runner


__all__ = ["END", "START", "Client", "Frozen", "execute", "point", "report", "service"]
