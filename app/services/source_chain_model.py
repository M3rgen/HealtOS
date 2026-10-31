"""In-memory source chains, owner decisions and device membership."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from app.core.timeutil import as_utc
from app.models.identity import CHAIN_DECISIONS
from app.parsers.identity import compatible
from app.services.source_chain_evidence import (
    MAX_CHAIN,
    PAIR_KEYS,
    Evidence,
    Span,
    back_to_back,
    established,
    ordered_pair,
    precedes,
    useful,
    worn_together,
)


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
    # Ordered fingerprint pair -> (keys whose rows of both hold the same reading, keys whose
    # rows hold two readings, whether every key both hold was counted): the latest rows of
    # raw_source_agreements summed (identity_reconcile_state.pair_agreement).
    agreement: dict[tuple[str, str], tuple[int, int, bool]] = field(default_factory=dict)
    # (from, to) -> the live links a link of the reconciler relied on (``live``).
    premises: dict[tuple[str, str], frozenset[tuple[str, str]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._changed()

    def _changed(self) -> None:
        self._following: dict[str, str] | None = None
        self._preceding: dict[str, str] | None = None
        self._devices: dict[str, frozenset[str]] | None = None
        self._joined: dict[str, frozenset[str]] | None = None
        self._distinct: dict[tuple[str, str], bool] = {}
        self._premise_live: dict[tuple[str, str], bool] = {}

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
        """Recorded, not rejected by the owner's latest decision, its forms not distinct, and
        - a link the reconciler decided only because other links were live (its *premise*:
        a form that would have been another candidate already continued as another source)
        - while those are live, unless the owner linked or confirmed it."""
        return self._live_link(old, new, frozenset())

    def _live_link(self, old: str, new: str, visiting: frozenset) -> bool:
        if (old, new) not in self.links or self.rejected(old, new) or self.pair_dead(old, new):
            return False
        return self.premise_holds(old, new, visiting)

    def premise_holds(self, old: str, new: str, visiting: frozenset = frozenset()) -> bool:
        premise = self.premises.get((old, new))
        if not premise or self.chain_decisions.get((old, new)) in ("link", "confirm"):
            return True
        if (old, new) in visiting:
            return False
        found = self._premise_live.get((old, new))
        if found is None:
            found = all(
                self._live_link(first, second, visiting | {(old, new)}) for first, second in premise
            )
            self._premise_live[(old, new)] = found
        return found

    usable = live

    def refuted(self, old: str, new: str) -> bool:
        """A recorded link whose two forms are two devices (refuted, or the owner's
        ``distinct``)."""
        return (old, new) in self.links and self.pair_dead(old, new)

    def state(self, old: str, new: str) -> str:
        if self.live(old, new):
            return "live"
        if self.rejected(old, new):
            return "rejected"
        return "refuted" if self.pair_dead(old, new) else "lapsed"

    def refutable(self, old: str, new: str) -> bool:
        """A recorded link whose forms became established distinct by evidence, with no refuted
        event yet and no owner decision about the pair - judged on every key both hold (never
        on part of them: a night's sample can be all disagreement while the archive agrees)."""
        pair = ordered_pair(old, new)
        return (
            (old, new) in self.links
            and (old, new) not in self.refuted_events
            and pair not in self.pair_decisions
            and self.evidence_distinct(old, new)
            and self.agreement_counted(old, new)
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

    def agreeing(self, left: str, right: str) -> bool:
        """Most keys the two fingerprints share hold the same reading in both (at least
        ``PAIR_KEYS`` of them): one device returned again under another name (a rename with
        reprocessing), whatever a few keys with two values say."""
        agree, disagree, _ = self.agreement.get(ordered_pair(left, right), (0, 0, False))
        return agree >= PAIR_KEYS and agree >= disagree

    def agreement_counted(self, left: str, right: str) -> bool:
        """Every key the two fingerprints share was counted (``agreeing`` is not a sample)."""
        return self.agreement.get(ordered_pair(left, right), (0, 0, False))[2]

    def evidence_distinct(self, left: str, right: str) -> bool:
        """Established by evidence (``established``), unless the keys they share mostly agree."""
        return established(self.evidence.get(ordered_pair(left, right), ())) and not (
            self.agreeing(left, right)
        )

    def pair_distinct(self, left: str, right: str) -> bool:
        """These two fingerprints: the owner's decision, else established by evidence."""
        if left == right:
            return False
        pair = ordered_pair(left, right)
        decided = self.pair_decisions.get(pair)
        if decided is not None:
            return decided == "distinct"
        return self.evidence_distinct(left, right)

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
        """Distinct by evidence alone, and either the keys some forms of the two share mostly
        agree (``agreeing``: a rename with reprocessing, however long both were seen), or
        their sightings only touch (``back_to_back``) while no form of one was seen alongside a
        form of the other for longer than ``RENAME_OVERLAP`` (a device worn together with
        another is no rename). Listed for the owner's review."""
        if self.owner_distinct(left, right):
            return False
        forms = [(one, other) for one in self.members(left) for other in self.members(right)]
        if any(self.agreeing(one, other) for one, other in forms):
            return True
        before, after = self.spans.get(left), self.spans.get(right)
        if before is None or after is None or not back_to_back(before, after):
            return False
        return not any(
            worn_together(self.spans[one], self.spans[other])
            for one, other in forms
            if one in self.spans and other in self.spans
        )

    def reviewed_members(self, fingerprint: str) -> frozenset[str]:
        """The fingerprints joined to this one by live links the owner linked or confirmed
        (not by a chain only the reconciler suggested, which may be wrong)."""
        found, frontier = {fingerprint}, [fingerprint]
        while frontier:
            current = frontier.pop()
            for old, new in self.links:
                if current not in (old, new) or not self.live(old, new):
                    continue
                if self.review(old, new) not in ("owner", "confirmed"):
                    continue
                other = new if current == old else old
                if other not in found:
                    found.add(other)
                    frontier.append(other)
        return frozenset(found)

    def owner_decided_distinct(self, left: str, right: str) -> bool:
        """The owner decided distinct a form of each, joined to it by links the owner
        reviewed (``reviewed_members``), and not-distinct not these two themselves."""
        if self.pair_decisions.get(ordered_pair(left, right)) == "not-distinct":
            return False
        return any(
            self.pair_decisions.get(ordered_pair(one, other)) == "distinct"
            for one in self.reviewed_members(left)
            for other in self.reviewed_members(right)
            if one != other
        )

    def certainly_distinct(self, left: str, right: str) -> bool:
        """For a deletion mark, which is permanent: these two fingerprints are established
        distinct by evidence between themselves (not through another form of their devices,
        whose chain may be wrong) while the keys they share - every one of them counted - do
        not mostly agree and their sightings do not merely touch, with no owner decision about
        them (``owner_decided_distinct`` is asked separately)."""
        if left == right:
            return False
        if ordered_pair(left, right) in self.pair_decisions:
            return False
        before, after = self.spans.get(left), self.spans.get(right)
        return (
            self.evidence_distinct(left, right)
            and self.agreement_counted(left, right)
            and before is not None
            and after is not None
            and not back_to_back(before, after)
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

    def kindred(self, left: str, right: str) -> bool:
        """The two fingerprints may be forms of one device (``Span.kindred``); unknown spans
        are taken as kindred."""
        before, after = self.spans.get(left), self.spans.get(right)
        return before is None or after is None or before.kindred(after)

    def compatible(self, left: str, right: str) -> bool:
        """No optional device field with two values in the two fingerprints (unknown spans are
        taken as compatible)."""
        before, after = self.spans.get(left), self.spans.get(right)
        if before is None or after is None:
            return True
        return compatible(before.data_source, after.data_source)

    def joinable(self, left: str, right: str) -> bool:
        """Every fingerprint of one device is compatible with every one of the other's: only
        then may a link make them one device (kindred is not transitive: a phone and a watch
        are never one device through a form without ``formFactor``)."""
        return all(
            self.compatible(one, other)
            for one in self.members(left)
            for other in self.members(right)
        )

    def conflicting_devices(self) -> list[tuple[str, str]]:
        """Pairs of fingerprints of one device (as the live links make it) with two values of
        an optional device field; there must be none."""
        found = []
        for members in {frozenset(self.members(name)) for name in self.spans}:
            ordered = sorted(members)
            for index, one in enumerate(ordered):
                found.extend(
                    (one, other)
                    for other in ordered[index + 1 :]
                    if not self.compatible(one, other)
                )
        return found

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
            agreement=dict(self.agreement),
            premises=dict(self.premises),
        )


@dataclass
class SourceState:
    """What a page needs to know about its sources (one statement)."""

    chains: Chains
    # Fingerprints of the page that already have a sighting, and when they were first seen.
    known: set[str] = field(default_factory=set)
    first_seen: dict[str, datetime] = field(default_factory=dict)
    # A fingerprint first seen now covers the archive (a full run built the data type's
    # spans, or it has no rows yet).
    history_known: bool = False
