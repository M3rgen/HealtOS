"""Source sighting spans, evidence thresholds and serialized chain evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import TYPE_CHECKING

from app.core.timeutil import as_utc
from app.parsers.identity import kindred, source_family

if TYPE_CHECKING:
    from app.services.source_chain_model import Chains


CHAIN_GAP = timedelta(minutes=5)
# Keys with two different readings that establish two distinct devices (from two fetches).
PAIR_KEYS = 3
# Two sources established distinct by evidence alone whose sightings overlap by at most this
# much look like a rename: listed for review, and never a reason to mark a deletion. Forms of
# two devices that were seen side by side for longer than this are worn together, not renamed.
RENAME_OVERLAP = timedelta(days=1)
# A device established distinct from another *retired before* it only when its newest record is
# this much older than the other's newest and than the candidate rename's first sighting: a
# watch left on its charger for hours or a few days while the band records on is not retired.
RETIRED_AFTER = timedelta(days=7)
# A chain is followed at most this far.
MAX_CHAIN = 64
RULE = (
    "B first seen more than CHAIN_GAP after A last seen; A the only predecessor candidate of "
    "B and B the only successor candidate of A; no other source of the class (unless "
    "established distinct from both, or retired before A) first seen in between or seen "
    "alongside either; A and B not established distinct; complete spans"
)


@dataclass
class Span:
    first: datetime
    last: datetime
    stable: str
    label: str
    complete: bool
    data_source: object = None
    # The physical time of the newest record returned under the fingerprint (a re-fetch of old
    # records widens ``last`` but not this).
    newest: datetime | None = None

    def widen(self, first: datetime, last: datetime, newest: datetime | None = None) -> None:
        self.first = min(self.first, as_utc(first))
        self.last = max(self.last, as_utc(last))
        if newest is not None:
            newest = as_utc(newest)
            self.newest = newest if self.newest is None else max(self.newest, newest)

    def as_dict(self) -> dict:
        return {"first": self.first.isoformat(), "last": self.last.isoformat()}

    @cached_property
    def family(self) -> str:
        """The stable class without the optional device fields (``source_family``)."""
        return source_family(self.data_source) if self.data_source is not None else self.stable

    def kindred(self, other: Span) -> bool:
        """The two sources may be forms of one device (app/parsers/identity.py ``kindred``):
        one stable class, or one ``dataSource`` but for an optional device field one lacks."""
        if self.stable == other.stable:
            return True
        if self.data_source is None or other.data_source is None or self.family != other.family:
            return False
        return kindred(self.data_source, other.data_source)


def precedes(old: Span, new_first: datetime) -> bool:
    """``old`` was last seen more than ``CHAIN_GAP`` before ``new_first``."""
    return old.last + CHAIN_GAP < new_first


def overlaps(left: Span, right: Span) -> bool:
    """Both were seen at one time."""
    return left.first <= right.last and right.first <= left.last


def gap_seconds(old: Span, new: Span) -> float:
    """Seconds from ``old``'s last to ``new``'s first sighting (negative: they overlap)."""
    return (new.first - old.last).total_seconds()


def worn_together(left: Span, right: Span) -> bool:
    """Both were seen side by side for longer than ``RENAME_OVERLAP``."""
    return min(left.last, right.last) - max(left.first, right.first) > RENAME_OVERLAP


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
    premise = chains.premises.get((old, new))
    if premise:
        values["premise"] = sorted(list(link) for link in premise)
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


def parse_premise(value: str | None) -> frozenset[tuple[str, str]]:
    try:
        found = json.loads(value or "{}").get("premise") or []
        return frozenset((str(first), str(second)) for first, second in found)
    except (ValueError, TypeError, AttributeError):
        return frozenset()


def parse_agreement(value: str | None) -> dict[tuple[str, str], tuple[int, int, bool]]:
    """``identity_reconcile_state.pair_agreement``:
    ``{"<first>|<second>": [agree, disagree, counted]}`` (``counted``: every shared key)."""
    try:
        found = json.loads(value or "{}")
        return {
            ordered_pair(*name.split("|", 1)): (
                int(counts[0]),
                int(counts[1]),
                bool(counts[2]) if len(counts) > 2 else False,
            )
            for name, counts in found.items()
        }
    except (ValueError, TypeError, IndexError, AttributeError):
        return {}


def dump_agreement(found: Mapping[tuple[str, str], tuple[int, int, bool]]) -> str:
    return json.dumps(
        {
            f"{first}|{second}": [counts[0], counts[1], bool(counts[2])]
            for (first, second), counts in sorted(found.items())
        },
        sort_keys=True,
    )
