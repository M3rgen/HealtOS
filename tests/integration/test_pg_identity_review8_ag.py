"""PostgreSQL side of review 8 AG: a device field Google added to a band's records (formFactor)
neither marks the old form's rows deleted nor stays silent; the reconciler chains the two forms
and the write path links a returning form at once. Synthetic data only."""

from __future__ import annotations

import pytest
from identity_records import HEART, T0, counted_rows, mark, store
from test_identity_review8_ag import (
    FORM,
    HOUR,
    VALUES,
    band,
    fp,
    marked,
    old_form_archived,
    readings,
    values,
)

from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.sync.deletions import deferred_deletions

pytestmark = pytest.mark.integration


async def test_a_field_added_and_dropped_again_marks_nothing(pg_sessions, clock) -> None:
    old, new = band(), band(None, form=FORM)
    await old_form_archived(pg_sessions, clock, old)
    clock.now = since = T0 + 8 * HOUR
    await store(pg_sessions, HEART, *readings(new))
    await mark(pg_sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    assert await marked(pg_sessions) == []
    async with pg_sessions() as session:
        assert sum(entry["deferred"] for entry in await deferred_deletions(session)) == 4
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(pg_sessions))[HEART.name]
    assert [entry["fingerprints"] for entry in stats.review] == [[fp(old)[:12], fp(new)[:12]]]
    assert stats.deletions_kept == 0
    assert values(await counted_rows(pg_sessions)) == sorted(VALUES)
    async with pg_sessions() as session:
        _, errors, _ = await verify_identities(session)
    assert errors == []
    for hours, form in ((12, old), (15, new)):
        clock.now = since = T0 + hours * HOUR
        await store(pg_sessions, HEART, *readings(form))
        await mark(pg_sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
        assert await marked(pg_sessions) == []
        assert values(await counted_rows(pg_sessions)) == sorted(VALUES)


async def test_marks_made_before_the_fix_stay_and_the_returning_form_is_linked_at_once(
    pg_sessions, clock
) -> None:
    from sqlalchemy import update

    from app.models.state import RawHealthData

    old, new = band(), band(form=FORM)
    await old_form_archived(pg_sessions, clock, old)
    clock.now = T0 + 8 * HOUR
    await store(pg_sessions, HEART, *readings(new))
    async with pg_sessions.begin() as session:
        await session.execute(
            update(RawHealthData)
            .where(RawHealthData.first_seen_at < T0 + 2 * HOUR)
            .values(source_deleted_at=T0 + 8 * HOUR)
        )
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(pg_sessions))[HEART.name]
    assert stats.deletions_kept == 0 and len(stats.review) == 1
    assert len(await marked(pg_sessions)) == 4
    assert values(await counted_rows(pg_sessions)) == sorted(VALUES)
    clock.now = since = T0 + 12 * HOUR
    await store(pg_sessions, HEART, *readings(old))
    assert values(await counted_rows(pg_sessions)) == sorted(VALUES)
    await mark(pg_sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    assert await marked(pg_sessions) == []
