"""Source evidence collection and reports for identity archive backfills."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from app.core.timeutil import as_utc
from app.parsers.identity import RecordIdentity, pair_key, source_label
from app.services.identity_backfill_stats import BackfillStats, _fingerprints
from app.services.source_chains import (
    Chains,
    Evidence,
    Span,
    gap_seconds,
    key_time,
    ordered_pair,
)


@dataclass
class _Timeline:
    spans: dict[str, Span] = field(default_factory=dict)
    # Unordered pairs of kindred fingerprints -> records (match keys) they share.
    shared: Counter = field(default_factory=Counter)
    # (parsed start, raw id) of rows whose stored start is stale: settled last, by id.
    stale: list[tuple[datetime, uuid.UUID]] = field(default_factory=list)
    # Ordered fingerprint pairs -> keys one page stored or last saw under both with two
    # readings, with that page's time (evidence of two devices; bounded by ``useful``).
    evidence: dict[tuple[str, str], list[Evidence]] = field(default_factory=dict)

    def sight(self, identity: RecordIdentity, data_source, first, last) -> None:
        newest = key_time(identity.key)
        span = self.spans.get(identity.source)
        if span is None:
            self.spans[identity.source] = Span(
                as_utc(first),
                as_utc(last),
                identity.stable,
                source_label(data_source),
                True,
                data_source,
                newest,
            )
        else:
            span.widen(first, last, newest)


def _together(
    found: list[tuple[datetime, datetime, RecordIdentity, str | None]],
) -> set[tuple[tuple[str, str], str, datetime]]:
    """Pairs of kindred fingerprints of one record's rows that one page stored or last saw
    together (the same ``first_seen_at`` or ``last_seen_at``: one transaction's clock) with
    two readings, with the key they are compared under (``pair_key``) and that time."""
    pairs: set[tuple[tuple[str, str], str, datetime]] = set()
    for position, (first, last, identity, value) in enumerate(found):
        for other_first, other_last, other, other_value in found[position + 1 :]:
            if (
                other.source == identity.source
                or other_value == value
                or not identity.kindred(other)
            ):
                continue
            pair = ordered_pair(identity.source, other.source)
            if other_first == first:
                pairs.add((pair, pair_key(identity, other), first))
            elif other_last == last:
                pairs.add((pair, pair_key(identity, other), last))
    return pairs


def _full(chains: Chains, chain: dict) -> list[str]:
    """The fingerprints of a reported chain entry (``fingerprints`` holds prefixes)."""
    return [
        name for prefix in chain["fingerprints"] for name in chains.spans if name.startswith(prefix)
    ][:2]


def _report(
    chains: Chains,
    timeline: _Timeline | None,
    new_links: set,
    ambiguous: list,
    refuted: list,
    stats: BackfillStats,
) -> None:
    for old, new in sorted(chains.links):
        before, after = chains.spans.get(old), chains.spans.get(new)
        entry = {
            "from": before.label if before else old[:12],
            "to": after.label if after else new[:12],
            "fingerprints": _fingerprints(old, new),
            "gap_seconds": gap_seconds(before, after) if before and after else None,
            "decided": "this run" if (old, new) in new_links else "earlier",
            "decided_by": chains.decided_by.get((old, new)),
            "usable": chains.live(old, new),
            "state": chains.state(old, new),
            "review": chains.review(old, new),
        }
        stats.chains.append(entry)
        if entry["review"] == "suggested" and entry["usable"]:
            stats.review.append(entry)
        if chains.returned(old, new) and entry["usable"]:
            stats.returned.append(
                {
                    **{name: entry[name] for name in ("from", "to", "fingerprints")},
                    "effect": "old form seen again: still one device (never two values for "
                    "one record); its records get rows of their own, linked to the device's",
                }
            )
    for old, new in refuted:
        stats.refuted.append(
            {
                "from": chains.spans[old].label if old in chains.spans else old[:12],
                "to": chains.spans[new].label if new in chains.spans else new[:12],
                "fingerprints": _fingerprints(old, new),
                "effect": "two devices (different values for one record): no longer used; the "
                "rows it linked count on their own",
            }
        )
    stats.ambiguous.extend(ambiguous)
    classes: dict[str, list[str]] = defaultdict(list)
    for fingerprint, span in chains.spans.items():
        classes[span.family].append(fingerprint)
    for members in classes.values():
        ordered = sorted(members, key=lambda name: (chains.spans[name].first, name))
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if chains.same_device(left, right) or not chains.kindred(left, right):
                    continue
                shared = None
                if timeline is not None:
                    shared = timeline.shared.get(ordered_pair(left, right), 0)
                    if not shared and not chains.distinct(left, right):
                        continue
                before, after = chains.spans[left], chains.spans[right]
                established = chains.distinct(left, right)
                entry = {
                    "sources": [before.label, after.label],
                    "fingerprints": _fingerprints(left, right),
                    "shared_keys": shared,
                    "gap_seconds": gap_seconds(before, after),
                    "established": established,
                    "why": (
                        "two devices (different values for one record, or the owner's decision)"
                        if established
                        else "not decided as one device (see ambiguous; identity-decide)"
                    ),
                }
                stats.kept_apart.append(entry)
                if established and chains.looks_like_rename(left, right):
                    # Distinct by evidence alone, yet one followed the other: a rename the
                    # evidence may have got wrong (no deletion is marked on its strength).
                    stats.review_distinct.append(
                        {
                            **entry,
                            "why": "two devices by evidence alone, but one began as the other "
                            "ended (it looks like a rename): confirm with identity-decide "
                            "distinct, or decide not-distinct and link",
                        }
                    )
