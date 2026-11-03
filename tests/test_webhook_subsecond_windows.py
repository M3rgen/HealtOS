import json
from datetime import datetime, timedelta

import httpx
import pytest
from retrieval_records import point
from sqlalchemy import select
from test_webhook_date_windows import bounded
from test_webhook_date_windows import fetching_receiver as fetching_receiver
from test_webhook_status_http import receiver as receiver

from app.core.timeutil import as_utc
from app.google_health.data_types import REGISTRY, query_iso, query_time
from app.models.retrieval import RetrievalReceipt
from app.models.state import RawHealthData, WebhookEvent, WebhookNotification
from app.sync.deletions import deletion_scope


@pytest.mark.parametrize("kind", ["weight", "body-fat"])
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("micros", [(0, 1), (200000, 900000), (999998, 999999)])
@pytest.mark.parametrize(
    "stamp", ["2026-08-17T10:00:00Z", "2026-08-17T23:59:59+03:00", "2026-08-17T23:59:59Z"]
)
async def test_subsecond_delivery_and_failed_replay(
    fetching_receiver, sessions, kind, retry, micros, stamp
):
    r = fetching_receiver
    r.processor.supported_data_types = set(r.incremental.specs)
    start, end = [datetime.fromisoformat(stamp).replace(microsecond=us) for us in micros]
    item = bounded(kind, start.isoformat(), end.isoformat())
    record = point(start.isoformat(), kind)
    filters = []
    failing = retry

    def google(request):
        filters.append(request.url.params["filter"])
        return httpx.Response(400 if failing else 200, json={"dataPoints": [record]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        r.incremental.client.http = http
        assert (await r.post(json.dumps(item).encode())).status_code == 204
        await r.finish()
        if retry:
            async with sessions() as session:
                assert (await session.scalar(select(WebhookNotification))).status == "failed"
            failing = False
            assert (await r.processor.process_pending(recover_running=False)).failed_elements == 0
        await r.processor.process_pending(recover_running=False)
    lower, upper = query_time(start), query_time(end, upper=True)
    assert lower == as_utc(start).replace(microsecond=0)
    assert upper == lower + timedelta(seconds=1)
    assert lower <= start < end <= upper
    expected = REGISTRY[kind].filter_for(query_iso(lower), query_iso(upper))
    assert filters == [expected] * (1 + retry)
    assert deletion_scope(REGISTRY[kind], start, end)[1:] == (lower, upper)
    async with sessions() as session:
        receipts = (await session.scalars(select(RetrievalReceipt))).all()
        assert all(
            (as_utc(x.queried_from), as_utc(x.queried_to), x.query_filter)
            == (lower, upper, expected)
            for x in receipts
        )
        row = await session.scalar(select(WebhookNotification))
        assert row.status == "completed" and row.sync_success_count == 1
        assert row.raw_json == item and row.last_error is None
        assert (await session.scalar(select(WebhookEvent))).processed_at is not None
        assert (await session.scalar(select(RawHealthData))).raw_json == record
