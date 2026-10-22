"""Review 6 of the record identities (lane AB). No value here is real health data.

AB1: one night's sample of the keys two sources share replaced the archive-wide agreement
count, so a decided rename was refuted (and deletions marked) on a partial count. AB2: a
later window's deferrals overwrote an earlier window's. AB3: a re-settlement that failed after
devices changed was never resumed. AB5: an owner decision decided reconciler chains inside it.
AB6: a growing interval's old name returned with a shorter end and stayed hidden behind the
newer name's stale longer end. AB7: rows grew without bound under name flapping. AB9: a
retired second watch re-fetched later blocked the band's rename. M4, M5 and the minor findings.
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
    raw_rows,
    store,
)
from sqlalchemy import func, select, update

from app.models.identity import (
    IdentityDeferredDeletion,
    IdentityReconcileState,
    RawHealthDataSupersession,
    RawSourceAgreement,
    RawSourceChain,
)
from app.models.state import RawHealthData
from app.parsers.identity import source_fingerprint
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import DecisionRefused, decide_sources
from app.services.source_chains import load_chains, ordered_pair
from app.sync.deletions import deferred_deletions

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)
A1, A2, WATCH = "Band A1", "Band A2", "Example Watch"


def fp(name: str) -> str:
    return source_fingerprint(fitbit(name))


async def nightly(sessions):
    return (await identity_backfill(sessions, incremental=True))["heart-rate"]


async def problems(sessions):
    async with sessions() as session:
        _, errors, notes = await verify_identities(session)
    return errors, notes


async def renamed_with_history(sessions, clock, keys):
    """A1's archive, renamed A2: the whole history returned again under A2 (equal values)."""
    for at in keys:
        await legacy(sessions, HEART, heart(60, at=at, name=A1), at, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 2 * HOUR
    await store(sessions, HEART, *(heart(60, at=at, name=A2) for at in keys))
    clock.now = T0 + 5 * HOUR
    await nightly(sessions)


# --- AB1: agreement is counted per key ------------------------------------------------------


async def test_a_nights_sample_never_replaces_the_archives_agreement(sessions, clock) -> None:
    """Review 6a test_refute: 40 keys agree; later fetches return a few old-name keys with
    other values (two fetches, the second for newer records). The nightly runs count only the
    keys they settle: the pair stays agreeing, the rename stays live, nothing is double."""
    keys = [T0 + index * MINUTE for index in range(40)]
    await renamed_with_history(sessions, clock, keys)
    pair = ordered_pair(fp(A1), fp(A2))
    clock.now = T0 + 30 * HOUR
    await store(
        sessions,
        HEART,
        *(heart(70, at=at, name=A1) for at in keys[:3]),
        *(heart(60, at=at, name=A2) for at in keys[:3]),
    )
    clock.now = T0 + 33 * HOUR
    await nightly(sessions)
    clock.now = T0 + 60 * HOUR
    newer = [clock.now - (index + 1) * MINUTE for index in range(3)]
    await store(
        sessions,
        HEART,
        *(heart(80, at=at, name=A1) for at in newer),
        *(heart(81, at=at, name=A2) for at in newer),
    )
    clock.now = T0 + 63 * HOUR
    report = await nightly(sessions)
    await nightly(sessions)
    async with sessions() as session:
        chains = await load_chains(session, "heart-rate")
    agree, disagree, counted = chains.agreement[pair]
    assert counted and agree >= 37 and disagree <= 6
    assert report.refuted == [] and chains.live(fp(A1), fp(A2))
    assert [entry["fingerprints"] for entry in report.review_distinct] == [
        [pair[0][:12], pair[1][:12]]
    ]
    assert (await problems(sessions))[0] == []


async def test_a_key_counted_again_counts_once(sessions, clock) -> None:
    """The same keys settled by several runs (the overlap, a full run, a decision) move the
    counts only by what changed."""
    keys = [T0 + index * MINUTE for index in range(6)]
    await renamed_with_history(sessions, clock, keys)
    clock.now = T0 + 30 * HOUR
    await store(
        sessions,
        HEART,
        *(heart(70, at=at, name=A1) for at in keys[:3]),
        *(heart(60, at=at, name=A2) for at in keys[:3]),
    )
    clock.now = T0 + 31 * HOUR
    await nightly(sessions)
    await identity_backfill(sessions)  # every key both hold
    async with sessions() as session:
        before = (await load_chains(session, "heart-rate")).agreement
    for _ in range(3):
        await nightly(sessions)
    await identity_backfill(sessions)
    async with sessions() as session:
        after = (await load_chains(session, "heart-rate")).agreement
        appended = await session.scalar(select(func.count()).select_from(RawSourceAgreement))
    assert {pair: counts[:2] for pair, counts in after.items()} == {
        pair: counts[:2] for pair, counts in before.items()
    }
    assert list(before.values()) == [(6, 0, True)]
    assert appended == 6  # one row per shared key, appended once


async def test_no_deletion_is_marked_on_a_partial_count(sessions, clock) -> None:
    """Review 6a test_flip: a night's count of three keys with two readings made the pair
    certainly distinct (37 agreeing keys forgotten), so a complete fetch marked rows whose
    readings the other name returned."""
    keys = [T0 + index * MINUTE for index in range(40)]
    for at in keys:
        await legacy(sessions, HEART, heart(60, at=at, name=A1), at)
    await identity_backfill(sessions)
    rename = T0 + 2 * HOUR
    clock.now = rename
    await store(
        sessions,
        HEART,
        *(heart(60, at=at, name=A2) for at in keys),
        *(heart(61, at=at, name=A1) for at in keys[:3]),
    )
    clock.now = rename + 3 * HOUR
    await nightly(sessions)
    clock.now = later = rename + 27 * HOUR
    newer = [later - (index + 1) * MINUTE for index in range(3)]
    await store(
        sessions,
        HEART,
        *(heart(62, at=at, name=A2) for at in newer),
        *(heart(63, at=at, name=A1) for at in newer),
    )
    clock.now = later + 3 * HOUR
    await nightly(sessions)
    async with sessions() as session:
        chains = await load_chains(session, "heart-rate")
    assert chains.agreeing(fp(A1), fp(A2)) and not chains.certainly_distinct(fp(A1), fp(A2))
    clock.now = since = later + 4 * HOUR
    await store(sessions, HEART, *(heart(60, at=at, name=A2) for at in keys))
    await mark(sessions, HEART, T0 - HOUR, T0 + HOUR, since)
    shown = {
        row.raw_json["heartRate"]["sampleTime"]["physicalTime"]
        for row in await counted_rows(sessions)
        if row.raw_json["heartRate"]["beatsPerMinute"] == 60
    }
    assert len(shown) == 40  # every reading Google returns is counted


async def test_verify_sums_the_agreement_again(sessions, clock) -> None:
    """The counts decide whether two sources are two devices: verify compares them with the
    per-key rows, and a full run sums them again."""
    keys = [T0 + index * MINUTE for index in range(6)]
    await renamed_with_history(sessions, clock, keys)
    clock.now = T0 + 30 * HOUR
    await store(
        sessions,
        HEART,
        *(heart(70, at=at, name=A1) for at in keys[:3]),
        *(heart(60, at=at, name=A2) for at in keys[:3]),
    )
    clock.now = T0 + 31 * HOUR
    await nightly(sessions)
    pair = "|".join(ordered_pair(fp(A1), fp(A2)))
    async with sessions.begin() as session:
        await session.execute(
            update(IdentityReconcileState)
            .where(IdentityReconcileState.data_type == "heart-rate")
            .values(pair_agreement=f'{{"{pair}": [0, 3, true]}}')
        )
    errors, _ = await problems(sessions)
    assert any("agreement counts of heart-rate" in error for error in errors)
    await identity_backfill(sessions)
    assert (await problems(sessions))[0] == []


# --- AB2: deferrals per row ------------------------------------------------------------------


async def test_a_later_window_keeps_an_earlier_windows_deferrals(sessions, clock) -> None:
    """Review 6b fa majw: a deletion deferred by one window was forgotten when a fetch of
    another window deferred others (11 deferred, 4 listed). Each stays listed until a complete
    fetch of a window that holds it decides it."""
    early = [T0 + index * MINUTE for index in range(7)]
    late = [T0 + 3 * HOUR + index * MINUTE for index in range(4)]
    for at in (*early, *late):
        await legacy(sessions, HEART, heart(60, at=at, name=A1), at)
    await identity_backfill(sessions)
    clock.now = T0 + 6 * HOUR
    since = clock.now
    await store(sessions, HEART, *(heart(60, at=at, name=WATCH) for at in early))
    await mark(sessions, HEART, T0, T0 + HOUR, since)
    clock.advance(minutes=10)
    since = clock.now
    await store(sessions, HEART, *(heart(60, at=at, name=WATCH) for at in late))
    await mark(sessions, HEART, T0 + 3 * HOUR, T0 + 4 * HOUR, since)
    async with sessions() as session:
        listed = await deferred_deletions(session, "heart-rate")
    assert [entry["deferred"] for entry in listed] == [11], listed
    # A1 returns the late records: decided (present), the early ones stay listed; the
    # watch's late rows are deferred now (A1 returned their readings).
    clock.advance(minutes=10)
    since = clock.now
    await store(sessions, HEART, *(heart(60, at=at, name=A1) for at in late))
    await mark(sessions, HEART, T0 + 3 * HOUR, T0 + 4 * HOUR, since)
    async with sessions() as session:
        listed = await deferred_deletions(session, "heart-rate")
        resolved = await session.scalar(
            select(func.count()).where(IdentityDeferredDeletion.resolved_at.is_not(None))
        )
    assert sorted((entry["rows_of"], entry["deferred"]) for entry in listed) == [
        (f"FITBIT / {A1} / DERIVED", 7),
        (f"FITBIT / {WATCH} / DERIVED", 4),
    ]
    assert resolved == 4


# --- AB3: an owed re-settlement is resumed ---------------------------------------------------


async def test_a_failed_re_settlement_is_resumed_by_the_next_run(sessions, clock, monkeypatch):
    """Review 6a S2: a decision committed the device change, then its re-settlement failed;
    the next nightly run saw no change, so the rejected chain's rows stayed linked in
    readers' eyes... now the run settles what the decision owed."""
    from app.services import identity_backfill as backfill

    keys = [T0 + index * MINUTE for index in range(3)]
    await renamed_with_history(sessions, clock, keys)
    assert len(await counted_rows(sessions)) == 3
    original = backfill._settle_starts

    async def failing(*args, **kwargs):
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(backfill, "_settle_starts", failing)
    with pytest.raises(RuntimeError):
        await decide_sources(sessions, "heart-rate", "reject", fp(A1)[:12], fp(A2)[:12])
    async with sessions() as session:
        state = await session.get(IdentityReconcileState, "heart-rate")
    assert state is not None and state.pending_settle is not None
    monkeypatch.setattr(backfill, "_settle_starts", original)
    clock.advance(hours=30)
    await nightly(sessions)
    async with sessions() as session:
        state = await session.get(IdentityReconcileState, "heart-rate")
    assert state is not None and state.pending_settle is None
    assert len(await counted_rows(sessions)) == 6  # each device once
    assert await problems(sessions) == (
        [],
        [
            "3 supersession(s) are no longer in force (their two sources are no longer one "
            "device: a chain was rejected or refuted, or the owner decided them distinct); "
            "the rows they linked count on their own",
            f"source chain of heart-rate 'FITBIT / {A1} / DERIVED' -> 'FITBIT / {A2} / "
            "DERIVED' was rejected by the owner: it is no longer used, and the rows it linked "
            "count on their own unless another chain joins their sources",
        ],
    )


# --- AB5: a decision decides no chain of the reconciler --------------------------------------


async def test_a_decision_records_no_chain_of_the_reconciler_and_is_undone_at_once(
    sessions, clock
) -> None:
    """Review 6b S1: ``distinct`` of the band's two names left a retired watch the only
    candidate before the new name, and the decision chained them inside it (decided_by
    reconciler, hiding the watch's later equal readings); ``not-distinct`` was then refused
    until that chain was rejected. The decision records itself only (the chain is left to the
    next reconciler run, listed), and its undo is accepted at once."""
    worn = [T0 + index * MINUTE for index in range(3)]
    for at in worn:  # one page stored both: evidence that they are two devices
        await legacy(sessions, HEART, heart(60, at=at, name=A1), T0 + 5 * MINUTE, last=T0 + HOUR)
        await legacy(sessions, HEART, heart(90, at=at, name=WATCH), T0 + 5 * MINUTE, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 5 * HOUR
    await store(sessions, HEART, heart(61, at=T0 + 5 * HOUR, name=A2))
    report = await nightly(sessions)
    assert report.chains == [] and "more than one" in report.ambiguous[0]["why"]
    before = len(await counted_rows(sessions))
    outcome = await decide_sources(sessions, "heart-rate", "distinct", fp(A1)[:12], fp(A2)[:12])
    async with sessions() as session:
        recorded = await session.scalar(select(func.count()).select_from(RawSourceChain))
    assert recorded == 0 and outcome.stats.chains == []
    assert [entry["fingerprints"] for entry in outcome.stats.left_to_reconciler] == [
        [fp(WATCH)[:12], fp(A2)[:12]]
    ]
    assert len(await counted_rows(sessions)) == before
    await decide_sources(sessions, "heart-rate", "not-distinct", fp(A1)[:12], fp(A2)[:12])
    assert len(await counted_rows(sessions)) == before


# --- AB6: a growing interval's current end is never hidden -----------------------------------


def period(minutes: int, name: str) -> dict:
    return {"dataSource": fitbit(name), "sedentaryPeriod": {"interval": interval(T0, minutes)}}


def ends(rows) -> list[str]:
    return sorted(row.raw_json["sedentaryPeriod"]["interval"]["endTime"] for row in rows)


async def test_the_old_name_back_with_a_shorter_end_is_counted(sessions, clock) -> None:
    """Review 6b fb sed_chain_shrinka: under a decided chain the new name returned a longer
    end, then the old name came back with the shorter one: every period stayed hidden behind
    the stale longer end. The end returned now is counted at once, and the stale one is
    marked by the complete fetch that no longer returned it."""
    await legacy(sessions, SEDENTARY, period(15, A1), T0 + 15 * MINUTE, last=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 5 * HOUR
    await store(sessions, SEDENTARY, period(15, A2))
    clock.now = T0 + 6 * HOUR
    await identity_backfill(sessions, incremental=True)
    clock.advance(minutes=10)
    since = clock.now
    await store(sessions, SEDENTARY, period(17, A2))
    await mark(sessions, SEDENTARY, T0 - HOUR, T0 + HOUR, since)
    clock.advance(minutes=10)
    since = clock.now
    await store(sessions, SEDENTARY, period(15, A1))
    assert "2026-10-18T09:15:00Z" in ends(await counted_rows(sessions, SEDENTARY))
    await mark(sessions, SEDENTARY, T0 - HOUR, T0 + HOUR, since)
    assert ends(await counted_rows(sessions, SEDENTARY)) == ["2026-10-18T09:15:00Z"]
    assert (await problems(sessions))[0] == []


# --- AB7: rows stay bounded under name flapping ----------------------------------------------


async def flap(sessions, clock, spec, record, steps) -> list[int]:
    """Each step: one complete fetch returning ``names`` with ``value``, then a nightly run.
    After every fetch exactly the returned record is counted (never hidden behind a stale
    one). Returns the rows after each step."""
    rows = []
    for names, value in steps:
        clock.advance(minutes=10)
        since = clock.now
        returned = [record(name, value) for name in names]
        await store(sessions, spec, *returned)
        await mark(sessions, spec, T0 - HOUR, T0 + HOUR, since)
        shown = [
            {key: part for key, part in row.raw_json.items() if key != "dataSource"}
            for row in await counted_rows(sessions, spec)
        ]
        wanted = {key: part for key, part in returned[0].items() if key != "dataSource"}
        assert wanted in shown, (names, value, shown)
        await identity_backfill(sessions, incremental=True)
        rows.append(len(await raw_rows(sessions, spec)))
    return rows


async def chained(sessions, clock, spec, record, *, old_form_first: bool = True) -> None:
    """A1 -> A2 decided; the record stored under both (``old_form_first``), or only under the
    new name (a record of after the rename)."""
    if old_form_first:
        await legacy(sessions, spec, record(A1, 0), T0 + 10 * MINUTE, last=T0 + HOUR)
    else:
        other = heart(55, at=T0 - HOUR, name=A1) if spec is HEART else None
        assert other is not None
        await legacy(sessions, spec, other, T0 - HOUR, last=T0 - HOUR + 10 * MINUTE)
    await identity_backfill(sessions)
    clock.now = T0 + 5 * HOUR
    await store(sessions, spec, record(A2, 0))
    clock.now = T0 + 6 * HOUR
    await identity_backfill(sessions, incremental=True)


COREV = [step for value in range(10) for step in (((A1, A2), value), ((A2,), value + 1))]


@pytest.mark.parametrize(
    ("pattern", "old_form_first"),
    [
        # Review 6b fb abflap2: A, A, B, B ... with values 0, 2, 0, 2.
        ([((A1,), 0), ((A1,), 2), ((A2,), 0), ((A2,), 2)] * 5, True),
        # corev: both forms co-returned, then the new name alone revised, rising (6,240 rows
        # for 720 records, deployed 1,440) - with the record first under the new name only.
        (COREV, True),
        (COREV, False),
    ],
)
async def test_rows_stay_bounded_under_name_flapping(
    sessions, clock, pattern, old_form_first
) -> None:
    def record(name: str, value: int) -> dict:
        return heart(60 + value, at=T0, name=name)

    await chained(sessions, clock, HEART, record, old_form_first=old_form_first)
    rows = await flap(sessions, clock, HEART, record, pattern)
    assert max(rows) <= 3, rows
    [row] = [
        row
        for row in await counted_rows(sessions)
        if row.raw_json["heartRate"]["sampleTime"]["physicalTime"] == "2026-10-18T09:00:00Z"
    ]
    names, value = pattern[-1]
    assert row.raw_json["heartRate"]["beatsPerMinute"] == 60 + value
    assert (await problems(sessions))[0] == []


async def test_a_flapping_end_stays_bounded_and_visible(sessions, clock) -> None:
    def record(name: str, value: int) -> dict:
        return period(15 + value, name)

    await chained(sessions, clock, SEDENTARY, record)
    pattern = [((A1,), 0), ((A2,), 2)] * 6
    rows = await flap(sessions, clock, SEDENTARY, record, pattern)
    assert max(rows) <= 3, rows
    assert ends(await counted_rows(sessions, SEDENTARY)) == ["2026-10-18T09:17:00Z"]


# --- AB9: a retired second watch does not block the band's rename ----------------------------


async def test_a_retired_watch_fetched_again_does_not_block_the_rename(sessions, clock) -> None:
    """Review 6b t1: a second watch worn beside the band (two devices: other values in two
    fetches, the second for newer records) was retired before the band was renamed; a later
    re-fetch of its old records sighted it again, after the new name appeared. The rename was
    left open ('more than one source ended before it', then 'seen alongside'): counted twice
    until decided by hand. It is chained. Review 7 S1: retired means its newest record is more
    than ``RETIRED_AFTER`` (days) older than the band's and the new name's first sighting, so
    the band records on for ten days after the watch's last record here."""
    worn = [T0 + index * MINUTE for index in range(3)]
    for at in worn:
        await legacy(sessions, HEART, heart(60, at=at, name=A1), T0 + 5 * MINUTE)
        await legacy(sessions, HEART, heart(90, at=at, name=WATCH), T0 + 5 * MINUTE)
    later = [T0 + 2 * HOUR + index * MINUTE for index in range(3)]
    for at in later:
        await legacy(sessions, HEART, heart(61, at=at, name=A1), T0 + 2 * HOUR + 5 * MINUTE)
        await legacy(sessions, HEART, heart(91, at=at, name=WATCH), T0 + 2 * HOUR + 5 * MINUTE)
    # The watch is retired; the band records on for days, then is renamed.
    band = [T0 + 10 * DAY + index * MINUTE for index in range(3)]
    for at in band:
        await legacy(sessions, HEART, heart(62, at=at, name=A1), T0 + 10 * DAY + 5 * MINUTE)
    await identity_backfill(sessions)
    clock.now = T0 + 10 * DAY + 10 * HOUR
    await store(sessions, HEART, *(heart(62, at=at, name=A2) for at in band))
    # A re-fetch of the watch's old records sights it again after the new name appeared.
    clock.now = T0 + 10 * DAY + 12 * HOUR
    await store(sessions, HEART, *(heart(91, at=at, name=WATCH) for at in later))
    report = await nightly(sessions)
    assert [(chain["from"], chain["to"]) for chain in report.chains] == [
        (f"FITBIT / {A1} / DERIVED", f"FITBIT / {A2} / DERIVED")
    ]
    assert report.ambiguous == []
    assert len(await counted_rows(sessions)) == 6 + 6 + 3


async def test_a_watch_that_kept_recording_still_blocks(sessions, clock) -> None:
    """Without weakening ambiguity elsewhere: a watch still recording when the band's name
    changed (its newest record not older than the band's) blocks the rename as before."""
    for at in (T0 + index * MINUTE for index in range(3)):
        await legacy(sessions, HEART, heart(60, at=at, name=A1), T0 + 5 * MINUTE)
        await legacy(sessions, HEART, heart(90, at=at, name=WATCH), T0 + 5 * MINUTE)
    for at in (T0 + 2 * HOUR + index * MINUTE for index in range(3)):
        await legacy(sessions, HEART, heart(61, at=at, name=A1), T0 + 2 * HOUR + 5 * MINUTE)
        await legacy(sessions, HEART, heart(91, at=at, name=WATCH), T0 + 2 * HOUR + 5 * MINUTE)
    await identity_backfill(sessions)
    clock.now = T0 + 20 * HOUR
    await store(
        sessions,
        HEART,
        heart(62, at=T0 + 20 * HOUR, name=A2),
        heart(92, at=T0 + 20 * HOUR, name=WATCH),
    )
    report = await nightly(sessions)
    assert report.chains == [] and report.ambiguous


# --- M4, M5 and minor findings ---------------------------------------------------------------


async def test_a_cycle_refusal_names_the_chain_to_reject(sessions, clock) -> None:
    """Review 6b M4: making A1 -> A2 live again while A2 -> A1 is live would close a cycle;
    the refusal names the chain to reject first."""
    await renamed_with_history(sessions, clock, [T0])
    await decide_sources(sessions, "heart-rate", "reject", fp(A1)[:12], fp(A2)[:12])
    await decide_sources(sessions, "heart-rate", "link", fp(A2)[:12], fp(A1)[:12])
    with pytest.raises(DecisionRefused, match="reject the chain " + fp(A2)[:12]):
        await decide_sources(sessions, "heart-rate", "confirm", fp(A1)[:12], fp(A2)[:12])


async def test_a_revived_row_of_a_decided_device_is_counted_once_at_once(sessions, clock):
    """Review 6b M5: a marked row whose record came back with the reading the device's other
    name shows was counted twice until the next reconciler run (240 records)."""
    keys = [T0 + index * MINUTE for index in range(3)]
    for at in keys:
        await legacy(sessions, HEART, heart(59, at=at, name=A1), at, deleted=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 5 * HOUR
    await store(sessions, HEART, *(heart(60, at=at, name=A2) for at in keys))
    clock.now = T0 + 6 * HOUR
    await nightly(sessions)
    assert len(await counted_rows(sessions)) == 3
    clock.advance(hours=1)
    await store(sessions, HEART, *(heart(60, at=at, name=A1) for at in keys))
    assert len(await counted_rows(sessions)) == 3
    assert (await problems(sessions))[0] == []


async def stored_row(sessions, record, google_id, seen, deleted=None):
    from app.parsers.common import payload_hash, temporal

    start, end, zone = temporal(HEART, record)
    row = RawHealthData(
        google_data_point_id=google_id,
        data_type="heart-rate",
        source_start_time=start,
        source_end_time=end,
        source_timezone=zone,
        raw_json=record,
        payload_hash=payload_hash(record),
        first_seen_at=seen,
        last_seen_at=seen,
        source_deleted_at=deleted,
    )
    async with sessions.begin() as session:
        session.add(row)
    return row


async def link(sessions, old, new, reason="display-name-change"):
    from app.parsers.identity import record_identity

    async with sessions.begin() as session:
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old.id,
                new_raw_id=new.id,
                data_type="heart-rate",
                old_google_data_point_id=old.google_data_point_id,
                new_google_data_point_id=new.google_data_point_id,
                reason=reason,
                old_fingerprint=record_identity(HEART, old.raw_json).source,
                new_fingerprint=record_identity(HEART, new.raw_json).source,
                same_reading=True,
                new_payload_hash=new.payload_hash,
            )
        )


async def test_a_hidden_root_beside_a_fitting_row_never_sticks_a_page(sessions, clock) -> None:
    """Review 6a minor (identity.py:588 vs :758): spares were probed only when the v2s row did
    not fit, while the row written onto was the first root in order; a v1 row first in order
    that did not fit (hidden behind another value) beside a fitting v2s row and an existing
    spare made the page insert that spare's id again (a RuntimeError on every sync). Any own
    row that fits is written onto."""
    from app.parsers.common import source_id
    from app.parsers.identity import new_record_id, record_identity, spare_record_id

    await renamed_with_history(sessions, clock, [T0 + HOUR])  # A1 -> A2 decided
    mine = heart(60, name=A1)
    identity = record_identity(HEART, mine)
    assert identity is not None
    other61, other60 = heart(61, name=A2), heart(60, name=A2)
    shows61 = await stored_row(
        sessions, other61, new_record_id(record_identity(HEART, other61).specific_key), T0
    )
    shows60 = await stored_row(
        sessions, other60, spare_record_id(record_identity(HEART, other60), 1), T0
    )
    v1 = await stored_row(sessions, heart(59, name=A1), source_id(HEART, mine), T0 + 2 * HOUR)
    v2s = await stored_row(sessions, heart(58, name=A1), new_record_id(identity.specific_key), T0)
    spare = await stored_row(sessions, heart(57, name=A1), spare_record_id(identity, 1), T0)
    await link(sessions, v1, shows61)
    await link(sessions, v2s, shows60)
    await link(sessions, spare, shows61)
    clock.now = T0 + 9 * HOUR
    stats = await store(sessions, HEART, mine)
    assert (stats.inserted, stats.updated) == (0, 1)  # onto the fitting v2s row
    assert any(row.id == shows60.id for row in await counted_rows(sessions))


async def test_the_reconciler_ignores_a_quarantined_holder(sessions, clock) -> None:
    """Review 6a minor: the reconciler took a row held by a quarantined row for hidden (unlike
    readers), so it was never linked to the live row of its device."""
    keys = [T0]
    await renamed_with_history(sessions, clock, keys)
    async with sessions() as session:
        holder = await session.scalar(select(RawHealthDataSupersession.new_raw_id))
    async with sessions.begin() as session:
        await session.execute(
            update(RawHealthData).where(RawHealthData.id == holder).values(is_quarantined=True)
        )
    report = (await identity_backfill(sessions))["heart-rate"]
    assert report.already_superseded == 0  # readers count it again: so does the reconciler
