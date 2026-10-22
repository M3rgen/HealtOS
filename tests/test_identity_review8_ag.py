"""Review 8 AG: Google added a device field to a source's records. No value here is real data.

Google began sending, for records of a band it had always sent without one,
``dataSource.device.formFactor`` (and for some data types the form factor without the
``displayName``). Same platform, recording method and name, the same readings at the same times:
the same records of the same device. ``formFactor`` is one of the stable source fields of the v2
key, so the new form had *another key* for every record: the next complete fetch of a window
marked the old form's rows deleted although their readings were returned (the deferral only
looked at rows of the same key), and the reconciler never compared the two forms (chains are
decided per stable class, links per key): the false marks were silent, and the owner could not
even link the two (``identity-decide`` refused two stable classes).

A device field one form has and the other lacks (``formFactor``, ``manufacturer``) no longer
keeps two forms apart: they share a *match key* (the v2 key without those fields; the key and
every row id are unchanged), the deletion marking defers on them, the reconciler chains them
like a rename and links their rows, and the owner can decide them like any rename.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from identity_records import HEART, STEPS, T0, counted_rows, interval, legacy, mark, store
from sqlalchemy import select

from app.models.identity import RawSourceDevice
from app.models.state import RawHealthData
from app.parsers.identity import record_identity, source_fingerprint
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import DecisionRefused, decide_sources
from app.sync.deletions import deferred_deletions

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
BAND = "Example Band"
FORM = "FITNESS_BAND"


def band(name: str | None = BAND, form: str | None = None, maker: str | None = None) -> dict:
    device: dict = {}
    if name is not None:
        device["displayName"] = name
    if form is not None:
        device["formFactor"] = form
    if maker is not None:
        device["manufacturer"] = maker
    source: dict = {"platform": "FITBIT", "recordingMethod": "DERIVED"}
    if device:
        source["device"] = device
    return source


def sample(value: int, at, source: dict) -> dict:
    return {
        "dataSource": source,
        "heartRate": {"sampleTime": {"physicalTime": at.isoformat()}, "beatsPerMinute": value},
    }


def fp(source: dict) -> str:
    return source_fingerprint(source)


TIMES = [T0 + index * 15 * MINUTE for index in range(4)]
VALUES = [61, 64, 58, 70]


def readings(source: dict) -> list[dict]:
    return [sample(value, at, source) for value, at in zip(VALUES, TIMES, strict=True)]


async def marked(sessions, spec=HEART) -> list[RawHealthData]:
    async with sessions() as session:
        query = select(RawHealthData).where(
            RawHealthData.data_type == spec.name, RawHealthData.source_deleted_at.is_not(None)
        )
        return list((await session.scalars(query)).all())


def values(rows) -> list[int]:
    return sorted(row.raw_json["heartRate"]["beatsPerMinute"] for row in rows)


async def old_form_archived(sessions, clock, old: dict) -> None:
    """The old form's rows, archived and reconciled (complete spans), last seen at T0 + 2h."""
    for record in readings(old):
        await legacy(sessions, HEART, record, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)


NEW_FORMS = {
    "form factor added": band(form=FORM),
    "form factor added, name dropped": band(None, form=FORM),
    "manufacturer added": band(maker="Example Inc."),
}


def test_the_match_key_leaves_out_added_device_fields_and_keeps_every_id() -> None:
    old = record_identity(HEART, sample(61, T0, band()))
    for new_source in NEW_FORMS.values():
        new = record_identity(HEART, sample(61, T0, new_source))
        assert old is not None and new is not None
        assert new.key != old.key  # the key (and every id) did not change
        assert new.match == old.match == old.key
    phone = record_identity(HEART, sample(61, T0, band(form="PHONE")))
    assert phone is not None and phone.match == old.match


@pytest.mark.parametrize("new_source", NEW_FORMS.values(), ids=NEW_FORMS.keys())
async def test_a_field_added_by_google_marks_nothing_and_is_chained(
    sessions, clock, new_source
) -> None:
    """The production sequence: rows of the band; hours later a complete fetch returns the same
    readings with the form factor added. Nothing is marked (P3); the reconciler chains the old
    form to the new one like a rename (listed for review: P4), and each reading counts once."""
    old = band()
    await old_form_archived(sessions, clock, old)
    clock.now = since = T0 + 8 * HOUR
    await store(sessions, HEART, *readings(new_source))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    assert await marked(sessions) == []
    async with sessions() as session:
        listed = await deferred_deletions(session)
    assert [entry["deferred"] for entry in listed] == [4]
    assert values(await counted_rows(sessions)) == sorted(VALUES * 2)  # until reconciled

    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    assert [(entry["fingerprints"], entry["decided"]) for entry in stats.chains] == [
        ([fp(old)[:12], fp(new_source)[:12]], "this run")
    ]
    assert [entry["fingerprints"] for entry in stats.review] == [
        [fp(old)[:12], fp(new_source)[:12]]
    ]
    assert stats.deletions_kept == stats.deletions_suspicious == 0
    assert stats.ambiguous == []
    counted = await counted_rows(sessions)
    assert values(counted) == sorted(VALUES)
    assert {fp(row.raw_json["dataSource"]) for row in counted} == {fp(new_source)}
    async with sessions() as session:
        assert await deferred_deletions(session) == []
        devices = (await session.scalars(select(RawSourceDevice.fingerprint))).all()
    assert set(devices) == {fp(old), fp(new_source)}

    async with sessions() as session:
        _, errors, _ = await verify_identities(session)
    assert errors == []

    # The next complete fetch: the deferrals are decided, nothing is marked.
    clock.now = since = T0 + 10 * HOUR
    await store(sessions, HEART, *readings(new_source))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    assert await marked(sessions) == []
    assert values(await counted_rows(sessions)) == sorted(VALUES)


async def test_google_toggling_the_field_back_and_forth_marks_nothing(sessions, clock) -> None:
    """A -> A + formFactor -> A -> A + formFactor: every complete fetch returns every reading
    under one of the two forms; none is ever marked and each counts once."""
    old, new = band(), band(form=FORM)
    await old_form_archived(sessions, clock, old)
    clock.now = since = T0 + 8 * HOUR
    await store(sessions, HEART, *readings(new))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 9 * HOUR
    await identity_backfill(sessions, incremental=True)
    for hours, form in ((12, old), (15, new), (18, old)):
        clock.now = since = T0 + hours * HOUR
        await store(sessions, HEART, *readings(form))
        await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
        assert await marked(sessions) == [], form
        assert values(await counted_rows(sessions)) == sorted(VALUES), form
        clock.advance(hours=1)
        await identity_backfill(sessions, incremental=True)
        assert values(await counted_rows(sessions)) == sorted(VALUES), form
        assert await marked(sessions) == []


async def test_the_field_dropped_again_before_any_reconciler_run_marks_nothing(
    sessions, clock
) -> None:
    """A -> A + formFactor -> A, with no reconciler run in between: each complete fetch defers
    the form it did not return (the other form returned every reading); nothing is marked."""
    old, new = band(), band(form=FORM)
    await old_form_archived(sessions, clock, old)
    for hours, form in ((8, new), (10, old), (12, new)):
        clock.now = since = T0 + hours * HOUR
        await store(sessions, HEART, *readings(form))
        await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
        assert await marked(sessions) == [], form
        async with sessions() as session:
            assert sum(entry["deferred"] for entry in await deferred_deletions(session)) == 4
    clock.now = T0 + 13 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    # The two forms alternated: the sightings cannot tell a rename, so the owner is asked.
    assert stats.ambiguous and stats.chains == []
    await decide_sources(sessions, HEART.name, "link", fp(old)[:12], fp(new)[:12])
    assert values(await counted_rows(sessions)) == sorted(VALUES)


async def test_marks_made_before_the_fix_are_kept_and_reconciled(sessions, clock) -> None:
    """Production already holds false marks of the old form (written by the marking before this
    fix). They stay (no mark is ever removed by the reconciler); the reconciler chains the two
    forms, so the marked rows' readings are counted once, through the new form's rows, and the
    report no longer counts them as genuine deletions."""
    old, new = band(), band(form=FORM)
    await old_form_archived(sessions, clock, old)
    clock.now = T0 + 8 * HOUR
    await store(sessions, HEART, *readings(new))
    async with sessions.begin() as session:
        rows = (
            await session.scalars(
                select(RawHealthData).where(RawHealthData.first_seen_at < T0 + 2 * HOUR)
            )
        ).all()
        for row in rows:
            row.source_deleted_at = T0 + 8 * HOUR
    assert len(await marked(sessions)) == 4
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    assert [entry["fingerprints"] for entry in stats.review] == [[fp(old)[:12], fp(new)[:12]]]
    assert stats.deletions_kept == 0
    assert len(await marked(sessions)) == 4
    assert values(await counted_rows(sessions)) == sorted(VALUES)
    # Google returns the old form again: its rows are live again and hold the reading once.
    clock.now = since = T0 + 12 * HOUR
    await store(sessions, HEART, *readings(old))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    assert await marked(sessions) == []
    assert values(await counted_rows(sessions)) == sorted(VALUES)


async def test_marks_made_before_the_fix_that_cannot_be_chained_are_listed(sessions, clock) -> None:
    """The same false marks, with both forms seen side by side (no chain): the reconciler lists
    them as suspicious deletions and the new form as ambiguous (P4), instead of counting them as
    genuine deletions; the owner's link settles them."""
    old, new = band(), band(form=FORM)
    await old_form_archived(sessions, clock, old)
    clock.now = T0 + 8 * HOUR
    await store(sessions, HEART, *readings(new), sample(66, T0 + 7 * HOUR, old))
    async with sessions.begin() as session:
        rows = (
            await session.scalars(
                select(RawHealthData).where(RawHealthData.first_seen_at < T0 + 2 * HOUR)
            )
        ).all()
        for row in rows:
            row.source_deleted_at = T0 + 8 * HOUR
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    assert stats.chains == []
    assert [entry["fingerprint"] for entry in stats.ambiguous] == [fp(new)[:12]]
    assert (stats.deletions_kept, stats.deletions_suspicious) == (0, 4)
    await decide_sources(sessions, HEART.name, "link", fp(old)[:12], fp(new)[:12])
    assert len(await marked(sessions)) == 4
    assert values(await counted_rows(sessions)) == sorted([*VALUES, 66])


async def test_both_forms_returned_side_by_side_are_listed_and_the_owner_decides(
    sessions, clock
) -> None:
    """Google sends the field for some records only (both forms in every fetch): the sightings
    overlap, so the reconciler cannot chain them; nothing is marked, the case is listed
    (ambiguous, deferred deletions), and the owner can link the two forms - and undo it."""
    old, new = band(), band(None, form=FORM)
    await old_form_archived(sessions, clock, old)
    clock.now = since = T0 + 8 * HOUR
    later = T0 + 7 * HOUR
    await store(sessions, HEART, *readings(new), sample(66, later, old))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    assert await marked(sessions) == []
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    assert stats.chains == []
    assert [entry["fingerprint"] for entry in stats.ambiguous] == [fp(new)[:12]]
    assert sum(entry["deferred"] for entry in stats.deferred_deletions) == 4
    outcome = await decide_sources(sessions, HEART.name, "link", fp(old)[:12], fp(new)[:12])
    assert outcome.chain == "live"
    assert values(await counted_rows(sessions)) == sorted([*VALUES, 66])
    await decide_sources(sessions, HEART.name, "reject", fp(old)[:12], fp(new)[:12])
    assert values(await counted_rows(sessions)) == sorted([*VALUES, *VALUES, 66])


async def test_two_form_factors_are_never_one_device(sessions, clock) -> None:
    """A field both forms have, with two values (a phone and a band), keeps them apart: never
    chained, never a reason to defer, and the owner cannot link them."""
    phone, watch = band("Example", form="PHONE"), band("Example", form="WATCH")
    await old_form_archived(sessions, clock, phone)
    clock.now = since = T0 + 8 * HOUR
    await store(sessions, HEART, *readings(watch))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    assert values(await marked(sessions)) == sorted(VALUES)
    clock.now = T0 + 9 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    assert stats.chains == [] and stats.ambiguous == []
    with pytest.raises(DecisionRefused):
        await decide_sources(sessions, HEART.name, "link", fp(phone)[:12], fp(watch)[:12])


async def test_an_interval_type_with_the_field_added_marks_nothing(sessions, clock) -> None:
    """Steps (intervals) of the band, then the same intervals with the form factor and no name."""
    old, new = band(), band(None, form=FORM)

    def counts(source):
        return [
            {"dataSource": source, "steps": {"interval": interval(at, 1), "count": str(value)}}
            for value, at in zip(VALUES, TIMES, strict=True)
        ]

    for record in counts(old):
        await legacy(sessions, STEPS, record, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
    clock.now = since = T0 + 8 * HOUR
    await store(sessions, STEPS, *counts(new))
    await mark(sessions, STEPS, T0 - HOUR, T0 + 2 * HOUR, since)
    assert await marked(sessions, STEPS) == []
    clock.now = T0 + 9 * HOUR
    await identity_backfill(sessions)
    assert len(await counted_rows(sessions, STEPS)) == 4


@pytest.mark.parametrize("new_source", NEW_FORMS.values(), ids=NEW_FORMS.keys())
async def test_a_renamed_band_that_gets_the_field_is_chained_on(
    sessions, clock, new_source
) -> None:
    """The production shape: the band was renamed before (a chain old name -> name), then
    Google added the form factor to the new name's records. The old name is not kindred to the
    new form (another name *and* the field), yet its device may continue as the new form (no
    device field with two values): chained on, nothing marked, each reading once."""
    first, old = band("Example Band Pro"), band()
    for record in readings(first):
        await legacy(sessions, HEART, record, T0 + HOUR, last=T0 + HOUR)
    clock.now = T0 + 2 * HOUR
    await identity_backfill(sessions)
    clock.now = since = T0 + 3 * HOUR
    await store(sessions, HEART, *readings(old))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    clock.now = T0 + 4 * HOUR
    await identity_backfill(sessions, incremental=True)
    clock.now = since = T0 + 9 * HOUR
    await store(sessions, HEART, *readings(new_source))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    assert await marked(sessions) == []
    clock.now = T0 + 10 * HOUR
    stats = (await identity_backfill(sessions))[HEART.name]
    assert sorted(entry["fingerprints"] for entry in stats.chains) == sorted(
        [[fp(first)[:12], fp(old)[:12]], [fp(old)[:12], fp(new_source)[:12]]]
    )
    assert stats.deletions_kept == 0 and stats.ambiguous == []
    assert values(await counted_rows(sessions)) == sorted(VALUES)
    clock.now = since = T0 + 11 * HOUR
    await store(sessions, HEART, *readings(new_source))
    await mark(sessions, HEART, T0 - HOUR, T0 + 2 * HOUR, since)
    assert await marked(sessions) == []
    async with sessions() as session:
        _, errors, _ = await verify_identities(session)
    assert errors == []


async def test_every_deferral_of_a_renamed_band_can_be_decided(sessions, clock) -> None:
    """The renamed band's both forms and the new form with the field seen side by side (no
    chain): the rows of both names are deferred, never marked, and the owner can answer each
    deferred pair - also the old name and the new form, which are not kindred themselves."""
    first, old, new = band("Example Band Pro"), band(), band(form=FORM)
    for record in readings(first):
        await legacy(sessions, HEART, record, T0 + HOUR, last=T0 + HOUR)
    clock.now = T0 + 2 * HOUR
    await identity_backfill(sessions)
    clock.now = T0 + 3 * HOUR
    await store(sessions, HEART, sample(66, T0 + 2 * HOUR, old))
    clock.now = T0 + 4 * HOUR
    await identity_backfill(sessions, incremental=True)
    clock.now = since = T0 + 9 * HOUR
    await store(sessions, HEART, *readings(new), sample(67, T0 + 8 * HOUR, old))
    await mark(sessions, HEART, T0 - HOUR, T0 + HOUR + 30 * MINUTE, since)
    assert await marked(sessions) == []
    async with sessions() as session:
        listed = await deferred_deletions(session)
    assert listed and {tuple(entry["fingerprints"]) for entry in listed} <= {
        (fp(first)[:12], fp(new)[:12]),
        (fp(old)[:12], fp(new)[:12]),
    }
    for entry in listed:
        left, right = entry["fingerprints"]
        await decide_sources(sessions, HEART.name, "distinct", left, right)
    clock.now = since = T0 + 10 * HOUR
    await store(sessions, HEART, *readings(new), sample(67, T0 + 8 * HOUR, old))
    await mark(sessions, HEART, T0 - HOUR, T0 + HOUR + 30 * MINUTE, since)
    async with sessions() as session:
        assert await deferred_deletions(session) == []
    assert values(await marked(sessions)) == sorted(VALUES)
