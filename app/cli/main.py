from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import typer
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.state import (
    BackfillJob,
    JobStatus,
    RawHealthData,
    SyncState,
    WebhookEvent,
    WebhookNotification,
)
from app.services.runtime import build_runtime

app = typer.Typer(help="Operate the Personal Health Data Mirror.", no_args_is_help=True)


def run(coroutine):
    return asyncio.run(coroutine)


@app.command()
def status() -> None:
    """Show synchronization and backfill status."""

    async def command():
        async with SessionLocal() as session:
            sync_rows = (
                await session.scalars(select(SyncState).order_by(SyncState.data_type))
            ).all()
            raw_count = await session.scalar(select(func.count(RawHealthData.id)))
            jobs = await session.execute(
                select(BackfillJob.status, func.count()).group_by(BackfillJob.status)
            )
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
            }

    typer.echo(json.dumps(run(command()), indent=2))


@app.command()
def backfill(
    data_type: str | None = typer.Option(None, "--data-type"),
    reset_abandoned: bool = typer.Option(
        False,
        "--reset-abandoned",
        help="Requeue abandoned windows (of --data-type, or all) with a fresh retry count first.",
    ),
) -> None:
    """Start or resume the historical backfill and wait for completion."""
    runtime = build_runtime(get_settings(), SessionLocal)

    async def command():
        try:
            if reset_abandoned:
                reset = await runtime.backfill.reset_abandoned([data_type] if data_type else None)
                typer.echo(f"Requeued {reset} abandoned backfill window(s)")
            completed = await runtime.backfill.run([data_type] if data_type else None)
            async with SessionLocal() as session:
                query = select(BackfillJob).where(
                    BackfillJob.status.in_([JobStatus.failed, JobStatus.abandoned])
                )
                if data_type:
                    query = query.where(BackfillJob.data_type == data_type)
                failed = (await session.scalars(query)).all()
            return completed, failed
        finally:
            await runtime.shutdown()

    completed, failed = run(command())
    typer.echo(f"Completed {completed} backfill windows")
    for job in failed:
        typer.echo(
            f"backfill {job.status.value}: {job.data_type} {job.window_start}: {job.last_error}",
            err=True,
        )
    if failed:
        raise typer.Exit(1)


@app.command()
def sync(data_type: str | None = typer.Argument(None)) -> None:
    """Run incremental synchronization for one or every configured data type."""
    runtime = build_runtime(get_settings(), SessionLocal)

    async def command():
        try:
            if data_type:
                return await runtime.incremental.run(data_type)
            return await runtime.incremental.run_all()
        finally:
            await runtime.shutdown()

    result = run(command())
    typer.echo(str(result))
    if isinstance(result, dict) and any(isinstance(item, Exception) for item in result.values()):
        raise typer.Exit(1)


@app.command()
def reconcile(days: int = typer.Option(7, min=1, max=3_650)) -> None:
    """Re-read a recent window across every mirrored data type."""
    runtime = build_runtime(get_settings(), SessionLocal)

    async def command():
        try:
            return await runtime.reconciliation.run(days)
        finally:
            await runtime.shutdown()

    result = run(command())
    typer.echo(str(result))
    if any(isinstance(item, Exception) for item in result.values()):
        raise typer.Exit(1)


@app.command()
def webhook_readiness() -> None:
    """Report secret-safe Google Health subscriber prerequisites."""
    typer.echo(json.dumps(get_settings().webhook_readiness, indent=2))


@app.command()
def webhook_status() -> None:
    """Show secret-safe local webhook receipt and processing status."""

    async def command():
        async with SessionLocal() as session:
            event_count = await session.scalar(select(func.count(WebhookEvent.payload_hash)))
            pending_count = await session.scalar(
                select(func.count(WebhookEvent.payload_hash)).where(
                    WebhookEvent.processed_at.is_(None)
                )
            )
            last_received = await session.scalar(select(func.max(WebhookEvent.created_at)))
            last_processed = await session.scalar(select(func.max(WebhookEvent.processed_at)))
            notification_count = await session.scalar(
                select(func.count(WebhookNotification.element_hash))
            )
            notification_states = await session.execute(
                select(WebhookNotification.status, func.count()).group_by(
                    WebhookNotification.status
                )
            )
            sync_attempts = await session.scalar(
                select(func.coalesce(func.sum(WebhookNotification.sync_attempt_count), 0))
            )
            sync_successes = await session.scalar(
                select(func.coalesce(func.sum(WebhookNotification.sync_success_count), 0))
            )
        return {
            "readiness": get_settings().webhook_readiness,
            "received_events": event_count,
            "pending_events": pending_count,
            "last_received_at": str(last_received) if last_received else None,
            "last_processed_at": str(last_processed) if last_processed else None,
            "notification_elements": notification_count,
            "notification_states": {row[0]: row[1] for row in notification_states},
            "incremental_sync_attempts": sync_attempts,
            "incremental_sync_successes": sync_successes,
        }

    typer.echo(json.dumps(run(command()), indent=2))


@app.command()
def webhook_replay_pending() -> None:
    """Retry durable pending webhook elements without asking Google to resend them."""

    runtime = build_runtime(get_settings(), SessionLocal)

    async def command():
        try:
            return await runtime.webhook_processor.process_pending()
        finally:
            await runtime.shutdown()

    result = run(command())
    typer.echo(json.dumps(result.__dict__, indent=2))
    if result.failed_elements:
        raise typer.Exit(1)


@app.command()
def verify() -> None:
    """Check raw uniqueness, payload hashes, version history, and checkpoint errors."""

    async def command():
        from app.parsers.common import payload_hash
        from app.services.raw_history import verify_versions

        errors = []
        count = 0
        async with SessionLocal() as session:
            rows = await session.stream(
                select(
                    RawHealthData.data_type,
                    RawHealthData.google_data_point_id,
                    RawHealthData.raw_json,
                    RawHealthData.payload_hash,
                ).execution_options(yield_per=1_000)
            )
            async for data_type, google_data_point_id, raw_json, expected_hash in rows:
                count += 1
                if payload_hash(raw_json) != expected_hash:
                    errors.append(f"payload hash mismatch: {data_type}/{google_data_point_id}")
            versions, version_errors = await verify_versions(session)
            errors.extend(version_errors)
            failed = (
                await session.scalars(select(SyncState).where(SyncState.last_error.is_not(None)))
            ).all()
        return count, versions, errors, failed

    count, versions, errors, failed = run(command())
    typer.echo(f"Verified {count} raw records and {versions} earlier versions")
    for error in errors:
        typer.echo(error, err=True)
    for state in failed:
        typer.echo(f"sync error: {state.data_type}: {state.last_error}", err=True)
    if errors or failed:
        raise typer.Exit(1)


SINCE_OPTION = typer.Option(
    None,
    "--since",
    formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
    help="Only records replaced at or after this UTC date/time.",
)


@app.command()
def history(
    data_type: str = typer.Option(..., "--data-type", help="Google data type, e.g. heart-rate."),
    google_id: str | None = typer.Option(
        None, "--id", help="One record's google_data_point_id (shown even with one version)."
    ),
    since: datetime | None = SINCE_OPTION,
    limit: int = typer.Option(20, min=1, max=10_000, help="Most recently changed records."),
    payload: bool = typer.Option(True, "--payload/--no-payload", help="Include raw payloads."),
) -> None:
    """List every stored version of raw records (oldest first) as JSON."""
    from app.services.raw_history import record_history

    async def command():
        async with SessionLocal() as session:
            return await record_history(
                session,
                data_type,
                google_id=google_id,
                since=since.replace(tzinfo=UTC) if since else None,
                limit=limit,
                include_payload=payload,
            )

    records = run(command())
    typer.echo(json.dumps(records, indent=2, ensure_ascii=False))
    if google_id is not None and not records:
        typer.echo(f"no record {data_type}/{google_id}", err=True)
        raise typer.Exit(1)


@app.command()
def import_versions(
    from_db: str = typer.Option(
        ...,
        "--from-db",
        help="Restored archive: a database name on the live server, or a full URL.",
    ),
    batch_size: int = typer.Option(2_000, min=1, max=50_000),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count only; write nothing."),
) -> None:
    """Add payload versions that only survive in an old dump; current rows stay as they are."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.services.raw_history import import_versions as run_import
    from app.services.raw_history import source_database_url

    url = source_database_url(from_db, get_settings().database_url)

    async def command():
        source = create_async_engine(url)
        try:
            return await run_import(SessionLocal, source, batch_size=batch_size, dry_run=dry_run)
        finally:
            await source.dispose()

    stats = run(command())
    typer.echo(json.dumps({"dry_run": dry_run, **stats.as_dict()}, indent=2))
    if stats.source_hash_mismatches:
        raise typer.Exit(1)


@app.command()
def probe(days: int = typer.Option(30, min=1, max=365)) -> None:
    """Probe accessible Google Health types without storing or starting a backfill."""
    runtime = build_runtime(get_settings(), SessionLocal)

    async def command():
        try:
            return await runtime.discovery.probe(days)
        finally:
            await runtime.shutdown()

    typer.echo(json.dumps(run(command()), indent=2, default=str))


if __name__ == "__main__":
    app()
