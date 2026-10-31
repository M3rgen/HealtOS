"""Synchronization status and runtime-backed archive updates."""

from __future__ import annotations

import json

import typer
from sqlalchemy import func, select

from app.cli._context import context
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState


def status() -> None:
    """Show synchronization and backfill status."""

    async def command():
        async with context.sessions() as session:
            sync_rows = (
                await session.scalars(select(SyncState).order_by(SyncState.data_type))
            ).all()
            raw_count = await session.scalar(select(func.count(RawHealthData.id)))
            jobs = await session.execute(
                select(BackfillJob.status, func.count()).group_by(BackfillJob.status)
            )
            from app.models.identity import IdentityReconcileState
            from app.services.identity import identity_tables_missing
            from app.sync.deletions import deferred_deletions

            identity = []
            open_chains: list[dict] = []
            deferred: list[dict] = []
            if not await identity_tables_missing(session):
                identity = (
                    await session.scalars(
                        select(IdentityReconcileState).order_by(IdentityReconcileState.data_type)
                    )
                ).all()
                open_chains = await _chains_to_review(session)
                deferred = await deferred_deletions(session)
            return {
                "raw_records": raw_count,
                "sync": [
                    {
                        "data_type": row.data_type,
                        "status": row.status.value,
                        "last_successful_sync": str(row.last_successful_sync),
                    }
                    for row in sync_rows
                ],
                "backfill": {row[0].value: row[1] for row in jobs},
                # The identity reconciler (identity-backfill; nightly --incremental).
                "identity_reconcile": [
                    {
                        "data_type": row.data_type,
                        "full_run_at": str(row.full_run_at) if row.full_run_at else None,
                        "checkpoint_at": str(row.checkpoint_at) if row.checkpoint_at else None,
                        "last_finished_at": (
                            str(row.last_finished_at) if row.last_finished_at else None
                        ),
                        "last_mode": row.last_mode,
                        "last_result": json.loads(row.last_result) if row.last_result else None,
                        "last_error": row.last_error,
                    }
                    for row in identity
                ],
                # Chains the reconciler decided that the owner has not confirmed or rejected
                # yet (health-mirror identity-decide confirm|reject).
                "identity_review_suggested": open_chains,
                # Deletions complete fetches left undecided: a source that may be the
                # record's device renamed returned its reading (identity-decide).
                "identity_deferred_deletions": deferred,
            }

    typer.echo(json.dumps(context.run(command()), indent=2))


async def _chains_to_review(session) -> list[dict]:
    from app.models.identity import RawSourceChain
    from app.services.source_chains import load_chains

    found = []
    names = (await session.scalars(select(RawSourceChain.data_type).distinct())).all()
    for data_type in sorted(names):
        chains = await load_chains(session, data_type)
        for old, new in sorted(chains.links):
            if chains.review(old, new) == "suggested" and chains.live(old, new):
                found.append(
                    {
                        "data_type": data_type,
                        "from": chains.spans[old].label if old in chains.spans else old[:12],
                        "to": chains.spans[new].label if new in chains.spans else new[:12],
                        "fingerprints": [old[:12], new[:12]],
                        "decided_by": chains.decided_by.get((old, new)),
                    }
                )
    return found


def backfill(
    data_type: str | None = typer.Option(None, "--data-type"),
    reset_abandoned: bool = typer.Option(
        False,
        "--reset-abandoned",
        help="Requeue abandoned windows (of --data-type, or all) with a fresh retry count first.",
    ),
) -> None:
    """Start or resume the historical backfill and wait for completion."""
    runtime = context.runtime()

    async def command():
        try:
            if reset_abandoned:
                reset = await runtime.backfill.reset_abandoned([data_type] if data_type else None)
                typer.echo(f"Requeued {reset} abandoned backfill window(s)")
            completed = await runtime.backfill.run([data_type] if data_type else None)
            async with context.sessions() as session:
                query = select(BackfillJob).where(
                    BackfillJob.status.in_([JobStatus.failed, JobStatus.abandoned])
                )
                if data_type:
                    query = query.where(BackfillJob.data_type == data_type)
                failed = (await session.scalars(query)).all()
            return completed, failed
        finally:
            await runtime.shutdown()

    completed, failed = context.run(command())
    typer.echo(f"Completed {completed} backfill windows")
    for job in failed:
        typer.echo(
            f"backfill {job.status.value}: {job.data_type} {job.window_start}: {job.last_error}",
            err=True,
        )
    if failed:
        raise typer.Exit(1)


def sync(data_type: str | None = typer.Argument(None)) -> None:
    """Run incremental synchronization for one or every configured data type."""
    runtime = context.runtime()

    async def command():
        try:
            if data_type:
                return await runtime.incremental.run(data_type)
            return await runtime.incremental.run_all()
        finally:
            await runtime.shutdown()

    result = context.run(command())
    typer.echo(str(result))
    if isinstance(result, dict) and any(isinstance(item, Exception) for item in result.values()):
        raise typer.Exit(1)


def reconcile(days: int = typer.Option(7, min=1, max=3_650)) -> None:
    """Re-read a recent window across every mirrored data type."""
    runtime = context.runtime()

    async def command():
        try:
            return await runtime.reconciliation.run(days)
        finally:
            await runtime.shutdown()

    result = context.run(command())
    typer.echo(str(result))
    if any(isinstance(item, Exception) for item in result.values()):
        raise typer.Exit(1)


def probe(days: int = typer.Option(30, min=1, max=365)) -> None:
    """Probe accessible Google Health types without archiving records or a backfill.

    The probe's Google responses are kept in raw_api_responses (source "probe").
    """
    runtime = context.runtime()

    async def command():
        try:
            return await runtime.discovery.probe(days)
        finally:
            await runtime.shutdown()

    typer.echo(json.dumps(context.run(command()), indent=2, default=str))
