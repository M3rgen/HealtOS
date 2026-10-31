"""Synthetic raw sources, stored corrections, and assertions for both coverage readers."""

import pytest
from derived_records import snapshot, store
from retrieval_records import point

from app.parsers.common import payload_hash

VALUES = [(None, None), ({}, None), ({"bad": 1}, None), ([], None), (["bad"], None),
          (42, None), (True, None), (False, None), (0, None), ("", ""),
          (" Çığ 🩺 ", " Çığ 🩺 "), ("ç" * 300, "ç" * 300)]  # fmt: skip
SOURCES = [({}, [None, None, None]), ({"platform": "Örnek"}, [None, None, "Örnek"])]
for value, label in VALUES:
    SOURCES.extend([
        ({"device": {"displayName": value, "model": "Yedek"}, "platform": "Örnek"},
         [label if value else "Yedek", None, "Örnek"]),
        ({"device": {"model": value, "formFactor": "Bant"}}, [label, "Bant", None]),
        ({"device": {"model": "Saat", "formFactor": value}}, ["Saat", label, None]),
        ({"device": {"model": "Saat"}, "platform": value}, ["Saat", None, label]),
        (value, [None, None, None]),
        ({"device": value, "platform": "Örnek"}, [None, None, "Örnek"]),
    ])  # fmt: skip


@pytest.fixture(params=SOURCES)
async def source_archive(sessions, request, kind):
    source, labels = request.param
    record = {**point(kind=kind), "dataSource": source}
    twin = {
        **point(kind=kind),
        "dataSource": {**source, "other": True} if isinstance(source, dict) else {"other": True},
    }
    neighbor = {**point(kind=kind), "dataSource": {"device": {"model": "Komşu"}}}
    await store(sessions, kind, {**record, "dataSource": {"platform": "Önce"}})
    assert (await store(sessions, kind, record, twin, neighbor)).versions_kept == 1
    expected = {
        f"kaynak-{payload_hash(row['dataSource'])[:8]}": values
        for row, values in [(record, labels), (twin, labels), (neighbor, ["Komşu", None, None])]
    }
    async with sessions() as session:
        before = await snapshot(session)
    yield expected
    async with sessions() as session:
        assert await snapshot(session) == before


def assert_sources(regimes, expected):
    assert len(regimes) == len(expected) == 3
    assert {
        row["source"]: [row[key] for key in ("device", "form_factor", "platform")]
        for row in regimes
    } == expected
    assert all(
        (row["first_date"], row["last_date"], row["value_days"], row["record_count"])
        == ("2026-09-01", "2026-09-01", 1, 1)
        for row in regimes
    )
