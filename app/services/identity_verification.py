"""Verify identity aliases, supersessions, source chains and archive integrity."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.timeutil import as_utc
from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession, in_force_sql
from app.models.state import RawHealthData
from app.parsers.common import google_name
from app.parsers.identity import reading, record_identity
from app.services.hollow_names import holds_hollow_row
from app.services.identity_schema import identity_tables_missing, verify_identity_schema
from app.services.source_chains import Chains

_ALIAS_MISMATCH_SQL = text(
    "SELECT a.data_type, a.identity_key FROM raw_health_data_identity a "
    "JOIN raw_health_data r ON r.id = a.raw_id "
    "WHERE r.data_type <> a.data_type OR r.google_data_point_id <> a.google_data_point_id"
)
_LINK_MISMATCH_SQL = text(
    "SELECT s.old_raw_id FROM raw_health_data_supersessions s "
    "JOIN raw_health_data o ON o.id = s.old_raw_id JOIN raw_health_data n ON n.id = s.new_raw_id "
    "WHERE o.data_type <> s.data_type OR n.data_type <> s.data_type "
    "OR o.google_data_point_id <> s.old_google_data_point_id "
    "OR n.google_data_point_id <> s.new_google_data_point_id"
)
# Every cycle of links, of any state (a cycle in force would hide all its rows).
_LINK_CYCLE_SQL = text(
    "WITH RECURSIVE walk(start_id, current_id, depth) AS ("
    " SELECT old_raw_id, new_raw_id, 1 FROM raw_health_data_supersessions"
    " UNION"
    " SELECT w.start_id, s.new_raw_id, w.depth + 1 FROM walk w"
    " JOIN raw_health_data_supersessions s ON s.old_raw_id = w.current_id"
    " WHERE w.depth < 64 AND w.current_id <> w.start_id)"
    " SELECT DISTINCT start_id FROM walk WHERE current_id = start_id"
)
# Live rows superseded only by links in force whose holders are all marked deleted: readers
# count them again.
_COUNTED_AGAIN_SQL = text(
    "SELECT count(DISTINCT s.old_raw_id) FROM raw_health_data_supersessions s "
    "JOIN raw_health_data o ON o.id = s.old_raw_id "
    f"WHERE o.source_deleted_at IS NULL AND {in_force_sql('s')} "
    "AND NOT EXISTS (SELECT 1 FROM raw_health_data_supersessions s2 "
    "JOIN raw_health_data h ON h.id = s2.new_raw_id WHERE s2.old_raw_id = s.old_raw_id "
    f"AND h.source_deleted_at IS NULL AND {in_force_sql('s2')})"
)
# Every identity-merged event (raw_health_data_events, guarded by 0011) has its link: a
# supersession table that was emptied or swapped for an empty copy shows up here.
_MERGED_WITHOUT_LINK_SQL = text(
    "SELECT e.raw_id FROM raw_health_data_events e WHERE e.event = 'identity-merged' "
    "AND NOT EXISTS (SELECT 1 FROM raw_health_data_supersessions s WHERE s.old_raw_id = e.raw_id)"
)


@dataclass
class _ChainRow:
    old: str
    new: str
    decided_by: str
    evidence: str
    created_at: datetime


async def _chain_rows(session: AsyncSession) -> dict[str, list[_ChainRow]]:
    from app.core.timeutil import as_utc
    from app.models.identity import RawSourceChain

    found: dict[str, list[_ChainRow]] = defaultdict(list)
    rows = await session.execute(
        select(
            RawSourceChain.data_type,
            RawSourceChain.from_fingerprint,
            RawSourceChain.to_fingerprint,
            RawSourceChain.decided_by,
            RawSourceChain.evidence,
            RawSourceChain.created_at,
        ).order_by(RawSourceChain.id)
    )
    for data_type, old, new, decided_by, evidence, created in rows.tuples():
        found[data_type].append(_ChainRow(old, new, decided_by, evidence, as_utc(created)))
    return found


async def _decision_rows(session: AsyncSession) -> dict[int, tuple]:
    from app.models.identity import RawSourceDecision

    rows = await session.execute(
        select(
            RawSourceDecision.id,
            RawSourceDecision.data_type,
            RawSourceDecision.decision,
            RawSourceDecision.first_fingerprint,
            RawSourceDecision.second_fingerprint,
        )
    )
    return {row[0]: tuple(row[1:]) for row in rows.tuples()}


async def verify_identities(session: AsyncSession) -> tuple[int, list[str], list[str]]:
    """Aliases, supersession links, source chains, devices and decisions are consistent.

    Returns (aliases, errors, notes). Besides the key checks: no links form a cycle (in any
    state), no link supersedes a record that carries its own Google id (but a row of a hollow
    name, by the row of its own record: app/services/hollow_names.py), every link joins two
    rows of one v2 key, and its two recorded fingerprints - and the rows' forms now - are one
    or were joined by recorded chain links (at any time: a later link may have given the
    holder a newer form); every identity-merged event still has its link. Every chain was
    decided by the reconciler on evidence that satisfies the rule (or by the owner, with the
    decision row), both its fingerprints have sightings, and those never narrowed since (they
    only widen); the live chains never fork or cycle; and ``raw_source_devices`` - what
    readers count by - says for every fingerprint the device its live chains make. Notes, not
    errors: chains rejected or refuted (their links no longer hide anything), links no longer
    in force, live superseded rows whose holders were marked deleted (readers count them
    again), and rows whose holder is itself superseded.
    """
    from app.models.identity import DECIDERS
    from app.services.identity_backfill import spec_for
    from app.services.raw_history import PROTECTIVE_TRIGGERS
    from app.services.source_chains import owner_decision_of, parse_evidence

    notes: list[str] = []
    missing = await identity_tables_missing(session)
    if missing:
        revision = None
        try:
            async with session.begin_nested():
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        except Exception:  # noqa: BLE001 - the revision is only for the message
            revision = None
        return (
            0,
            [
                f"the database is at revision {revision or 'unknown'}, without the record "
                f"identity tables of 20261021_0012 ({', '.join(missing)}): run `alembic upgrade "
                "head` (docs/RUNBOOK.md, 20261021_0012) before verifying with this version"
            ],
            notes,
        )
    guarded = {table for table, _, _ in PROTECTIVE_TRIGGERS}
    errors: list[str] = await verify_identity_schema(session, guarded)
    count = await session.scalar(select(func.count()).select_from(RawHealthDataIdentity)) or 0
    for data_type, key in (await session.execute(_ALIAS_MISMATCH_SQL)).tuples():
        errors.append(f"identity alias {data_type}/{key} does not match its raw row")
    for (raw_id,) in (await session.execute(_LINK_MISMATCH_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} does not match its rows")
    for (raw_id,) in (await session.execute(_LINK_CYCLE_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} forms a cycle")
    for (raw_id,) in (await session.execute(_MERGED_WITHOUT_LINK_SQL)).tuples():
        errors.append(
            f"raw row {raw_id} has an identity-merged event but no supersession link "
            "(the supersession table lost rows or was replaced)"
        )
    errors.extend(await _foreign_history(session))
    counted_again = await session.scalar(_COUNTED_AGAIN_SQL) or 0
    if counted_again:
        notes.append(
            f"{counted_again} superseded row(s) count again: every row that held each was "
            "marked deleted at the source while the row itself is live"
        )

    recorded = await _chain_rows(session)
    decisions = await _decision_rows(session)
    chains: dict[str, Chains] = {}
    for data_type in sorted(
        {*recorded, *(row[0] for row in decisions.values()), *await _device_types(session)}
    ):
        chains[data_type] = await _verify_chains(session, data_type)
    old_row, new_row = aliased(RawHealthData), aliased(RawHealthData)
    link = RawHealthDataSupersession
    links = (
        select(
            link.old_raw_id,
            link.new_raw_id,
            link.data_type,
            link.old_fingerprint,
            link.new_fingerprint,
            old_row.raw_json,
            new_row.raw_json,
            old_row.source_deleted_at,
            new_row.source_deleted_at,
            old_row.last_seen_at,
            new_row.last_seen_at,
            new_row.is_quarantined,
        )
        .join(old_row, old_row.id == link.old_raw_id)
        .join(new_row, new_row.id == link.new_raw_id)
    )
    differing = void = stale = 0
    # Rows superseded in force, and the rows they point at (a holder superseded itself).
    superseded_rows: set[uuid.UUID] = set()
    targets: set[uuid.UUID] = set()
    result = await session.stream(links.execution_options(yield_per=1_000))
    async for row in result:
        raw_id, holder_id, data_type, old_source, new_source = row[:5]
        old_json, new_json, old_deleted, new_deleted, old_seen, new_seen, held_aside = row[5:]
        spec = spec_for(data_type)
        if google_name(old_json) is not None:
            if old_source == new_source and holds_hollow_row(spec, old_json, new_json):
                # A row of a hollow name, held by the row of its own record (always in force).
                superseded_rows.add(raw_id)
                targets.add(holder_id)
                continue
            errors.append(f"supersession of raw row {raw_id} hides a record with a Google id")
        old, new = record_identity(spec, old_json), record_identity(spec, new_json)
        if old is None or new is None or old.match != new.match or not old.compatible(new):
            errors.append(f"supersession of raw row {raw_id} links two different records")
            continue
        current = chains.get(data_type) or Chains()
        if not (
            current.ever_joined(old_source, new_source)
            and current.ever_joined(old.source, old_source)
            and current.ever_joined(new.source, new_source)
        ):
            errors.append(f"supersession of raw row {raw_id} links two different sources")
        if not current.in_force(old_source, new_source):
            void += 1
            continue
        superseded_rows.add(raw_id)
        targets.add(holder_id)
        if old_deleted is None and new_deleted is None:
            if reading(spec, old_json) != reading(spec, new_json):
                differing += 1
            if (
                not held_aside
                and as_utc(old_seen) > as_utc(new_seen)
                and (reading(spec, old_json), old.shape) != (reading(spec, new_json), new.shape)
            ):
                stale += 1
    if differing:
        notes.append(
            f"{differing} live superseded row(s) hold a value their holder no longer has (an "
            "older value of the record; the next complete fetch of their window marks them)"
        )
    if stale:
        notes.append(
            f"{stale} superseded row(s) were returned after the row hiding them, with another "
            "reading or end: hidden behind a stale holder until a complete fetch of their window "
            "marks it (a fetch that was not complete leaves it)"
        )
    if void:
        notes.append(
            f"{void} supersession(s) are no longer in force (their two sources are no longer one "
            "device: a chain was rejected or refuted, or the owner decided them distinct); the "
            "rows they linked count on their own"
        )
    stacked = len(targets & superseded_rows)
    if stacked:
        notes.append(
            f"{stacked} holder row(s) are superseded themselves (readers follow both links)"
        )

    for data_type, current in sorted(chains.items()):
        conflicting = current.conflicting_devices()
        if conflicting:
            one, other = conflicting[0]
            # A note, never an error: links are append-only, so an error could never be cleared.
            notes.append(
                f"source chains of {data_type}: {len(conflicting)} pair(s) of sources with two "
                f"values of a device field are one device (e.g. {one[:12]} and {other[:12]}): "
                "no supersession joins their rows; reject the chain that joined the wrong form "
                "(identity-sources lists them)"
            )
        for problem in current.problems():
            errors.append(f"source chains of {data_type}: {problem} (a chain never forks)")
        wrong = current.devices_to_log()
        if wrong:
            errors.append(
                f"raw_source_devices of {data_type} does not match its live chains for "
                f"{len(wrong)} source(s) (e.g. {sorted(wrong)[0][:12]}): readers would count "
                "supersessions wrongly"
            )
    for data_type, rows in sorted(recorded.items()):
        current = chains[data_type]
        for row in rows:
            name = f"source chain of {data_type} {row.old[:12]} -> {row.new[:12]}"
            if row.decided_by not in DECIDERS:
                errors.append(f"{name} was not decided by the reconciler ({row.decided_by!r})")
            owner = row.decided_by == "owner"
            decided = parse_evidence(row.evidence, owner=owner)
            if decided is None:
                errors.append(f"{name} has no evidence that satisfies the chain rule")
            if owner:
                decision = decisions.get(owner_decision_of(row.evidence) or -1)
                if decision != (data_type, "link", row.old, row.new):
                    errors.append(f"{name} names no owner decision to link them")
            before, after = current.spans.get(row.old), current.spans.get(row.new)
            if before is None or after is None:
                errors.append(f"{name} has no sightings for both sources (they were removed)")
                continue
            name = f"source chain of {data_type} {before.label!r} -> {after.label!r}"
            if decided is not None and (
                before.first > decided[0].first
                or before.last < decided[0].last
                or after.first > decided[1].first
                or after.last < decided[1].last
            ):
                errors.append(f"{name}: the sightings narrowed since it was decided")
            if current.rejected(row.old, row.new):
                notes.append(
                    f"{name} was rejected by the owner: it is no longer used, and the rows it "
                    "linked count on their own unless another chain joins their sources"
                )
            elif current.refuted(row.old, row.new):
                notes.append(
                    f"{name} is refuted (two devices: different values for one record, or the "
                    "owner's decision): it is no longer used, and the rows it linked count on "
                    "their own"
                )
    for decision_id, (data_type, _, first, second) in sorted(decisions.items()):
        spans = chains[data_type].spans
        if first not in spans or second not in spans:
            errors.append(
                f"owner decision {decision_id} of {data_type} names a source without sightings"
            )
    for data_type, (doubled, by_one) in sorted((await _counted_twice(session, chains)).items()):
        if doubled:
            notes.append(
                f"{doubled} record(s) of {data_type} are counted more than once within one "
                "device (rows of one key and reading that no link in force joins yet): the next "
                "identity-backfill links them, unless they are listed under conflicts"
            )
        if by_one:
            notes.append(
                f"{by_one} record(s) of {data_type} have more than one counted row of one source "
                "(an older value counted beside the one returned last, which a reading of its "
                "own kept visible): the next complete fetch of their window marks the one no "
                "longer returned"
            )
    errors.extend(await _verify_agreement(session))
    if session.get_bind().dialect.name == "postgresql":
        notes.extend(await _index_notes(session))
    return count, errors, notes


_BUILD_DELETED_INDEX = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_raw_health_data_source_deleted ON "
    "raw_health_data (data_type, google_data_point_id) WHERE source_deleted_at IS NOT NULL"
)


async def _index_notes(session: AsyncSession) -> list[str]:
    """The partial index of 0012 missing, and every invalid index on raw_health_data (an
    interrupted concurrent build or rebuild, ``..._ccnew``), each with its statement."""
    notes = []
    valid = (await session.execute(_DELETED_INDEX_SQL)).scalar_one_or_none()
    if valid is None:
        notes.append(
            "index ix_raw_health_data_source_deleted is missing (the analytics views are slower "
            f"without it): build it with {_BUILD_DELETED_INDEX} when no dump or long "
            "transaction runs (docs/RUNBOOK.md)"
        )
    for (name,) in (await session.execute(_INVALID_INDEXES_SQL)).tuples():
        if name == "ix_raw_health_data_source_deleted":
            notes.append(
                f"index {name} is invalid (an interrupted concurrent build; it is not used): "
                f"rebuild it with REINDEX INDEX CONCURRENTLY {name} when no dump or long "
                "transaction runs (docs/RUNBOOK.md)"
            )
        else:
            notes.append(
                f"index {name} on raw_health_data is invalid (left by an interrupted concurrent "
                "build or REINDEX; it holds no data of its own and is not used, but writers "
                f"keep it up to date): DROP INDEX CONCURRENTLY {name} removes it, or REINDEX "
                f"INDEX CONCURRENTLY {name} rebuilds it (docs/RUNBOOK.md)"
            )
    return notes


async def _verify_agreement(session: AsyncSession) -> list[str]:
    """The agreement counts per pair (identity_reconcile_state.pair_agreement, which decide
    whether a pair is taken for two devices) are the latest rows of raw_source_agreements
    summed."""
    from app.models.identity import IdentityReconcileState
    from app.services.identity_backfill import summed_agreement
    from app.services.source_chains import parse_agreement

    summed = await summed_agreement(session)
    stored = {
        state.data_type: parse_agreement(state.pair_agreement)
        for state in (await session.scalars(select(IdentityReconcileState))).all()
    }
    errors = []
    for data_type in sorted(set(summed) | set(stored)):
        expected = summed.get(data_type, {})
        actual = {
            pair: (agree, disagree)
            for pair, (agree, disagree, _) in stored.get(data_type, {}).items()
            if agree or disagree
        }
        if expected != actual:
            errors.append(
                f"the agreement counts of {data_type} (identity_reconcile_state.pair_agreement) "
                "are not the sum of raw_source_agreements: they decide whether two sources are "
                "taken for two devices; run identity-backfill (a full run) to count them again"
            )
    return errors


async def _foreign_history(session: AsyncSession) -> list[str]:
    """A row without a Google id holds the payloads of one source only: a payload written onto
    another source's row would hide one source's history in another's."""
    from app.models.raw_versions import RawHealthDataVersion
    from app.services.identity_backfill import spec_for

    found: dict[str, list[uuid.UUID]] = defaultdict(list)
    if session.get_bind().dialect.name == "postgresql":
        for data_type, raw_id in (await session.execute(_FOREIGN_HISTORY_SQL)).tuples():
            found[data_type].append(raw_id)
    else:
        result = await session.stream(
            select(
                RawHealthData.data_type,
                RawHealthData.id,
                RawHealthData.raw_json,
                RawHealthDataVersion.raw_json,
            ).join(RawHealthDataVersion, RawHealthDataVersion.raw_id == RawHealthData.id)
        )
        async for data_type, raw_id, current, earlier in result:
            if google_name(current) or google_name(earlier):
                continue
            spec = spec_for(data_type)
            now, then = record_identity(spec, current), record_identity(spec, earlier)
            if now is not None and then is not None and now.source != then.source:
                found[data_type].append(raw_id)
    return [
        f"{len(set(raw_ids))} raw row(s) of {data_type} hold payloads of more than one source "
        f"in their history (e.g. {sorted(set(raw_ids), key=str)[0]}): one source's payload "
        "was written onto another source's row"
        for data_type, raw_ids in sorted(found.items())
    ]


async def _counted_twice(
    session: AsyncSession, chains: dict[str, Chains]
) -> dict[str, tuple[int, int]]:
    """Per data type, the records counted more than once within one device - rows of one key,
    one device (as the live chains make it) and one reading that readers all count - and
    the records with more than one counted row of one fingerprint (two values of one source
    at one instant: one of them is no longer returned)."""
    from app.models.eligibility import raw_counted
    from app.services.identity_backfill import spec_for

    counted = (raw_counted(), RawHealthData.source_start_time.is_not(None))
    shared = (
        select(RawHealthData.data_type, RawHealthData.source_start_time)
        .where(*counted)
        .group_by(RawHealthData.data_type, RawHealthData.source_start_time)
        .having(func.count() > 1)
        .subquery()
    )
    query = (
        select(RawHealthData.data_type, RawHealthData.source_start_time, RawHealthData.raw_json)
        .join(
            shared,
            (shared.c.data_type == RawHealthData.data_type)
            & (shared.c.source_start_time == RawHealthData.source_start_time),
        )
        .where(*counted)
        .order_by(RawHealthData.data_type, RawHealthData.source_start_time)
    )
    doubled: Counter = Counter()
    twice_by_one: Counter = Counter()
    group: Counter = Counter()
    by_source: Counter = Counter()
    current: tuple | None = None

    def close() -> None:
        if current is not None:
            doubled[current[0]] += sum(1 for count in group.values() if count > 1)
            twice_by_one[current[0]] += sum(1 for count in by_source.values() if count > 1)

    result = await session.stream(query.execution_options(yield_per=5_000))
    async for data_type, start, raw_json in result:
        if (data_type, start) != current:
            close()
            current, group, by_source = (data_type, start), Counter(), Counter()
        spec = spec_for(data_type)
        identity = record_identity(spec, raw_json)
        if identity is None:
            continue
        device = (chains.get(data_type) or Chains()).device(identity.source)
        group[(identity.match, device, reading(spec, raw_json))] += 1
        by_source[(identity.key, identity.source)] += 1
    close()
    return {
        data_type: (doubled[data_type], twice_by_one[data_type])
        for data_type in set(doubled) | set(twice_by_one)
        if doubled[data_type] or twice_by_one[data_type]
    }


async def _device_types(session: AsyncSession) -> list[str]:
    from app.models.identity import RawSourceDevice

    return list((await session.scalars(select(RawSourceDevice.data_type).distinct())).all())


async def _verify_chains(session: AsyncSession, data_type: str) -> Chains:
    from app.services.source_chains import load_chains

    return await load_chains(session, data_type)


# Rows whose earlier payloads came from another source than the current one (PostgreSQL;
# records with their own Google id may change source, every other row never does).
_FOREIGN_HISTORY_SQL = text(
    "SELECT DISTINCT r.data_type, r.id FROM raw_health_data_versions v "
    "JOIN raw_health_data r ON r.id = v.raw_id "
    "WHERE NOT (r.raw_json ? 'name' OR r.raw_json ? 'dataPointName' "
    "OR v.raw_json ? 'name' OR v.raw_json ? 'dataPointName') "
    # A missing dataSource and a JSON null one are the same source (one fingerprint).
    "AND COALESCE(v.raw_json -> 'dataSource', 'null'::jsonb) "
    "IS DISTINCT FROM COALESCE(r.raw_json -> 'dataSource', 'null'::jsonb)"
)
_INVALID_INDEXES_SQL = text(
    "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND i.indrelid = 'raw_health_data'::regclass "
    "AND NOT i.indisvalid ORDER BY c.relname"
)
_DELETED_INDEX_SQL = text(
    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = 'ix_raw_health_data_source_deleted'"
)
