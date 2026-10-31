"""Statistics and status summaries for identity archive backfills."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# How many entries of each list the status summary keeps.
SUMMARY_ITEMS = 20


@dataclass
class BackfillStats:
    mode: str = "full"
    rows: int = 0
    # Rows with a Google id: they keep it, nothing to do.
    named: int = 0
    # Rows without any parsable time (no v2 identity; they keep their v1 id).
    unidentified: int = 0
    # Records seen (match keys: the v2 key without the optional device fields).
    identities: int = 0
    aliases_added: int = 0
    aliases_present: int = 0
    already_superseded: int = 0
    # Records (match keys) held by more than one device (each keeps its own row).
    distinct_sources: int = 0
    # Deleted-marked rows that hold their device's record with no live row of that device
    # beside them: genuine deletions, left as they are ...
    deletions_kept: int = 0
    # ... and those where a fingerprint not established distinct holds the key live
    # (possibly a rename nobody decided yet: see ambiguous).
    deletions_suspicious: int = 0
    # Linked rows (inserted by this run) that carried a source-deletion mark (kept as history).
    superseded_marked: int = 0
    # Rows whose stored source_start_time differs from the parsed one (settled last).
    start_mismatches: int = 0
    # Rows of one device and record left unlinked (both count) because linking them would
    # hide a live row behind a deleted one, close a cycle of links or join two values of an
    # optional device field (never: a guard): listed in status.
    conflicts: int = 0
    # Rows of another fingerprint of the device that measured something else than the row
    # holding the record: kept separate (both count).
    kept_differing: int = 0
    # Links inserted by this run (a dry run: the links it would insert), per reason.
    linked: Counter = field(default_factory=Counter)
    # Per data type: the chains (this run's and earlier ones), the chains awaiting the
    # owner's review, source pairs that share a class but stay apart, fingerprints the rule
    # leaves open, links refuted, chains whose old form was seen again.
    chains: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    kept_apart: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    refuted: list[dict] = field(default_factory=list)
    returned: list[dict] = field(default_factory=list)
    # Pairs established distinct by evidence alone that look like a rename.
    review_distinct: list[dict] = field(default_factory=list)
    # Chains an owner decision made possible, left to the next reconciler run.
    left_to_reconciler: list[dict] = field(default_factory=list)
    # Deletions the last complete fetches left undecided, and the sources they wait for.
    deferred_deletions: list[dict] = field(default_factory=list)
    # Ordered pair of fingerprints with evidence -> [keys whose rows of both held the same
    # reading, keys whose rows held two] among the keys this run settled ...
    agreement: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    # ... and per (pair, key) whether they did (recorded in raw_source_agreements).
    agreement_keys: dict[tuple[tuple[str, str], str], bool] = field(default_factory=dict)
    # Why an incremental run did nothing, or did a full pass instead.
    skipped: str | None = None
    # The data type failed (the run went on with the others).
    error: str | None = None

    _LISTS = (
        "chains",
        "review",
        "kept_apart",
        "ambiguous",
        "refuted",
        "returned",
        "review_distinct",
        "deferred_deletions",
        "left_to_reconciler",
    )

    def add(self, other: BackfillStats) -> None:
        for name in self.__dataclass_fields__:
            if name == "linked":
                self.linked.update(other.linked)
            elif name == "agreement":
                for pair, (agree, disagree) in other.agreement.items():
                    counts = self.agreement.setdefault(pair, [0, 0])
                    counts[0] += agree
                    counts[1] += disagree
            elif name == "agreement_keys":
                # Per batch only (recorded with it): a full run's would be every key.
                continue
            elif name in self._LISTS:
                getattr(self, name).extend(getattr(other, name))
            elif name in ("mode", "skipped", "error"):
                setattr(self, name, getattr(self, name) or getattr(other, name))
            else:
                setattr(self, name, getattr(self, name) + getattr(other, name))

    def as_dict(self) -> dict[str, Any]:
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        values["linked"] = dict(sorted(self.linked.items()))
        values["superseded"] = sum(self.linked.values())
        values.pop("agreement_keys", None)
        values["agreement"] = [
            {"fingerprints": _fingerprints(*pair), "same_reading": agree, "two_readings": other}
            for pair, (agree, other) in sorted(self.agreement.items())
        ]
        return values

    def summary(self) -> dict[str, Any]:
        """What the status command shows of a run: the counts, and every case left open
        (ambiguous sources, pairs kept apart that are not established distinct, pairs
        established distinct that look like a rename, chains awaiting review, refuted chains,
        suspicious deletions, rows left double because they could not be linked)."""

        def short(entries: list[dict], *names: str) -> list[dict]:
            return [{name: entry.get(name) for name in names} for entry in entries][:SUMMARY_ITEMS]

        undecided = [entry for entry in self.kept_apart if not entry.get("established")]
        return {
            "mode": self.mode,
            "rows": self.rows,
            "superseded": sum(self.linked.values()),
            "aliases_added": self.aliases_added,
            "chains_decided": sum(1 for chain in self.chains if chain["decided"] == "this run"),
            "ambiguous": len(self.ambiguous),
            "refuted": len(self.refuted),
            "review_suggested": len(self.review),
            "kept_apart_undecided": len(undecided),
            "kept_differing": self.kept_differing,
            "deletions_kept": self.deletions_kept,
            "deletions_suspicious": self.deletions_suspicious,
            "conflicts": self.conflicts,
            "review_distinct": len(self.review_distinct),
            "deferred_deletions": sum(
                entry.get("deferred", 0) for entry in self.deferred_deletions
            ),
            "skipped": self.skipped,
            "error": self.error,
            "open": {
                "review": short(self.review, "from", "to", "fingerprints", "decided"),
                "ambiguous": short(self.ambiguous, "source", "fingerprint", "why", "candidates"),
                "kept_apart": short(undecided, "sources", "fingerprints", "why"),
                "review_distinct": short(self.review_distinct, "sources", "fingerprints", "why"),
                "refuted": short(self.refuted, "from", "to", "fingerprints"),
                "deferred_deletions": short(
                    self.deferred_deletions,
                    "rows_of",
                    "returned_by",
                    "fingerprints",
                    "deferred",
                    "at",
                    "decide",
                ),
            },
        }


def _fingerprints(*names: str) -> list[str]:
    return [name[:12] for name in names]
