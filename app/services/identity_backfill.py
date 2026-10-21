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
   app/services/source_chains.py and appended to ``raw_source_chains``; the cases the rule
   leaves open are listed in the report (``ambiguous``), every chain not yet reviewed by the
   owner under ``review``.
2. **Links.** Per v2 key, the rows are grouped by device (one fingerprint, or several joined
   by usable chain links). Within one device the row that holds the record (one already
   linked to, else the newest live row; a deleted-marked row only when the device has no
   live row) keeps it, and every other row of that device is linked to it in
   ``raw_health_data_supersessions`` with the reason, plus an ``identity-merged`` event - a
   row of *another fingerprint* of the device only when it measured the same (``reading``): a
   value of its own is never hidden (``kept_differing``). Rows of different devices are never
   linked. Every row whose own id does not name its key and fingerprint gets an alias
   (``synthetic:v2s:`` rows need none). A deleted-marked row with no live row of its device
   stays as it is: a genuine deletion (``deletions_kept``), or a *suspicious* one when another
   fingerprint not established distinct holds the key live (``deletions_suspicious``: possibly
   a rename nobody decided, listed for review).

A full run reads all rows in keyset order of ``(source_start_time, id)`` (index
``ix_raw_health_data_type_start``): every row of a v2 key has the same start, and a batch
always ends after the last row of its start time, so a key is never split. Rows whose stored
``source_start_time`` differs from the parsed start (an older parser) are noted by id during
the first pass and settled last with the rows of their parsed start - also when ``--after``
skips the rest of the second pass. An incremental run reads only the start times of rows
created or given a new payload (``updated_at`` and ``payload_received_at``) since its checkpoint
minus ``OVERLAP``, and, when it decided a new link, since the new fingerprint's first sighting.

Each batch of the second pass is one transaction holding the data type's exclusive lock
(writers of that data type wait for it, then continue): it waits at most ``LOCK_TIMEOUT``
for it, and once it holds it at most ``ROW_LOCK_TIMEOUT`` for any row lock of another
transaction, backs off and retries, so it never queues every writer behind a long sync or
import. Inserts are ``ON CONFLICT DO NOTHING`` in chunks (far below PostgreSQL's 32,767
parameters), so a run is idempotent and can be stopped and run again at any time. Only one
run at a time: a second one refuses at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, select, text, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import event
from app.core.timeutil import as_utc
from app.google_health.data_types import METRICS_SCOPE, REGISTRY, DataTypeSpec, RecordKind
from app.models.identity import (
    IDENTITY_TABLE,
    MERGED_EVENT,
    IdentityReconcileState,
    RawHealthDataIdentity,
    RawHealthDataSupersession,
)
from app.models.raw_versions import RawHealthDataEvent, RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.common import google_name
from app.parsers.identity import (
    RecordIdentity,
    instant,
    new_record_id,
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
    Span,
    backfill_lock,
    decide_chains,
    describe,
    gap_seconds,
    insert_chain,
    load_chains,
    lock_data_type,
    ordered_pair,
    record_evidence,
    record_refutations,
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
    # Rows left unlinked because linking them would chain two links or hide a live row
    # behind a deleted one.
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
    # Why an incremental run did nothing (no full run yet).
    skipped: str | None = None

    _LISTS = ("chains", "review", "kept_apart", "ambiguous", "refuted", "returned")

    def add(self, other: BackfillStats) -> None:
        for name in self.__dataclass_fields__:
            if name == "linked":
                self.linked.update(other.linked)
            elif name in self._LISTS:
                getattr(self, name).extend(getattr(other, name))
            elif name in ("mode", "skipped"):
                setattr(self, name, getattr(self, name) or getattr(other, name))
            else:
                setattr(self, name, getattr(self, name) + getattr(other, name))

    def as_dict(self) -> dict[str, Any]:
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        values["linked"] = dict(sorted(self.linked.items()))
        values["superseded"] = sum(self.linked.values())
        return values

    def summary(self) -> dict[str, Any]:
        """What the status command shows of a run: the counts, and every case left open
        (ambiguous sources, pairs kept apart that are not established distinct, chains
        awaiting review, refuted chains, suspicious deletions)."""

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
            "skipped": self.skipped,
            "open": {
                "review": short(self.review, "from", "to", "fingerprints", "decided"),
                "ambiguous": short(self.ambiguous, "source", "fingerprint", "why", "candidates"),
                "kept_apart": short(undecided, "sources", "fingerprints", "why"),
                "refuted": short(self.refuted, "from", "to", "fingerprints"),
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
) -> T:
    """Run ``work`` in one transaction that holds ``data_type``'s exclusive lock.

    The lock is waited for at most ``LOCK_TIMEOUT`` (a waiting exclusive request queues every
    new writer behind it), and once held, any row lock at most ``ROW_LOCK_TIMEOUT``; then the
    transaction is rolled back and retried after a pause (``notice`` is told)."""
    for attempt in range(LOCK_ATTEMPTS):
        try:
            async with sessions.begin() as session:
                await lock_data_type(session, data_type, exclusive=True, wait=LOCK_TIMEOUT)
                await _prepare(session, statement_timeout)
                return await work(session)
        except DBAPIError as exc:
            if _sqlstate(exc) != _LOCK_NOT_AVAILABLE:
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
        "held it); nothing of this batch was written - run again later, it resumes"
    )


# --- pass 1: sighting timelines -----------------------------------------------------------


@dataclass
class _Timeline:
    spans: dict[str, Span] = field(default_factory=dict)
    # Unordered fingerprint pairs -> v2 keys they share.
    shared: Counter = field(default_factory=Counter)
    # (parsed start, raw id) of rows whose stored start is stale: settled last, by id.
    stale: list[tuple[datetime, uuid.UUID]] = field(default_factory=list)
    # Ordered fingerprint pairs -> keys one page stored or last saw under both with two
    # readings (evidence of two devices; at most PAIR_KEYS each).
    evidence: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    def sight(self, identity: RecordIdentity, data_source, first, last) -> None:
        span = self.spans.get(identity.source)
        if span is None:
            self.spans[identity.source] = Span(
                as_utc(first),
                as_utc(last),
                identity.stable,
                source_label(data_source),
                True,
                data_source,
            )
        else:
            span.widen(first, last)


def _together(found: list[tuple[datetime, datetime, str, str | None]]) -> set[tuple[str, str]]:
    """Fingerprint pairs of one key's rows that one page stored or last saw together (the
    same ``first_seen_at`` or ``last_seen_at``: one transaction's clock) with two readings."""
    pairs: set[tuple[str, str]] = set()
    for position, (first, last, source, value) in enumerate(found):
        for other_first, other_last, other, other_value in found[position + 1 :]:
            if (
                other != source
                and other_value != value
                and (other_first == first or other_last == last)
            ):
                pairs.add(ordered_pair(source, other))
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
            sources: dict[str, set[str]] = defaultdict(set)
            stored: dict[str, list] = defaultdict(list)
            for raw_id, _, raw_json, start, first, last, _, received in batch:
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
                sources[identity.key].add(identity.source)
                stored[identity.key].append(
                    (as_utc(first), as_utc(last), identity.source, reading(spec, raw_json))
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
                        sources[key].add(identity.source)
        for key, found in stored.items():
            if len(found) < 2:
                continue
            for pair in _together(found):
                keys = timeline.evidence.setdefault(pair, set())
                if len(keys) < PAIR_KEYS:
                    keys.add(key)
        for found in sources.values():
            ordered = sorted(found)
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    timeline.shared[(left, right)] += 1
        cursor = (batch[-1][3], batch[-1][0])
    return timeline


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
    for old, new in sorted(chains.following.items()):
        before, after = chains.spans.get(old), chains.spans.get(new)
        entry = {
            "from": before.label if before else old[:12],
            "to": after.label if after else new[:12],
            "fingerprints": _fingerprints(old, new),
            "gap_seconds": gap_seconds(before, after) if before and after else None,
            "decided": "this run" if (old, new) in new_links else "earlier",
            "decided_by": chains.decided_by.get(old),
            "usable": chains.usable(old, new),
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
                    "one record); its records go onto the device's rows",
                }
            )
    for old, new in refuted:
        stats.refuted.append(
            {
                "from": chains.spans[old].label if old in chains.spans else old[:12],
                "to": chains.spans[new].label if new in chains.spans else new[:12],
                "fingerprints": _fingerprints(old, new),
                "effect": "two devices (different values for one record): no longer used; "
                "rows it linked held equal readings and stay linked",
            }
        )
    stats.ambiguous.extend(ambiguous)
    classes: dict[str, list[str]] = defaultdict(list)
    for fingerprint, span in chains.spans.items():
        classes[span.stable].append(fingerprint)
    for members in classes.values():
        ordered = sorted(members, key=lambda name: (chains.spans[name].first, name))
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if chains.same_device(left, right):
                    continue
                shared = None
                if timeline is not None:
                    shared = timeline.shared.get(ordered_pair(left, right), 0)
                    if not shared:
                        continue
                before, after = chains.spans[left], chains.spans[right]
                established = chains.distinct(left, right)
                stats.kept_apart.append(
                    {
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
) -> tuple[Chains, list[str]]:
    """Merge the spans and evidence (full run), refute, decide and record the chains, under
    the exclusive lock. ``apply``: an owner decision applied first (identity-decide).

    Returns the chains (with a dry run's planned links) and the fingerprints a link of this
    run (or the decision) made part of a device."""
    decided_by = "backfill" if timeline is not None else "reconciler"

    async def work(session: AsyncSession) -> tuple[Chains, list[str]]:
        now = await source_chains.database_now(session)
        chains = await load_chains(session, spec.name)
        before = dict(chains.following)
        if timeline is not None:
            for fingerprint, span in timeline.spans.items():
                stored = chains.spans.get(fingerprint)
                if stored is not None:
                    span.widen(stored.first, stored.last)
                chains.spans[fingerprint] = span
            for fingerprint in chains.spans:
                chains.spans[fingerprint].complete = True
            found = [(*pair, key) for pair, keys in timeline.evidence.items() for key in keys]
            if dry_run:
                for left, right, key in found:
                    chains.add_evidence(left, right, key)
            else:
                await write_spans(session, spec.name, timeline.spans, now)
                await record_evidence(session, spec.name, chains, found, now)
        changed: set[str] = set()
        if apply is not None:
            members_before = {name: chains.members(name) for name in chains.spans}
            await apply(session, chains)
            changed |= {
                name for name in chains.spans if chains.members(name) != members_before[name]
            }
        if dry_run:
            refuted = [
                (old, new)
                for old, new in sorted(chains.following.items())
                if old not in chains.refuted_events and chains.refuted(old, new)
            ]
        else:
            refuted = await record_refutations(session, spec.name, chains, now)
        decision = decide_chains(chains)
        recorded: set[tuple[str, str]] = set()
        for old, new in decision.links:
            if dry_run:
                recorded.add((old, new))
                chains.decided_by[old] = decided_by
                continue
            shared = None
            if timeline is not None:
                shared = timeline.shared.get(ordered_pair(old, new), 0)
            # decide_chains added the link in memory; record it (or forget it).
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
            else:
                chains.forget(old, new)
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
        _report(chains, timeline, recorded, decision.ambiguous, refuted, stats)
        for old, new in recorded:
            changed |= {old, new}
        for old, new in chains.following.items():
            if (old, new) not in before.items():
                changed |= {old, new}
        return chains, sorted(changed)

    return await _exclusively(sessions, spec.name, work, statement_timeout, notice)


# --- pass 2: links ------------------------------------------------------------------------


@dataclass
class _Plan:
    aliases: dict[str, _Row] = field(default_factory=dict)
    links: list[tuple[_Row, _Row, str]] = field(default_factory=list)


async def _plan_batch(
    session: AsyncSession,
    spec: DataTypeSpec,
    rows: list[_Row],
    chains: Chains,
    stats: BackfillStats,
    *,
    count_groups: bool = True,
) -> _Plan:
    plan = _Plan()
    groups: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        groups[row.identity.key].append(row)
    alias = await aliases(session, spec.name, {row.identity.specific_key for row in rows})
    ids = [row.raw_id for row in rows]
    linked = set(
        (
            await session.scalars(
                select(RawHealthDataSupersession.old_raw_id).where(
                    among(session, RawHealthDataSupersession.old_raw_id, ids)
                )
            )
        ).all()
    )
    targets = set(
        (
            await session.scalars(
                select(RawHealthDataSupersession.new_raw_id).where(
                    among(session, RawHealthDataSupersession.new_raw_id, ids)
                )
            )
        ).all()
    )

    for members in groups.values():
        if count_groups:
            stats.identities += 1
        devices: dict[str, list[_Row]] = defaultdict(list)
        for row in members:
            devices[chains.device(row.identity.source)].append(row)
        if count_groups and len(devices) > 1:
            stats.distinct_sources += 1
        for device in devices.values():
            candidates = [row for row in device if row.raw_id not in linked]
            if count_groups:
                stats.already_superseded += len(device) - len(candidates)
            if not candidates:
                continue
            pointed = [row for row in candidates if row.raw_id in targets]
            pool = pointed or [row for row in candidates if not row.deleted] or candidates
            holder = max(pool, key=lambda row: row.order)
            if count_groups and holder.deleted and all(row.deleted for row in device):
                others = [
                    row
                    for row in members
                    if row not in device
                    and not row.deleted
                    and not chains.distinct(row.identity.source, holder.identity.source)
                ]
                if others:
                    stats.deletions_suspicious += 1
                else:
                    stats.deletions_kept += 1
            measured = reading(spec, holder.raw_json)
            for row in candidates:
                if row is holder:
                    continue
                if row.raw_id in targets or (holder.deleted and not row.deleted):
                    stats.conflicts += 1
                    continue
                if (
                    row.identity.source != holder.identity.source
                    and reading(spec, row.raw_json) != measured
                ):
                    # Another fingerprint that measured something else: never hidden.
                    stats.kept_differing += 1
                    continue
                reason = supersession_reason(spec, row.raw_json, holder.raw_json)
                plan.links.append((row, holder, reason))
        # Every row reachable by its key and fingerprint; the holder of its own first.
        for row in sorted(
            members, key=lambda row: (row.raw_id not in linked, row.order), reverse=True
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


async def _write_plan(
    session: AsyncSession, spec: DataTypeSpec, plan: _Plan, stats: BackfillStats, *, dry_run: bool
) -> None:
    if dry_run:
        for row, _, reason in plan.links:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
        stats.aliases_added += len(plan.aliases)
        return
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
        }
        for old, new, reason in sorted(plan.links, key=lambda link: str(link[0].raw_id))
    ]
    # RETURNING names exactly the links this run added.
    inserted: set[uuid.UUID] = set()
    for offset in range(0, len(values), INSERT_CHUNK):
        statement = (
            dialect_insert(session, table)
            .values(values[offset : offset + INSERT_CHUNK])
            .on_conflict_do_nothing(index_elements=["old_raw_id"])
            .returning(table.c.old_raw_id)
        )
        inserted.update((await session.execute(statement)).scalars().all())
    for row, _, reason in plan.links:
        if row.raw_id in inserted:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
    # One identity-merged event per row, ever: a link inserted again after a downgrade
    # (-x allow-identity-loss=1) and upgrade finds the event of the first one.
    merged: set[uuid.UUID] = set()
    for chunk in _chunked(sorted(inserted)):
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
    events = [
        {
            "raw_id": old.raw_id,
            "data_type": spec.name,
            "google_data_point_id": old.google_data_point_id,
            "event": MERGED_EVENT,
            "occurred_at": now,
            "previous_deleted_at": None,
        }
        for old, _, _ in plan.links
        if old.raw_id in inserted and old.raw_id not in merged
    ]
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
    for raw_id, google_id, raw_json, start, first, last, deleted, _ in batch:
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
            raw_id, google_id, raw_json, as_utc(first), as_utc(last), deleted is not None, identity
        )
        if instant(identity.start) != instant(start):
            mismatched.append(row)
        else:
            rows.append(row)
    return rows, mismatched


async def _batch_chains(session: AsyncSession, data_type: str, planned: Chains) -> Chains:
    """The recorded chains now, plus a dry run's planned links, evidence and decisions."""
    chains = await load_chains(session, data_type)
    for old, new in planned.following.items():
        if old not in chains.following and new not in chains.preceding:
            chains.add(old, new)
    for pair, keys in planned.evidence.items():
        for key in keys:
            chains.add_evidence(*pair, key)
    for (first, second), decision in planned.chain_decisions.items():
        chains.decide(decision, first, second)
    for (first, second), decision in planned.pair_decisions.items():
        chains.decide(decision, first, second)
    for fingerprint, span in planned.spans.items():
        chains.spans.setdefault(fingerprint, span)
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
    rows, mismatched = _rows_of(spec, found, stats if count else None)
    rows.extend(mismatched)
    if extra:
        by_id = (
            await session.execute(
                select(*_COLUMNS).where(among(session, RawHealthData.id, sorted(extra)))
            )
        ).tuples()
        stale, stale_mismatched = _rows_of(spec, by_id.all(), None)
        keys = {row.identity.key for row in (*stale, *stale_mismatched)}
        known = {row.raw_id for row in rows}
        rows = [row for row in rows if row.identity.key in keys]
        rows.extend(row for row in (*stale, *stale_mismatched) if row.raw_id not in known)
    chains = await _batch_chains(session, spec.name, planned)
    plan = await _plan_batch(session, spec, rows, chains, stats, count_groups=count)
    await _write_plan(session, spec, plan, stats, dry_run=dry_run)


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

        reached = await _exclusively(sessions, spec.name, work, statement_timeout, notice)
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

        await _exclusively(sessions, data_type, settle, statement_timeout, notice)
        total.add(stats)
    async with sessions() as session:
        undated = await session.scalar(
            select(func.count()).where(
                RawHealthData.data_type == data_type, RawHealthData.source_start_time.is_(None)
            )
        )
    total.unidentified += undated or 0
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
    """Apply an owner decision (``apply``, run under the exclusive lock first) and settle
    the whole data type again: chains decided from the sightings, then pass 2 over every
    row (stale stored starts included: their rows have the start of their key)."""
    spec = spec_for(data_type)
    total = BackfillStats(mode="decision")
    async with sessions() as session:
        started = await source_chains.database_now(session)
    planned, _ = await _settle_chains(
        sessions,
        spec,
        None,
        total,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        notice=notice,
        apply=apply,
    )
    await _link_pass(
        sessions,
        spec,
        planned,
        total,
        batch_size=batch_size,
        dry_run=dry_run,
        after=None,
        statement_timeout=statement_timeout,
        progress=progress,
        notice=notice,
    )
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
    for the rows changed since the checkpoint (minus ``OVERLAP``)."""
    spec = spec_for(data_type)
    total = BackfillStats(mode="incremental")
    state = await _reconcile_state(sessions, data_type)
    if state is None or state.full_run_at is None or state.checkpoint_at is None:
        total.skipped = "no full identity-backfill of this data type yet"
        return total
    async with sessions() as session:
        started = await source_chains.database_now(session)
    planned, changed = await _settle_chains(
        sessions,
        spec,
        None,
        total,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        notice=notice,
    )
    since = as_utc(state.checkpoint_at) - OVERLAP
    for fingerprint in changed:
        # Every row of a newly joined fingerprint was written after its first sighting.
        if fingerprint in planned.spans:
            since = min(since, planned.spans[fingerprint].first - OVERLAP)
    async with sessions() as session:
        starts = sorted(
            {
                as_utc(start)
                for start in (
                    await session.scalars(
                        select(RawHealthData.source_start_time)
                        .where(
                            RawHealthData.data_type == data_type,
                            # ix_raw_health_data_updated_at finds what a fetch touched; only
                            # rows created or given a new payload since can need a link.
                            RawHealthData.updated_at >= since,
                            RawHealthData.payload_received_at >= since,
                            RawHealthData.source_start_time.is_not(None),
                        )
                        .distinct()
                    )
                ).all()
            }
        )
    for offset in range(0, len(starts), max(1, batch_size // 10)):
        chunk = starts[offset : offset + max(1, batch_size // 10)]
        stats = BackfillStats(mode="incremental")

        async def settle(session: AsyncSession, chunk=chunk, stats=stats) -> None:
            await _settle_starts(
                session, spec, chunk, (), planned, stats, dry_run=dry_run, count=True
            )

        await _exclusively(sessions, data_type, settle, statement_timeout, notice)
        total.add(stats)
        if progress is not None:
            progress(data_type, total, chunk[-1])
    if not dry_run:
        await _save_state(sessions, data_type, started=started, stats=total)
    return total


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
    a data type that never had a full run is skipped (``skipped`` says so)."""
    async with backfill_lock(sessions):
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
                if not dry_run:
                    await _save_state(sessions, name, started=started, stats=None, error=f"{exc}")
                raise
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
                    "raw_source_chains, raw_source_sightings"
                )
            )


def describe_source(chains: Chains, fingerprint: str) -> dict:
    return describe(chains, fingerprint)
