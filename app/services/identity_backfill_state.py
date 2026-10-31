"""Checkpoint and deferred reconciliation state for identity backfills."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.identity import IdentityReconcileState
from app.services import source_chains
from app.services.identity_backfill_stats import BackfillStats
from app.services.source_chains import ordered_pair

# Re-read before the checkpoint for clock skew and transactions committed afterwards.
OVERLAP = timedelta(hours=1)
# Keep INSERTs far below PostgreSQL's 32,767 parameters.
INSERT_CHUNK = 1000
Notice = Callable[[str], None]


class Exclusive(Protocol):
    async def __call__[T](
        self,
        sessions: async_sessionmaker,
        data_type: str,
        work: Callable[[AsyncSession], Awaitable[T]],
        statement_timeout: str,
        notice: Notice | None = None,
        *,
        dry_run: bool = False,
    ) -> T: ...


async def _prepare(session: AsyncSession, statement_timeout: str) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout}'"))
        # The alias table grows from empty during the run. A plan made while its
        # statistics still say "empty" filters every row of the data type against every
        # key (minutes per batch at millions of rows): plan each lookup with the current
        # statistics, which ANALYZE keeps up to date.
        await session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))


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


def _chunked(values: Sequence, size: int = INSERT_CHUNK):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


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
