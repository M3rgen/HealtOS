"""Review 8 AG, round 2: the reviewer's adversarial tests of d0fa72c (synthetic data only).

B1: kindred is not transitive (a form without ``formFactor`` is kindred to a phone and to a
watch of its name): no decision and no reconciler run may make a phone and a watch one device,
and no supersession may join them (verify would fail for good: links are append-only). S1/S2:
sources without optional device fields chain exactly as before. S3: a source that is kindred to
neither form of a chain (another device) does not block it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from identity_records import HEART, T0, counted_rows, legacy, mark, store

from app.parsers.identity import source_fingerprint
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import DecisionRefused, decide_sources

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def src(name=None, form=None, maker=None):
    device = {}
    if name is not None:
        device["displayName"] = name
    if form is not None:
        device["formFactor"] = form
    if maker is not None:
        device["manufacturer"] = maker
    out = {"platform": "FITBIT", "recordingMethod": "DERIVED"}
    if device:
        out["device"] = device
    return out


def sample(value, at, source):
    return {
        "dataSource": source,
        "heartRate": {"sampleTime": {"physicalTime": at.isoformat()}, "beatsPerMinute": value},
    }


def fp(s):
    return source_fingerprint(s)[:12]


TIMES = [T0 + i * 15 * MINUTE for i in range(4)]
VALUES = [61, 64, 58, 70]


def readings(source, times=TIMES, values=VALUES):
    return [sample(v, t, source) for v, t in zip(values, times, strict=True)]


def vals(rows):
    return sorted(r.raw_json["heartRate"]["beatsPerMinute"] for r in rows)


async def errors_of(sessions):
    async with sessions() as session:
        _, errors, _ = await verify_identities(session)
    return errors


# --- 1. non-transitive "compatible": owner links P~A and A~W -> phone and watch one device ---
async def test_owner_links_phone_to_bare_and_bare_to_watch(sessions, clock):
    phone, bare, watch = src("X", form="PHONE"), src("X"), src("X", form="WATCH")
    clock.now = T0 + 3 * HOUR
    # all three return the same readings side by side (one page)
    await store(sessions, HEART, *readings(phone), *readings(bare), *readings(watch))
    clock.now = T0 + 4 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    print("ambiguous", [(e["fingerprint"], e["why"]) for e in stats.ambiguous])
    await decide_sources(sessions, HEART.name, "link", fp(phone), fp(bare))
    # The bare form is the phone's now: joining it to the watch would make phone and watch one.
    with pytest.raises(DecisionRefused, match="two values of a device field"):
        await decide_sources(sessions, HEART.name, "link", fp(bare), fp(watch))
    clock.now = T0 + 5 * HOUR
    await identity_backfill(sessions)
    errs = await errors_of(sessions)
    print("errors", errs)
    print("counted", vals(await counted_rows(sessions)))
    assert errs == []


# --- 1b. reconciler alone: P retired, then bare, then watch; same readings re-sent ---
async def test_reconciler_chains_phone_bare_watch(sessions, clock):
    phone, bare, watch = src("X", form="PHONE"), src("X"), src("X", form="WATCH")
    for r in readings(phone):
        await legacy(sessions, HEART, r, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
    clock.now = since = T0 + 8 * HOUR
    await store(sessions, HEART, *readings(bare))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 9 * HOUR
    s1 = (await identity_backfill(sessions))[HEART.name]
    print("chains1", [(e["fingerprints"]) for e in s1.chains])
    clock.now = since = T0 + 14 * HOUR
    await store(sessions, HEART, *readings(watch))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 15 * HOUR
    s2 = (await identity_backfill(sessions))[HEART.name]
    print("chains2", [(e["fingerprints"]) for e in s2.chains])
    errs = await errors_of(sessions)
    print("errors", errs)
    assert errs == []
    assert [e["fingerprints"] for e in s2.chains] == [[fp(phone), fp(bare)]]
    # The watch is never joined to the phone's device, and the case is listed (P4).
    assert [(e["fingerprint"], "another value" in e["why"]) for e in s2.ambiguous] == [
        (fp(watch), True)
    ]
    assert vals(await counted_rows(sessions)) == sorted(VALUES * 2)


# --- 2. a phone of the family seen alongside blocks the band's ff chain forever ---
async def test_phone_alongside_blocks_formfactor_chain(sessions, clock):
    band, band_ff, phone = (
        src("Band"),
        src("Band", form="FITNESS_BAND"),
        src("Pixel", form="PHONE", maker="Google"),
    )
    later = [t + 6 * HOUR for t in TIMES]
    for r in [*readings(band), *readings(phone, values=[90, 91, 92, 93])]:
        await legacy(sessions, HEART, r, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
    clock.now = since = T0 + 8 * HOUR
    # band now with formFactor; phone still sends (new records)
    await store(
        sessions, HEART, *readings(band_ff), *readings(phone, times=later, values=[80, 81, 82, 83])
    )
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    print("chains", [e["fingerprints"] for e in stats.chains])
    print("ambiguous", [(e["fingerprint"], e["why"], e["candidates"]) for e in stats.ambiguous])
    print("counted", vals(await counted_rows(sessions)))
    assert [e["fingerprints"] for e in stats.chains] == [[fp(band), fp(band_ff)]]


# --- 3. data with NO ff/mfr: same-class rename blocked by a watch (ff) of the family ---
async def test_bare_rename_with_watch_alongside(sessions, clock):
    old, new, watch = src("Charge 5"), src("Charge 6"), src("Pixel Watch", form="WATCH")
    later = [t + 6 * HOUR for t in TIMES]
    for r in [*readings(old), *readings(watch, values=[90, 91, 92, 93])]:
        await legacy(sessions, HEART, r, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
    clock.now = since = T0 + 8 * HOUR
    await store(
        sessions, HEART, *readings(new), *readings(watch, times=later, values=[80, 81, 82, 83])
    )
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    print("chains", [e["fingerprints"] for e in stats.chains])
    print("ambiguous", [(e["fingerprint"], e["why"]) for e in stats.ambiguous])
    assert [e["fingerprints"] for e in stats.chains] == [[fp(old), fp(new)]]


# --- 4. data with NO ff: device switch band -> watch(ff) now auto-chained ---
async def test_band_then_watch_sequential(sessions, clock):
    band, watch = src("Charge 5"), src("Pixel Watch", form="WATCH")
    for r in readings(band):
        await legacy(sessions, HEART, r, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
    clock.now = T0 + 8 * HOUR
    later = [t + 6 * HOUR for t in TIMES]
    await store(sessions, HEART, *readings(watch, times=later))
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    print("chains", [e["fingerprints"] for e in stats.chains])
    assert stats.chains == []


# --- 5. owner link across match keys then reject: exact reversibility ---
async def test_link_reject_link_reject(sessions, clock):
    old, new = src("Band"), src(None, form="FITNESS_BAND")
    for r in readings(old):
        await legacy(sessions, HEART, r, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
    clock.now = since = T0 + 8 * HOUR
    await store(sessions, HEART, *readings(new), sample(66, T0 + 7 * HOUR, old))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 9 * HOUR
    await identity_backfill(sessions)
    base = vals(await counted_rows(sessions))
    for _ in range(2):
        await decide_sources(sessions, HEART.name, "link", fp(old), fp(new))
        assert vals(await counted_rows(sessions)) == sorted([*VALUES, 66])
        assert await errors_of(sessions) == []
        await decide_sources(sessions, HEART.name, "reject", fp(old), fp(new))
        assert vals(await counted_rows(sessions)) == base
        assert await errors_of(sessions) == []
    # fresh fetch of both forms after reject: nothing marked, verify clean
    clock.now = since = T0 + 12 * HOUR
    await store(sessions, HEART, *readings(new), sample(66, T0 + 7 * HOUR, old))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 13 * HOUR
    await identity_backfill(sessions)
    assert vals(await counted_rows(sessions)) == base
    assert await errors_of(sessions) == []


# --- 6. three forms: bare, +ff, +mfr side by side ---
async def test_three_forms(sessions, clock):
    a, b, c = src("Band"), src("Band", form="FITNESS_BAND"), src("Band", maker="Acme")
    for r in readings(a):
        await legacy(sessions, HEART, r, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
    clock.now = since = T0 + 8 * HOUR
    await store(sessions, HEART, *readings(b)[:2], *readings(c)[2:])
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    print("chains", [e["fingerprints"] for e in stats.chains])
    print("ambiguous", [(e["fingerprint"], e["why"]) for e in stats.ambiguous])
    async with sessions() as session:
        from sqlalchemy import select

        from app.models.state import RawHealthData

        m = (
            await session.scalars(
                select(RawHealthData).where(RawHealthData.source_deleted_at.is_not(None))
            )
        ).all()
    print("marked", vals(m), "counted", vals(await counted_rows(sessions)))
    assert m == []
    assert await errors_of(sessions) == []


async def links_dump(sessions):
    from sqlalchemy import select

    from app.models.identity import RawHealthDataSupersession as L

    async with sessions() as session:
        rows = (await session.execute(select(L.old_fingerprint, L.new_fingerprint))).tuples().all()
    return sorted((a[:6], b[:6]) for a, b in rows)


async def test_reconciler_chain_then_dump(sessions, clock):
    phone, bare, watch = src("X", form="PHONE"), src("X"), src("X", form="WATCH")
    print("fps", fp(phone)[:6], fp(bare)[:6], fp(watch)[:6])
    for r in readings(phone):
        await legacy(sessions, HEART, r, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
    clock.now = since = T0 + 8 * HOUR
    await store(sessions, HEART, *readings(bare))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 9 * HOUR
    await identity_backfill(sessions)
    print("links1", await links_dump(sessions))
    clock.now = since = T0 + 14 * HOUR
    await store(sessions, HEART, *readings(watch))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 15 * HOUR
    await identity_backfill(sessions)
    print("links2", await links_dump(sessions))
    # phone form comes back (Google toggles), then full run
    clock.now = since = T0 + 20 * HOUR
    await store(sessions, HEART, *readings(phone))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 21 * HOUR
    await identity_backfill(sessions)
    print("links3", await links_dump(sessions))
    print("counted", vals(await counted_rows(sessions)))
    errs = await errors_of(sessions)
    print("errors", errs)
    assert errs == []
    assert all(
        {old, new} != {fp(phone)[:6], fp(watch)[:6]} for old, new in await links_dump(sessions)
    )


async def test_owner_links_then_rejects_verify_stays_broken(sessions, clock):
    phone, bare, watch = src("X", form="PHONE"), src("X"), src("X", form="WATCH")
    clock.now = T0 + 3 * HOUR
    await store(sessions, HEART, *readings(phone), *readings(bare), *readings(watch))
    clock.now = T0 + 4 * HOUR
    await identity_backfill(sessions)
    await decide_sources(sessions, HEART.name, "link", fp(phone), fp(bare))
    with pytest.raises(DecisionRefused):
        await decide_sources(sessions, HEART.name, "link", fp(bare), fp(watch))
    print("links", await links_dump(sessions))
    assert all(
        {old, new} != {fp(phone)[:6], fp(watch)[:6]} for old, new in await links_dump(sessions)
    )
    await decide_sources(sessions, HEART.name, "reject", fp(phone), fp(bare))
    # Rejected, the bare form can join the watch instead; nothing joins phone and watch.
    await decide_sources(sessions, HEART.name, "link", fp(bare), fp(watch))
    assert await errors_of(sessions) == []
    await decide_sources(sessions, HEART.name, "reject", fp(bare), fp(watch))
    print("counted after rejects", vals(await counted_rows(sessions)))
    errs = await errors_of(sessions)
    print("errors after rejects", errs)
    assert errs == []


def hk(name=None, form=None, maker=None, suffix="0A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9"):
    device = {
        k: v for k, v in (("displayName", name), ("formFactor", form), ("manufacturer", maker)) if v
    }
    out = {
        "platform": "HEALTH_KIT",
        "recordingMethod": "PASSIVELY_MEASURED",
        "application": {"packageName": f"com.apple.health.{suffix}"},
    }
    if device:
        out["device"] = device
    return out


async def test_healthkit_rename_with_bare_source_alongside(sessions, clock):
    """Prod-shape: iPhone (PHONE+mfr) renamed (package suffix changes), and a HealthKit source with
    no device fields recording alongside throughout. Only the iPhone's own fields matter."""
    old = hk("iPhone", "PHONE", "Apple", suffix="11111111-1111-1111-1111-111111111111")
    new = hk("iPhone", "PHONE", "Apple", suffix="22222222-2222-2222-2222-222222222222")
    app = hk(suffix="33333333-3333-3333-3333-333333333333")
    later = [t + 6 * HOUR for t in TIMES]
    for r in [*readings(old), *readings(app, values=[90, 91, 92, 93])]:
        await legacy(sessions, HEART, r, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
    clock.now = since = T0 + 8 * HOUR
    await store(
        sessions, HEART, *readings(new), *readings(app, times=later, values=[80, 81, 82, 83])
    )
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    print("chains", [e["fingerprints"] for e in stats.chains])
    print("ambiguous", [(e["fingerprint"], e["why"]) for e in stats.ambiguous])
    assert [e["fingerprints"] for e in stats.chains] == [[fp(old), fp(new)]]
