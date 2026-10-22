"""The identity reconciler (``health-mirror identity-backfill``): chains, aliases and links.

The write path (app/services/identity.py) never decides that two source fingerprints are one
device; it stores an unknown fingerprint's records on rows of their own. This module (and the
owner's ``identity-decide``, app/services/identity_decisions.py) decides, and links what was
decided. Per data type:

1. **Chains.** A *full* run first reads every row without a Google id and every earlier
   version (``raw_health_data`` is only read): each gives a sighting of its source
   fingerprint (received .. last seen), and two rows of one key that one page stored or last
   saw together, with two different readings, are evidence of two devices
   (``raw_source_pairs``). The spans are merged into ``raw_source_sightings`` (complete from
   now on). An *incremental* run (nightly) uses the sightings and evidence as the write path
   keeps them. Either way links whose two fingerprints became established distinct get a
   ``refuted`` event, then the chains are decided per stable class by the rule of
   app/services/source_chains.py and appended to ``raw_source_chains``, and the devices that
   changed are logged (``raw_source_devices``, what readers count by); the cases the rule
   leaves open are listed in the report (``ambiguous``), every chain not yet reviewed by the
   owner under ``review``, pairs that look like a rename although evidence made them two
   devices under ``review_distinct``.
2. **Links.** Per record (match key: the v2 key without the optional device fields, so a form with
   ``formFactor`` added is compared with the form without), the rows are grouped by device (one
   fingerprint, or several joined by live chain links). Rows superseded by a link in force are
   settled; a row whose links are no longer in force (its chain was rejected or refuted) is
   settled again like any other. Within one device the row that holds the record (a live row
   first, one that already holds others first, then the newest) keeps it, and every other row of
   that device is linked to it in ``raw_health_data_supersessions`` with the reason and both
   fingerprints, plus an ``identity-merged`` event (once per row) - a row of *another
   fingerprint* of the device only when it measured the same (``reading``): a value of its own
   is never hidden (``kept_differing``). The rows a linked row held are linked to the holder
   too, so two devices joined later end with one holder. Rows of different devices are never
   linked, nor is a live row linked to a deleted one or a link made that would close a cycle
   (``conflicts``: both rows count, listed in status). Every row whose own id does not name its
   key and fingerprint gets an alias (``synthetic:v2s:`` rows need none). A deleted-marked row
   with no live row of its device stays as it is: a genuine deletion (``deletions_kept``), or a
   *suspicious* one when another compatible fingerprint not established distinct holds the
   record live (``deletions_suspicious``: possibly a rename nobody decided, listed for review).

A full run reads all rows in keyset order of ``(source_start_time, id)`` (index
``ix_raw_health_data_type_start``): every row of a v2 key has the same start, and a batch
always ends after the last row of its start time, so a key is never split. Rows whose stored
``source_start_time`` differs from the parsed start (an older parser) are noted by id during
the first pass and settled last with the rows of their parsed start - also when ``--after``
skips the rest of the second pass. An incremental run reads only the start times of rows
created or given a new payload (``updated_at`` and ``payload_received_at``) since its checkpoint
minus ``OVERLAP``, and, when it decided a new link, since the new fingerprint's first sighting;
they are streamed, never held in memory at once. A data type that never had a full run gets
its first full pass from the incremental run, and a data type that fails is reported while
the others still run.

Each batch of the second pass is one transaction holding the data type's exclusive lock
(writers of that data type wait for it, then continue): it tries for it without queueing for
``LOCK_TIMEOUT`` and then waits in the queue at most ``QUEUE_TIMEOUT``, and once it holds it
waits at most ``ROW_LOCK_TIMEOUT`` for any row lock of another transaction; then it backs off
and retries, so it never queues every writer behind a long sync or import. A dry run takes
no lock at all and writes nothing. Inserts are ``ON CONFLICT DO NOTHING`` in chunks (far below
PostgreSQL's 32,767 parameters), so a run is idempotent and can be stopped and run again at
any time. Only one run at a time: a second one refuses at once.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import uuid
from array import array
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, select, text, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.logging import event
from app.core.timeutil import as_utc
from app.google_health.data_types import METRICS_SCOPE, REGISTRY, DataTypeSpec, RecordKind
from app.models.identity import (
    IDENTITY_TABLE,
    MERGED_EVENT,
    IdentityReconcileState,
    RawHealthDataIdentity,
    RawHealthDataSupersession,
    RawSourceAgreement,
)
from app.models.raw_versions import RawHealthDataEvent, RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.common import google_name
from app.parsers.identity import (
    RecordIdentity,
    compatible,
    instant,
    new_record_id,
    pair_key,
    reading,
    record_identity,
    source_label,
    supersession_reason,
)
from app.services import source_chains
from app.services.identity import aliases, among
from app.services.source_chains import (
    LOCK_TIMEOUT,
    PAIR_KEYS,
    Chains,
    Evidence,
    LockBusy,
    Span,
    backfill_lock,
    decide_chains,
    describe,
    dump_agreement,
    gap_seconds,
    insert_chain,
    key_time,
    load_chains,
    lock_data_type,
    ordered_pair,
    parse_agreement,
    record_devices,
    record_evidence,
    record_refutations,
    useful,
    write_spans,
)
from app.services.storage import dialect_insert

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "120s"
# An incremental run re-reads this much before its checkpoint (clock skew between the app
# processes and the database, transactions that committed after the checkpoint was taken).
OVERLAP = timedelta(hours=1)
# A batch that cannot get the exclusive lock (or a row lock) within its timeout retries this
# often, waiting 2, 4, 8 ... (at most 60) seconds in between.
LOCK_ATTEMPTS = 8
# Rows of one INSERT (aliases: 4 parameters each, links: 6): far below 32,767 parameters.
INSERT_CHUNK = 1000
_LOCK_NOT_AVAILABLE = "55P03"
# How many entries of each list the status summary keeps.
SUMMARY_ITEMS = 20

Notice = Callable[[str], None]


class LockUnavailable(RuntimeError):
    """A batch could not get its data type's exclusive lock."""


@dataclass
class BackfillStats:
    mode: str = "full"
    rows: int = 0
    # Rows with a Google id: they keep it, nothing to do.
    named: int = 0
    # Rows without any parsable time (no v2 identity; they keep their v1 id).
    unidentified: int = 0
    # v2 keys seen.
    identities: int = 0
    aliases_added: int = 0
    aliases_present: int = 0
    already_superseded: int = 0
    # Keys held by more than one device (each keeps its own row).
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
    # hide a live row behind a deleted one or close a cycle of links: listed in status.
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


@dataclass
class _Row:
    raw_id: uuid.UUID
    google_data_point_id: str
    raw_json: Any
    first_seen_at: datetime
    last_seen_at: datetime
    deleted: bool
    identity: RecordIdentity
    payload_hash: str = ""
    quarantined: bool = False

    @property
    def order(self) -> tuple:
        return (not self.deleted, self.last_seen_at, self.first_seen_at, str(self.raw_id))


def spec_for(data_type: str) -> DataTypeSpec:
    return REGISTRY.get(data_type) or DataTypeSpec(data_type, RecordKind.interval, METRICS_SCOPE)


_COLUMNS = (
    RawHealthData.id,
    RawHealthData.google_data_point_id,
    RawHealthData.raw_json,
    RawHealthData.source_start_time,
    RawHealthData.first_seen_at,
    RawHealthData.last_seen_at,
    RawHealthData.source_deleted_at,
    RawHealthData.payload_received_at,
    RawHealthData.payload_hash,
    RawHealthData.is_quarantined,
)


async def _read_batch(
    session: AsyncSession,
    data_type: str,
    batch_size: int,
    cursor: tuple[datetime, uuid.UUID] | None,
    after: datetime | None,
) -> list:
    query = select(*_COLUMNS).where(
        RawHealthData.data_type == data_type, RawHealthData.source_start_time.is_not(None)
    )
    if cursor is not None:
        query = query.where(tuple_(RawHealthData.source_start_time, RawHealthData.id) > cursor)
    elif after is not None:
        query = query.where(RawHealthData.source_start_time >= after)
    order = (RawHealthData.source_start_time, RawHealthData.id)
    rows = list((await session.execute(query.order_by(*order).limit(batch_size))).tuples())
    if len(rows) < batch_size:
        return rows
    # Finish the last start time: an identity's rows all share it.
    last_start, last_id = rows[-1][3], rows[-1][0]
    rest = await session.execute(
        select(*_COLUMNS)
        .where(
            RawHealthData.data_type == data_type,
            RawHealthData.source_start_time == last_start,
            RawHealthData.id > last_id,
        )
        .order_by(RawHealthData.id)
    )
    rows.extend(rest.tuples())
    return rows


async def _prepare(session: AsyncSession, statement_timeout: str) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout}'"))
        # The alias table grows from empty during the run. A plan made while its
        # statistics still say "empty" filters every row of the data type against every
        # key (minutes per batch at millions of rows): plan each lookup with the current
        # statistics, which ANALYZE keeps up to date.
        await session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _sqlstate(exc: DBAPIError) -> str | None:
    original = getattr(exc, "orig", None)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


async def _exclusively[T](
    sessions: async_sessionmaker,
    data_type: str,
    work: Callable[[AsyncSession], Awaitable[T]],
    statement_timeout: str,
    notice: Notice | None = None,
    *,
    dry_run: bool = False,
) -> T:
    """Run ``work`` in one transaction that holds ``data_type``'s exclusive lock.

    The lock is tried for without queueing for at most ``LOCK_TIMEOUT`` (writers never wait
    behind the request), then waited for in the queue at most ``QUEUE_TIMEOUT``; once held, any
    row lock is waited for at most ``ROW_LOCK_TIMEOUT``; then the transaction is rolled back
    and retried after a pause (``notice`` is told). A dry run writes nothing and takes no
    lock: it reads what is committed."""
    if dry_run:
        async with sessions.begin() as session:
            await _prepare(session, statement_timeout)
            return await work(session)
    for attempt in range(LOCK_ATTEMPTS):
        try:
            async with sessions.begin() as session:
                await lock_data_type(session, data_type, exclusive=True, wait=LOCK_TIMEOUT)
                await _prepare(session, statement_timeout)
                return await work(session)
        except (DBAPIError, LockBusy) as exc:
            if isinstance(exc, DBAPIError) and _sqlstate(exc) != _LOCK_NOT_AVAILABLE:
                raise
            delay = min(60, 2 ** (attempt + 1))
            event(
                logger,
                "IDENTITY_RECONCILE_LOCK_BUSY",
                data_type=data_type,
                attempt=attempt + 1,
                retry_in_seconds=delay,
            )
            if notice is not None:
                notice(
                    f"{data_type}: another transaction holds the identity lock or a row this "
                    f"batch needs; nothing was written, retrying in {delay} s "
                    f"(attempt {attempt + 1} of {LOCK_ATTEMPTS})"
                )
            await _pause(delay)
    raise LockUnavailable(
        f"{data_type}: could not get the identity lock (or a row it needs) in "
        f"{LOCK_ATTEMPTS} attempts (a long sync, deletion check or import of this data type "
        "held it); nothing of this batch was written. Run again later: an incremental run (or "
        "the next night's) settles everything since its checkpoint again and any re-settlement "
        "a run or decision still owes (identity_reconcile_state.pending_settle); a full run "
        "starts its passes over (--after skips the second pass up to a start time)"
    )


# --- pass 1: sighting timelines -----------------------------------------------------------


@dataclass
class _Timeline:
    spans: dict[str, Span] = field(default_factory=dict)
    # Unordered pairs of compatible fingerprints -> records (match keys) they share.
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
    """Pairs of compatible fingerprints of one record's rows that one page stored or last saw
    together (the same ``first_seen_at`` or ``last_seen_at``: one transaction's clock) with
    two readings, with the key they are compared under (``pair_key``) and that time."""
    pairs: set[tuple[tuple[str, str], str, datetime]] = set()
    for position, (first, last, identity, value) in enumerate(found):
        for other_first, other_last, other, other_value in found[position + 1 :]:
            if (
                other.source == identity.source
                or other_value == value
                or not identity.compatible(other)
            ):
                continue
            pair = ordered_pair(identity.source, other.source)
            if other_first == first:
                pairs.add((pair, pair_key(identity, other), first))
            elif other_last == last:
                pairs.add((pair, pair_key(identity, other), last))
    return pairs


async def _timeline(
    sessions: async_sessionmaker, spec: DataTypeSpec, batch_size: int, statement_timeout: str
) -> _Timeline:
    timeline = _Timeline()
    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        async with sessions.begin() as session:
            await _prepare(session, statement_timeout)
            batch = await _read_batch(session, spec.name, batch_size, cursor, None)
            if not batch:
                break
            row_keys: dict[uuid.UUID, tuple[str, datetime]] = {}
            # Match key -> fingerprint -> its optional device fields.
            sources: dict[str, dict[str, tuple]] = defaultdict(dict)
            stored: dict[str, list] = defaultdict(list)
            for raw_id, _, raw_json, start, first, last, _, received, _, _ in batch:
                identity = record_identity(spec, raw_json)
                if google_name(raw_json) is not None or identity is None:
                    continue
                if identity.start is not None and instant(identity.start) != instant(start):
                    timeline.stale.append((as_utc(identity.start), raw_id))
                data_source = raw_json.get("dataSource")
                # The current payload arrived at payload_received_at (NULL before 0011: the
                # row never changed its source then, so its first sighting is the row's).
                timeline.sight(identity, data_source, received or first, last)
                row_keys[raw_id] = (identity.key, first)
                sources[identity.match][identity.source] = identity.optional
                stored[identity.match].append(
                    (as_utc(first), as_utc(last), identity, reading(spec, raw_json))
                )
            if row_keys:
                versions = await session.execute(
                    select(
                        RawHealthDataVersion.raw_id,
                        RawHealthDataVersion.raw_json,
                        RawHealthDataVersion.received_at,
                        RawHealthDataVersion.last_seen_at,
                    ).where(among(session, RawHealthDataVersion.raw_id, list(row_keys)))
                )
                for raw_id, raw_json, received, last in versions.tuples():
                    identity = record_identity(spec, raw_json)
                    if identity is None:
                        continue
                    key, first = row_keys[raw_id]
                    timeline.sight(identity, raw_json.get("dataSource"), received or first, last)
                    if identity.key == key:
                        sources[identity.match][identity.source] = identity.optional
        for found in stored.values():
            if len(found) < 2:
                continue
            for pair, shared_key, seen in sorted(_together(found)):
                kept = timeline.evidence.setdefault(pair, [])
                item = Evidence(shared_key, seen)
                if useful(kept, item):
                    kept.append(item)
        for forms in sources.values():
            ordered = sorted(forms)
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    if compatible(forms[left], forms[right]):
                        timeline.shared[(left, right)] += 1
        cursor = (batch[-1][3], batch[-1][0])
    return timeline


def _full(chains: Chains, chain: dict) -> list[str]:
    """The fingerprints of a reported chain entry (``fingerprints`` holds prefixes)."""
    return [
        name for prefix in chain["fingerprints"] for name in chains.spans if name.startswith(prefix)
    ][:2]


def _fingerprints(*names: str) -> list[str]:
    return [name[:12] for name in names]


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


async def _settle_chains(
    sessions: async_sessionmaker,
    spec: DataTypeSpec,
    timeline: _Timeline | None,
    stats: BackfillStats,
    *,
    dry_run: bool,
    statement_timeout: str,
    notice: Notice | None = None,
    apply: Callable[[AsyncSession, Chains], Awaitable[None]] | None = None,
) -> tuple[Chains, DeviceChange]:
    """Merge the spans and evidence (full run), refute, decide and record the chains, and log
    the devices that changed, under the exclusive lock. ``apply``: an owner decision applied
    first (identity-decide).

    Returns the chains (with a dry run's planned links) and the fingerprint pairs that are
    no longer, or newly, one device (a link of this run, the decision, a refutation)."""
    decided_by = "backfill" if timeline is not None else "reconciler"

    async def work(session: AsyncSession) -> tuple[Chains, DeviceChange]:
        now = await source_chains.database_now(session)
        chains = await load_chains(session, spec.name)
        if timeline is not None:
            for fingerprint, span in timeline.spans.items():
                stored = chains.spans.get(fingerprint)
                if stored is not None:
                    span.widen(stored.first, stored.last, stored.newest)
                chains.spans[fingerprint] = span
            for fingerprint in chains.spans:
                chains.spans[fingerprint].complete = True
            found = [
                (*pair, item.key, item.seen)
                for pair, items in timeline.evidence.items()
                for item in items
            ]
            if dry_run:
                for left, right, key, seen in found:
                    chains.add_evidence(left, right, key, seen)
            else:
                await write_spans(session, spec.name, timeline.spans, now)
                await record_evidence(session, spec.name, chains, found)
        names = set(chains.spans) | {name for link in chains.links for name in link}
        members_before = {name: chains.members(name) for name in names}
        if apply is not None:
            await apply(session, chains)
        if dry_run:
            refuted = [
                (old, new) for old, new in sorted(chains.links) if chains.refutable(old, new)
            ]
            chains.refuted_events.update(refuted)
            chains._changed()
        else:
            refuted = await record_refutations(session, spec.name, chains, now)
        if apply is None:
            decision = decide_chains(chains)
        else:
            # An owner decision records only itself: chains it makes possible are decided by
            # the next reconciler run (and reviewed like any other), never inside it.
            decision = decide_chains(chains.copy())
            stats.left_to_reconciler.extend(
                {
                    "from": chains.spans[old].label,
                    "to": chains.spans[new].label,
                    "fingerprints": _fingerprints(old, new),
                    "why": "the chain rule would link them now: left to the next reconciler run "
                    "(identity-backfill --incremental, nightly), which lists it for review",
                }
                for old, new in decision.links
            )
            decision.links = []
        recorded: set[tuple[str, str]] = set()
        for old, new in decision.links:
            if dry_run:
                recorded.add((old, new))
                chains.decided_by[(old, new)] = decided_by
                continue
            shared = None
            if timeline is not None:
                shared = timeline.shared.get(ordered_pair(old, new), 0)
            # decide_chains added the link in memory; record it (or forget it).
            chains.forget(old, new)
            if await insert_chain(
                session,
                spec.name,
                chains,
                old,
                new,
                decided_by=decided_by,
                now=now,
                shared_keys=shared,
            ):
                recorded.add((old, new))
                event(
                    logger,
                    "SOURCE_CHAIN_LINKED",
                    data_type=spec.name,
                    old=chains.spans[old].label,
                    new=chains.spans[new].label,
                    gap_seconds=gap_seconds(chains.spans[old], chains.spans[new]),
                )
        for old, new in refuted:
            event(
                logger,
                "SOURCE_CHAIN_REFUTED",
                data_type=spec.name,
                old=chains.spans[old].label if old in chains.spans else old[:12],
                new=chains.spans[new].label if new in chains.spans else new[:12],
            )
        if decision.ambiguous:
            event(
                logger, "SOURCE_CHAIN_AMBIGUOUS", data_type=spec.name, count=len(decision.ambiguous)
            )
        if not dry_run:
            # Readers count supersessions by these devices: logged in this same transaction.
            await record_devices(session, spec.name, chains, now)
        _report(chains, timeline, recorded, decision.ambiguous, refuted, stats)
        stats.deferred_deletions = await _deferred_of(session, spec.name)
        change = DeviceChange()
        for name in names | {name for link in chains.links for name in link}:
            before = members_before.get(name, frozenset((name,)))
            after = chains.members(name)
            change.lapsed.update(ordered_pair(name, other) for other in before - after)
            change.joined.update(ordered_pair(name, other) for other in after - before)
        # Kept with the change itself, so a re-settlement that fails is resumed by the next run
        # (cleared once it finished); a dry run shows what is still owed too, writing nothing.
        change = await _owe_settlement(session, spec.name, change, dry_run=dry_run)
        return chains, change

    return await _exclusively(sessions, spec.name, work, statement_timeout, notice, dry_run=dry_run)


@dataclass
class DeviceChange:
    """Fingerprint pairs (ordered) that are no longer one device - their links lapsed: both
    sides are settled again, so each device counts once - and that newly are."""

    lapsed: set[tuple[str, str]] = field(default_factory=set)
    joined: set[tuple[str, str]] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.lapsed or self.joined)

    def dump(self) -> str | None:
        if not self:
            return None
        return json.dumps(
            {"lapsed": sorted(map(list, self.lapsed)), "joined": sorted(map(list, self.joined))},
            sort_keys=True,
        )

    @classmethod
    def load(cls, value: str | None) -> DeviceChange:
        try:
            found = json.loads(value or "{}")
            return cls(
                {ordered_pair(*pair) for pair in found.get("lapsed", [])},
                {ordered_pair(*pair) for pair in found.get("joined", [])},
            )
        except (ValueError, TypeError, AttributeError):
            return cls()


async def _reconcile_row(session: AsyncSession, data_type: str) -> IdentityReconcileState:
    """The data type's bookkeeping row, locked (created when missing)."""
    state = await session.get(IdentityReconcileState, data_type, with_for_update=True)
    if state is None:
        state = IdentityReconcileState(data_type=data_type, runs=0)
        session.add(state)
        await session.flush()
    return state


async def _owe_settlement(
    session: AsyncSession, data_type: str, change: DeviceChange, *, dry_run: bool = False
) -> DeviceChange:
    """Add ``change`` to the re-settlement the data type still owes (a run that failed after
    devices changed) and return all of it: settled by this run, cleared once it finished. A
    dry run only reads what is owed (no lock, nothing written)."""
    if dry_run:
        found = await session.get(IdentityReconcileState, data_type)
        owed = DeviceChange.load(found.pending_settle if found is not None else None)
        owed.lapsed |= change.lapsed
        owed.joined |= change.joined
        return owed
    state = await _reconcile_row(session, data_type)
    owed = DeviceChange.load(state.pending_settle)
    owed.lapsed |= change.lapsed
    owed.joined |= change.joined
    state.pending_settle = owed.dump()
    return owed


async def _settled(sessions: async_sessionmaker, data_type: str) -> None:
    """The re-settlement owed is done."""
    async with sessions.begin() as session:
        state = await session.get(IdentityReconcileState, data_type, with_for_update=True)
        if state is not None and state.pending_settle is not None:
            state.pending_settle = None


# --- pass 2: links ------------------------------------------------------------------------


@dataclass
class _Plan:
    aliases: dict[str, _Row] = field(default_factory=dict)
    links: list[tuple[_Row, _Row, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _Link:
    old: uuid.UUID
    new: uuid.UUID
    old_source: str
    new_source: str
    created: datetime


async def _links_of(session: AsyncSession, ids: Sequence[uuid.UUID]) -> list[_Link]:
    """Every supersession whose old or new row is one of ``ids`` (any state)."""
    link = RawHealthDataSupersession
    found: dict[tuple[uuid.UUID, uuid.UUID], _Link] = {}
    for chunk in _chunked(sorted(ids), 5000):
        for column in (link.old_raw_id, link.new_raw_id):
            rows = await session.execute(
                select(
                    link.old_raw_id,
                    link.new_raw_id,
                    link.old_fingerprint,
                    link.new_fingerprint,
                    link.created_at,
                ).where(among(session, column, chunk))
            )
            for old, new, old_source, new_source, created in rows.tuples():
                found[(old, new)] = _Link(old, new, old_source, new_source, created)
    return list(found.values())


async def _plan_batch(
    session: AsyncSession,
    spec: DataTypeSpec,
    rows: list[_Row],
    chains: Chains,
    stats: BackfillStats,
    *,
    count_groups: bool = True,
) -> _Plan:
    """Per key and device, in two steps:

    1. per fingerprint, its *root* (live first, then the one seen last): every other row of
       that fingerprint not yet superseded by one of its own is linked to it (a duplicate, an
       older shape or value of what the source returns; such a link is always in force);
    2. per device, its *holder* among the roots that are counted (live first, the one the
       others already link to, the one that already holds others, then the newest): every
       other counted root that measured the same is linked to it (a value of its own is
       never hidden: ``kept_differing``).

    So only roots get links across fingerprints: when a chain is rejected or refuted, each
    fingerprint still counts once, and two forms joined later are linked root to root. The
    rows a newly linked row held are linked to its holder too (one step for readers), where
    they are of the holder's fingerprint or measured the same. A link that would close a
    cycle of links (in any state) is not made (``conflicts``). Links no longer in force are
    history: their rows are settled again like any other."""
    plan = _Plan()
    groups: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        groups[row.identity.match].append(row)
    alias = await aliases(session, spec.name, {row.identity.specific_key for row in rows})
    links = await _links_of(session, [row.raw_id for row in rows])
    by_id = {row.raw_id: row for row in rows}
    existing = {(link.old, link.new) for link in links}
    edges: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    # Rows superseded by a row of their own fingerprint (always in force), rows superseded
    # in force by a counted row (hidden from readers), and the rows each row holds in force.
    own_superseded: set[uuid.UUID] = set()
    hidden: set[uuid.UUID] = set()
    dependents: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for link in links:
        edges[link.old].add(link.new)
        if link.old_source == link.new_source:
            own_superseded.add(link.old)
        if chains.in_force(link.old_source, link.new_source):
            dependents[link.new].append(link.old)
            holder = by_id.get(link.new)
            # Hidden like readers see it (held_elsewhere): its holder is neither marked
            # deleted nor quarantined.
            if holder is None or not (holder.deleted or holder.quarantined):
                hidden.add(link.old)
    pointed = set(dependents)

    def reaches(start: uuid.UUID, target: uuid.UUID) -> bool:
        seen, frontier = {start}, [start]
        while frontier:
            for following in edges.get(frontier.pop(), ()):
                if following == target:
                    return True
                if following not in seen:
                    seen.add(following)
                    frontier.append(following)
        return False

    def measured(row: _Row) -> str | None:
        return reading(spec, row.raw_json)

    def newest_form(row: _Row) -> str:
        span = chains.spans.get(row.identity.source)
        return span.first.isoformat() if span is not None else ""

    def add_link(row: _Row, holder: _Row) -> None:
        plan.links.append((row, holder, supersession_reason(spec, row.raw_json, holder.raw_json)))
        edges[row.raw_id].add(holder.raw_id)
        existing.add((row.raw_id, holder.raw_id))
        dependents[holder.raw_id].append(row.raw_id)
        hidden.add(row.raw_id)
        # What the row held, the holder holds now (one step for readers): rows of the
        # holder's fingerprint, and rows of others that measured the same.
        for dependent in list(dependents.get(row.raw_id, ())):
            held = by_id.get(dependent)
            if (
                held is not None
                and not held.deleted
                and (
                    held.identity.source == holder.identity.source
                    or measured(held) == measured(holder)
                )
                and (dependent, holder.raw_id) not in existing
                and not reaches(holder.raw_id, dependent)
            ):
                plan.links.append(
                    (held, holder, supersession_reason(spec, held.raw_json, holder.raw_json))
                )
                edges[dependent].add(holder.raw_id)
                existing.add((dependent, holder.raw_id))
                dependents[holder.raw_id].append(dependent)

    def join(row: _Row, holder: _Row) -> bool:
        if (row.raw_id, holder.raw_id) in existing:
            return True
        if reaches(holder.raw_id, row.raw_id):
            # Left double (listed) unless the row is marked deleted: then it counts nowhere.
            stats.conflicts += not row.deleted
            return False
        add_link(row, holder)
        return True

    for members in groups.values():
        if count_groups:
            stats.identities += 1
        devices: dict[str, list[_Row]] = defaultdict(list)
        for row in members:
            devices[chains.device(row.identity.source)].append(row)
        if count_groups:
            if len(devices) > 1:
                stats.distinct_sources += 1
        _tally_agreement(spec, members, chains, stats)
        for device in devices.values():
            if count_groups:
                stats.already_superseded += sum(row.raw_id in hidden for row in device)
            by_source: dict[str, list[_Row]] = defaultdict(list)
            for row in device:
                by_source[row.identity.source].append(row)
            roots: list[_Row] = []
            for own in by_source.values():
                candidates = [row for row in own if row.raw_id not in own_superseded] or own
                root = max(
                    candidates,
                    key=lambda row: (
                        not row.deleted,
                        # A growing interval: the end returned last holds the record.
                        row.last_seen_at if row.identity.shape else _EPOCH,
                        row.identity.shape or "",
                        row.last_seen_at,
                        row.raw_id in pointed,
                        row.first_seen_at,
                        str(row.raw_id),
                    ),
                )
                for row in candidates:
                    if row is not root:
                        join(row, root)
                roots.append(root)
            # A root marked deleted counts nowhere, and a link would keep it hidden when its
            # record comes back with another value (a row of its own again, every time):
            # only live roots are linked across fingerprints.
            counted = [root for root in roots if root.raw_id not in hidden and not root.deleted]
            if len(counted) > 1:
                holder = max(
                    counted,
                    key=lambda row: (
                        # A growing interval: the end returned last holds the record (an end
                        # Google no longer returns is stale, longer or not).
                        row.last_seen_at if row.identity.shape else _EPOCH,
                        row.identity.shape or "",
                        sum((other.raw_id, row.raw_id) in existing for other in counted),
                        row.raw_id in pointed,
                        row.last_seen_at,
                        # Of rows returned together, the device's newest form (the one
                        # returned again, as a rule).
                        newest_form(row),
                        row.first_seen_at,
                        str(row.raw_id),
                    ),
                )
                value = measured(holder)
                for root in counted:
                    if root is holder:
                        continue
                    if measured(root) != value:
                        # Another fingerprint that measured something else: never hidden.
                        stats.kept_differing += 1
                        continue
                    join(root, holder)
            if count_groups and all(row.deleted for row in device):
                holder = max(device, key=lambda row: row.order)
                others = [
                    row
                    for row in members
                    if row not in device
                    and not row.deleted
                    and row.identity.compatible(holder.identity)
                    and not chains.distinct(row.identity.source, holder.identity.source)
                ]
                if others:
                    stats.deletions_suspicious += 1
                else:
                    stats.deletions_kept += 1
        # Every row reachable by its key and fingerprint: its fingerprint's root first.
        for row in sorted(
            members,
            key=lambda row: (row.raw_id not in own_superseded, row.order),
            reverse=True,
        ):
            name = row.identity.specific_key
            if row.google_data_point_id == new_record_id(name):
                continue
            if name in alias:
                if count_groups:
                    stats.aliases_present += 1
                continue
            plan.aliases.setdefault(name, row)
    return plan


def _review_agreeing(
    chains: Chains,
    stats: BackfillStats,
    summary: dict[tuple[str, str], tuple[int, int, bool]],
) -> None:
    """Pairs established distinct by evidence whose shared keys mostly agree (a rename with
    reprocessing; ``summary``: the counts over every key counted so far): listed under
    ``review_distinct`` at once (the reconciler's next run no longer takes them for two
    devices)."""
    listed = {tuple(entry["fingerprints"]) for entry in stats.review_distinct}
    for pair, (agree, disagree, _) in sorted(summary.items()):
        left, right = pair
        if (
            agree < PAIR_KEYS
            or agree < disagree
            or pair in chains.pair_decisions
            or not source_chains.established(chains.evidence.get(pair, ()))
            or tuple(_fingerprints(left, right)) in listed
        ):
            continue
        labels = [chains.spans[name].label if name in chains.spans else name[:12] for name in pair]
        stats.review_distinct.append(
            {
                "sources": labels,
                "fingerprints": _fingerprints(left, right),
                "same_reading_keys": agree,
                "two_reading_keys": disagree,
                "why": "different values for a few records, but the same readings for most "
                "records both hold: it looks like a rename whose records were reprocessed "
                "(no deletion is marked on it); decide not-distinct and link, or distinct",
            }
        )


def _tally_agreement(
    spec: DataTypeSpec,
    members: list[_Row],
    chains: Chains,
    stats: BackfillStats,
    pairs: set[tuple[str, str]] | None = None,
) -> None:
    """For every pair of fingerprints with evidence of two devices (or of ``pairs``) that both
    hold this key: whether some row of each holds the same reading (agree) or not. Noted per
    key (``agreement_keys``), so a key counted again replaces its earlier count."""
    readings: dict[str, set[str | None]] = defaultdict(set)
    forms: dict[str, RecordIdentity] = {}
    for row in members:
        readings[row.identity.source].add(reading(spec, row.raw_json))
        forms[row.identity.source] = row.identity
    names = sorted(readings)
    for position, left in enumerate(names):
        for right in names[position + 1 :]:
            pair = ordered_pair(left, right)
            if pair not in (chains.evidence if pairs is None else pairs):
                continue
            if not forms[left].compatible(forms[right]):
                continue
            key = pair_key(forms[left], forms[right])
            same = bool(readings[left] & readings[right])
            if (pair, key) not in stats.agreement_keys:
                counts = stats.agreement.setdefault(pair, [0, 0])
                counts[0 if same else 1] += 1
            stats.agreement_keys[(pair, key)] = same


async def _record_agreement(
    session: AsyncSession, data_type: str, found: dict[tuple[tuple[str, str], str], bool]
) -> None:
    """Append the per-key agreement that changed (``raw_source_agreements``) and move the
    pair's counts (``identity_reconcile_state.pair_agreement``) by exactly that, in this
    transaction: a key counted again counts once."""
    if not found:
        return
    table = RawSourceAgreement
    latest: dict[tuple[tuple[str, str], str], bool] = {}
    for chunk in _chunked(sorted({key for _, key in found}), 5000):
        rows = await session.execute(
            select(
                table.first_fingerprint,
                table.second_fingerprint,
                table.identity_key,
                table.same_reading,
            )
            .where(table.data_type == data_type, among(session, table.identity_key, chunk))
            .order_by(table.id)
        )
        for first, second, key, same in rows.tuples():
            latest[((first, second), key)] = same
    changed = {name: same for name, same in found.items() if latest.get(name) is not same}
    if not changed:
        return
    now = await source_chains.database_now(session)
    values = [
        {
            "data_type": data_type,
            "first_fingerprint": first,
            "second_fingerprint": second,
            "identity_key": key,
            "same_reading": same,
            "counted_at": now,
        }
        for ((first, second), key), same in sorted(changed.items())
    ]
    for chunk in _chunked(values):
        await session.execute(insert(table), list(chunk))
    state = await _reconcile_row(session, data_type)
    summary = parse_agreement(state.pair_agreement)
    for (pair, key), same in changed.items():
        agree, disagree, counted = summary.get(pair, (0, 0, False))
        before = latest.get((pair, key))
        agree += int(same) - int(before is True)
        disagree += int(not same) - int(before is False)
        summary[pair] = (agree, disagree, counted)
    state.pair_agreement = dump_agreement(summary)


async def _agreement_of(
    sessions: async_sessionmaker, data_type: str
) -> dict[tuple[str, str], tuple[int, int, bool]]:
    async with sessions() as session:
        state = await session.get(IdentityReconcileState, data_type)
        return parse_agreement(state.pair_agreement if state is not None else None)


async def _count_shared(
    sessions: async_sessionmaker,
    spec: DataTypeSpec,
    *,
    batch_size: int,
    statement_timeout: str,
) -> set[tuple[str, str]]:
    """Count the agreement of every key shared by the pairs established distinct by evidence
    whose keys were not all counted yet (evidence that appeared after the last full run): the
    start times with rows of more than one source since the later of the two was first seen.
    Before chains are refuted, so a refutation is judged on every key, never on a sample.
    Reads rows and writes only the agreement bookkeeping (no data type lock). Returns the
    pairs, now counted."""
    async with sessions() as session:
        chains = await load_chains(session, spec.name)
    pairs = {
        pair
        for pair, items in chains.evidence.items()
        if source_chains.established(items)
        and not chains.agreement_counted(*pair)
        and pair[0] in chains.spans
        and pair[1] in chains.spans
    }
    if not pairs:
        return set()
    touched = (
        min(max(chains.spans[left].first, chains.spans[right].first) for left, right in pairs)
        - OVERLAP
    )
    starts = await _collect_starts(sessions, spec.name, since=None, touched=touched, lapsed=set())
    step = max(1, batch_size // 10)
    for offset in range(0, len(starts), step):
        chunk = [_from_micros(value) for value in starts[offset : offset + step]]
        async with sessions.begin() as session:
            await _prepare(session, statement_timeout)
            rows, mismatched = _rows_of(spec, await _rows_at(session, spec, chunk), None)
            groups: dict[str, list[_Row]] = defaultdict(list)
            for row in (*rows, *mismatched):
                groups[row.identity.match].append(row)
            stats = BackfillStats()
            for members in groups.values():
                _tally_agreement(spec, members, chains, stats, pairs)
            await _record_agreement(session, spec.name, stats.agreement_keys)
    await _mark_counted(sessions, spec.name, pairs)
    return pairs


async def summed_agreement(
    session: AsyncSession, data_type: str | None = None
) -> dict[str, dict[tuple[str, str], tuple[int, int]]]:
    """The latest per-key agreement of each pair (of ``data_type``, or of every data type),
    summed in the database: per data type and pair, (keys with one reading, keys with two).
    Only the sums are read, however many keys there are."""
    table, newer = RawSourceAgreement, aliased(RawSourceAgreement)
    latest = ~(
        select(newer.id)
        .where(
            newer.data_type == table.data_type,
            newer.identity_key == table.identity_key,
            newer.first_fingerprint == table.first_fingerprint,
            newer.second_fingerprint == table.second_fingerprint,
            newer.id > table.id,
        )
        .exists()
    )
    query = select(
        table.data_type,
        table.first_fingerprint,
        table.second_fingerprint,
        func.count().filter(table.same_reading.is_(True)),
        func.count().filter(table.same_reading.is_(False)),
    ).where(latest)
    if data_type is not None:
        query = query.where(table.data_type == data_type)
    query = query.group_by(table.data_type, table.first_fingerprint, table.second_fingerprint)
    found: dict[str, dict[tuple[str, str], tuple[int, int]]] = defaultdict(dict)
    for name, first, second, agree, disagree in (await session.execute(query)).tuples():
        found[name][(first, second)] = (agree, disagree)
    return found


async def _summed(session: AsyncSession, data_type: str) -> dict[tuple[str, str], tuple[int, int]]:
    """The latest per-key agreement of each pair of the data type, summed."""
    return (await summed_agreement(session, data_type)).get(data_type, {})


async def _rows_at(session: AsyncSession, spec: DataTypeSpec, starts: Sequence[datetime]) -> list:
    found = []
    for chunk in _chunked(sorted(set(starts)), 2000):
        found.extend(
            (
                await session.execute(
                    select(*_COLUMNS).where(
                        RawHealthData.data_type == spec.name,
                        among(session, RawHealthData.source_start_time, chunk),
                    )
                )
            )
            .tuples()
            .all()
        )
    return found


async def _mark_counted(
    sessions: async_sessionmaker,
    data_type: str,
    pairs: set[tuple[str, str]],
    *,
    recount: bool = False,
) -> dict[tuple[str, str], tuple[int, int, bool]]:
    """Every key of these pairs was counted (a full run, or the count of their shared keys);
    returns the counts of the data type. ``recount`` (a full run): the counts are summed
    again from raw_source_agreements first."""
    async with sessions.begin() as session:
        state = await _reconcile_row(session, data_type)
        summary = parse_agreement(state.pair_agreement)
        if recount:
            summary = {
                pair: (agree, disagree, summary.get(pair, (0, 0, False))[2])
                for pair, (agree, disagree) in (await _summed(session, data_type)).items()
            }
        for pair in pairs:
            agree, disagree, _ = summary.get(pair, (0, 0, False))
            summary[pair] = (agree, disagree, True)
        if summary:
            state.pair_agreement = dump_agreement(summary)
        return summary


async def _write_plan(
    session: AsyncSession, spec: DataTypeSpec, plan: _Plan, stats: BackfillStats, *, dry_run: bool
) -> None:
    if dry_run:
        for row, _, reason in plan.links:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
        stats.aliases_added += len(plan.aliases)
        return
    await _record_agreement(session, spec.name, stats.agreement_keys)
    if plan.aliases:
        values = [
            {
                "data_type": spec.name,
                "identity_key": key,
                "raw_id": row.raw_id,
                "google_data_point_id": row.google_data_point_id,
            }
            for key, row in sorted(plan.aliases.items())
        ]
        table = RawHealthDataIdentity.__table__
        for offset in range(0, len(values), INSERT_CHUNK):
            statement = (
                dialect_insert(session, table)
                .values(values[offset : offset + INSERT_CHUNK])
                .on_conflict_do_nothing(index_elements=["data_type", "identity_key"])
                .returning(table.c.identity_key)
            )
            stats.aliases_added += len((await session.execute(statement)).scalars().all())
    if not plan.links:
        return
    table = RawHealthDataSupersession.__table__
    values = [
        {
            "old_raw_id": old.raw_id,
            "new_raw_id": new.raw_id,
            "data_type": spec.name,
            "old_google_data_point_id": old.google_data_point_id,
            "new_google_data_point_id": new.google_data_point_id,
            "reason": reason,
            "old_fingerprint": old.identity.source,
            "new_fingerprint": new.identity.source,
            "same_reading": reading(spec, old.raw_json) == reading(spec, new.raw_json),
            "new_payload_hash": new.payload_hash,
        }
        for old, new, reason in sorted(
            plan.links, key=lambda link: (str(link[0].raw_id), str(link[1].raw_id))
        )
    ]
    # RETURNING names exactly the links this run added.
    inserted: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for offset in range(0, len(values), INSERT_CHUNK):
        statement = (
            dialect_insert(session, table)
            .values(values[offset : offset + INSERT_CHUNK])
            .on_conflict_do_nothing(index_elements=["old_raw_id", "new_raw_id"])
            .returning(table.c.old_raw_id, table.c.new_raw_id)
        )
        inserted.update((await session.execute(statement)).tuples().all())
    for row, holder, reason in plan.links:
        if (row.raw_id, holder.raw_id) in inserted:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
    # One identity-merged event per row, ever: a row linked again (or a link inserted again
    # after a downgrade with -x allow-identity-loss=1 and an upgrade) finds the first one.
    olds = {old for old, _ in inserted}
    merged: set[uuid.UUID] = set()
    for chunk in _chunked(sorted(olds)):
        merged.update(
            (
                await session.scalars(
                    select(RawHealthDataEvent.raw_id).where(
                        RawHealthDataEvent.event == MERGED_EVENT,
                        among(session, RawHealthDataEvent.raw_id, chunk),
                    )
                )
            ).all()
        )
    now = datetime.now(UTC)
    events = []
    for old, _, _ in plan.links:
        if old.raw_id in olds and old.raw_id not in merged:
            merged.add(old.raw_id)
            events.append(
                {
                    "raw_id": old.raw_id,
                    "data_type": spec.name,
                    "google_data_point_id": old.google_data_point_id,
                    "event": MERGED_EVENT,
                    "occurred_at": now,
                    "previous_deleted_at": None,
                }
            )
    for chunk in _chunked(events):
        await session.execute(insert(RawHealthDataEvent), list(chunk))


def _chunked(values: Sequence, size: int = INSERT_CHUNK):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _rows_of(
    spec: DataTypeSpec, batch: Sequence, stats: BackfillStats | None
) -> tuple[list[_Row], list[_Row]]:
    """(rows to settle, rows whose stored start differs from the parsed one)."""
    rows: list[_Row] = []
    mismatched: list[_Row] = []
    for raw_id, google_id, raw_json, start, first, last, deleted, _, digest, quarantined in batch:
        if stats is not None:
            stats.rows += 1
        if google_name(raw_json) is not None:
            if stats is not None:
                stats.named += 1
            continue
        identity = record_identity(spec, raw_json)
        if identity is None:
            if stats is not None:
                stats.unidentified += 1
            continue
        row = _Row(
            raw_id,
            google_id,
            raw_json,
            as_utc(first),
            as_utc(last),
            deleted is not None,
            identity,
            digest,
            bool(quarantined),
        )
        if instant(identity.start) != instant(start):
            mismatched.append(row)
        else:
            rows.append(row)
    return rows, mismatched


async def _batch_chains(session: AsyncSession, data_type: str, planned: Chains) -> Chains:
    """The recorded chains now, plus a dry run's planned links, evidence and decisions."""
    chains = await load_chains(session, data_type)
    for link in planned.links - chains.links:
        chains.links.add(link)
        chains.decided_by[link] = planned.decided_by.get(link, "reconciler")
    for (left, right), items in planned.evidence.items():
        for item in items:
            chains.add_evidence(left, right, item.key, item.seen)
    chains.chain_decisions.update(planned.chain_decisions)
    chains.pair_decisions.update(planned.pair_decisions)
    chains.refuted_events |= planned.refuted_events
    for fingerprint, span in planned.spans.items():
        chains.spans.setdefault(fingerprint, span)
    chains._changed()
    return chains


async def _settle_starts(
    session: AsyncSession,
    spec: DataTypeSpec,
    starts: Sequence[datetime],
    extra: Sequence[uuid.UUID],
    planned: Chains,
    stats: BackfillStats,
    *,
    dry_run: bool,
    count: bool,
) -> None:
    """Every row stored at one of ``starts``, plus the rows ``extra`` (by id: rows whose
    stored start is stale), settled together (keys of the extra rows only, then)."""
    found = await _rows_at(session, spec, starts)
    rows, mismatched = _rows_of(spec, found, stats if count else None)
    rows.extend(mismatched)
    if extra:
        by_id = (
            await session.execute(
                select(*_COLUMNS).where(among(session, RawHealthData.id, sorted(extra)))
            )
        ).tuples()
        stale, stale_mismatched = _rows_of(spec, by_id.all(), None)
        keys = {row.identity.match for row in (*stale, *stale_mismatched)}
        known = {row.raw_id for row in rows}
        rows = [row for row in rows if row.identity.match in keys]
        rows.extend(row for row in (*stale, *stale_mismatched) if row.raw_id not in known)
    chains = await _batch_chains(session, spec.name, planned)
    plan = await _plan_batch(session, spec, rows, chains, stats, count_groups=count)
    await _write_plan(session, spec, plan, stats, dry_run=dry_run)


async def _deferred_of(session: AsyncSession, data_type: str) -> list[dict]:
    """The deletions complete fetches deferred that no later one decided (app/sync/deletions.py)."""
    from app.sync.deletions import deferred_deletions

    return await deferred_deletions(session, data_type)


async def _reconcile_state(sessions: async_sessionmaker, data_type: str):
    async with sessions() as session:
        return await session.get(IdentityReconcileState, data_type)


async def _save_state(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    started: datetime,
    stats: BackfillStats | None,
    error: str | None = None,
    checkpoint: bool = True,
    mode: str | None = None,
) -> None:
    async with sessions.begin() as session:
        finished = await source_chains.database_now(session)
        state = await session.get(IdentityReconcileState, data_type)
        if state is None:
            state = IdentityReconcileState(data_type=data_type, runs=0)
            session.add(state)
        state.last_started_at = started
        state.last_finished_at = finished
        state.runs = (state.runs or 0) + 1
        state.updated_at = finished
        if stats is not None:
            state.last_mode = stats.mode
            state.last_result = json.dumps(stats.summary(), sort_keys=True)
            state.last_error = None
            if stats.skipped is None and checkpoint:
                state.checkpoint_at = started
                if stats.mode == "full":
                    state.full_run_at = finished
        else:
            # The mode of this failed run, not of the last that succeeded.
            state.last_mode = mode or state.last_mode
            state.last_error = (error or "failed")[:2000]


async def _link_pass(
    sessions: async_sessionmaker,
    spec: DataTypeSpec,
    planned: Chains,
    total: BackfillStats,
    *,
    batch_size: int,
    dry_run: bool,
    after: datetime | None,
    statement_timeout: str,
    progress: Callable[[str, BackfillStats, datetime], None] | None,
    notice: Notice | None,
) -> None:
    """Pass 2 over every row of the data type (from ``after``), batch by batch."""
    cursor: tuple[datetime, uuid.UUID] | None = None
    batches = 0
    while True:
        stats = BackfillStats(mode=total.mode)
        analyze = not dry_run and ((batches & (batches - 1)) == 0 or batches % 50 == 0)
        batches += 1

        async def work(session: AsyncSession, stats=stats, analyze=analyze, cursor=cursor):
            if analyze and session.get_bind().dialect.name == "postgresql":
                await session.execute(text(f"ANALYZE {IDENTITY_TABLE}"))
            batch = await _read_batch(session, spec.name, batch_size, cursor, after)
            if not batch:
                return None
            # Stale-start rows were noted by id in pass 1 (also those before ``after``) and
            # are settled last, with the rows of their parsed start.
            rows, _ = _rows_of(spec, batch, stats)
            chains = await _batch_chains(session, spec.name, planned)
            plan = await _plan_batch(session, spec, rows, chains, stats)
            await _write_plan(session, spec, plan, stats, dry_run=dry_run)
            return (batch[-1][3], batch[-1][0])

        reached = await _exclusively(
            sessions, spec.name, work, statement_timeout, notice, dry_run=dry_run
        )
        if reached is None:
            break
        cursor = reached
        total.add(stats)
        if progress is not None:
            progress(spec.name, total, as_utc(cursor[0]))


async def backfill_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    batch_size: int = 5_000,
    dry_run: bool = False,
    after: datetime | None = None,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
    notice: Notice | None = None,
) -> BackfillStats:
    """A full run for one data type (the whole archive; ``after`` skips the second pass
    up to a start time)."""
    spec = spec_for(data_type)
    total = BackfillStats(mode="full")
    async with sessions() as session:
        started = await source_chains.database_now(session)
    timeline = await _timeline(sessions, spec, batch_size, statement_timeout)
    planned, _ = await _settle_chains(
        sessions,
        spec,
        timeline,
        total,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        notice=notice,
    )
    await _link_pass(
        sessions,
        spec,
        planned,
        total,
        batch_size=batch_size,
        dry_run=dry_run,
        after=after,
        statement_timeout=statement_timeout,
        progress=progress,
        notice=notice,
    )
    stale = sorted(timeline.stale)
    total.start_mismatches = len(stale)
    for offset in range(0, len(stale), batch_size):
        chunk = stale[offset : offset + batch_size]
        stats = BackfillStats(mode="full")

        async def settle(session: AsyncSession, chunk=chunk, stats=stats) -> None:
            await _settle_starts(
                session,
                spec,
                [start for start, _ in chunk],
                [raw_id for _, raw_id in chunk],
                planned,
                stats,
                dry_run=dry_run,
                count=False,
            )

        await _exclusively(sessions, data_type, settle, statement_timeout, notice, dry_run=dry_run)
        total.add(stats)
    async with sessions() as session:
        undated = await session.scalar(
            select(func.count()).where(
                RawHealthData.data_type == data_type, RawHealthData.source_start_time.is_(None)
            )
        )
    total.unidentified += undated or 0
    if dry_run:
        summary = await _agreement_of(sessions, data_type)
    else:
        # Pass 2 counted every key of the pairs with evidence when it began (unless --after
        # skipped some), and settled every row (so no re-settlement is owed).
        summary = await _mark_counted(
            sessions, data_type, set(planned.evidence) if after is None else set(), recount=True
        )
        if after is None:
            await _settled(sessions, data_type)
    _review_agreeing(planned, total, summary)
    if not dry_run:
        await _save_state(sessions, data_type, started=started, stats=total)
    return total


async def relink_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    apply: Callable[[AsyncSession, Chains], Awaitable[None]],
    *,
    batch_size: int = 5_000,
    dry_run: bool = False,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
    notice: Notice | None = None,
) -> BackfillStats:
    """Apply an owner decision (``apply``, run under the exclusive lock first), decide the
    chains again from the sightings, then settle again exactly the rows whose devices changed:
    the rows of every link between two fingerprints that are no longer one device (each side
    then counts once), and, for two fingerprints that newly are, every row written since the
    later of them was first seen (``_settle_changed``)."""
    spec = spec_for(data_type)
    total = BackfillStats(mode="decision")
    async with sessions() as session:
        started = await source_chains.database_now(session)
    planned, change = await _settle_chains(
        sessions,
        spec,
        None,
        total,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        notice=notice,
        apply=apply,
    )
    await _settle_changed(
        sessions,
        spec,
        planned,
        change,
        total,
        since=None,
        batch_size=batch_size,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        progress=progress,
        notice=notice,
    )
    if not dry_run:
        await _settled(sessions, data_type)
    _review_agreeing(planned, total, await _agreement_of(sessions, data_type))
    if not dry_run:
        await _save_state(sessions, data_type, started=started, stats=total, checkpoint=False)
    return total


async def reconcile_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    batch_size: int = 5_000,
    dry_run: bool = False,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
    notice: Notice | None = None,
) -> BackfillStats:
    """An incremental run for one data type: chains from the sightings as they are, links
    for the rows changed since the checkpoint (minus ``OVERLAP``) and for every row whose
    device changed (``_settle_changed``). A data type that never had a full run gets its first
    full pass instead (``skipped`` says so), so none is left out for good."""
    spec = spec_for(data_type)
    state = await _reconcile_state(sessions, data_type)
    if state is None or state.full_run_at is None or state.checkpoint_at is None:
        total = await backfill_data_type(
            sessions,
            data_type,
            batch_size=batch_size,
            dry_run=dry_run,
            statement_timeout=statement_timeout,
            progress=progress,
            notice=notice,
        )
        total.skipped = "no full identity-backfill of this data type yet: ran its first full pass"
        return total
    total = BackfillStats(mode="incremental")
    async with sessions() as session:
        started = await source_chains.database_now(session)
    if not dry_run:
        await _count_shared(
            sessions, spec, batch_size=batch_size, statement_timeout=statement_timeout
        )
    planned, change = await _settle_chains(
        sessions,
        spec,
        None,
        total,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        notice=notice,
    )
    await _settle_changed(
        sessions,
        spec,
        planned,
        change,
        total,
        since=as_utc(state.checkpoint_at) - OVERLAP,
        batch_size=batch_size,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        progress=progress,
        notice=notice,
    )
    if not dry_run:
        await _settled(sessions, data_type)
    _review_agreeing(planned, total, await _agreement_of(sessions, data_type))
    if not dry_run:
        await _save_state(sessions, data_type, started=started, stats=total)
    return total


async def _settle_changed(
    sessions: async_sessionmaker,
    spec: DataTypeSpec,
    planned: Chains,
    change: DeviceChange,
    total: BackfillStats,
    *,
    since: datetime | None,
    batch_size: int,
    dry_run: bool,
    statement_timeout: str,
    progress: Callable[[str, BackfillStats, datetime], None] | None,
    notice: Notice | None,
) -> None:
    """Settle again, batch by batch, the start times of

    * rows created or given a new payload since ``since`` (an incremental run), and rows whose
      deletion mark was cleared since then;
    * every row of a link between two fingerprints that are no longer one device (``lapsed``:
      a rejected or refuted chain, a ``distinct`` decision), whatever its age - its two sides
      are linked again within each device, so neither counts twice;
    * every row first seen since the later of two fingerprints that are newly one device was
      (``joined``): each of its rows was, also one stored before migration 20261020_0011
      (without ``payload_received_at``).

    The start times are read first, each set in one short transaction (none is held open
    while they are settled), and kept as integers."""
    touched: datetime | None = None
    for left, right in change.joined:
        spans = [planned.spans.get(left), planned.spans.get(right)]
        if spans[0] is None or spans[1] is None:
            touched = datetime(1970, 1, 1, tzinfo=UTC)
            continue
        later = max(span.first for span in spans if span is not None) - OVERLAP
        touched = later if touched is None else min(touched, later)
    starts = await _collect_starts(
        sessions, spec.name, since=since, touched=touched, lapsed=change.lapsed
    )
    step = max(1, batch_size // 10)
    for offset in range(0, len(starts), step):
        chunk = [_from_micros(value) for value in starts[offset : offset + step]]
        stats = BackfillStats(mode=total.mode)

        async def settle(session: AsyncSession, chunk=chunk, stats=stats) -> None:
            await _settle_starts(
                session, spec, chunk, (), planned, stats, dry_run=dry_run, count=True
            )

        await _exclusively(sessions, spec.name, settle, statement_timeout, notice, dry_run=dry_run)
        total.add(stats)
        if progress is not None:
            progress(spec.name, total, chunk[-1])


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _micros(value: datetime) -> int:
    return (as_utc(value) - _EPOCH) // timedelta(microseconds=1)


def _from_micros(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


async def _collect_starts(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    since: datetime | None,
    touched: datetime | None,
    lapsed: set[tuple[str, str]],
) -> array:
    """The distinct start times (microseconds since the epoch, sorted) of the rows
    ``_settle_changed`` settles; each query streamed in a short read-only transaction of its
    own, so a long run never holds one snapshot (vacuum is not held back)."""
    sources: list[array] = []
    queries = []
    if since is not None:
        queries.append(
            select(RawHealthData.source_start_time).where(
                RawHealthData.data_type == data_type,
                # ix_raw_health_data_updated_at finds what a fetch touched; only rows created
                # or given a new payload since can need a link.
                RawHealthData.updated_at >= since,
                RawHealthData.payload_received_at >= since,
                RawHealthData.source_start_time.is_not(None),
            )
        )
        # A row whose deletion mark was cleared with its payload unchanged counts again: a
        # link it needs is not left for a full run (raw_health_data_events, by trigger).
        returned = RawHealthDataEvent
        queries.append(
            select(RawHealthData.source_start_time)
            .join(returned, returned.raw_id == RawHealthData.id)
            .where(
                returned.data_type == data_type,
                returned.event == "source-returned",
                returned.occurred_at >= since,
                RawHealthData.source_start_time.is_not(None),
            )
        )
    if touched is not None:
        # first_seen_at: the clock the sightings use (the application's, when the page was
        # stored); a row's source never changes, so every row of the later source was first
        # seen since it was.
        # Only start times with another row can need a link (ix_raw_health_data_type_start).
        other = aliased(RawHealthData)
        queries.append(
            select(RawHealthData.source_start_time).where(
                RawHealthData.data_type == data_type,
                RawHealthData.first_seen_at >= touched,
                RawHealthData.source_start_time.is_not(None),
                select(other.id)
                .where(
                    other.data_type == data_type,
                    other.source_start_time == RawHealthData.source_start_time,
                    other.id != RawHealthData.id,
                )
                .exists(),
            )
        )
    if lapsed:
        link = RawHealthDataSupersession
        pairs = sorted({*lapsed, *((right, left) for left, right in lapsed)})
        for column in (link.old_raw_id, link.new_raw_id):
            queries.append(
                select(RawHealthData.source_start_time)
                .join(link, column == RawHealthData.id)
                .where(
                    link.data_type == data_type,
                    tuple_(link.old_fingerprint, link.new_fingerprint).in_(pairs),
                    RawHealthData.source_start_time.is_not(None),
                )
            )
    for query in queries:
        found = array("q")
        async with sessions() as session:
            if session.get_bind().dialect.name == "postgresql":
                result = await session.stream(
                    query.distinct()
                    .order_by(RawHealthData.source_start_time)
                    .execution_options(yield_per=10_000)
                )
                async for (start,) in result:
                    found.append(_micros(start))
            else:
                result = await session.scalars(
                    query.distinct().order_by(RawHealthData.source_start_time)
                )
                found.extend(_micros(start) for start in result.all())
            await session.rollback()
        sources.append(found)
    merged = array("q")
    for value in heapq.merge(*sources):
        if not merged or merged[-1] != value:
            merged.append(value)
    return merged


async def data_types_in_archive(sessions: async_sessionmaker) -> list[str]:
    async with sessions() as session:
        return sorted((await session.scalars(select(RawHealthData.data_type).distinct())).all())


async def identity_backfill(
    sessions: async_sessionmaker,
    *,
    data_types: Sequence[str] | None = None,
    batch_size: int = 5_000,
    dry_run: bool = False,
    after: datetime | None = None,
    incremental: bool = False,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
    notice: Notice | None = None,
) -> dict[str, BackfillStats]:
    """Raises ``BackfillRunning`` when another run holds the single-run lock.

    ``incremental``: only what changed since each data type's checkpoint (the nightly run);
    a data type that never had a full run gets its first full pass (``skipped`` says so).
    A data type that fails (its lock stays busy, a statement times out) is reported with its
    ``error`` and saved in ``identity_reconcile_state``; the run goes on with the others."""
    async with backfill_lock(sessions, dry_run=dry_run):
        names = list(data_types) if data_types else await data_types_in_archive(sessions)
        results: dict[str, BackfillStats] = {}
        for name in names:
            async with sessions() as session:
                started = await source_chains.database_now(session)
            try:
                if incremental:
                    results[name] = await reconcile_data_type(
                        sessions,
                        name,
                        batch_size=batch_size,
                        dry_run=dry_run,
                        statement_timeout=statement_timeout,
                        progress=progress,
                        notice=notice,
                    )
                else:
                    results[name] = await backfill_data_type(
                        sessions,
                        name,
                        batch_size=batch_size,
                        dry_run=dry_run,
                        after=after,
                        statement_timeout=statement_timeout,
                        progress=progress,
                        notice=notice,
                    )
            except Exception as exc:
                logger.exception(
                    "Identity reconciliation of a data type failed",
                    extra={"event": "IDENTITY_RECONCILE_DATA_TYPE_FAILED", "data_type": name},
                )
                if not dry_run:
                    await _save_state(
                        sessions,
                        name,
                        started=started,
                        stats=None,
                        error=f"{exc}",
                        mode="incremental" if incremental else "full",
                    )
                results[name] = BackfillStats(
                    mode="incremental" if incremental else "full", error=f"{exc}"[:2000]
                )
        if not dry_run:
            await analyze_identity_tables(sessions)
    return results


async def analyze_identity_tables(sessions: async_sessionmaker) -> None:
    async with sessions.begin() as session:
        if session.get_bind().dialect.name == "postgresql":
            # Fresh statistics for the lookups every stored page makes from now on.
            await session.execute(
                text(
                    "ANALYZE raw_health_data_identity, raw_health_data_supersessions, "
                    "raw_source_chains, raw_source_sightings, raw_source_devices"
                )
            )


def describe_source(chains: Chains, fingerprint: str) -> dict:
    return describe(chains, fingerprint)
