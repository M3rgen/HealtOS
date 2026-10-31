"""Compatibility and output contracts for the extracted backfill statistics."""

from collections import Counter

from app.services import identity_backfill as legacy
from app.services.identity_backfill_stats import SUMMARY_ITEMS, BackfillStats, _fingerprints


def test_legacy_exports_are_the_extracted_implementations() -> None:
    assert legacy.BackfillStats is BackfillStats
    assert BackfillStats.__module__ == "app.services.identity_backfill_stats"
    assert legacy.SUMMARY_ITEMS == SUMMARY_ITEMS == 20
    assert legacy._fingerprints is _fingerprints
    assert _fingerprints("a" * 16, "short", "b" * 16) == ["a" * 12, "short", "b" * 12]


def test_add_preserves_counters_lists_metadata_and_sorted_serialization() -> None:
    counters = (
        "rows",
        "named",
        "unidentified",
        "identities",
        "aliases_added",
        "aliases_present",
        "already_superseded",
        "distinct_sources",
        "deletions_kept",
        "deletions_suspicious",
        "superseded_marked",
        "start_mismatches",
        "conflicts",
        "kept_differing",
    )
    lists = (
        "chains",
        "review",
        "kept_apart",
        "ambiguous",
        "refuted",
        "returned",
        "review_distinct",
        "left_to_reconciler",
        "deferred_deletions",
    )
    a, b, c = "a" * 16, "b" * 16, "c" * 16
    total = BackfillStats(
        **dict.fromkeys(counters, 1),
        **{name: [{"batch": 1}] for name in lists},
        mode="",
        skipped="first reason",
        linked=Counter({"z": 2, "a": 1}),
        agreement={(b, c): [1, 2]},
        agreement_keys={((b, c), "first-key"): True},
    )
    batch = BackfillStats(
        **dict.fromkeys(counters, 2),
        **{name: [{"batch": 2}] for name in lists},
        mode="incremental",
        skipped="later reason",
        error="batch failed",
        linked=Counter({"a": 3}),
        agreement={(b, c): [3, 4], (a, b): [5, 6]},
        agreement_keys={((a, b), "batch-key"): False},
    )

    total.add(batch)
    total.add(BackfillStats(mode="full", error="later error"))

    result = total.as_dict()
    assert result == {
        **dict.fromkeys(counters, 3),
        **{name: [{"batch": 1}, {"batch": 2}] for name in lists},
        "mode": "incremental",
        "skipped": "first reason",
        "error": "batch failed",
        "linked": {"a": 4, "z": 2},
        "superseded": 6,
        "agreement": [
            {"fingerprints": [a[:12], b[:12]], "same_reading": 5, "two_readings": 6},
            {"fingerprints": [b[:12], c[:12]], "same_reading": 4, "two_readings": 6},
        ],
    }
    assert list(result["linked"]) == ["a", "z"]
    assert total.agreement_keys == {((b, c), "first-key"): True}
    assert batch.agreement == {(b, c): [3, 4], (a, b): [5, 6]}


def test_summary_preserves_open_cases_order_and_limit_without_truncating_counts() -> None:
    entries = [
        {
            "from": f"old-{i}",
            "to": f"new-{i}",
            "fingerprints": [f"pair-{i}"],
            "decided": "this run" if i % 2 else "earlier",
            "source": f"source-{i}",
            "fingerprint": f"fingerprint-{i}",
            "why": f"reason-{i}",
            "candidates": [f"candidate-{i}"],
            "sources": [f"source-{i}"],
            "rows_of": f"rows-{i}",
            "returned_by": f"returned-{i}",
            "deferred": i,
            "at": f"at-{i}",
        }
        for i in range(22)
    ]
    stats = BackfillStats(
        mode="decision",
        rows=90,
        aliases_added=4,
        linked=Counter({"rename": 3}),
        kept_differing=5,
        deletions_kept=6,
        deletions_suspicious=7,
        conflicts=8,
        chains=entries.copy(),
        review=entries.copy(),
        ambiguous=entries.copy(),
        review_distinct=entries.copy(),
        refuted=entries.copy(),
        deferred_deletions=entries.copy(),
        kept_apart=[item for entry in entries for item in ({"established": True}, entry)],
        skipped="reason",
        error="failure",
    )
    open_fields = {
        "review": ("from", "to", "fingerprints", "decided"),
        "ambiguous": ("source", "fingerprint", "why", "candidates"),
        "kept_apart": ("sources", "fingerprints", "why"),
        "review_distinct": ("sources", "fingerprints", "why"),
        "refuted": ("from", "to", "fingerprints"),
        "deferred_deletions": (
            "rows_of",
            "returned_by",
            "fingerprints",
            "deferred",
            "at",
            "decide",
        ),
    }

    assert stats.summary() == {
        "mode": "decision",
        "rows": 90,
        "superseded": 3,
        "aliases_added": 4,
        "chains_decided": 11,
        "ambiguous": 22,
        "refuted": 22,
        "review_suggested": 22,
        "kept_apart_undecided": 22,
        "kept_differing": 5,
        "deletions_kept": 6,
        "deletions_suspicious": 7,
        "conflicts": 8,
        "review_distinct": 22,
        "deferred_deletions": 231,
        "skipped": "reason",
        "error": "failure",
        "open": {
            name: [{field: entry.get(field) for field in fields} for entry in entries[:20]]
            for name, fields in open_fields.items()
        },
    }
    assert len(stats.as_dict()["review"]) == 22
    assert len(stats.as_dict()["kept_apart"]) == 44
