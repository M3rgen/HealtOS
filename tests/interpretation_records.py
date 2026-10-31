"""Shared production reader setup for interpretation archive tests."""

from datetime import timedelta

import pytest
from derived_records import BAND, DAY, T0, point, store, summary

from app.services import codex_health
from app.services.codex_health import read_health

QUERY = dict(start_date=str(DAY), end_date=str(DAY), timezone="UTC")
# Work budgets are independent of the page, so a smaller limit is no remedy.
SCOPE_ERROR = "Yorum sorgusu tamamlanamadı; aralığı ya da metriği daralt."
PAGE_ERROR = "Yorum sorgusu tamamlanamadı; aralığı veya sayfa boyutunu daralt."


@pytest.fixture(autouse=True)
def production_reader(sessions, monkeypatch):
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)


async def call(action, **kwargs):
    return await read_health({"action": action, **QUERY, **kwargs})


async def resting(sessions, values, start=DAY, source=BAND, method=None):
    records = []
    for offset, value in enumerate(values):
        fields = dict(beatsPerMinute=value)
        if method is not None:
            fields["dailyRestingHeartRateMetadata"] = {"calculationMethod": method}
        records.append(
            summary(
                "daily-resting-heart-rate",
                f"{start}-{offset}",
                day=start + timedelta(days=offset),
                source=source,
                **fields,
            )
        )
    await store(sessions, "daily-resting-heart-rate", *records)


def exercise(name, **labels):
    return point(
        "exercise",
        name,
        {
            "interval": {
                "startTime": (T0 - timedelta(minutes=30)).isoformat(),
                "endTime": (T0 + timedelta(hours=1)).isoformat(),
            },
            "activeDuration": "1800s",
            "metricsSummary": {"distanceMillimeters": 4000000, "caloriesKcal": 300},
            **labels,
        },
    )
