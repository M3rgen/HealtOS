"""SQL loading and append-only chain, evidence, decision and device writes.

Transactions and locks belong to the caller. Storage's dialect helpers stay local imports
because storage itself consumes the source-chain API. Callers obtain bookkeeping times via
``source_chains.database_now`` so the existing clock override applies to these writes too.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    exists,
    func,
    literal,
    select,
    text,
    union_all,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import as_utc
from app.models.identity import (
    CHAIN_DECISIONS,
    PAIR_DECISIONS,
    REFUTED,
    IdentityReconcileState,
    RawSourceChain,
    RawSourceChainEvent,
    RawSourceDecision,
    RawSourceDevice,
    RawSourcePair,
    RawSourceSighting,
)
from app.models.state import RawHealthData
from app.services.source_chain_evidence import (
    PAIR_KEYS,
    Evidence,
    Span,
    evidence,
    key_time,
    ordered_pair,
    parse_agreement,
    parse_premise,
)
from app.services.source_chain_model import Chains, SourceState


async def database_now(session: AsyncSession) -> datetime:
    """The database clock (bookkeeping, and sightings of a page without a response time)."""
    if session.get_bind().dialect.name == "postgresql":
        return as_utc(await session.scalar(text("SELECT clock_timestamp()")))
    return datetime.now(UTC)


def _span(row: RawSourceSighting) -> Span:
    return Span(
        as_utc(row.first_seen_at),
        as_utc(row.last_seen_at),
        row.stable_class,
        row.label,
        row.complete,
        row.data_source,
        as_utc(row.newest_record_at) if row.newest_record_at is not None else None,
    )


async def load_state(
    session: AsyncSession, data_type: str, seen: Iterable[str] = ()
) -> SourceState:
    """Links, refutations, owner decisions, evidence and logged devices of ``data_type``;
    for the fingerprints ``seen``, which have a sighting, and whether the history is known.
    One round trip (a ``UNION ALL``)."""
    chain, event, decision, pair, device = (
        RawSourceChain,
        RawSourceChainEvent,
        RawSourceDecision,
        RawSourcePair,
        RawSourceDevice,
    )
    zero = literal(0, Integer)
    never = literal(None, DateTime(timezone=True))
    parts: list[Any] = [
        select(
            literal("p", String),
            pair.first_fingerprint,
            pair.second_fingerprint,
            pair.identity_key,
            zero,
            pair.seen_together_at,
        ).where(pair.data_type == data_type),
        select(
            literal("c", String),
            chain.from_fingerprint,
            chain.to_fingerprint,
            chain.decided_by,
            zero,
            never,
        ).where(chain.data_type == data_type),
        select(
            literal("e", String),
            event.from_fingerprint,
            event.to_fingerprint,
            event.event,
            zero,
            never,
        ).where(event.data_type == data_type),
        select(
            literal("d", String),
            decision.first_fingerprint,
            decision.second_fingerprint,
            decision.decision,
            decision.id,
            never,
        ).where(decision.data_type == data_type),
        select(
            literal("v", String),
            device.fingerprint,
            device.device,
            literal("", String),
            device.id,
            never,
        ).where(device.data_type == data_type),
    ]
    parts.append(
        select(
            literal("q", String),
            chain.from_fingerprint,
            chain.to_fingerprint,
            chain.evidence,
            zero,
            never,
        ).where(chain.data_type == data_type, chain.evidence.contains('"premise"'))
    )
    parts.append(
        select(
            literal("g", String),
            literal("", String),
            literal("", String),
            IdentityReconcileState.pair_agreement,
            zero,
            never,
        ).where(
            IdentityReconcileState.data_type == data_type,
            IdentityReconcileState.pair_agreement.is_not(None),
        )
    )
    seen = sorted(set(seen))
    if seen:
        sighting = RawSourceSighting
        parts.append(
            select(
                literal("s", String),
                sighting.fingerprint,
                literal("", String),
                literal("", String),
                zero,
                sighting.first_seen_at,
            ).where(sighting.data_type == data_type, sighting.fingerprint.in_(seen))
        )
        complete = select(sighting.fingerprint).where(
            sighting.data_type == data_type, sighting.complete.is_(True)
        )
        rows = select(RawHealthData.id).where(RawHealthData.data_type == data_type)
        parts.append(
            select(
                literal("h", String),
                literal("", String),
                literal("", String),
                literal("", String),
                literal(1, Integer),
                never,
            ).where(exists(complete) | ~exists(rows))
        )
    chains = Chains()
    decisions: list[tuple[int, str, str, str]] = []
    devices: list[tuple[int, str, str]] = []
    state = SourceState(chains)
    result = await session.execute(union_all(*parts))
    for kind, first, second, value, number, at in result.tuples():
        if kind == "c":
            chains.links.add((first, second))
            chains.decided_by[(first, second)] = value
        elif kind == "e" and value == REFUTED:
            chains.refuted_events.add((first, second))
        elif kind == "d":
            decisions.append((number, value, first, second))
        elif kind == "p":
            chains.evidence.setdefault((first, second), []).append(Evidence(value, as_utc(at)))
        elif kind == "v":
            devices.append((number, first, second))
        elif kind == "s":
            state.known.add(first)
            state.first_seen[first] = as_utc(at)
        elif kind == "h":
            state.history_known = True
        elif kind == "g":
            chains.agreement.update(parse_agreement(value))
        elif kind == "q":
            premise = parse_premise(value)
            if premise:
                chains.premises[(first, second)] = premise
    for _, fingerprint, name in sorted(devices):
        chains.logged_devices[fingerprint] = name
    for _, value, first, second in sorted(decisions):
        chains.decide(value, first, second)
    return state


async def load_chains(
    session: AsyncSession, data_type: str, fingerprints: Iterable[str] | None = None
) -> Chains:
    """Every link, refutation, decision, piece of evidence and logged device of
    ``data_type``, and the spans of the linked and the given fingerprints (all spans of the
    data type when ``fingerprints`` is ``None``)."""
    chains = (await load_state(session, data_type)).chains
    query = select(RawSourceSighting).where(RawSourceSighting.data_type == data_type)
    if fingerprints is not None:
        wanted = sorted({*fingerprints, *(name for link in chains.links for name in link)})
        if not wanted:
            return chains
        query = query.where(RawSourceSighting.fingerprint.in_(wanted))
    for row in (await session.scalars(query)).all():
        chains.spans[row.fingerprint] = _span(row)
    return chains


def _upsert(session: AsyncSession, *, complete_wins: bool):
    from app.services.storage import dialect_insert

    table = RawSourceSighting.__table__
    statement = dialect_insert(session, table)
    postgresql = session.get_bind().dialect.name == "postgresql"
    lowest, highest = (func.least, func.greatest) if postgresql else (func.min, func.max)
    newest, incoming = table.c.newest_record_at, statement.excluded.newest_record_at
    updates = {
        "first_seen_at": lowest(table.c.first_seen_at, statement.excluded.first_seen_at),
        "last_seen_at": highest(table.c.last_seen_at, statement.excluded.last_seen_at),
        # Only ever later (SQLite's max() of a NULL is NULL: coalesced either way).
        "newest_record_at": highest(
            func.coalesce(newest, incoming), func.coalesce(incoming, newest)
        ),
        "updated_at": statement.excluded.updated_at,
    }
    if complete_wins:
        updates["complete"] = literal(True)
    return statement.on_conflict_do_update(
        index_elements=["data_type", "fingerprint"], set_=updates
    )


async def record_sightings(
    session: AsyncSession,
    data_type: str,
    seen: Mapping[str, tuple[str, str, object, datetime | None]],
    at: datetime,
    *,
    history_known: bool,
) -> None:
    """Note that the fingerprints ``seen`` (fingerprint -> stable class, label, dataSource,
    the physical time of its newest record in the page) were returned by Google at ``at``
    (when the response was fetched). A fingerprint seen for the first time is complete when
    ``history_known``."""
    if not seen:
        return
    values = [
        {
            "data_type": data_type,
            "fingerprint": fingerprint,
            "stable_class": stable,
            "label": label,
            "data_source": data_source,
            "first_seen_at": at,
            "last_seen_at": at,
            "newest_record_at": newest,
            "complete": history_known,
            "updated_at": at,
        }
        for fingerprint, (stable, label, data_source, newest) in sorted(seen.items())
    ]
    # Sorted, so two writers of one data type lock the rows in the same order.
    await session.execute(_upsert(session, complete_wins=False), values)


async def write_spans(
    session: AsyncSession, data_type: str, spans: Mapping[str, Span], now: datetime
) -> None:
    """Merge spans built from the whole archive (they are complete from now on), and mark
    complete every other sighting of the data type: a fingerprint the full run did not find
    in the archive has no row older than the run, so its sightings cover all of it."""
    if spans:
        values = [
            {
                "data_type": data_type,
                "fingerprint": fingerprint,
                "stable_class": span.stable,
                "label": span.label,
                "data_source": span.data_source,
                "first_seen_at": span.first,
                "last_seen_at": span.last,
                "newest_record_at": span.newest,
                "complete": True,
                "updated_at": now,
            }
            for fingerprint, span in sorted(spans.items())
        ]
        statement = _upsert(session, complete_wins=True)
        for offset in range(0, len(values), 1000):
            await session.execute(statement, values[offset : offset + 1000])
    await session.execute(
        update(RawSourceSighting)
        .where(RawSourceSighting.data_type == data_type, RawSourceSighting.complete.is_(False))
        .values(complete=True, updated_at=now)
        .execution_options(synchronize_session=False)
    )


async def record_evidence(
    session: AsyncSession,
    data_type: str,
    chains: Chains,
    found: Iterable[tuple[str, str, str, datetime]],
) -> int:
    """Note keys returned under two fingerprints with two readings (``(fingerprint,
    fingerprint, key, when its fetch began)``): per pair and fetch at most ``PAIR_KEYS`` keys,
    the newest first, and only while they can still help establish the pair (``useful``)."""
    from app.services.storage import dialect_insert

    grouped: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
    for left, right, key, seen in found:
        if left != right:
            grouped[ordered_pair(left, right)].append(Evidence(key, as_utc(seen)))
    values = []
    for (first, second), items in sorted(grouped.items()):
        per_fetch: dict[datetime, int] = defaultdict(int)
        items.sort(key=lambda item: (item.seen, -(key_time(item.key) or item.seen).timestamp()))
        for item in items:
            if per_fetch[item.seen] >= PAIR_KEYS:
                continue
            if not chains.add_evidence(first, second, item.key, item.seen):
                continue
            per_fetch[item.seen] += 1
            values.append(
                {
                    "data_type": data_type,
                    "first_fingerprint": first,
                    "second_fingerprint": second,
                    "identity_key": item.key,
                    "seen_together_at": item.seen,
                }
            )
    if values:
        statement = dialect_insert(session, RawSourcePair.__table__).on_conflict_do_nothing()
        for offset in range(0, len(values), 1000):
            await session.execute(statement, values[offset : offset + 1000])
    return len(values)


async def record_refutations(
    session: AsyncSession, data_type: str, chains: Chains, now: datetime
) -> list[tuple[str, str]]:
    """Append a ``refuted`` event for every recorded link whose two fingerprints became
    established distinct by evidence (once per link; not when the owner decided the pair).
    Returns the new ones."""
    from app.services.storage import dialect_insert

    found = [(old, new) for old, new in sorted(chains.links) if chains.refutable(old, new)]
    if not found:
        return []
    values = [
        {
            "data_type": data_type,
            "from_fingerprint": old,
            "to_fingerprint": new,
            "event": REFUTED,
            "evidence": json.dumps(
                {
                    "keys": [
                        {"key": item.key, "seen": item.seen.isoformat()}
                        for item in sorted(
                            chains.evidence.get(ordered_pair(old, new), ()),
                            key=lambda item: (item.seen, item.key),
                        )
                    ],
                },
                sort_keys=True,
            ),
            "occurred_at": now,
        }
        for old, new in found
    ]
    statement = dialect_insert(session, RawSourceChainEvent.__table__).on_conflict_do_nothing()
    await session.execute(statement, values)
    chains.refuted_events.update(found)
    chains._changed()
    return found


async def record_devices(
    session: AsyncSession, data_type: str, chains: Chains, now: datetime
) -> dict[str, str]:
    """Append the device of every fingerprint whose device changed (``raw_source_devices``;
    readers decide from it which supersessions are in force). Returns them."""
    changed = chains.devices_to_log()
    if changed:
        session.add_all(
            RawSourceDevice(data_type=data_type, fingerprint=name, device=device, decided_at=now)
            for name, device in sorted(changed.items())
        )
        await session.flush()
        chains.logged_devices.update(changed)
    return changed


async def insert_chain(
    session: AsyncSession,
    data_type: str,
    chains: Chains,
    old: str,
    new: str,
    *,
    decided_by: str,
    now: datetime,
    shared_keys: int | None = None,
    decision_id: int | None = None,
) -> bool:
    """Record ``old -> new`` (rule or decision checked by the caller, under the exclusive
    lock). ``False`` when it would fork a live chain or another writer recorded it first. A
    link recorded before (rejected or refuted) is not recorded again: the owner's decision
    makes it live."""
    from app.services.storage import dialect_insert

    if chains.following.get(old) not in (None, new) or chains.preceding.get(new) not in (
        None,
        old,
    ):
        return False
    if (old, new) in chains.links:
        return True
    statement = (
        dialect_insert(session, RawSourceChain.__table__)
        .values(
            data_type=data_type,
            from_fingerprint=old,
            to_fingerprint=new,
            from_label=chains.spans[old].label,
            to_label=chains.spans[new].label,
            decided_by=decided_by,
            evidence=evidence(chains, old, new, shared_keys, decision_id),
            decided_at=now,
        )
        .on_conflict_do_nothing()
        .returning(RawSourceChain.id)
    )
    inserted = (await session.execute(statement)).scalar() is not None
    if inserted:
        chains.add(old, new)
        chains.decided_by[(old, new)] = decided_by
    return inserted


async def record_decision(
    session: AsyncSession,
    data_type: str,
    chains: Chains,
    decision: str,
    first: str,
    second: str,
    *,
    note: str | None,
    now: datetime,
) -> int:
    """Append the owner's decision (``raw_source_decisions``); returns its id."""
    if decision not in (*CHAIN_DECISIONS, *PAIR_DECISIONS):
        raise ValueError(f"unknown decision {decision!r}")
    if decision in PAIR_DECISIONS:
        first, second = ordered_pair(first, second)
    row = RawSourceDecision(
        data_type=data_type,
        decision=decision,
        first_fingerprint=first,
        second_fingerprint=second,
        first_label=chains.spans[first].label,
        second_label=chains.spans[second].label,
        note=note,
        decided_at=now,
    )
    session.add(row)
    await session.flush()
    chains.decide(decision, first, second)
    return row.id
