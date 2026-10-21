"""Which source fingerprints are one device: decided per source, by the reconciler or the owner.

A record without a Google id is identified by its v2 key (app/parsers/identity.py), which
leaves out ``displayName`` and the package's ``.<UUID>`` suffix. Two records with one key and
two different ``dataSource`` fingerprints are either

* the same record before and after a change of its source (Google renamed the device, Apple
  Health dropped the suffix, or the old name came back): one record, one value; or
* two devices that measured the same instant: two records, as a rule with two values.

The write path never decides which (app/services/identity.py): an unknown fingerprint always
gets rows of its own, so at worst a renamed device is counted twice until the reconciler has
run. Only the reconciler (``health-mirror identity-backfill``, nightly as an incremental run)
and the owner (``health-mirror identity-decide``) decide, per data type and *stable class*
(the source fields a rename keeps).

**Evidence of two devices** (``raw_source_pairs``): a key that one Google response returned
under two fingerprints *with two different readings*. A renamed device never returns two values
for one record; two devices that measured one instant do. Two fingerprints are *established
distinct* once ``PAIR_KEYS`` (three) different keys gave such evidence. One isolated
co-sighting is never enough: a single record carried in both forms (a stray duplicate while
Google switches a name) is not a second device, and with equal readings it is no evidence at
all (it is the same measurement). The owner's ``distinct`` / ``not-distinct`` decision
overrides the evidence either way.

**The chain rule.** From every fingerprint's sighting span (``raw_source_sightings``: first and
last time a record carrying it was received, stamped with the fetch time), ``A -> B`` is a link
of one device's chain only when

1. every fingerprint of the class has a complete span (built from the whole archive by a full
   ``identity-backfill``, or first seen after that);
2. ``B`` was first seen more than ``CHAIN_GAP`` (five minutes) after ``A`` was last seen;
3. ``A`` is ``B``'s only predecessor candidate (a fingerprint last seen before ``B`` appeared
   with no other one appearing in between) and ``B`` is ``A``'s only successor candidate;
4. no other fingerprint of the class was first seen in between, or was seen alongside
   (overlapping spans) either ``A`` or ``B``;
5. ``A`` and ``B`` are not established distinct, and neither already has a link the other way
   (a chain never forks or cycles).

A fingerprint established distinct from *both* ``A`` and ``B`` (a second watch worn alongside)
is left out of rules 3 and 4: it is known to be another device, so it says nothing about
``A -> B``. A fingerprint established distinct from ``B`` is never ``B``'s candidate.

Everything else is *ambiguous*: nothing is linked, both keep their rows (visible; at worst
counted twice), and every run's report and ``health-mirror status`` list the case with why, for
the owner to decide (``identity-decide link`` or ``distinct``).

A link joins its two fingerprints into one *device* in both directions. It is kept for good
(``raw_source_chains`` is append-only, with the spans it was decided on as evidence). The old
form returning later does not undo it (a name can change back); the device's rows simply take
that form again. A link stops being used only when it is *refuted* (the two fingerprints
became established distinct: a ``refuted`` event in ``raw_source_chain_events``) or *rejected*
by the owner; the supersessions made under it stay valid (they only ever merged equal
readings). Every link the reconciler decides is listed for review until the owner confirms or
rejects it.

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

from sqlalchemy import Integer, String, exists, func, literal, select, text, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import as_utc
from app.models.identity import (
    CHAIN_DECISIONS,
    PAIR_DECISIONS,
    REFUTED,
    RawSourceChain,
    RawSourceChainEvent,
    RawSourceDecision,
    RawSourcePair,
    RawSourceSighting,
)
from app.models.state import RawHealthData

CHAIN_GAP = timedelta(minutes=5)
# Keys with two different readings in one response that establish two distinct devices.
PAIR_KEYS = 3
# A chain is followed at most this far.
MAX_CHAIN = 64
# pg_advisory locks: (namespace, key). Per data type: (DATA_TYPE_LOCK, crc32(name)).
DATA_TYPE_LOCK = 0x48454931  # "HEI1"
BACKFILL_LOCK = (0x48454932, 1)
# How long a reconciler batch waits for the exclusive lock before it backs off and retries,
# and, once it holds it, for any row lock (a long transaction of another writer): it must
# never keep every writer of the data type waiting behind it.
LOCK_TIMEOUT = "10s"
ROW_LOCK_TIMEOUT = "5s"
RULE = (
    "B first seen more than CHAIN_GAP after A last seen; A the only predecessor candidate of "
    "B and B the only successor candidate of A; no other source of the class (unless "
    "established distinct from both) first seen in between or seen alongside either; A and B "
    "not established distinct; complete spans"
)


class BackfillRunning(RuntimeError):
    """Another ``identity-backfill`` holds the single-run lock."""


async def database_now(session: AsyncSession) -> datetime:
    """The database clock (bookkeeping, and sightings of a page without a response time)."""
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
    """Both were seen at one time."""
    return left.first <= right.last and right.first <= left.last


def gap_seconds(old: Span, new: Span) -> float:
    """Seconds from ``old``'s last to ``new``'s first sighting (negative: they overlap)."""
    return (new.first - old.last).total_seconds()


def ordered_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


@dataclass
class Chains:
    """The recorded links of one data type, the evidence and decisions about its sources,
    and (when loaded) the sighting spans."""

    following: dict[str, str] = field(default_factory=dict)
    spans: dict[str, Span] = field(default_factory=dict)
    # from fingerprint -> who decided its link (DECIDERS).
    decided_by: dict[str, str] = field(default_factory=dict)
    # Ordered fingerprint pair -> keys returned under both with two readings (raw_source_pairs).
    evidence: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    # The owner's latest decision: (from, to) -> link/reject/confirm; ordered pair ->
    # distinct/not-distinct.
    chain_decisions: dict[tuple[str, str], str] = field(default_factory=dict)
    pair_decisions: dict[tuple[str, str], str] = field(default_factory=dict)
    # from fingerprints whose link has a refuted event.
    refuted_events: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.preceding = {new: old for old, new in self.following.items()}
        self._devices: dict[str, frozenset[str]] | None = None

    def add(self, old: str, new: str) -> None:
        self.following[old] = new
        self.preceding[new] = old
        self._devices = None

    def forget(self, old: str, new: str) -> None:
        """Drop a link planned in memory that could not be recorded."""
        if self.following.get(old) == new:
            del self.following[old]
            self.preceding.pop(new, None)
            self._devices = None

    def decide(self, decision: str, first: str, second: str) -> None:
        """Apply an owner decision in memory (a dry run, or one just recorded)."""
        if decision in CHAIN_DECISIONS:
            self.chain_decisions[(first, second)] = decision
        else:
            self.pair_decisions[ordered_pair(first, second)] = decision
        self._devices = None

    def add_evidence(self, left: str, right: str, key: str) -> None:
        found = self.evidence.setdefault(ordered_pair(left, right), set())
        if len(found) < PAIR_KEYS:
            found.add(key)
            self._devices = None

    def distinct(self, left: str, right: str) -> bool:
        """Established distinct: the owner said so, or ``PAIR_KEYS`` keys were returned in one
        response under both with two readings (and the owner did not say otherwise)."""
        if left == right:
            return False
        pair = ordered_pair(left, right)
        decided = self.pair_decisions.get(pair)
        if decided is not None:
            return decided == "distinct"
        return len(self.evidence.get(pair, ())) >= PAIR_KEYS

    def rejected(self, old: str, new: str) -> bool:
        return self.chain_decisions.get((old, new)) == "reject"

    def refuted(self, old: str, new: str) -> bool:
        """A recorded link whose two forms are established distinct."""
        return self.following.get(old) == new and self.distinct(old, new)

    def usable(self, old: str, new: str) -> bool:
        """Recorded, neither rejected by the owner nor refuted: one device."""
        return (
            self.following.get(old) == new
            and not self.rejected(old, new)
            and not self.distinct(old, new)
        )

    def review(self, old: str, new: str) -> str:
        """``confirmed``/``rejected`` by the owner, ``owner`` (the owner linked it), or
        ``suggested`` (decided by the reconciler, not reviewed yet)."""
        decided = self.chain_decisions.get((old, new))
        if decided == "reject":
            return "rejected"
        if decided == "confirm":
            return "confirmed"
        if self.decided_by.get(old) == "owner" or decided == "link":
            return "owner"
        return "suggested"

    def _components(self, usable_only: bool) -> dict[str, frozenset[str]]:
        neighbours: dict[str, set[str]] = defaultdict(set)
        for old, new in self.following.items():
            if not usable_only or self.usable(old, new):
                neighbours[old].add(new)
                neighbours[new].add(old)
        found: dict[str, frozenset[str]] = {}
        for start in neighbours:
            if start in found:
                continue
            seen, frontier = {start}, [start]
            while frontier:
                current = frontier.pop()
                for following in neighbours[current] - seen:
                    seen.add(following)
                    frontier.append(following)
            members = frozenset(seen)
            for name in members:
                found[name] = members
        return found

    def members(self, fingerprint: str) -> frozenset[str]:
        """The device's fingerprints (``fingerprint`` and every one joined to it by usable
        links, either way)."""
        if self._devices is None:
            self._devices = self._components(usable_only=True)
        return self._devices.get(fingerprint, frozenset((fingerprint,)))

    def device(self, fingerprint: str) -> str:
        """A stable name of the device: its smallest fingerprint."""
        return min(self.members(fingerprint))

    def same_device(self, left: str, right: str) -> bool:
        return left == right or right in self.members(left)

    def ever_joined(self, left: str, right: str) -> bool:
        """``left`` and ``right`` are joined by recorded links (any state, either way): the
        rows of one were ever allowed to hold the other's form."""
        return left == right or right in self._components(usable_only=False).get(left, ())

    def returned(self, old: str, new: str) -> bool:
        """The old form was seen again after (or within the gap of) the new form's first
        sighting: the name changed back, or both forms are in use. Not a contradiction."""
        before, after = self.spans.get(old), self.spans.get(new)
        return before is not None and after is not None and not precedes(before, after.first)

    def reaches(self, start: str, target: str) -> bool:
        """``target`` follows ``start`` along recorded links."""
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


def describe(chains: Chains, fingerprint: str) -> dict:
    span = chains.spans.get(fingerprint)
    return {"source": span.label if span else "(unknown)", "fingerprint": fingerprint[:12]}


def decide_chains(chains: Chains) -> Decision:
    """New links by the rule of the module docstring (``chains`` gains them); every
    fingerprint the rule leaves open is listed in ``ambiguous`` with why."""
    decision = Decision()
    spans = chains.spans
    classes: dict[str, list[str]] = defaultdict(list)
    for fingerprint, span in spans.items():
        classes[span.stable].append(fingerprint)

    def note(new: str, why: str, candidates: Iterable[str] = ()) -> None:
        decision.ambiguous.append(
            {
                **describe(chains, new),
                "first_seen": spans[new].first.isoformat(),
                "why": why,
                "candidates": [
                    describe(chains, name)
                    for name in sorted(
                        set(candidates), key=lambda name: (spans[name].first, spans[name].label)
                    )
                ],
            }
        )

    def blocks(other: str, old: str, new: str) -> bool:
        # A source established distinct from both says nothing about old -> new.
        return not (chains.distinct(other, old) and chains.distinct(other, new))

    def between(old: str, new: str, members: list[str]) -> list[str]:
        return [
            name
            for name in members
            if name not in (old, new)
            and blocks(name, old, new)
            and spans[old].last < spans[name].first < spans[new].first
        ]

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
            # Fingerprints that could be this one's earlier form.
            earlier = [
                name
                for name in ordered
                if name != new
                and not chains.distinct(name, new)
                and spans[name].first < spans[new].first
            ]
            if not earlier:
                continue
            ended = [name for name in earlier if spans[name].last < spans[new].first]
            if not ended:
                note(
                    new,
                    "seen alongside another source of its class (two devices, or a rename "
                    "the sightings cannot tell apart)",
                    earlier,
                )
                continue
            near = [name for name in ended if not precedes(spans[name], spans[new].first)]
            if near:
                note(new, "a source of its class ended less than five minutes before", near)
                continue
            predecessors = [old for old in ended if not between(old, new, ordered)]
            if not predecessors:
                note(new, "another source of its class appeared in between", ended)
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
                and not chains.distinct(name, old)
                and precedes(spans[old], spans[name].first)
                and not between(old, name, ordered)
            ]
            if successors != [new]:
                note(new, "its predecessor has more than one successor candidate", successors)
                continue
            alongside = [
                name
                for name in ordered
                if name not in (old, new)
                and blocks(name, old, new)
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


def evidence(
    chains: Chains,
    old: str,
    new: str,
    shared_keys: int | None = None,
    decision_id: int | None = None,
) -> str:
    before, after = chains.spans[old], chains.spans[new]
    values: dict = {
        "chain_gap_seconds": CHAIN_GAP.total_seconds(),
        "old": before.as_dict(),
        "new": after.as_dict(),
        "gap_seconds": gap_seconds(before, after),
        "shared_keys": shared_keys,
    }
    if decision_id is None:
        values["rule"] = RULE
    else:
        values["owner_decision"] = decision_id
    return json.dumps(values, sort_keys=True)


def parse_evidence(value: str, *, owner: bool = False) -> tuple[Span, Span] | None:
    """The spans a link was decided on, or ``None`` when they are malformed or (a link of
    the reconciler) do not satisfy the gap rule."""
    try:
        data = json.loads(value)
        spans = tuple(
            Span(
                datetime.fromisoformat(data[name]["first"]),
                datetime.fromisoformat(data[name]["last"]),
                "",
                "",
                True,
            )
            for name in ("old", "new")
        )
    except (ValueError, KeyError, TypeError):
        return None
    old, new = spans
    if old.first > old.last or new.first > new.last:
        return None
    if not owner and not precedes(old, new.first):
        return None
    return old, new


def owner_decision_of(value: str) -> int | None:
    try:
        found = json.loads(value).get("owner_decision")
    except (ValueError, AttributeError):
        return None
    return found if isinstance(found, int) else None


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


@dataclass
class SourceState:
    """What a page needs to know about its sources (one statement)."""

    chains: Chains
    # Fingerprints of the page that already have a sighting.
    known: set[str] = field(default_factory=set)
    # A fingerprint first seen now covers the archive (a full run built the data type's
    # spans, or it has no rows yet).
    history_known: bool = False


async def load_state(
    session: AsyncSession, data_type: str, seen: Iterable[str] = ()
) -> SourceState:
    """Links, refutations, owner decisions and evidence of ``data_type``; for the
    fingerprints ``seen``, which have a sighting, and whether the history is known. One
    round trip (a ``UNION ALL``)."""
    chain, event, decision, pair = (
        RawSourceChain,
        RawSourceChainEvent,
        RawSourceDecision,
        RawSourcePair,
    )
    zero = literal(0, Integer)
    parts = [
        select(
            literal("c", String),
            chain.from_fingerprint,
            chain.to_fingerprint,
            chain.decided_by,
            zero,
        ).where(chain.data_type == data_type),
        select(
            literal("e", String), event.from_fingerprint, event.to_fingerprint, event.event, zero
        ).where(event.data_type == data_type),
        select(
            literal("d", String),
            decision.first_fingerprint,
            decision.second_fingerprint,
            decision.decision,
            decision.id,
        ).where(decision.data_type == data_type),
        select(
            literal("p", String),
            pair.first_fingerprint,
            pair.second_fingerprint,
            pair.identity_key,
            zero,
        ).where(pair.data_type == data_type),
    ]
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
            ).where(exists(complete) | ~exists(rows))
        )
    following: dict[str, str] = {}
    decided_by: dict[str, str] = {}
    refuted: set[str] = set()
    decisions: list[tuple[int, str, str, str]] = []
    evidence: dict[tuple[str, str], set[str]] = defaultdict(set)
    state = SourceState(Chains())
    for kind, first, second, value, number in (await session.execute(union_all(*parts))).tuples():
        if kind == "c":
            following[first] = second
            decided_by[first] = value
        elif kind == "e" and value == REFUTED:
            refuted.add(first)
        elif kind == "d":
            decisions.append((number, value, first, second))
        elif kind == "p":
            evidence[(first, second)].add(value)
        elif kind == "s":
            state.known.add(first)
        elif kind == "h":
            state.history_known = True
    chains = Chains(following, decided_by=decided_by, refuted_events=refuted)
    chains.evidence = dict(evidence)
    for _, value, first, second in sorted(decisions):
        chains.decide(value, first, second)
    state.chains = chains
    return state


async def load_chains(
    session: AsyncSession, data_type: str, fingerprints: Iterable[str] | None = None
) -> Chains:
    """Every link, refutation, decision and piece of evidence of ``data_type``, and the
    spans of the linked and the given fingerprints (all spans of the data type when
    ``fingerprints`` is ``None``)."""
    chains = (await load_state(session, data_type)).chains
    query = select(RawSourceSighting).where(RawSourceSighting.data_type == data_type)
    if fingerprints is not None:
        wanted = sorted({*fingerprints, *chains.following, *chains.preceding})
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
    session: AsyncSession,
    data_type: str,
    seen: Mapping[str, tuple[str, str, object]],
    at: datetime,
    *,
    history_known: bool,
) -> None:
    """Note that the fingerprints ``seen`` (fingerprint -> stable class, label, dataSource)
    were returned by Google at ``at`` (when the response was fetched). A fingerprint seen
    for the first time is complete when ``history_known``."""
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
            "complete": history_known,
            "updated_at": at,
        }
        for fingerprint, (stable, label, data_source) in sorted(seen.items())
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
    found: Iterable[tuple[str, str, str]],
    at: datetime,
) -> int:
    """Note keys returned under two fingerprints with two readings in one response
    (``(fingerprint, fingerprint, key)``); a pair stops collecting once established."""
    from app.services.storage import dialect_insert

    values = []
    for left, right, key in sorted(found):
        if left == right:
            continue
        first, second = ordered_pair(left, right)
        keys = chains.evidence.get((first, second), set())
        if len(keys) >= PAIR_KEYS or key in keys:
            continue
        chains.add_evidence(first, second, key)
        values.append(
            {
                "data_type": data_type,
                "first_fingerprint": first,
                "second_fingerprint": second,
                "identity_key": key,
                "seen_together_at": at,
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
    established distinct (once per link). Returns the new ones."""
    from app.services.storage import dialect_insert

    found = [
        (old, new)
        for old, new in sorted(chains.following.items())
        if old not in chains.refuted_events and chains.refuted(old, new)
    ]
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
                    "keys": sorted(chains.evidence.get(ordered_pair(old, new), ())),
                    "decision": chains.pair_decisions.get(ordered_pair(old, new)),
                },
                sort_keys=True,
            ),
            "occurred_at": now,
        }
        for old, new in found
    ]
    statement = dialect_insert(session, RawSourceChainEvent.__table__).on_conflict_do_nothing()
    await session.execute(statement, values)
    chains.refuted_events.update(old for old, _ in found)
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
    decision_id: int | None = None,
) -> bool:
    """Record ``old -> new`` (rule or decision checked by the caller, under the exclusive
    lock). ``False`` when it would fork a chain, or a link for either was recorded first."""
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
            evidence=evidence(chains, old, new, shared_keys, decision_id),
            decided_at=now,
        )
        .on_conflict_do_nothing()
        .returning(RawSourceChain.from_fingerprint)
    )
    inserted = (await session.execute(statement)).scalar() is not None
    if inserted:
        chains.add(old, new)
        chains.decided_by[old] = decided_by
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
    every new writer behind it, so the reconciler must not wait long). Once the exclusive
    lock is held, every row lock is waited for at most ``ROW_LOCK_TIMEOUT``."""
    if session.get_bind().dialect.name != "postgresql":
        return
    if wait is not None:
        await session.execute(text(f"SET LOCAL lock_timeout = '{wait}'"))
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await session.execute(
        text(f"SELECT {function}(:namespace, :key)"),
        {"namespace": DATA_TYPE_LOCK, "key": _key(data_type)},
    )
    if exclusive:
        await session.execute(text(f"SET LOCAL lock_timeout = '{ROW_LOCK_TIMEOUT}'"))
    elif wait is not None:
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
