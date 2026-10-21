"""The owner's decisions about source fingerprints (``health-mirror identity-decide``).

The reconciler decides a chain only when the sightings leave no doubt
(app/services/source_chains.py); everything else is listed as ``ambiguous``, ``kept_apart`` or
``review_distinct`` in its report and in ``health-mirror status``, and every chain it decided is
listed under ``review`` until the owner confirms or rejects it. This module records what the
owner decides, append-only (``raw_source_decisions``, with the time and an optional note), and
applies it:

* ``link FROM TO``: the two fingerprints are one device (``FROM`` its earlier form). A chain
  ``FROM -> TO`` is recorded (``decided_by = owner``, naming the decision), or a chain recorded
  before and rejected is made live again; the whole data type is then settled again, so the
  rows of both forms are linked (equal readings only).
* ``reject FROM TO``: the chain ``FROM -> TO`` is wrong. It is no longer used: the links made
  under it no longer hide anything (readers count both sides again at once), each form gets
  rows of its own from now on, and both fingerprints are free for another chain.
* ``confirm FROM TO``: reviewed, the chain is right (it leaves the review list; a rejected
  one is live again).
* ``distinct A B``: two devices, whatever the evidence: a chain between them is no longer used
  (as after ``reject``).
* ``not-distinct A B``: ignore the evidence of two devices between them (a stray duplicate):
  a chain between them refuted by that evidence is live again; the whole data type is settled
  again.

The latest decision per question wins, so every decision is undone by the opposite one, in
what readers count too: supersessions are in force only while their two fingerprints are one
device (app/models/identity.py, ``in_force``). A decision that would make two live chains fork
or cycle is refused (reject the other chain first). ``--dry-run`` writes nothing, takes no
lock that writers wait for, and reports what the decision would change. Fingerprints are given
by their first characters as the report prints them (``fingerprints``; 12 are shown, any
unique prefix of at least 6 is accepted). ``verify`` checks that every chain the owner linked
names its decision.

A deletion deferred because a fingerprint might have been the device renamed is decided by the
next complete fetch of its window after the decision (the decision itself fetches nothing): the
outcome's ``deletions`` says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import event
from app.models.identity import CHAIN_DECISIONS, DECISIONS, RawHealthDataSupersession
from app.services import source_chains
from app.services.identity_backfill import (
    STATEMENT_TIMEOUT,
    BackfillStats,
    Notice,
    _full,
    _report,
    analyze_identity_tables,
    relink_data_type,
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
# the others too when they join two devices - ``confirm`` of a rejected chain - or let the
# rule decide a chain).
RELINK = ("link", "not-distinct")


class DecisionRefused(ValueError):
    """The decision does not apply (unknown source, wrong class, a fork, ...)."""


def match(chains: Chains, prefix: str, data_type: str = "") -> str:
    """The fingerprint of the data type that starts with ``prefix``."""
    prefix = prefix.strip().lower()
    if len(prefix) < MIN_PREFIX:
        raise DecisionRefused(f"give at least {MIN_PREFIX} characters of the fingerprint")
    if not chains.spans:
        raise DecisionRefused(
            f"no source of {data_type or 'this data type'} has been seen: check --data-type "
            "(a Google data type name like heart-rate, as `health-mirror status` lists them) "
            "or run identity-backfill first"
        )
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
    recorded = (first, second) in chains.links
    if decision in ("reject", "confirm") and not recorded:
        raise DecisionRefused(f"there is no chain {first[:12]} -> {second[:12]} to {decision}")
    pair = _pair(first, second)
    if decision in ("link", "confirm"):
        if chains.pair_decisions.get(pair) == "distinct":
            raise DecisionRefused(
                "you decided these two sources are distinct devices; if they are one device, "
                "decide not-distinct first"
            )
        if chains.pair_decisions.get(pair) is None and (
            source_chains.established(chains.evidence.get(pair, ()))
            or (first, second) in chains.refuted_events
        ):
            raise DecisionRefused(
                "these two sources returned different values for one record in separate "
                "fetches (two devices); if you are sure they are one device, decide "
                "not-distinct first"
            )
        apart = sorted(
            (one, other)
            for one in chains.members(first)
            for other in chains.members(second)
            if (one, other) != (first, second)
            and _pair(one, other) != pair
            and chains.pair_distinct(one, other)
        )
        if apart and not chains.same_device(first, second):
            one, other = apart[0]
            raise DecisionRefused(
                f"{one[:12]} and {other[:12]} are distinct devices, and they are forms of these "
                "two sources' devices; if they are one device, decide not-distinct for them first"
            )
        if chains.same_device(first, second) and not chains.live(first, second):
            raise DecisionRefused(
                f"{first[:12]} and {second[:12]} are already one device through other chains"
            )
    if decision == "distinct" and chains.same_device(first, second):
        direct = [link for link in ((first, second), (second, first)) if chains.live(*link)]
        if not direct:
            raise DecisionRefused(
                "these two sources are one device through other chains: reject the wrong "
                "chain between them instead"
            )
    if decision in ("link", "confirm", "not-distinct"):
        planned = chains.copy()
        planned.decide(decision, first, second)
        if decision == "link" and not recorded:
            planned.add(first, second)
        problems = planned.problems()
        if problems:
            following, preceding = chains.following, chains.preceding
            other = following.get(first) if following.get(first) not in (None, second) else None
            earlier = preceding.get(second) if preceding.get(second) not in (None, first) else None
            if other is not None:
                reason = (
                    f"{first[:12]} already continues as {other[:12]} (a live chain; a chain "
                    "never forks): reject that chain first"
                )
            elif earlier is not None:
                reason = (
                    f"{second[:12]} already continues {earlier[:12]} (a live chain): reject "
                    "that chain first"
                )
            else:
                reason = "; ".join(problems)
            raise DecisionRefused(f"the chains would no longer be chains: {reason}")


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
    # The two sources' chain after the decision (live, rejected or refuted), when recorded.
    chain: str | None = None
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
            "chain": self.chain,
            "links_between_their_rows": self.links_between,
            "deletions": (
                "a deletion left undecided because one of these sources might be the other "
                "renamed is decided by the next complete fetch of its window (the daily "
                "deletion check, or `health-mirror backfill` of that window)"
            ),
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
                    "review_distinct",
                    "refuted",
                )
            },
        }


async def _links_between(session: AsyncSession, data_type: str, first: str, second: str) -> int:
    """Supersessions between rows of the two sources (any state)."""
    link = RawHealthDataSupersession
    return (
        await session.scalar(
            select(func.count()).where(
                link.data_type == data_type,
                or_(
                    and_(link.old_fingerprint == first, link.new_fingerprint == second),
                    and_(link.old_fingerprint == second, link.new_fingerprint == first),
                ),
            )
        )
        or 0
    )


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
    first = match(chains, first_prefix, data_type)
    second = match(chains, second_prefix, data_type)
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

    joined: list[bool] = []

    async def apply(session: AsyncSession, current: Chains) -> None:
        # Under the exclusive lock: the chains may have changed since they were read.
        check(current, decision, first, second)
        before = current.members(first)
        joined.append(False)
        if dry_run:
            current.decide(decision, first, second)
            if decision == "link" and (first, second) not in current.links:
                current.add(first, second)
            joined[-1] = current.members(first) > before
            return
        now = await source_chains.database_now(session)
        outcome.decision_id = await record_decision(
            session, data_type, current, decision, first, second, note=note, now=now
        )
        if decision == "link" and (first, second) not in current.links:
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
        joined[-1] = current.members(first) > before
        event(
            logger,
            "SOURCE_DECISION_RECORDED",
            data_type=data_type,
            decision=decision,
            first=current.spans[first].label,
            second=current.spans[second].label,
            decision_id=outcome.decision_id,
        )

    async with backfill_lock(sessions, dry_run=dry_run):
        outcome.stats = await relink_data_type(
            sessions,
            data_type,
            apply,
            link=lambda: decision in RELINK or any(joined),
            batch_size=batch_size,
            dry_run=dry_run,
            statement_timeout=statement_timeout,
            progress=progress,
            notice=notice,
        )
        if not dry_run:
            await analyze_identity_tables(sessions)
    for entry in outcome.stats.chains:
        if _full(chains, entry) == [first, second]:
            outcome.chain = entry.get("state")
    return outcome


async def list_sources(sessions: async_sessionmaker, data_type: str) -> dict[str, Any]:
    """Every source fingerprint of the data type with its span, device, chain links (with
    their review state), established-distinct pairs and the owner's decisions; plus what the
    chain rule would leave open now."""
    async with sessions() as session:
        chains = await load_chains(session, data_type)
    planned = chains.copy()
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
        "review_distinct": stats.review_distinct,
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
