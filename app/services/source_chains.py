"""Which source fingerprints are one device: decided per source, by the reconciler only.

A record without a Google id is identified by its v2 key (app/parsers/identity.py), which
leaves out ``displayName`` and the package's ``.<UUID>`` suffix. Two records with one key and
two different ``dataSource`` fingerprints are either

* the same record before and after a change of its source (Google renamed the device, Apple
  Health dropped the suffix): Google then returns only the new form, everywhere, from the
  moment of the change; or
* two devices that recorded at the same instant: Google keeps returning both.

The write path never decides which (app/services/identity.py): an unknown fingerprint always
gets rows of its own, so at worst a renamed device is counted twice until the reconciler has
run. Only the reconciler (``health-mirror identity-backfill``, nightly as an incremental run)
decides, per data type and *stable class* (the source fields a rename keeps), from every
fingerprint's sighting span (``raw_source_sightings``: first and last time any record carrying
it was received, by the database clock). ``A -> B`` is a link of one device's chain only when

1. every fingerprint of the class has a complete span (built from the whole archive by a full
   ``identity-backfill``, or first seen after that);
2. ``B`` was first seen more than ``CHAIN_GAP`` (five minutes) after ``A`` was last seen;
3. ``A`` is ``B``'s only predecessor candidate (a fingerprint last seen before ``B`` appeared
   with no other fingerprint appearing in between) and ``B`` is ``A``'s only successor
   candidate (first to appear after ``A`` ended);
4. no other fingerprint of the class was first seen in between, or was seen alongside
   (overlapping spans) either ``A`` or ``B``;
5. neither already has a link the other way of the chain (a chain never forks or cycles).

Everything else is *ambiguous*: nothing is linked, both stay separate rows (visible; at worst
counted twice), and the run's report lists the case with its reason for review. A three-step
rename ``A -> B -> C`` chains step by step even if ``B`` never met ``A``'s rows.

A decided link is kept for good (``raw_source_chains`` is append-only, with the spans it was
decided on as evidence). When a later sighting contradicts it (``A`` seen again, after or
within five minutes of ``B``'s first sighting), a ``contradicted`` event is appended to
``raw_source_chain_events`` and the link is no longer used for new pairings; supersessions
made under it stay valid (``verify`` notes it).

Two fingerprints returned together *for one key* (``raw_source_pairs``: two records of one
instant, one per source, in one page or one fetch) are *decided distinct* without any reconciler
run, and a row of one never keeps a deleted record of the other. Overlapping spans alone do not
decide that: a device whose old name comes back overlaps its new name too.

Concurrency: ``store_records`` and deletion marking hold a shared transaction lock per data
type, a reconciler batch the exclusive one (waiting at most ``LOCK_TIMEOUT``, then retrying),
and only one reconciler runs at a time (a session lock); see :func:`lock_data_type` and
:func:`backfill_lock`.
"""

from __future__ import annotations

import json
import zlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import as_utc
from app.models.identity import (
    CONTRADICTED,
    RawSourceChain,
    RawSourceChainEvent,
    RawSourcePair,
    RawSourceSighting,
)
from app.models.state import RawHealthData

CHAIN_GAP = timedelta(minutes=5)
# A chain is followed at most this far (it is a path: at most one successor each).
MAX_CHAIN = 64
# pg_advisory locks: (namespace, key). Per data type: (DATA_TYPE_LOCK, crc32(name)).
DATA_TYPE_LOCK = 0x48454931  # "HEI1"
BACKFILL_LOCK = (0x48454932, 1)
# How long a reconciler batch waits for the exclusive lock before it backs off and retries.
LOCK_TIMEOUT = "10s"
RULE = (
    "B first seen more than CHAIN_GAP after A last seen; A the only predecessor candidate of "
    "B and B the only successor candidate of A; no other source of the class first seen in "
    "between or seen alongside either; complete spans"
)


class BackfillRunning(RuntimeError):
    """Another ``identity-backfill`` holds the single-run lock."""


async def database_now(session: AsyncSession) -> datetime:
    """The database clock (one clock for every process that writes sightings)."""
    if session.get_bind().dialect.name == "postgresql":
        return as_utc(await session.scalar(text("SELECT clock_timestamp()")))
    return datetime.now(UTC)


@dataclass
class Span:
    first: datetime
    last: datetime
    stable: str
    label: str
    complete: bool
    data_source: object = None

    def widen(self, first: datetime, last: datetime) -> None:
        self.first = min(self.first, as_utc(first))
        self.last = max(self.last, as_utc(last))

    def as_dict(self) -> dict:
        return {"first": self.first.isoformat(), "last": self.last.isoformat()}


def precedes(old: Span, new_first: datetime) -> bool:
    """``old`` was last seen more than ``CHAIN_GAP`` before ``new_first``."""
    return old.last + CHAIN_GAP < new_first


def overlaps(left: Span, right: Span) -> bool:
    """Both were seen at one time (returned together): two distinct sources."""
    return left.first <= right.last and right.first <= left.last


def gap_seconds(old: Span, new: Span) -> float:
    """Seconds from ``old``'s last to ``new``'s first sighting (negative: they overlap)."""
    return (new.first - old.last).total_seconds()


@dataclass
class Chains:
    """The recorded links of one data type, their contradictions, and the spans."""

    following: dict[str, str] = field(default_factory=dict)
    spans: dict[str, Span] = field(default_factory=dict)
    # Fingerprints whose outgoing link has a contradiction event.
    contradicted: set[str] = field(default_factory=set)
    # Ordered fingerprint pairs returned together for one key (raw_source_pairs).
    together: set[tuple[str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.preceding = {new: old for old, new in self.following.items()}

    def add(self, old: str, new: str) -> None:
        self.following[old] = new
        self.preceding[new] = old

    def contradicts(self, old: str, new: str) -> bool:
        """The spans now say the two forms were seen together (or within the gap)."""
        before, after = self.spans.get(old), self.spans.get(new)
        return before is not None and after is not None and not precedes(before, after.first)

    def usable(self, old: str, new: str) -> bool:
        """Recorded, never contradicted, and the spans still agree: used for pairing."""
        return (
            self.following.get(old) == new
            and old not in self.contradicted
            and not self.contradicts(old, new)
        )

    def root(self, fingerprint: str) -> str:
        """The first fingerprint of ``fingerprint``'s device, along usable links."""
        current = fingerprint
        for _ in range(MAX_CHAIN):
            previous = self.preceding.get(current)
            if previous is None or previous == fingerprint or not self.usable(previous, current):
                return current
            current = previous
        return current

    def members(self, fingerprint: str) -> list[str]:
        """The device's fingerprints in order, along usable links."""
        current = self.root(fingerprint)
        found = [current]
        for _ in range(MAX_CHAIN):
            following = self.following.get(current)
            if following is None or following in found or not self.usable(current, following):
                break
            found.append(following)
            current = following
        return found

    def predecessors(self, fingerprint: str) -> list[str]:
        """Earlier fingerprints of the device, closest first (along usable links)."""
        members = self.members(fingerprint)
        return list(reversed(members[: members.index(fingerprint)]))

    def same_device(self, left: str, right: str) -> bool:
        return left == right or self.root(left) == self.root(right)

    def distinct(self, left: str, right: str) -> bool:
        """Decided distinct: once returned together for one key (``raw_source_pairs``).

        Overlapping sighting spans are not enough: a device whose old name comes back
        overlaps its new name, but never shows both for one record."""
        return left != right and tuple(sorted((left, right))) in self.together

    def reaches(self, start: str, target: str) -> bool:
        """``target`` follows ``start`` along recorded links (usable or not)."""
        current = start
        for _ in range(MAX_CHAIN):
            current = self.following.get(current, "")
            if not current:
                return False
            if current == target:
                return True
        return True  # longer than any chain may be: treat as a cycle


# --- the rule ------------------------------------------------------------------------------


@dataclass
class Decision:
    links: list[tuple[str, str]] = field(default_factory=list)
    # One entry per fingerprint that might continue another but is not linked, with why.
    ambiguous: list[dict] = field(default_factory=list)


def _appeared_between(spans: Mapping[str, Span], members, after: datetime, before: datetime):
    return [name for name in members if after < spans[name].first < before]


def decide_chains(chains: Chains) -> Decision:
    """New links by the rule of the module docstring (``chains`` gains them)."""
    decision = Decision()
    spans = chains.spans
    classes: dict[str, list[str]] = defaultdict(list)
    for fingerprint, span in spans.items():
        classes[span.stable].append(fingerprint)

    def note(new: str, why: str, candidates: Iterable[str] = ()) -> None:
        decision.ambiguous.append(
            {
                "source": spans[new].label,
                "fingerprint": new[:12],
                "first_seen": spans[new].first.isoformat(),
                "why": why,
                "candidates": sorted(spans[name].label for name in candidates),
            }
        )

    for members in classes.values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda name: (spans[name].first, name))
        incomplete = [name for name in ordered if not spans[name].complete]
        if incomplete:
            for name in incomplete:
                note(name, "history incomplete: run identity-backfill (a full run) first")
            continue
        for new in ordered:
            if new in chains.preceding:
                continue
            others = [name for name in ordered if name != new]
            ended_before = [name for name in others if spans[name].last < spans[new].first]
            predecessors = [
                old
                for old in ended_before
                if precedes(spans[old], spans[new].first)
                and not _appeared_between(
                    spans,
                    (name for name in others if name != old),
                    spans[old].last,
                    spans[new].first,
                )
            ]
            if not predecessors:
                near = [old for old in ended_before if not precedes(spans[old], spans[new].first)]
                if near:
                    note(new, "a source of its class ended less than five minutes before", near)
                continue
            if len(predecessors) > 1:
                note(new, "more than one source of its class ended before it", predecessors)
                continue
            old = predecessors[0]
            if old in chains.following:
                note(new, "its predecessor already continues as another source", [old])
                continue
            successors = [
                name
                for name in ordered
                if name != old
                and precedes(spans[old], spans[name].first)
                and not _appeared_between(
                    spans,
                    (other for other in ordered if other not in (old, name)),
                    spans[old].last,
                    spans[name].first,
                )
            ]
            if successors != [new]:
                note(new, "its predecessor has more than one successor candidate", successors)
                continue
            alongside = [
                name
                for name in ordered
                if name not in (old, new)
                and (overlaps(spans[name], spans[old]) or overlaps(spans[name], spans[new]))
            ]
            if alongside:
                note(new, "another source of its class was seen alongside", alongside)
                continue
            if chains.reaches(new, old):
                note(new, "linking would close a cycle", [old])
                continue
            chains.add(old, new)
            decision.links.append((old, new))
    return decision


def evidence(chains: Chains, old: str, new: str, shared_keys: int | None = None) -> str:
    before, after = chains.spans[old], chains.spans[new]
    return json.dumps(
        {
            "rule": RULE,
            "chain_gap_seconds": CHAIN_GAP.total_seconds(),
            "old": before.as_dict(),
            "new": after.as_dict(),
            "gap_seconds": gap_seconds(before, after),
            "shared_keys": shared_keys,
        },
        sort_keys=True,
    )


def parse_evidence(value: str) -> tuple[Span, Span] | None:
    """The spans a link was decided on, or ``None`` when they do not satisfy the gap rule."""
    try:
        data = json.loads(value)
        old = Span(
            datetime.fromisoformat(data["old"]["first"]),
            datetime.fromisoformat(data["old"]["last"]),
            "",
            "",
            True,
        )
        new = Span(
            datetime.fromisoformat(data["new"]["first"]),
            datetime.fromisoformat(data["new"]["last"]),
            "",
            "",
            True,
        )
    except (ValueError, KeyError, TypeError):
        return None
    if old.first > old.last or new.first > new.last or not precedes(old, new.first):
        return None
    return old, new


# --- storage -----------------------------------------------------------------------------


def _span(row: RawSourceSighting) -> Span:
    return Span(
        as_utc(row.first_seen_at),
        as_utc(row.last_seen_at),
        row.stable_class,
        row.label,
        row.complete,
        row.data_source,
    )


async def load_chains(
    session: AsyncSession, data_type: str, fingerprints: Iterable[str] | None = None
) -> Chains:
    """Every link and contradiction of ``data_type``, and the spans of the linked and the
    given fingerprints (all spans of the data type when ``fingerprints`` is ``None``)."""
    links = (
        await session.execute(
            select(RawSourceChain.from_fingerprint, RawSourceChain.to_fingerprint).where(
                RawSourceChain.data_type == data_type
            )
        )
    ).tuples()
    chains = Chains(dict(links.all()))
    if chains.following:
        chains.contradicted = set(
            (
                await session.scalars(
                    select(RawSourceChainEvent.from_fingerprint).where(
                        RawSourceChainEvent.data_type == data_type,
                        RawSourceChainEvent.event == CONTRADICTED,
                    )
                )
            ).all()
        )
    query = select(RawSourceSighting).where(RawSourceSighting.data_type == data_type)
    pairs = select(RawSourcePair.first_fingerprint, RawSourcePair.second_fingerprint).where(
        RawSourcePair.data_type == data_type
    )
    if fingerprints is not None:
        wanted = sorted({*fingerprints, *chains.following, *chains.preceding})
        if not wanted:
            return chains
        query = query.where(RawSourceSighting.fingerprint.in_(wanted))
        pairs = pairs.where(
            RawSourcePair.first_fingerprint.in_(wanted)
            | RawSourcePair.second_fingerprint.in_(wanted)
        )
    chains.together = set((await session.execute(pairs)).tuples().all())
    for row in (await session.scalars(query)).all():
        chains.spans[row.fingerprint] = _span(row)
    return chains


async def _history_known(session: AsyncSession, data_type: str) -> bool:
    """A span first seen now covers the archive: a full run built this data type's spans,
    or the data type has no rows yet."""
    built = select(RawSourceSighting.fingerprint).where(
        RawSourceSighting.data_type == data_type, RawSourceSighting.complete.is_(True)
    )
    rows = select(RawHealthData.id).where(RawHealthData.data_type == data_type)
    return bool(await session.scalar(select(exists(built) | ~exists(rows))))


def _upsert(session: AsyncSession, *, complete_wins: bool):
    from app.services.storage import dialect_insert

    table = RawSourceSighting.__table__
    statement = dialect_insert(session, table)
    postgresql = session.get_bind().dialect.name == "postgresql"
    lowest, highest = (func.least, func.greatest) if postgresql else (func.min, func.max)
    updates = {
        "first_seen_at": lowest(table.c.first_seen_at, statement.excluded.first_seen_at),
        "last_seen_at": highest(table.c.last_seen_at, statement.excluded.last_seen_at),
        "updated_at": statement.excluded.updated_at,
    }
    if complete_wins:
        updates["complete"] = literal(True)
    return statement.on_conflict_do_update(
        index_elements=["data_type", "fingerprint"], set_=updates
    )


async def record_sightings(
    session: AsyncSession, data_type: str, seen: Mapping[str, tuple[str, str, object]]
) -> datetime:
    """Note that the fingerprints ``seen`` (fingerprint -> stable class, label, dataSource)
    were received now (the database clock, returned)."""
    now = await database_now(session)
    if not seen:
        return now
    known = set(
        (
            await session.scalars(
                select(RawSourceSighting.fingerprint).where(
                    RawSourceSighting.data_type == data_type,
                    RawSourceSighting.fingerprint.in_(sorted(seen)),
                )
            )
        ).all()
    )
    complete = await _history_known(session, data_type) if set(seen) - known else False
    values = [
        {
            "data_type": data_type,
            "fingerprint": fingerprint,
            "stable_class": stable,
            "label": label,
            "data_source": data_source,
            "first_seen_at": now,
            "last_seen_at": now,
            "complete": complete,
            "updated_at": now,
        }
        for fingerprint, (stable, label, data_source) in sorted(seen.items())
    ]
    # Sorted, so two writers of one data type lock the rows in the same order.
    await session.execute(_upsert(session, complete_wins=False), values)
    return now


async def write_spans(
    session: AsyncSession, data_type: str, spans: Mapping[str, Span], now: datetime
) -> None:
    """Merge spans built from the whole archive (they are complete from now on)."""
    if not spans:
        return
    values = [
        {
            "data_type": data_type,
            "fingerprint": fingerprint,
            "stable_class": span.stable,
            "label": span.label,
            "data_source": span.data_source,
            "first_seen_at": span.first,
            "last_seen_at": span.last,
            "complete": True,
            "updated_at": now,
        }
        for fingerprint, span in sorted(spans.items())
    ]
    statement = _upsert(session, complete_wins=True)
    for offset in range(0, len(values), 1000):
        await session.execute(statement, values[offset : offset + 1000])


async def record_pairs(
    session: AsyncSession, data_type: str, pairs: Iterable[tuple[str, str]], now: datetime
) -> None:
    """Note fingerprints returned together for one key (once per pair)."""
    from app.services.storage import dialect_insert

    ordered = sorted({tuple(sorted(pair)) for pair in pairs if pair[0] != pair[1]})
    if not ordered:
        return
    values = [
        {
            "data_type": data_type,
            "first_fingerprint": first,
            "second_fingerprint": second,
            "seen_together_at": now,
        }
        for first, second in ordered
    ]
    statement = dialect_insert(session, RawSourcePair.__table__).on_conflict_do_nothing()
    for offset in range(0, len(values), 1000):
        await session.execute(statement, values[offset : offset + 1000])


async def record_contradictions(
    session: AsyncSession, data_type: str, chains: Chains, now: datetime
) -> list[tuple[str, str]]:
    """Append a ``contradicted`` event for every recorded link the spans now contradict
    (once per link); those links are no longer used. Returns the new ones."""
    from app.services.storage import dialect_insert

    found = [
        (old, new)
        for old, new in sorted(chains.following.items())
        if old not in chains.contradicted and chains.contradicts(old, new)
    ]
    if not found:
        return []
    values = [
        {
            "data_type": data_type,
            "from_fingerprint": old,
            "to_fingerprint": new,
            "event": CONTRADICTED,
            "evidence": json.dumps(
                {"old": chains.spans[old].as_dict(), "new": chains.spans[new].as_dict()},
                sort_keys=True,
            ),
            "occurred_at": now,
        }
        for old, new in found
    ]
    statement = dialect_insert(session, RawSourceChainEvent.__table__).on_conflict_do_nothing()
    await session.execute(statement, values)
    chains.contradicted.update(old for old, _ in found)
    return found


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
) -> bool:
    """Record ``old -> new`` (rule checked by the caller, under the exclusive lock).

    Refused when it would fork a chain or close a cycle; ``False`` when a link for either
    fingerprint was recorded first."""
    from app.services.storage import dialect_insert

    if chains.following.get(old) not in (None, new) or chains.preceding.get(new) not in (
        None,
        old,
    ):
        return False
    statement = (
        dialect_insert(session, RawSourceChain.__table__)
        .values(
            data_type=data_type,
            from_fingerprint=old,
            to_fingerprint=new,
            from_label=chains.spans[old].label,
            to_label=chains.spans[new].label,
            decided_by=decided_by,
            evidence=evidence(chains, old, new, shared_keys),
            decided_at=now,
        )
        .on_conflict_do_nothing()
        .returning(RawSourceChain.from_fingerprint)
    )
    inserted = (await session.execute(statement)).scalar() is not None
    if inserted:
        chains.add(old, new)
    return inserted


# --- locks -------------------------------------------------------------------------------


def _key(data_type: str) -> int:
    # Same value on every process and server version (hashtext is not guaranteed stable).
    return zlib.crc32(data_type.encode()) - 2**31


async def lock_data_type(
    session: AsyncSession, data_type: str, *, exclusive: bool, wait: str | None = None
) -> None:
    """Transaction lock on ``data_type``'s identities: shared for writers of records and
    deletion marks, exclusive for a reconciler batch (PostgreSQL only).

    ``wait``: wait at most this long (``lock_timeout``; a waiting exclusive request queues
    every new writer behind it, so the reconciler must not wait long)."""
    if session.get_bind().dialect.name != "postgresql":
        return
    if wait is not None:
        await session.execute(text(f"SET LOCAL lock_timeout = '{wait}'"))
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await session.execute(
        text(f"SELECT {function}(:namespace, :key)"),
        {"namespace": DATA_TYPE_LOCK, "key": _key(data_type)},
    )
    if wait is not None:
        await session.execute(text("SET LOCAL lock_timeout TO DEFAULT"))


@asynccontextmanager
async def backfill_lock(sessions: async_sessionmaker):
    """Hold the single-run lock of identity-backfill (a session lock on its own connection,
    outside any transaction); refuse at once when another run holds it."""
    bind = sessions.kw.get("bind")
    if bind is None or bind.dialect.name != "postgresql":
        yield
        return
    connection = await bind.connect()
    try:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:namespace, :key)"),
            {"namespace": BACKFILL_LOCK[0], "key": BACKFILL_LOCK[1]},
        )
        if not acquired:
            raise BackfillRunning(
                "another identity-backfill is running (PostgreSQL advisory lock "
                f"{BACKFILL_LOCK[0]}/{BACKFILL_LOCK[1]} is held); wait for it to finish or "
                "stop it, then run again (it resumes where the archive left off)"
            )
        try:
            yield
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:namespace, :key)"),
                {"namespace": BACKFILL_LOCK[0], "key": BACKFILL_LOCK[1]},
            )
    finally:
        await connection.close()
