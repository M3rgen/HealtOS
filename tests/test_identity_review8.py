"""Review 8 of the record identities (lane AD). No value here is real health data.

AD1: a sedentary period still growing when its device's old and new names were linked (by the
owner, or the nightly reconciler) was marked deleted by the next complete fetch, although Google
still returned it under the new name with a later end. AD4: ``status`` kept listing deferrals
whose two sources were linked since.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from identity_records import (
    HEART,
    SEDENTARY,
    T0,
    counted_rows,
    fitbit,
    heart,
    interval,
    legacy,
    mark,
    store,
)
from sqlalchemy import select

from app.models.identity import IdentityDeferredDeletion
from app.models.state import RawHealthData
from app.parsers.identity import source_fingerprint
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import decide_sources
from app.sync.deletions import deferred_deletions

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
A1, A2, WATCH = "Band A1", "Band A2", "Example Watch"


def fp(name: str) -> str:
    return source_fingerprint(fitbit(name))


def period(minutes: int, name: str, start=T0) -> dict:
    return {"dataSource": fitbit(name), "sedentaryPeriod": {"interval": interval(start, minutes)}}


async def marks(sessions) -> dict[str, list[str]]:
    """Device name -> end of each of its rows marked deleted."""
    async with sessions() as session:
        rows = (await session.scalars(select(RawHealthData))).all()
    found: dict[str, list[str]] = {}
    for row in rows:
        if row.source_deleted_at is not None:
            name = row.raw_json["dataSource"]["device"]["displayName"]
            found.setdefault(name, []).append(
                row.raw_json["sedentaryPeriod"]["interval"]["endTime"]
            )
    return found


def ends(rows) -> list[str]:
    return sorted(row.raw_json["sedentaryPeriod"]["interval"]["endTime"] for row in rows)


# --- AD1: a growing period linked across a rename is not marked -------------------------------


async def test_a_growing_period_the_owner_linked_is_not_marked(sessions, clock) -> None:
    """Review 7 P3 (owner): renamed while a period grew; the owner linked the two names; the
    next complete fetch returned the period, longer, under the new name only. The old name's
    row (hidden behind it) was marked deleted, permanently: that name never returns."""
    await legacy(sessions, SEDENTARY, period(15, A1, T0 - 4 * HOUR), T0 - 3 * HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 20 * MINUTE
    await store(sessions, SEDENTARY, period(15, A1))
    clock.now = T0 + 35 * MINUTE
    await store(sessions, SEDENTARY, period(30, A2))
    await decide_sources(sessions, "sedentary-period", "link", fp(A1)[:12], fp(A2)[:12])
    clock.now = T0 + 50 * MINUTE
    since = clock.now
    await store(sessions, SEDENTARY, period(45, A2))
    await mark(sessions, SEDENTARY, T0 - HOUR, T0 + HOUR, since)
    assert await marks(sessions) == {}
    assert ends(await counted_rows(sessions, SEDENTARY)) == [
        "2026-10-18T05:15:00Z",
        "2026-10-18T09:45:00Z",
    ]


@pytest.mark.parametrize("returned", [True, False])
async def test_a_growing_period_the_nightly_linked_is_marked_only_when_gone(
    sessions, clock, returned
) -> None:
    """Review 7 P3 (nightly): the reconciler chained the rename and linked the old name's row to
    the new name's longer one; the next complete fetch returned the period longer still and
    marked the old name's row. It is kept while its start is returned; a period no longer
    returned at all is marked under both names."""
    await legacy(sessions, SEDENTARY, period(15, A1), T0 + 15 * MINUTE, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 5 * HOUR
    await store(sessions, SEDENTARY, period(30, A2))
    clock.now = T0 + 6 * HOUR
    await identity_backfill(sessions, incremental=True)
    assert ends(await counted_rows(sessions, SEDENTARY)) == ["2026-10-18T09:30:00Z"]
    clock.advance(minutes=15)
    since = clock.now
    if returned:
        await store(sessions, SEDENTARY, period(45, A2))
    await mark(sessions, SEDENTARY, T0 - HOUR, T0 + HOUR, since)
    if returned:
        assert await marks(sessions) == {}
        assert ends(await counted_rows(sessions, SEDENTARY)) == ["2026-10-18T09:45:00Z"]
    else:
        assert await marks(sessions) == {
            A1: ["2026-10-18T09:15:00Z"],
            A2: ["2026-10-18T09:30:00Z"],
        }


# --- AD4: deferrals of a pair linked since are no longer listed --------------------------------


async def test_deferrals_of_a_pair_linked_since_are_resolved(sessions, clock) -> None:
    """Review 7 AD4: after the owner linked the two sources, ``status`` still asked for a
    decision on their deferrals until a complete fetch of their window (the weekly re-fetch).
    They are not listed once the two are one device, and the next complete fetch of the data
    type, of any window, resolves them (the rows stay, with ``resolved_at``)."""
    keys = [T0 + index * MINUTE for index in range(3)]
    for at in keys:
        await legacy(sessions, HEART, heart(60, at=at, name=A1), at)
    await identity_backfill(sessions)
    clock.now = since = T0 + 6 * HOUR
    await store(sessions, HEART, *(heart(60, at=at, name=WATCH) for at in keys))
    await mark(sessions, HEART, T0, T0 + HOUR, since)
    async with sessions() as session:
        assert [entry["deferred"] for entry in await deferred_deletions(session)] == [3]
    await decide_sources(sessions, "heart-rate", "not-distinct", fp(A1)[:12], fp(WATCH)[:12])
    await decide_sources(sessions, "heart-rate", "link", fp(A1)[:12], fp(WATCH)[:12])
    async with sessions() as session:
        assert await deferred_deletions(session) == []
    clock.advance(minutes=10)
    since = clock.now
    later = T0 + 30 * HOUR
    await store(sessions, HEART, heart(61, at=later, name=WATCH))
    await mark(sessions, HEART, later - HOUR, later + HOUR, since)
    async with sessions() as session:
        rows = (await session.scalars(select(IdentityDeferredDeletion))).all()
    assert len(rows) == 3
    assert all(row.resolved_at is not None for row in rows)
