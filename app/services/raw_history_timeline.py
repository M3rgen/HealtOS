"""Merge archived sightings into a record's existing version timeline."""

from __future__ import annotations

from typing import Any

from app.services.raw_history_types import _Candidate, _Old


def _merge(
    existing: list[_Old], current_hash: str, incoming: list[_Candidate]
) -> tuple[list[tuple[str, Any]], int]:
    """One record's history timeline with the dump's sightings slotted in by time.

    Returns the history entries (``("old", _Old)`` or ``("new", _Candidate)``, oldest
    first, the current row excluded) and how many sightings were already present. Equal
    neighbouring payloads are one occurrence: a sighting next to a stored version with the
    same payload adds nothing, and consecutive sightings of one payload become one version.
    """
    incoming = sorted(incoming, key=lambda item: item.last_seen_at)
    merged: list[tuple[str, Any]] = []
    position = 0
    for old in existing:
        # On a tie the archived sighting (older evidence) goes first.
        while position < len(incoming) and incoming[position].last_seen_at <= old.last_seen_at:
            merged.append(("new", incoming[position]))
            position += 1
        merged.append(("old", old))
    merged.extend(("new", item) for item in incoming[position:])
    merged.append(("current", current_hash))

    def digest(entry: tuple[str, Any]) -> str:
        kind, item = entry
        return item if kind == "current" else item.payload_hash

    timeline: list[tuple[str, Any]] = []
    present = 0
    start = 0
    while start < len(merged):
        end = start + 1
        while end < len(merged) and digest(merged[end]) == digest(merged[start]):
            end += 1
        run = merged[start:end]
        stored = [entry for entry in run if entry[0] != "new"]
        sightings = [item for kind, item in run if kind == "new"]
        if stored:
            present += len(sightings)
            timeline.extend(entry for entry in stored if entry[0] == "old")
        else:
            first = sightings[0]
            first.last_seen_at = max(item.last_seen_at for item in sightings)
            known = [item.superseded_at for item in sightings if item.superseded_at is not None]
            first.superseded_at = max(known) if known else None
            present += len(sightings) - 1
            timeline.append(("new", first))
        start = end
    return timeline, present
