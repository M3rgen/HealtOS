"""The conservative write path and the reconciler against PostgreSQL: concurrent writers,
lock waits, large batches, and verify against tampering (synthetic records only)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta

import pytest
from identity_records import (
    HEART,
    NEW_NAME,
    STEPS,
    T0,
    heart,
    legacy,
    name_of,
    raw_rows,
    steps,
    store,
    versions_of,
)
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError

from app.models.identity import RawHealthDataIdentity
from app.models.state import RawHealthData
from app.parsers.common import payload_hash, source_id, temporal
from app.services import identity_backfill as reconciler
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.services.raw_history import verify_guards
from app.services.source_chains import lock_data_type
from app.services.storage import store_records

pytestmark = pytest.mark.integration

HOUR = timedelta(hours=1)
WATCH = "Example Watch"


async def execute(sessions, *statements: str) -> None:
    async with sessions.begin() as session:
        for statement in statements:
            await session.execute(text(statement))


async def problems(sessions) -> tuple[list[str], list[str]]:
    async with sessions() as session:
        _, errors, notes = await verify_identities(session)
        errors += await verify_guards(session)
    return errors, notes


async def test_concurrent_new_records_of_two_devices_never_share_a_row(pg_sessions) -> None:
    """Re-review 1 #2: two writers store the first record of two devices at one instant at
    the same time. Each gets the row of its own fingerprint; neither payload is overwritten."""
    async with pg_sessions.begin() as first:
        await store_records(first, HEART, [heart(60)])
        second = asyncio.create_task(store(pg_sessions, HEART, heart(90, name=WATCH)))
        await asyncio.sleep(0.3)
    await asyncio.wait_for(second, 10)
    rows = await raw_rows(pg_sessions, HEART)
    assert sorted(name_of(row) for row in rows) == ["Example Band Pro", WATCH]
    assert all(row.google_data_point_id.startswith("synthetic:v2s:") for row in rows)
    for row in rows:
        assert await versions_of(pg_sessions, row.id) == []


async def test_a_concurrent_writer_of_the_same_record_re_resolves(pg_sessions) -> None:
    """The second writer's new row collides (ON CONFLICT DO NOTHING): it resolves the page
    again and writes onto the row the first one created, as a version."""
    async with pg_sessions.begin() as first:
        await store_records(first, HEART, [heart(60)])
        second = asyncio.create_task(store(pg_sessions, HEART, heart(61)))
        await asyncio.sleep(0.3)
        assert not second.done()  # waits for the first writer's new row
    stats = await asyncio.wait_for(second, 10)
    [row] = await raw_rows(pg_sessions, HEART)
    assert (stats.inserted, stats.updated, stats.versions_kept) == (0, 1, 1)
    assert [v["heartRate"]["beatsPerMinute"] for v in await versions_of(pg_sessions, row.id)] == [
        60
    ]


async def test_a_row_changed_after_resolution_is_resolved_again(
    pg_sessions, monkeypatch, caplog
) -> None:
    """The rows a page writes onto are locked and checked against what was resolved; one
    changed meanwhile makes the page resolve again (never a write on a stale decision)."""
    from app.services import storage

    await store(pg_sessions, HEART, heart(60))
    [row] = await raw_rows(pg_sessions, HEART)
    original = storage._lock_rows
    calls = []

    async def meanwhile(session, spec, google_ids, resolution):
        if not calls:
            changed = heart(62)
            await execute(
                pg_sessions,
                "UPDATE raw_health_data SET raw_json = "
                f"'{json.dumps(changed)}'::jsonb, "
                f"payload_hash = '{payload_hash(changed)}' WHERE id = '{row.id}'",
            )
        calls.append(1)
        return await original(session, spec, google_ids, resolution)

    monkeypatch.setattr(storage, "_lock_rows", meanwhile)
    # The migration's fileConfig disables existing loggers while the database is set up.
    monkeypatch.setattr(logging.getLogger("app.services.storage"), "disabled", False)
    with caplog.at_level(logging.INFO, logger="app.services.storage"):
        stats = await store(pg_sessions, HEART, heart(60))
    assert len(calls) == 2 and stats.updated == 1
    assert any(record.message == "RECORD_BATCH_RESOLVED_AGAIN" for record in caplog.records)
    [row] = await raw_rows(pg_sessions, HEART)
    assert row.raw_json == heart(60)


async def test_aliases_of_a_large_batch_stay_under_the_parameter_limit(pg_sessions) -> None:
    """Re-review 2 (low): 9,000 aliases in one batch (36,000 values) are inserted in
    chunks; PostgreSQL accepts at most 32,767 parameters per statement."""
    rows = []
    for minute in range(9_000):
        record = steps(5, start=T0 + timedelta(minutes=minute))
        start, end, zone = temporal(STEPS, record)
        rows.append(
            {
                "id": uuid.uuid4(),
                "google_data_point_id": source_id(STEPS, record),
                "data_type": STEPS.name,
                "source_start_time": start,
                "source_end_time": end,
                "source_timezone": zone,
                "raw_json": record,
                "payload_hash": payload_hash(record),
                "first_seen_at": T0,
                "last_seen_at": T0,
            }
        )
    async with pg_sessions.begin() as session:
        await session.execute(insert(RawHealthData), rows)
    report = await identity_backfill(pg_sessions, batch_size=10_000)
    assert report["steps"].aliases_added == 9_000
    async with pg_sessions() as session:
        aliases = await session.scalar(select(func.count()).select_from(RawHealthDataIdentity))
    assert aliases == 9_000


async def test_a_busy_lock_backs_off_and_gives_up_clearly(pg_sessions, monkeypatch) -> None:
    """Re-review 2 (low): a batch tries for the exclusive lock (never queueing writers behind
    it for long), retries, then gives up with a clear message - for that data type only: the
    run goes on with the others (review 4) and reports the failure."""
    await legacy(pg_sessions, HEART, heart(60), T0)
    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(reconciler, "LOCK_TIMEOUT", "200ms")
    monkeypatch.setattr(reconciler, "LOCK_ATTEMPTS", 3)
    monkeypatch.setattr(reconciler, "_pause", pause)
    await legacy(pg_sessions, STEPS, steps(5), T0)
    async with pg_sessions.begin() as writer:
        await lock_data_type(writer, HEART.name, exclusive=False)  # a long sync page
        report = await identity_backfill(pg_sessions, data_types=["heart-rate", "steps"])
        assert "could not get the identity lock" in (report["heart-rate"].error or "")
        assert report["steps"].error is None and report["steps"].rows == 1
        # The batch backed off: writers of the data type go ahead.
        await asyncio.wait_for(store(pg_sessions, HEART, heart(61, at=T0 + HOUR)), 5)
    assert pauses == [2, 4, 8]
    report = await identity_backfill(pg_sessions, data_types=["heart-rate"])
    assert report["heart-rate"].rows == 2


async def linked_chain(sessions) -> None:
    """A rename archived before v2 (6 h apart), chained and linked by a full run."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + 7 * HOUR)
    await identity_backfill(sessions)


async def test_sightings_only_widen_and_an_old_name_seen_again_is_no_error(pg_sessions) -> None:
    """Re-review 2 (tampering): sightings cannot be deleted or narrowed. The old name seen
    again (a widening) is no contradiction (B3: a name can change back): the chain stays in
    use, verify reports neither an error nor a note."""
    await linked_chain(pg_sessions)
    assert await problems(pg_sessions) == ([], [])
    for statement in (
        "DELETE FROM raw_source_sightings",
        "TRUNCATE raw_source_sightings",
        "UPDATE raw_source_sightings SET last_seen_at = last_seen_at - interval '1 day'",
        "UPDATE raw_source_sightings SET first_seen_at = first_seen_at + interval '1 day'",
        "UPDATE raw_source_sightings SET complete = false",
    ):
        with pytest.raises(DBAPIError, match="only widens|keeps every record"):
            await execute(pg_sessions, statement)
    await execute(
        pg_sessions,
        "UPDATE raw_source_sightings SET last_seen_at = last_seen_at + interval '1 day' "
        "WHERE label LIKE '%Example Band Pro%'",
    )
    assert await problems(pg_sessions) == ([], [])


async def test_verify_detects_forged_chains_hidden_rows_views_and_swapped_tables(
    pg_sessions,
) -> None:
    """Re-review 2 (tampering) as the table owner: a forged chain, sightings removed behind a
    disabled guard, row-level security, a redefined analytics view, a guarded table renamed
    away and replaced by an empty copy."""
    await linked_chain(pg_sessions)
    assert await problems(pg_sessions) == ([], [])

    async def finds(*statements: str, expected: list[str]) -> None:
        await execute(pg_sessions, *statements)
        errors, _ = await problems(pg_sessions)
        text_ = "\n".join(errors)
        for message in expected:
            assert message in text_, (message, errors)

    await finds(
        "INSERT INTO raw_source_chains (data_type, from_fingerprint, to_fingerprint, from_label, "
        "to_label, decided_by, evidence, decided_at) VALUES "
        f"('heart-rate', '{'a' * 64}', '{'b' * 64}', 'A', 'B', 'live', '{{}}', now())",
        expected=[
            "was not decided by the reconciler ('live')",
            "has no evidence that satisfies the chain rule",
            "has no sightings for both sources",
        ],
    )
    await finds(
        "ALTER TABLE raw_source_sightings DISABLE TRIGGER raw_source_sightings_no_delete",
        "DELETE FROM raw_source_sightings WHERE label LIKE '%Example Band Pro%'",
        expected=["protective trigger raw_source_sightings.raw_source_sightings_no_delete"],
    )
    await finds(
        "ALTER TABLE raw_health_data_supersessions ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE raw_health_data_supersessions FORCE ROW LEVEL SECURITY",
        "CREATE POLICY hide ON raw_health_data_supersessions USING (false)",
        expected=["guarded table raw_health_data_supersessions has row-level security"],
    )
    await finds(
        "CREATE OR REPLACE VIEW analytics_steps AS SELECT * FROM steps "
        "WHERE is_quarantined = false",
        expected=["analytics view analytics_steps was redefined"],
    )
    table = "raw_health_data_supersessions"
    await finds(
        f"ALTER TABLE {table} RENAME TO kept_supersessions",
        f"CREATE TABLE {table} (LIKE kept_supersessions INCLUDING ALL)",
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION healthos_refuse_change()",
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
        "FOR EACH STATEMENT EXECUTE FUNCTION healthos_refuse_change()",
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_append_only",
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_no_truncate",
        expected=[
            "has an identity-merged event but no supersession link",
            "sits on kept_supersessions, which is not a guarded table",
        ],
    )
