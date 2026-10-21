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
distinct* when at least ``PAIR_KEYS`` (three) keys gave such evidence, in at least two fetches,
and a later fetch returned such a key for a record *newer* than the first fetch: both sources
still produce new records side by side. One response is never enough, however many keys it
holds (Google switching a name can return a few old records in both forms), nor are old records
returned again in both forms. The owner's ``distinct`` / ``not-distinct`` decision overrides the
evidence either way. Distinctness holds between *devices*: a source established distinct from
one form of a device is distinct from all its forms. Pairs established by evidence alone whose
sightings only touch (one began within ``RENAME_OVERLAP`` of the other's end: it looks like a
rename) are listed for the owner's review, and a deletion is never marked on their strength.

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
5. ``A`` and ``B`` are not established distinct, neither already has a live link the other way
   (a chain never forks or cycles), and no link between them was rejected or refuted.

A fingerprint established distinct from ``A`` or ``B`` and with ``PAIR_KEYS`` keys of evidence
against the other (from any responses: a second watch worn alongside, its evidence against the
new name perhaps from one re-fetch of old records) is left out of rules 3 and 4: it is known to
be another device, so it says nothing about ``A -> B`` (a chain only ever merges equal
readings; deletions are never marked on that weaker evidence). A fingerprint established
distinct from ``B`` is never ``B``'s candidate.

Everything else is *ambiguous*: nothing is linked, both keep their rows (visible; at worst
counted twice), and every run's report and ``health-mirror status`` list the case with why, for
the owner to decide (``identity-decide link`` or ``distinct``).

A link joins its two fingerprints into one *device* in both directions. It is kept for good
(``raw_source_chains`` is append-only, with the spans it was decided on as evidence). The old
form returning later does not undo it (a name can change back); the device's rows simply take
that form again. A link is *live* until it is *refuted* (its two fingerprints became
established distinct: a ``refuted`` event in ``raw_source_chain_events``, recorded by the
reconciler) or the two are decided distinct by the owner, or the owner's latest decision about
it is ``reject``; a later ``not-distinct`` or ``link`` makes it live again. Only live links make
devices and must form chains (``problems`` lists any fork or cycle); a dead link frees both its
fingerprints for another link. Whenever devices change, ``record_devices`` appends the new
device of each fingerprint to ``raw_source_devices``: readers count a supersession only while
its two fingerprints are one device there (app/models/identity.py, ``in_force``), so a wrong
link is undone in what they count as soon as it is rejected or refuted. Every link the
reconciler decides is listed for review until the owner confirms or rejects it.

Concurrency: ``store_records`` and deletion marking hold a shared transaction lock per data
type, a reconciler batch the exclusive one, and only one reconciler runs at a time (a session
lock); see :func:`lock_data_type` and :func:`backfill_lock`. The reconciler asks for the
exclusive lock without waiting in the lock queue (so writers never queue behind it) and, only
when that fails for ``LOCK_TIMEOUT``, waits in the queue for at most ``QUEUE_TIMEOUT``.
"""

from __future__ import annotations

import asyncio
import json
import re
import zlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import as_utc
from app.models.identity import (
    CHAIN_DECISIONS,
    PAIR_DECISIONS,
    REFUTED,
    RawSourceChain,
    RawSourceChainEvent,
    RawSourceDecision,
    RawSourceDevice,
    RawSourcePair,
    RawSourceSighting,
)
from app.models.state import RawHealthData

CHAIN_GAP = timedelta(minutes=5)
# Keys with two different readings that establish two distinct devices (from two fetches).
PAIR_KEYS = 3
# Two sources established distinct by evidence alone whose sightings overlap by at most this
# much look like a rename: listed for review, and never a reason to mark a deletion.
RENAME_OVERLAP = timedelta(days=1)
# A chain is followed at most this far.
MAX_CHAIN = 64
# pg_advisory locks: (namespace, key). Per data type: (DATA_TYPE_LOCK, crc32(name)).
DATA_TYPE_LOCK = 0x48454931  # "HEI1"
BACKFILL_LOCK = (0x48454932, 1)
# How long a reconciler batch tries for the exclusive lock without queueing (writers never
# wait behind it meanwhile), then how long it waits in the lock queue (every new writer of the
# data type waits behind it then), before it backs off and retries; once it holds the lock, how
# long it waits for any row lock (a long transaction of another writer).
LOCK_TIMEOUT = "100ms"
QUEUE_TIMEOUT = "1s"
ROW_LOCK_TIMEOUT = "2s"
RULE = (
    "B first seen more than CHAIN_GAP after A last seen; A the only predecessor candidate of "
    "B and B the only successor candidate of A; no other source of the class (unless "
    "established distinct from both) first seen in between or seen alongside either; A and B "
    "not established distinct; complete spans"
)


class BackfillRunning(RuntimeError):
    """Another ``identity-backfill`` holds the single-run lock."""


class LockBusy(RuntimeError):
    """The data type's exclusive lock could not be had in time (nothing was written)."""


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


def back_to_back(left: Span, right: Span) -> bool:
    """One began at most ``RENAME_OVERLAP`` before the other ended, and neither spans the
    other: the sightings look like one source followed by another."""
    earlier, later = sorted((left, right), key=lambda span: (span.first, span.last))
    return later.last >= earlier.last and earlier.last - later.first <= RENAME_OVERLAP


def ordered_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


_STAMP = re.compile(r"^v2:(\d{8}T\d{6}):")


def key_time(key: str) -> datetime | None:
    """The physical time a v2 key leads with (app/parsers/identity.py)."""
    found = _STAMP.match(key)
    if found is None:
        return None
    return datetime.strptime(found.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)


@dataclass(frozen=True)
class Evidence:
    """A key returned under two fingerprints with two readings, and when its fetch began."""

    key: str
    seen: datetime


def established(found: Iterable[Evidence]) -> bool:
    """``PAIR_KEYS`` keys, from two fetches at least, and a later fetch returned a key newer
    than the first fetch (both sources still produce new records)."""
    found = list(found)
    if len({item.key for item in found}) < PAIR_KEYS:
        return False
    first = min(item.seen for item in found)
    return any(item.seen > first and (key_time(item.key) or first) > first for item in found)


def useful(found: list[Evidence], item: Evidence) -> bool:
    """Whether recording ``item`` can still help establish the pair (bounded: a pair that
    cannot be established by a key gains no more than ``PAIR_KEYS`` keys)."""
    if established(found) or any(other.key == item.key for other in found):
        return False
    if len(found) < PAIR_KEYS:
        return True
    first = min(other.seen for other in found)
    newer = (key_time(item.key) or first) > first
    return (item.seen > first and newer) or item.seen < first


@dataclass
class Chains:
    """The recorded links of one data type (every state), the evidence and decisions about
    its sources, the devices logged for readers and (when loaded) the sighting spans."""

    # Every recorded link (from, to), live or not.
    links: set[tuple[str, str]] = field(default_factory=set)
    spans: dict[str, Span] = field(default_factory=dict)
    # (from, to) -> who decided the link (DECIDERS).
    decided_by: dict[tuple[str, str], str] = field(default_factory=dict)
    # Ordered fingerprint pair -> keys returned under both with two readings (raw_source_pairs).
    evidence: dict[tuple[str, str], list[Evidence]] = field(default_factory=dict)
    # The owner's latest decision: (from, to) -> link/reject/confirm; ordered pair ->
    # distinct/not-distinct.
    chain_decisions: dict[tuple[str, str], str] = field(default_factory=dict)
    pair_decisions: dict[tuple[str, str], str] = field(default_factory=dict)
    # Links (from, to) with a refuted event.
    refuted_events: set[tuple[str, str]] = field(default_factory=set)
    # Fingerprint -> its latest row in raw_source_devices (what readers use now).
    logged_devices: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._changed()

    def _changed(self) -> None:
        self._following: dict[str, str] | None = None
        self._preceding: dict[str, str] | None = None
        self._devices: dict[str, frozenset[str]] | None = None
        self._joined: dict[str, frozenset[str]] | None = None
        self._distinct: dict[tuple[str, str], bool] = {}

    # --- links and their state

    def pair_dead(self, old: str, new: str) -> bool:
        """The two forms are two devices: the owner said so, or the link was refuted (and the
        owner did not say otherwise since)."""
        decided = self.pair_decisions.get(ordered_pair(old, new))
        if decided is not None:
            return decided == "distinct"
        return (old, new) in self.refuted_events

    def rejected(self, old: str, new: str) -> bool:
        return self.chain_decisions.get((old, new)) == "reject"

    def live(self, old: str, new: str) -> bool:
        """Recorded, not rejected by the owner's latest decision, its forms not distinct."""
        return (
            (old, new) in self.links
            and not self.rejected(old, new)
            and not self.pair_dead(old, new)
        )

    usable = live

    def refuted(self, old: str, new: str) -> bool:
        """A recorded link whose two forms are two devices (refuted, or the owner's
        ``distinct``)."""
        return (old, new) in self.links and self.pair_dead(old, new)

    def state(self, old: str, new: str) -> str:
        if self.live(old, new):
            return "live"
        return "rejected" if self.rejected(old, new) else "refuted"

    def refutable(self, old: str, new: str) -> bool:
        """A recorded link whose forms became established distinct by evidence, with no refuted
        event yet and no owner decision about the pair."""
        pair = ordered_pair(old, new)
        return (
            (old, new) in self.links
            and (old, new) not in self.refuted_events
            and pair not in self.pair_decisions
            and established(self.evidence.get(pair, ()))
        )

    def _live_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        if self._following is None or self._preceding is None:
            following: dict[str, str] = {}
            preceding: dict[str, str] = {}
            for old, new in sorted(self.links):
                if self.live(old, new):
                    following.setdefault(old, new)
                    preceding.setdefault(new, old)
            self._following, self._preceding = following, preceding
        return self._following, self._preceding

    @property
    def following(self) -> dict[str, str]:
        """Live links: from -> to."""
        return self._live_maps()[0]

    @property
    def preceding(self) -> dict[str, str]:
        """Live links: to -> from."""
        return self._live_maps()[1]

    def problems(self) -> list[str]:
        """Live links that fork (two successors or predecessors of one fingerprint) or close
        a cycle; there must be none."""
        found: list[str] = []
        out: dict[str, list[str]] = defaultdict(list)
        into: dict[str, list[str]] = defaultdict(list)
        for old, new in sorted(self.links):
            if self.live(old, new):
                out[old].append(new)
                into[new].append(old)
        for name, targets in sorted(out.items()):
            if len(targets) > 1:
                found.append(f"{name[:12]} continues as {len(targets)} live sources")
        for name, sources in sorted(into.items()):
            if len(sources) > 1:
                found.append(f"{name[:12]} continues {len(sources)} live sources")
        for start in sorted(out):
            current, seen = start, set()
            while current in out and current not in seen:
                seen.add(current)
                current = out[current][0]
                if current == start:
                    found.append(f"live links from {start[:12]} close a cycle")
                    break
        return found

    def add(self, old: str, new: str) -> None:
        """A link planned in memory (live unless decided otherwise)."""
        self.links.add((old, new))
        self._changed()

    def forget(self, old: str, new: str) -> None:
        """Drop a link planned in memory that could not be recorded."""
        self.links.discard((old, new))
        self._changed()

    def decide(self, decision: str, first: str, second: str) -> None:
        """Apply an owner decision in memory (a dry run, or one just recorded)."""
        if decision in CHAIN_DECISIONS:
            self.chain_decisions[(first, second)] = decision
        else:
            self.pair_decisions[ordered_pair(first, second)] = decision
        self._changed()

    def add_evidence(self, left: str, right: str, key: str, seen: datetime) -> bool:
        found = self.evidence.setdefault(ordered_pair(left, right), [])
        item = Evidence(key, as_utc(seen))
        if not useful(found, item):
            return False
        found.append(item)
        self._changed()
        return True

    # --- distinctness

    def pair_distinct(self, left: str, right: str) -> bool:
        """These two fingerprints: the owner's decision, else established by evidence."""
        if left == right:
            return False
        pair = ordered_pair(left, right)
        decided = self.pair_decisions.get(pair)
        if decided is not None:
            return decided == "distinct"
        return established(self.evidence.get(pair, ()))

    def distinct(self, left: str, right: str) -> bool:
        """Established distinct *devices*: some form of one is distinct from some form of the
        other (the owner said so, or the evidence established it)."""
        if left == right:
            return False
        pair = ordered_pair(left, right)
        found = self._distinct.get(pair)
        if found is None:
            found = any(
                self.pair_distinct(one, other)
                for one in self.members(left)
                for other in self.members(right)
            )
            self._distinct[pair] = found
        return found

    def owner_distinct(self, left: str, right: str) -> bool:
        """The owner decided some form of one distinct from some form of the other."""
        return any(
            self.pair_decisions.get(ordered_pair(one, other)) == "distinct"
            for one in self.members(left)
            for other in self.members(right)
            if one != other
        )

    def looks_like_rename(self, left: str, right: str) -> bool:
        """Distinct by evidence alone, and their sightings only touch (``back_to_back``)."""
        before, after = self.spans.get(left), self.spans.get(right)
        return (
            before is not None
            and after is not None
            and not self.owner_distinct(left, right)
            and back_to_back(before, after)
        )

    # --- devices

    def _components(self, live_only: bool) -> dict[str, frozenset[str]]:
        neighbours: dict[str, set[str]] = defaultdict(set)
        for old, new in self.links:
            if not live_only or self.live(old, new):
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
        """The device's fingerprints (``fingerprint`` and every one joined to it by live
        links, either way)."""
        if self._devices is None:
            self._devices = self._components(live_only=True)
        return self._devices.get(fingerprint, frozenset((fingerprint,)))

    def device(self, fingerprint: str) -> str:
        """A stable name of the device: its smallest fingerprint."""
        return min(self.members(fingerprint))

    def same_device(self, left: str, right: str) -> bool:
        return left == right or right in self.members(left)

    def in_force(self, old_fingerprint: str, new_fingerprint: str) -> bool:
        """A supersession between rows of these fingerprints hides its old row (readers use
        ``raw_source_devices``, which ``record_devices`` keeps equal to this)."""
        return self.same_device(old_fingerprint, new_fingerprint)

    def ever_joined(self, left: str, right: str) -> bool:
        """``left`` and ``right`` are joined by recorded links (any state, either way): the
        rows of one were ever allowed to hold the other's form."""
        if self._joined is None:
            self._joined = self._components(live_only=False)
        return left == right or right in self._joined.get(left, ())

    def devices_to_log(self) -> dict[str, str]:
        """Fingerprint -> device, for every fingerprint of a recorded link whose logged device
        is not its device now (readers know no device of a fingerprint never logged: a
        supersession between it and another fingerprint is not in force)."""
        names = {name for link in self.links for name in link} | set(self.logged_devices)
        return {
            name: self.device(name)
            for name in sorted(names)
            if self.logged_devices.get(name) != self.device(name)
        }

    def returned(self, old: str, new: str) -> bool:
        """The old form was seen again after (or within the gap of) the new form's first
        sighting: the name changed back, or both forms are in use. Not a contradiction."""
        before, after = self.spans.get(old), self.spans.get(new)
        return before is not None and after is not None and not precedes(before, after.first)

    def reaches(self, start: str, target: str) -> bool:
        """``target`` follows ``start`` along live links."""
        current = start
        for _ in range(MAX_CHAIN):
            current = self.following.get(current, "")
            if not current:
                return False
            if current == target:
                return True
        return True  # longer than any chain may be: treat as a cycle

    def review(self, old: str, new: str) -> str:
        """``confirmed``/``rejected`` by the owner, ``owner`` (the owner linked it), or
        ``suggested`` (decided by the reconciler, not reviewed yet)."""
        decided = self.chain_decisions.get((old, new))
        if decided == "reject":
            return "rejected"
        if decided == "confirm":
            return "confirmed"
        if self.decided_by.get((old, new)) == "owner" or decided == "link":
            return "owner"
        return "suggested"

    def copy(self) -> Chains:
        return Chains(
            set(self.links),
            spans=self.spans,
            decided_by=dict(self.decided_by),
            evidence={pair: list(found) for pair, found in self.evidence.items()},
            chain_decisions=dict(self.chain_decisions),
            pair_decisions=dict(self.pair_decisions),
            refuted_events=set(self.refuted_events),
            logged_devices=dict(self.logged_devices),
        )


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

    def apart(left: str, right: str) -> bool:
        # Established distinct, or (only to stop blocking a chain, which merges equal readings
        # alone) ``PAIR_KEYS`` keys with two values in any responses, the owner not saying
        # otherwise: a second device whose evidence against one form comes from one re-fetch.
        pair = ordered_pair(left, right)
        return chains.distinct(left, right) or (
            chains.pair_decisions.get(pair) != "not-distinct"
            and len({item.key for item in chains.evidence.get(pair, ())}) >= PAIR_KEYS
        )

    def blocks(other: str, old: str, new: str) -> bool:
        # A source established distinct from one of them and apart from the other says
        # nothing about old -> new.
        return not (
            (chains.distinct(other, old) and apart(other, new))
            or (chains.distinct(other, new) and apart(other, old))
        )

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
            if (old, new) in chains.links or (new, old) in chains.links:
                note(new, "a link between them was rejected or refuted", [old])
                continue
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
    parts = [
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
                never,
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
        elif kind == "h":
            state.history_known = True
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


# --- locks -------------------------------------------------------------------------------


def _key(data_type: str) -> int:
    # Same value on every process and server version (hashtext is not guaranteed stable).
    return zlib.crc32(data_type.encode()) - 2**31


def seconds(value: str) -> float:
    """A PostgreSQL duration like ``10s``, ``500ms`` or ``2min`` in seconds."""
    found = re.fullmatch(r"([0-9]+)(ms|s|min)?", value)
    if found is None:
        raise ValueError(f"not a duration: {value!r}")
    number, unit = int(found.group(1)), found.group(2) or "ms"
    return number * {"ms": 0.001, "s": 1, "min": 60}[unit]


async def lock_data_type(
    session: AsyncSession, data_type: str, *, exclusive: bool, wait: str | None = None
) -> None:
    """Transaction lock on ``data_type``'s identities: shared for writers of records and
    deletion marks, exclusive for a reconciler batch (PostgreSQL only).

    ``wait`` (exclusive): try for the lock without queueing for this long - a request waiting
    in the queue would make every new writer of the data type wait behind it - then queue
    for at most ``QUEUE_TIMEOUT``; ``LockBusy`` when it is still held. Once the exclusive lock
    is held, every row lock is waited for at most ``ROW_LOCK_TIMEOUT``."""
    if session.get_bind().dialect.name != "postgresql":
        return
    arguments = {"namespace": DATA_TYPE_LOCK, "key": _key(data_type)}
    if not exclusive:
        await session.execute(
            text("SELECT pg_advisory_xact_lock_shared(:namespace, :key)"), arguments
        )
        return
    if wait is not None:
        loop = asyncio.get_running_loop()
        deadline, pause = loop.time() + seconds(wait), 0.01
        while True:
            if await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"), arguments
            ):
                break
            if loop.time() >= deadline:
                await _queue_for_lock(session, arguments)
                break
            await asyncio.sleep(pause)
            pause = min(pause * 2, 0.05)
    else:
        await session.execute(text("SELECT pg_advisory_xact_lock(:namespace, :key)"), arguments)
    await session.execute(text(f"SET LOCAL lock_timeout = '{ROW_LOCK_TIMEOUT}'"))


async def _queue_for_lock(session: AsyncSession, arguments: dict) -> None:
    from sqlalchemy.exc import DBAPIError

    await session.execute(text(f"SET LOCAL lock_timeout = '{QUEUE_TIMEOUT}'"))
    try:
        async with session.begin_nested():
            await session.execute(text("SELECT pg_advisory_xact_lock(:namespace, :key)"), arguments)
    except DBAPIError as exc:
        original = getattr(exc, "orig", None)
        code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if code != "55P03":
            raise
        raise LockBusy("the data type's exclusive lock is held by other writers") from exc


@asynccontextmanager
async def backfill_lock(sessions: async_sessionmaker, *, dry_run: bool = False):
    """Hold the single-run lock of identity-backfill (a session lock on its own connection,
    outside any transaction); refuse at once when another run holds it. A dry run writes
    nothing and takes no lock."""
    bind = sessions.kw.get("bind")
    if dry_run or bind is None or bind.dialect.name != "postgresql":
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
