"""Which source fingerprints are one device: decided per source, never per record.

A record without a Google id is identified by its v2 key (app/parsers/identity.py), which
leaves out ``displayName`` and the package's ``.<UUID>`` suffix. Two records with one key and
two different ``dataSource`` fingerprints are either

* the same record before and after a change of its source (Google renamed the device, Apple
  Health dropped the suffix): Google then returns only the new form, everywhere, from the
  moment of the change; or
* two devices that recorded at the same instant: Google keeps returning both.

So the question is asked of the *sources*, per data type: each fingerprint has a sighting
span (``raw_source_sightings``: the first and the last time any record carrying it was
received). Fingerprint ``old`` -> ``new`` is a link of one device's chain only when

1. they share at least one v2 key (they describe the same records);
2. ``old``'s last sighting lies more than ``CHAIN_GAP`` before ``new``'s first sighting: they
   were never returned together, nor within five minutes of each other (the pages of one
   fetch, or the CLI and the server fetching side by side, count as "together");
3. ``old`` is the end of its chain and ``new`` the start of its own (a chain never forks);
4. both spans are complete (built from the whole archive by ``identity-backfill``, or the data
   type had no rows when the fingerprint first appeared);
5. ``old`` is the only fingerprint that satisfies 1-4 for ``new``. Two candidates are
   ambiguous: nothing is linked, both stay separate (the dry run lists them).

Spans that overlap are two devices: their records are never paired, never merged and never
make each other "present" when deciding deletions. A decided link is kept for good
(``raw_source_chains`` is append-only), but it is only *used* while the sightings still agree:
if the old fingerprint is ever seen again within ``CHAIN_GAP`` of the new one, the link is
contradicted and both are treated as separate sources again (``verify`` reports it).

The tolerance only ever errs towards keeping sources apart: a real rename whose two forms
appear within five minutes of each other (Google switching names in the middle of a fetch)
stays two sources, so both rows remain visible instead of one hiding the other. The archived
rename of 19 Oct had a gap of six hours.

Concurrency: ``store_records`` and deletion marking hold a shared transaction lock per data
type, an ``identity-backfill`` batch the exclusive one, and only one backfill runs at a time
(a session lock); see :func:`lock_data_type` and :func:`backfill_lock`.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import exists, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import as_utc
from app.models.identity import RawSourceChain, RawSourceSighting
from app.models.state import RawHealthData

CHAIN_GAP = timedelta(minutes=5)
# A chain is followed at most this far (it is a path: at most one successor each).
MAX_CHAIN = 64
# pg_advisory locks: (namespace, key). Per data type: (DATA_TYPE_LOCK, hashtext(name)).
DATA_TYPE_LOCK = 0x48454931  # "HEI1"
BACKFILL_LOCK = (0x48454932, 1)


class BackfillRunning(RuntimeError):
    """Another ``identity-backfill`` holds the single-run lock."""


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


def precedes(old: Span, new_first: datetime) -> bool:
    """``old`` was last seen more than ``CHAIN_GAP`` before ``new_first``."""
    return old.last + CHAIN_GAP < new_first


def gap_seconds(old: Span, new: Span) -> float:
    """Seconds from ``old``'s last to ``new``'s first sighting (negative: they overlap)."""
    return (new.first - old.last).total_seconds()


@dataclass
class Chains:
    """The recorded links of one data type and the spans they are checked against."""

    following: dict[str, str] = field(default_factory=dict)
    spans: dict[str, Span] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.preceding = {new: old for old, new in self.following.items()}

    def add(self, old: str, new: str) -> None:
        self.following[old] = new
        self.preceding[new] = old

    def valid(self, old: str, new: str) -> bool:
        """The link is recorded and no sighting contradicts it (unknown spans do not)."""
        if self.following.get(old) != new:
            return False
        before, after = self.spans.get(old), self.spans.get(new)
        return before is None or after is None or precedes(before, after.first)

    def root(self, fingerprint: str) -> str:
        """The first fingerprint of ``fingerprint``'s device, along valid links."""
        current = fingerprint
        for _ in range(MAX_CHAIN):
            previous = self.preceding.get(current)
            if previous is None or previous == fingerprint or not self.valid(previous, current):
                return current
            current = previous
        return current

    def members(self, fingerprint: str) -> list[str]:
        """The device's fingerprints in order, along valid links."""
        current = self.root(fingerprint)
        found = [current]
        for _ in range(MAX_CHAIN):
            following = self.following.get(current)
            if following is None or following in found or not self.valid(current, following):
                break
            found.append(following)
            current = following
        return found

    def position(self, fingerprint: str) -> int:
        members = self.members(fingerprint)
        return members.index(fingerprint) if fingerprint in members else 0

    def same_device(self, left: str, right: str) -> bool:
        return left == right or self.root(left) == self.root(right)

    def tail(self, fingerprint: str) -> str:
        return self.members(fingerprint)[-1]

    def reaches(self, start: str, target: str) -> bool:
        """``target`` follows ``start`` along recorded links (valid or not)."""
        current = start
        for _ in range(MAX_CHAIN):
            current = self.following.get(current, "")
            if not current:
                return False
            if current == target:
                return True
        return True  # longer than any chain may be: treat as a cycle


def eligible_predecessors(
    chains: Chains, new: str, new_first: datetime, devices: Iterable[str]
) -> list[str]:
    """Rule 1-4 of the module docstring: tails of ``devices`` (fingerprints sharing a key
    with ``new``) that may be ``new``'s predecessor."""
    if new in chains.preceding or new not in chains.spans or not chains.spans[new].complete:
        return []
    found: set[str] = set()
    for fingerprint in devices:
        if chains.same_device(fingerprint, new):
            continue
        tail = chains.tail(fingerprint)
        span = chains.spans.get(tail)
        if (
            span is None
            or not span.complete
            or tail in chains.following
            or span.stable != chains.spans[new].stable
            or chains.reaches(new, tail)
            or not precedes(span, new_first)
        ):
            continue
        found.add(tail)
    return sorted(found)


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


async def load_chains(
    session: AsyncSession, data_type: str, fingerprints: Iterable[str] | None = None
) -> Chains:
    """Every link of ``data_type``, and the spans of the linked and the given fingerprints
    (all spans of the data type when ``fingerprints`` is ``None``)."""
    links = (
        await session.execute(
            select(RawSourceChain.from_fingerprint, RawSourceChain.to_fingerprint).where(
                RawSourceChain.data_type == data_type
            )
        )
    ).tuples()
    chains = Chains(dict(links.all()))
    query = select(RawSourceSighting).where(RawSourceSighting.data_type == data_type)
    if fingerprints is not None:
        wanted = sorted({*fingerprints, *chains.following, *chains.preceding})
        if not wanted:
            return chains
        query = query.where(RawSourceSighting.fingerprint.in_(wanted))
    for row in (await session.scalars(query)).all():
        chains.spans[row.fingerprint] = _span(row)
    return chains


async def _history_known(session: AsyncSession, data_type: str) -> bool:
    """A span first seen now covers the archive: the backfill built this data type's spans,
    or the data type has no rows yet."""
    built = select(RawSourceSighting.fingerprint).where(
        RawSourceSighting.data_type == data_type, RawSourceSighting.complete.is_(True)
    )
    rows = select(RawHealthData.id).where(RawHealthData.data_type == data_type)
    return bool(await session.scalar(select(exists(built) | ~exists(rows))))


def _upsert(session: AsyncSession, values: list[dict], *, complete_wins: bool):
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
    ), values


async def record_sightings(
    session: AsyncSession,
    data_type: str,
    seen: Mapping[str, tuple[str, str, object]],
    now: datetime,
) -> None:
    """Note that the fingerprints ``seen`` (fingerprint -> stable class, label, dataSource)
    were received now."""
    if not seen:
        return
    known = set(
        (
            await session.scalars(
                select(RawSourceSighting.fingerprint).where(
                    RawSourceSighting.data_type == data_type,
                    RawSourceSighting.fingerprint.in_(sorted(seen)),
                )
            )
        ).all()
    )
    complete = await _history_known(session, data_type) if set(seen) - known else False
    values = [
        {
            "data_type": data_type,
            "fingerprint": fingerprint,
            "stable_class": stable,
            "label": label,
            "data_source": data_source,
            "first_seen_at": now,
            "last_seen_at": now,
            "complete": complete,
            "updated_at": now,
        }
        for fingerprint, (stable, label, data_source) in sorted(seen.items())
    ]
    # Sorted, so two writers of one data type lock the rows in the same order.
    statement, values = _upsert(session, values, complete_wins=False)
    await session.execute(statement, values)


async def write_spans(
    session: AsyncSession, data_type: str, spans: Mapping[str, Span], now: datetime
) -> None:
    """Merge spans built from the whole archive (they are complete from now on)."""
    if not spans:
        return
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
    statement, values = _upsert(session, values, complete_wins=True)
    for offset in range(0, len(values), 1000):
        await session.execute(statement, values[offset : offset + 1000])


def evidence(chains: Chains, old: str, new: str, shared_keys: int) -> str:
    before, after = chains.spans[old], chains.spans[new]
    return json.dumps(
        {
            "rule": "old last seen more than CHAIN_GAP before new first seen; unique candidate",
            "chain_gap_seconds": CHAIN_GAP.total_seconds(),
            "old": {"first": before.first.isoformat(), "last": before.last.isoformat()},
            "new": {"first": after.first.isoformat(), "last": after.last.isoformat()},
            "gap_seconds": gap_seconds(before, after),
            "shared_keys": shared_keys,
        },
        sort_keys=True,
    )


async def insert_chain(
    session: AsyncSession,
    data_type: str,
    chains: Chains,
    old: str,
    new: str,
    *,
    decided_by: str,
    shared_keys: int,
    now: datetime,
) -> bool:
    """Record ``old -> new`` (rule checked by the caller, under the data type's lock).

    Refused when it would fork a chain or close a cycle; ``False`` when another writer
    recorded a link for either fingerprint first."""
    from app.services.storage import dialect_insert

    if old in chains.following or new in chains.preceding or chains.reaches(new, old):
        return False
    statement = (
        dialect_insert(session, RawSourceChain.__table__)
        .values(
            data_type=data_type,
            from_fingerprint=old,
            to_fingerprint=new,
            from_label=chains.spans[old].label,
            to_label=chains.spans[new].label,
            decided_by=decided_by,
            evidence=evidence(chains, old, new, shared_keys),
            decided_at=now,
        )
        .on_conflict_do_nothing()
        .returning(RawSourceChain.from_fingerprint)
    )
    inserted = (await session.execute(statement)).scalar() is not None
    if inserted:
        chains.add(old, new)
    return inserted


# --- locks -------------------------------------------------------------------------------


def _key(data_type: str) -> int:
    # Same value on every process and server version (hashtext is not guaranteed stable).
    return zlib.crc32(data_type.encode()) - 2**31


async def lock_data_type(session: AsyncSession, data_type: str, *, exclusive: bool) -> None:
    """Transaction lock on ``data_type``'s identities: shared for writers of records and
    deletion marks, exclusive for an identity-backfill batch (PostgreSQL only)."""
    if session.get_bind().dialect.name != "postgresql":
        return
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await session.execute(
        text(f"SELECT {function}(:namespace, :key)"),
        {"namespace": DATA_TYPE_LOCK, "key": _key(data_type)},
    )


@asynccontextmanager
async def backfill_lock(sessions: async_sessionmaker):
    """Hold the single-run lock of identity-backfill (a session lock on its own connection,
    outside any transaction); refuse at once when another run holds it."""
    bind = sessions.kw.get("bind")
    if bind is None or bind.dialect.name != "postgresql":
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
