"""Reconciler chain decisions from complete sighting spans and device evidence.

The chain rule is documented in :mod:`app.services.source_chains`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.services.source_chain_evidence import (
    PAIR_KEYS,
    RETIRED_AFTER,
    ordered_pair,
    overlaps,
    precedes,
)
from app.services.source_chain_model import Chains


@dataclass
class Decision:
    links: list[tuple[str, str]] = field(default_factory=list)
    # Per new link: the live links that kept another candidate out (``Chains.premises``).
    premises: dict[tuple[str, str], frozenset[tuple[str, str]]] = field(default_factory=dict)
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
    classes = kindred_groups(chains)

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
        # Two fingerprints that are not kindred are two devices.
        pair = ordered_pair(left, right)
        return (
            not chains.kindred(left, right)
            or chains.distinct(left, right)
            or (
                chains.pair_decisions.get(pair) != "not-distinct"
                and len({item.key for item in chains.evidence.get(pair, ())}) >= PAIR_KEYS
            )
        )

    def retired_before(name: str, other: str, new: str) -> bool:
        # Another device than ``other`` (established distinct) whose newest record is more than
        # ``RETIRED_AFTER`` older than ``other``'s and than ``new``'s first sighting: it stopped
        # recording long before, so it is neither ``other``'s rival as a predecessor of ``new``
        # nor seen alongside ``new`` (its later sightings are re-fetches of its old records).
        # A device unworn for less than that (a watch on its charger) may be what ``new`` is.
        mine, theirs = spans[name].newest, spans[other].newest
        return (
            mine is not None
            and theirs is not None
            and mine + RETIRED_AFTER < theirs
            and mine + RETIRED_AFTER < spans[new].first
            and chains.distinct(name, other)
        )

    def blocks(other: str, old: str, new: str) -> bool:
        # A source established distinct from one of them and apart from the other, one that
        # retired long before ``old`` did, or one that can be neither (another value of an
        # optional device field than both: another device) says nothing about old -> new.
        return not (
            not (chains.kindred(other, old) or chains.kindred(other, new))
            or (chains.distinct(other, old) and apart(other, new))
            or (chains.distinct(other, new) and apart(other, old))
            or retired_before(other, old, new)
        )

    def kin(name: str, old: str, new: str) -> bool:
        # A form of either device is no other source: it says nothing about old -> new.
        return name in chains.members(old) or name in chains.members(new)

    def between(old: str, new: str, members: list[str], *, links: bool = True) -> list[str]:
        # ``links=False``: as if no live link made a form of either device (a premise).
        return [
            name
            for name in members
            if name not in (old, new)
            and not (links and kin(name, old, new))
            and blocks(name, old, new)
            and spans[old].last < spans[name].first < spans[new].first
        ]

    def premise_of(old: str, new: str, members: list[str]) -> frozenset[tuple[str, str]]:
        # The live links without which another source of the class would have competed with
        # or blocked old -> new: a predecessor candidate that continues as another source, a
        # successor candidate that continues another one, a form of either device seen in
        # between or alongside.
        found: set[tuple[str, str]] = set()

        def device_links(name: str) -> set[tuple[str, str]]:
            device = chains.members(name)
            return {
                link
                for link in chains.links
                if link[0] in device and link[1] in device and chains.live(*link)
            }

        for name in members:
            if name in (old, new):
                continue
            if (
                name in chains.following
                and name not in chains.members(new)
                and chains.kindred(name, new)
                and chains.joinable(name, new)
                and not chains.distinct(name, new)
                and spans[name].last < spans[new].first
                and (
                    not precedes(spans[name], spans[new].first)
                    or not between(name, new, members, links=False)
                )
            ):
                found.add((name, chains.following[name]))
            if (
                name in chains.preceding
                and name not in chains.members(old)
                and chains.kindred(name, old)
                and chains.joinable(name, old)
                and not chains.distinct(name, old)
                and precedes(spans[old], spans[name].first)
                and not between(old, name, members, links=False)
            ):
                found.add((chains.preceding[name], name))
            if kin(name, old, new) and blocks(name, old, new):
                if spans[old].last < spans[name].first < spans[new].first or (
                    overlaps(spans[name], spans[old]) or overlaps(spans[name], spans[new])
                ):
                    found.update(device_links(name))
        found.discard((old, new))
        return frozenset(found)

    for members in classes:
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
            # Fingerprints that could be this one's earlier form: not of its device, and not
            # one that already continues as another source (a chain never forks).
            earlier = [
                name
                for name in ordered
                if name not in chains.members(new)
                and name not in chains.following
                and chains.kindred(name, new)
                and not chains.distinct(name, new)
                and spans[name].first < spans[new].first
            ]
            # A form of a device with another value of a device field than a form of this one's
            # can never be joined to it: listed, never linked.
            unjoinable = [name for name in earlier if not chains.joinable(name, new)]
            earlier = [name for name in earlier if name not in unjoinable]
            # A candidate that retired long before another one that ended before this one
            # appeared (two devices, the other kept recording for days after it) is not this
            # one's earlier form.
            ended_before = [name for name in earlier if spans[name].last < spans[new].first]
            earlier = [
                name
                for name in earlier
                if not any(
                    retired_before(name, other, new) for other in ended_before if other != name
                )
            ]
            if not earlier:
                if unjoinable:
                    note(
                        new,
                        "its earlier form belongs to a device with another value of a device "
                        "field (form factor or manufacturer): never linked",
                        unjoinable,
                    )
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
                if name not in chains.members(old)
                and name not in chains.preceding
                and chains.kindred(name, old)
                and chains.joinable(name, old)
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
                if not kin(name, old, new)
                and blocks(name, old, new)
                and (overlaps(spans[name], spans[old]) or overlaps(spans[name], spans[new]))
            ]
            if alongside:
                note(new, "another source of its class was seen alongside", alongside)
                continue
            if chains.reaches(new, old):
                note(new, "linking would close a cycle", [old])
                continue
            premise = premise_of(old, new, ordered)
            chains.add(old, new)
            if premise:
                chains.premises[(old, new)] = premise
                decision.premises[(old, new)] = premise
            decision.links.append((old, new))
    return decision


def kindred_groups(chains: Chains) -> list[list[str]]:
    """The fingerprints connected by kindred pairs, per source family (the rule runs per group:
    without optional device fields a group is exactly a stable class)."""
    families: dict[str, list[str]] = defaultdict(list)
    for fingerprint, span in chains.spans.items():
        families[span.family].append(fingerprint)
    groups: list[list[str]] = []
    for members in families.values():
        left = set(members)
        while left:
            start = min(left)
            left.discard(start)
            group, frontier = [start], [start]
            while frontier:
                current = frontier.pop()
                joined = sorted(name for name in left if chains.kindred(current, name))
                for name in joined:
                    left.discard(name)
                group.extend(joined)
                frontier.extend(joined)
            groups.append(sorted(group))
    return groups
