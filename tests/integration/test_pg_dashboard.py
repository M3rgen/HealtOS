"""dashboard_data civil-day boundaries and source-deletion filters on PostgreSQL.

The query bounds are timestamptz values for local midnights; with a non-UTC server
session time zone (see conftest) this proves records land on the right civil day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text, update

from app.google_health.data_types import REGISTRY
from app.models.state import RawHealthData
from app.services.dashboard import dashboard_data
from app.services.storage import store_records

pytestmark = pytest.mark.integration

ZONE = "Europe/Istanbul"  # UTC+03:00 all year: local midnight is 21:00Z the day before.
PREFIX = "users/me/dataTypes"


def steps(name: str, start: str, end: str, count: int) -> dict:
    return {
        "name": f"{PREFIX}/steps/dataPoints/{name}",
        "steps": {"interval": {"startTime": start, "endTime": end}, "count": str(count)},
    }


def sleep(name: str, start: str, end: str) -> dict:
    return {
        "name": f"{PREFIX}/sleep/dataPoints/{name}",
        "sleep": {"interval": {"startTime": start, "endTime": end}},
    }


def resting(name: str, day: str, bpm: int) -> dict:
    return {
        "name": f"{PREFIX}/daily-resting-heart-rate/dataPoints/{name}",
        "dailyRestingHeartRate": {"date": day, "beatsPerMinute": bpm},
    }


async def test_local_midnight_and_source_deleted_exclusion(pg_sessions) -> None:
    batches = {
        "steps": [
            steps("before-window", "2026-07-31T20:58:00Z", "2026-07-31T20:59:00Z", 7),
            steps("late", "2026-08-01T20:58:00Z", "2026-08-01T20:59:00Z", 100),
            steps("straddle", "2026-08-01T20:59:30Z", "2026-08-01T21:00:30Z", 60),
            steps("early", "2026-08-01T21:00:00Z", "2026-08-01T21:01:00Z", 200),
            steps("deleted", "2026-08-01T22:00:00Z", "2026-08-01T22:01:00Z", 1000),
            steps("after-window", "2026-08-02T21:00:00Z", "2026-08-02T21:01:00Z", 9),
        ],
        "sleep": [
            sleep("wakes-before-midnight", "2026-08-01T16:59:59Z", "2026-08-01T20:59:59Z"),
            sleep("wakes-at-midnight", "2026-08-01T15:00:00Z", "2026-08-01T21:00:00Z"),
        ],
        "daily-resting-heart-rate": [
            resting("aug-1", "2026-08-01", 61),
            resting("aug-2", "2026-08-02", 99),
        ],
    }
    async with pg_sessions.begin() as session:
        for data_type, records in batches.items():
            await store_records(session, REGISTRY[data_type], records)
        await session.execute(
            update(RawHealthData)
            .where(
                RawHealthData.google_data_point_id.in_(
                    [
                        f"{PREFIX}/steps/dataPoints/deleted",
                        f"{PREFIX}/daily-resting-heart-rate/dataPoints/aug-2",
                    ]
                )
            )
            .values(source_deleted_at=datetime(2026, 8, 3, tzinfo=UTC))
        )

    async with pg_sessions() as session:
        assert await session.scalar(text("SHOW TimeZone")) == "America/Los_Angeles"
        default = await dashboard_data(session, date(2026, 8, 2), 2, ZONE)
        with_deleted = await dashboard_data(
            session, date(2026, 8, 2), 2, ZONE, include_source_deleted=True
        )
        valid_only = await dashboard_data(
            session,
            date(2026, 8, 2),
            2,
            ZONE,
            valid_sources_only=True,
            include_source_deleted=True,
        )

    aug_2, aug_1 = default["days"]
    assert (aug_1["date"], aug_2["date"]) == ("2026-08-01", "2026-08-02")
    # 23:58 local and the first half of the straddling minute stay on Aug 1;
    # 00:00 local and the second half move to Aug 2. Out-of-window records are ignored.
    assert (aug_1["steps"], aug_2["steps"]) == (130, 230)
    assert default["charts"]["steps"][0] == 230
    assert default["charts"]["steps"][1] is None
    # Sleep belongs to the local day it ends on: 23:59:59 vs exactly 00:00.
    assert (aug_1["sleep_seconds"], aug_2["sleep_seconds"]) == (4 * 3600, 6 * 3600)
    # Daily summaries: the source-deleted Aug 2 value is hidden.
    assert (aug_1["resting_bpm"], aug_2["resting_bpm"]) == (61, None)

    aug_2, aug_1 = with_deleted["days"]
    assert (aug_1["steps"], aug_2["steps"]) == (130, 1230)
    assert with_deleted["charts"]["steps"][1] == 1000
    assert (aug_1["resting_bpm"], aug_2["resting_bpm"]) == (61, 99)

    # valid_sources_only wins over include_source_deleted.
    aug_2, aug_1 = valid_only["days"]
    assert (aug_1["steps"], aug_2["steps"]) == (130, 230)
    assert aug_2["resting_bpm"] is None
