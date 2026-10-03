from datetime import UTC, datetime, timedelta

from app.google_health.data_types import REGISTRY
from app.services.storage import StoreStats
from app.sync.reconciliation import ReconciliationService


async def test_reconciliation_runs_every_configured_type_with_lookback():
    class Incremental:
        specs = {"steps": object(), "sleep": object()}
        calls = []

        async def run(self, data_type, start):
            self.calls.append((data_type, start))
            return data_type

    incremental = Incremental()
    result = await ReconciliationService(incremental).run(7)
    assert result == {"steps": "steps", "sleep": "sleep"}
    assert [call[0] for call in incremental.calls] == ["steps", "sleep"]
    assert all(isinstance(call[1], datetime) for call in incremental.calls)


async def test_reconciliation_chunks_rollup_types_to_api_limit():
    class Incremental:
        specs = {"total-calories": REGISTRY["total-calories"]}

        def __init__(self):
            self.calls = []

        async def run(self, data_type, start, end=None):
            self.calls.append((start, end or datetime.now(UTC)))
            return StoreStats(inserted=1)

    incremental = Incremental()
    result = await ReconciliationService(incremental).run(90)

    assert result["total-calories"].inserted == 7
    assert len(incremental.calls) == 7
    assert all(end - start <= timedelta(days=14) for start, end in incremental.calls)
