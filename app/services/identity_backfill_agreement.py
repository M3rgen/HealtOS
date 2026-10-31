"""Append-only per-key agreement counts for identity backfills."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.google_health.data_types import DataTypeSpec
from app.models.identity import IdentityReconcileState, RawSourceAgreement
from app.parsers.identity import RecordIdentity, pair_key, reading
from app.services import source_chains
from app.services.identity import among
from app.services.identity_backfill_reading import (
    _collect_starts,
    _from_micros,
    _Row,
    _rows_at,
    _rows_of,
)
from app.services.identity_backfill_state import OVERLAP, _chunked, _prepare, _reconcile_row
from app.services.identity_backfill_stats import BackfillStats, _fingerprints
from app.services.source_chains import (
    PAIR_KEYS,
    Chains,
    dump_agreement,
    load_chains,
    ordered_pair,
    parse_agreement,
)


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
            if not forms[left].kindred(forms[right]):
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
