"""Review 7 of the record identities (lane AC). No value here is real health data.

S1: a device unworn for hours (a watch on its charger) counted as retired, so a rename of it
was chained to the other device. The verify agreement sums read every key into memory. A dry
run did not show the re-settlement still owed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from identity_records import HEART, T0, counted_rows, fitbit, heart, legacy, store
from sqlalchemy import event, select

from app.models.identity import IdentityReconcileState, RawSourceAgreement
from app.parsers.identity import source_fingerprint
from app.services.identity import verify_identities
from app.services.identity_backfill import _summed, identity_backfill
from app.services.identity_decisions import decide_sources

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
A1, A2 = "Band A1", "Band A2"
BAND, WATCH, RENAMED = "Band A1", "Example Watch", "Example Watch (renamed)"


def fp(name: str) -> str:
    return source_fingerprint(fitbit(name))


async def nightly(sessions, **options):
    return (await identity_backfill(sessions, incremental=True, **options))["heart-rate"]


def bpm(row) -> tuple[str, int]:
    return (
        row.raw_json["dataSource"]["device"]["displayName"],
        row.raw_json["heartRate"]["beatsPerMinute"],
    )


# --- S1: a device unworn for hours is not retired ----------------------------------------------


async def watch_renamed_while_band_unworn(sessions, clock):
    """Band and watch worn together (two fetches: established distinct); the watch is taken
    off, the band worn four hours longer; next day only the watch is worn, under a new name."""
    for base in (T0, T0 + 2 * HOUR):
        for index in range(3):
            at = base + index * MINUTE
            await legacy(sessions, HEART, heart(60 + index, at=at, name=BAND), base + 5 * MINUTE)
            await legacy(sessions, HEART, heart(90 + index, at=at, name=WATCH), base + 5 * MINUTE)
    for index in range(3):
        at = T0 + 6 * HOUR + index * MINUTE
        await legacy(sessions, HEART, heart(70, at=at, name=BAND), T0 + 6 * HOUR + 5 * MINUTE)
    await identity_backfill(sessions)
    clock.now = T0 + 20 * HOUR
    await store(
        sessions,
        HEART,
        *(heart(95, at=T0 + 20 * HOUR + index * MINUTE, name=RENAMED) for index in range(3)),
    )
    return await nightly(sessions)


async def test_a_watch_on_its_charger_is_not_retired(sessions, clock) -> None:
    """Review 7 S1: the watch's newest record was four hours older than the band's, so it
    counted as retired and the band was chained to the watch's new name. Both remain
    candidates: the new name is left open for the owner."""
    report = await watch_renamed_while_band_unworn(sessions, clock)
    assert report.chains == []
    assert [entry["why"] for entry in report.ambiguous] == [
        "more than one source of its class ended before it"
    ]


async def test_a_re_fetch_never_hides_the_band_behind_the_renamed_watch(sessions, clock) -> None:
    """Review 7 S1: with the wrong chain, a re-fetch of the old window (the band as itself,
    the watch's history under its new name; at one second both measured 61) hid the band's
    reading behind the watch's. Both count."""
    await watch_renamed_while_band_unworn(sessions, clock)
    clock.now = T0 + 30 * HOUR
    at = T0 + 2 * HOUR + MINUTE
    await store(sessions, HEART, heart(61, at=at, name=BAND), heart(61, at=at, name=RENAMED))
    stamp = at.isoformat()[:19]
    shown = sorted(
        bpm(row)
        for row in await counted_rows(sessions)
        if row.raw_json["heartRate"]["sampleTime"]["physicalTime"].startswith(stamp)
    )
    assert shown == [(BAND, 61), (WATCH, 91), (RENAMED, 61)]


# --- the agreement sums are read summed ---------------------------------------------------------


async def test_verify_and_a_full_run_read_the_agreement_summed(sessions) -> None:
    """Review 7: verify (and a full run's recount) read every per-key agreement row into a
    dict: at millions of keys, as much memory. The database sums the latest row of each key
    (one row per pair is read), and the sums are the same."""
    first, second = "a" * 64, "b" * 64
    rows = [("k1", True), ("k2", True), ("k3", False), ("k1", False), ("k4", True), ("k4", True)]
    async with sessions.begin() as session:
        for key, same in rows:
            session.add(
                RawSourceAgreement(
                    data_type="steps",
                    first_fingerprint=first,
                    second_fingerprint=second,
                    identity_key=key,
                    same_reading=same,
                    counted_at=T0,
                )
            )
    engine = sessions.kw["bind"].sync_engine
    statements: list[str] = []

    def seen(_connection, _cursor, statement, *_args) -> None:
        if "raw_source_agreements" in statement:
            statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", seen)
    try:
        async with sessions() as session:
            _, errors, _ = await verify_identities(session)
            summed = await _summed(session, "steps")
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    # k1 latest differs, k2 and k4 agree, k3 differs; none was counted into the state.
    assert summed == {(first, second): (2, 2)}
    assert any("agreement counts of steps" in error for error in errors)
    # Every read of the per-key rows is summed by the database.
    assert len(statements) == 2 and all("GROUP BY" in found for found in statements)


# --- a dry run shows the re-settlement still owed --------------------------------------------


async def test_a_dry_run_shows_the_re_settlement_still_owed(sessions, clock, monkeypatch):
    """Review 7: a decision whose re-settlement failed left it owed (pending_settle); a dry run
    then previewed nothing to settle. It previews what the next run settles, writing nothing."""
    from app.services import identity_backfill as backfill

    keys = [T0 + index * MINUTE for index in range(3)]
    for at in keys:
        await legacy(sessions, HEART, heart(60, at=at, name=A1), at, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 2 * HOUR
    await store(sessions, HEART, *(heart(60, at=at, name=A2) for at in keys))
    clock.now = T0 + 5 * HOUR
    await nightly(sessions)
    original = backfill._settle_starts

    async def failing(*args, **kwargs):
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(backfill, "_settle_starts", failing)
    with pytest.raises(RuntimeError):
        await decide_sources(sessions, "heart-rate", "reject", fp(A1)[:12], fp(A2)[:12])
    monkeypatch.setattr(backfill, "_settle_starts", original)

    async def owed() -> str | None:
        async with sessions() as session:
            return await session.scalar(
                select(IdentityReconcileState.pending_settle).where(
                    IdentityReconcileState.data_type == "heart-rate"
                )
            )

    before = await owed()
    assert before is not None
    counted = len(await counted_rows(sessions))
    clock.advance(hours=30)
    preview = await nightly(sessions, dry_run=True)
    assert preview.rows == 6
    assert await owed() == before and len(await counted_rows(sessions)) == counted
    report = await nightly(sessions)
    assert report.rows == preview.rows and await owed() is None
