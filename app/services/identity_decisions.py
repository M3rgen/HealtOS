"""The owner's decisions about source fingerprints (``health-mirror identity-decide``).

The reconciler decides a chain only when the sightings leave no doubt
(app/services/source_chains.py); everything else is listed as ``ambiguous`` or ``kept_apart``
in its report and in ``health-mirror status``, and every chain it decided is listed under
``review`` until the owner confirms or rejects it. This module records what the owner decides,
append-only (``raw_source_decisions``, with the time and an optional note), and applies it:

* ``link FROM TO``: the two fingerprints are one device (``FROM`` its earlier form). A chain
  ``FROM -> TO`` is recorded (``decided_by = owner``, naming the decision); the whole data type
  is then settled again, so the rows of both forms are linked (equal readings only) and the
  marks that were deferred between them are decided by the next complete fetch.
* ``reject FROM TO``: the chain ``FROM -> TO`` is wrong. It is no longer used; the rows it
  linked stay linked (they measured the same), and from now on each form gets rows of its own.
* ``confirm FROM TO``: reviewed, the chain is right (it leaves the review list).
* ``distinct A B``: two devices, whatever the evidence (a chain between them is no longer used).
* ``not-distinct A B``: ignore the evidence of two devices between them (a stray duplicate);
  the whole data type is settled again.

The latest decision per question wins (a ``link`` after a ``reject`` uses the chain again).
``--dry-run`` writes nothing and reports what the decision would change. Fingerprints are
given by their first characters as the report prints them (``fingerprints``; 12 are shown,
any unique prefix of at least 6 is accepted). ``verify`` checks that every chain the owner
linked names its decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.logging import event
from app.models.identity import CHAIN_DECISIONS, DECISIONS, RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.identity import record_identity
from app.services import source_chains
from app.services.identity_backfill import (
    STATEMENT_TIMEOUT,
    BackfillStats,
    Notice,
    _report,
    analyze_identity_tables,
    relink_data_type,
    spec_for,
)
from app.services.source_chains import (
    Chains,
    backfill_lock,
    decide_chains,
    insert_chain,
    load_chains,
    record_decision,
)

logger = logging.getLogger(__name__)

MIN_PREFIX = 6
# Decisions after which rows may need linking: the whole data type is settled again (after
# the others too when they let the rule decide a chain).
RELINK = ("link", "not-distinct")


class DecisionRefused(ValueError):
    """The decision does not apply (unknown source, wrong class, a fork, ...)."""


def match(chains: Chains, prefix: str) -> str:
    """The fingerprint of the data type that starts with ``prefix``."""
    prefix = prefix.strip().lower()
    if len(prefix) < MIN_PREFIX:
        raise DecisionRefused(f"give at least {MIN_PREFIX} characters of the fingerprint")
    found = [name for name in chains.spans if name.lower().startswith(prefix)]
    if not found:
        raise DecisionRefused(f"no source of this data type has a fingerprint {prefix}...")
    if len(found) > 1:
        raise DecisionRefused(f"{prefix}... names {len(found)} sources; give more characters")
    return found[0]


def check(chains: Chains, decision: str, first: str, second: str) -> None:
    """Refuse a decision that does not apply to the chains as they are."""
    if decision not in DECISIONS:
        raise DecisionRefused(f"unknown decision {decision!r} (one of {', '.join(DECISIONS)})")
    if first == second:
        raise DecisionRefused("the two fingerprints are the same")
    if chains.spans[first].stable != chains.spans[second].stable:
        raise DecisionRefused(
            "the two sources differ in their stable fields (platform, recording method, device "
            "form factor or manufacturer, package without suffix): they never share a record"
        )
    linked = chains.following.get(first) == second
    if decision in ("reject", "confirm") and not linked:
        raise DecisionRefused(f"there is no chain {first[:12]} -> {second[:12]} to {decision}")
    if decision == "link" and not linked:
        if first in chains.following:
            raise DecisionRefused(
                f"{first[:12]} already continues as {chains.following[first][:12]} (a chain "
                "never forks): reject that chain first"
            )
        if second in chains.preceding:
            raise DecisionRefused(
                f"{second[:12]} already continues {chains.preceding[second][:12]}: reject that "
                "chain first"
            )
        if chains.reaches(second, first):
            raise DecisionRefused("linking them would close a cycle of chains")
    if (
        decision == "link"
        and len(chains.evidence.get(_pair(first, second), ())) >= (source_chains.PAIR_KEYS)
        and chains.pair_decisions.get(_pair(first, second)) != "not-distinct"
    ):
        raise DecisionRefused(
            "these two sources returned different values for one record "
            f"{len(chains.evidence[_pair(first, second)])} or more times (two devices); if you "
            "are sure they are one device, decide not-distinct first"
        )


def _pair(first: str, second: str) -> tuple[str, str]:
    return source_chains.ordered_pair(first, second)


@dataclass
class Outcome:
    decision: str
    data_type: str
    first: dict
    second: dict
    dry_run: bool
    decision_id: int | None = None
    # Supersession links between rows of the two sources (they stay whatever is decided).
    links_between: int = 0
    stats: BackfillStats = field(default_factory=BackfillStats)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "decision": self.decision,
            "data_type": self.data_type,
            "from": self.first,
            "to": self.second,
            "decision_id": self.decision_id,
            "links_between_their_rows": self.links_between,
            "effect": {
                name: value
                for name, value in self.stats.as_dict().items()
                if name
                in (
                    "superseded",
                    "linked",
                    "kept_differing",
                    "conflicts",
                    "deletions_kept",
                    "deletions_suspicious",
                    "chains",
                    "review",
                    "ambiguous",
                    "kept_apart",
                    "refuted",
                )
            },
        }


async def _links_between(session: AsyncSession, data_type: str, first: str, second: str) -> int:
    spec = spec_for(data_type)
    old_row, new_row = aliased(RawHealthData), aliased(RawHealthData)
    rows = await session.stream(
        select(old_row.raw_json, new_row.raw_json)
        .select_from(RawHealthDataSupersession)
        .join(old_row, old_row.id == RawHealthDataSupersession.old_raw_id)
        .join(new_row, new_row.id == RawHealthDataSupersession.new_raw_id)
        .where(RawHealthDataSupersession.data_type == data_type)
        .execution_options(yield_per=1_000)
    )
    wanted = {first, second}
    found = 0
    async for old_json, new_json in rows:
        old, new = record_identity(spec, old_json), record_identity(spec, new_json)
        if old is not None and new is not None and {old.source, new.source} == wanted:
            found += 1
    return found


async def decide_sources(
    sessions: async_sessionmaker,
    data_type: str,
    decision: str,
    first_prefix: str,
    second_prefix: str,
    *,
    note: str | None = None,
    dry_run: bool = False,
    batch_size: int = 5_000,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress=None,
    notice: Notice | None = None,
) -> Outcome:
    """Record and apply one owner decision (see the module docstring). Raises
    ``DecisionRefused`` (nothing written) or ``BackfillRunning``."""
    async with sessions() as session:
        chains = await load_chains(session, data_type)
    first, second = match(chains, first_prefix), match(chains, second_prefix)
    check(chains, decision, first, second)
    outcome = Outcome(
        decision,
        data_type,
        source_chains.describe(chains, first),
        source_chains.describe(chains, second),
        dry_run,
    )
    async with sessions() as session:
        outcome.links_between = await _links_between(session, data_type, first, second)

    async def apply(session: AsyncSession, current: Chains) -> None:
        # Under the exclusive lock: the chains may have changed since they were read.
        check(current, decision, first, second)
        if dry_run:
            current.decide(decision, first, second)
            if decision == "link" and current.following.get(first) != second:
                current.add(first, second)
            return
        now = await source_chains.database_now(session)
        outcome.decision_id = await record_decision(
            session, data_type, current, decision, first, second, note=note, now=now
        )
        if decision == "link" and current.following.get(first) != second:
            inserted = await insert_chain(
                session,
                data_type,
                current,
                first,
                second,
                decided_by="owner",
                now=now,
                decision_id=outcome.decision_id,
            )
            if not inserted:
                raise DecisionRefused("another chain of these sources was recorded meanwhile")
        event(
            logger,
            "SOURCE_DECISION_RECORDED",
            data_type=data_type,
            decision=decision,
            first=current.spans[first].label,
            second=current.spans[second].label,
            decision_id=outcome.decision_id,
        )

    async with backfill_lock(sessions):
        outcome.stats = await relink_data_type(
            sessions,
            data_type,
            apply,
            link=decision in RELINK,
            batch_size=batch_size,
            dry_run=dry_run,
            statement_timeout=statement_timeout,
            progress=progress,
            notice=notice,
        )
        if not dry_run:
            await analyze_identity_tables(sessions)
    return outcome


async def list_sources(sessions: async_sessionmaker, data_type: str) -> dict[str, Any]:
    """Every source fingerprint of the data type with its span, device, chain links (with
    their review state), established-distinct pairs and the owner's decisions; plus what the
    chain rule would leave open now."""
    async with sessions() as session:
        chains = await load_chains(session, data_type)
    planned = Chains(
        dict(chains.following),
        spans=chains.spans,
        decided_by=dict(chains.decided_by),
        evidence={pair: set(keys) for pair, keys in chains.evidence.items()},
        chain_decisions=dict(chains.chain_decisions),
        pair_decisions=dict(chains.pair_decisions),
        refuted_events=set(chains.refuted_events),
    )
    decision = decide_chains(planned)
    stats = BackfillStats(mode="sources")
    _report(chains, None, set(), decision.ambiguous, [], stats)
    sources = []
    for fingerprint, span in sorted(chains.spans.items(), key=lambda item: item[1].first):
        sources.append(
            {
                "fingerprint": fingerprint[:12],
                "source": span.label,
                "first_seen": span.first.isoformat(),
                "last_seen": span.last.isoformat(),
                "complete": span.complete,
                "device": sorted(
                    name[:12] for name in chains.members(fingerprint) if name != fingerprint
                ),
                "distinct_from": sorted(
                    other[:12] for other in chains.spans if chains.distinct(fingerprint, other)
                ),
            }
        )
    return {
        "data_type": data_type,
        "sources": sources,
        "chains": stats.chains,
        "review": stats.review,
        "kept_apart": stats.kept_apart,
        "would_link": [
            {"from": chains.spans[old].label, "to": chains.spans[new].label}
            for old, new in decision.links
        ],
        "ambiguous": decision.ambiguous,
        "decisions": [
            {"question": [name[:12] for name in question], "decision": value}
            for question, value in sorted(
                {**chains.chain_decisions, **chains.pair_decisions}.items()
            )
        ],
        "chain_decisions": list(CHAIN_DECISIONS),
    }
