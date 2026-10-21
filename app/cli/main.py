from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

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
            from app.models.identity import IdentityReconcileState
            from app.services.identity import identity_tables_missing

            identity = []
            open_chains: list[dict] = []
            if not await identity_tables_missing(session):
                identity = (
                    await session.scalars(
                        select(IdentityReconcileState).order_by(IdentityReconcileState.data_type)
                    )
                ).all()
                open_chains = await _chains_to_review(session)
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
            }

    typer.echo(json.dumps(run(command()), indent=2))


async def _chains_to_review(session) -> list[dict]:
    from app.models.identity import RawSourceChain
    from app.services.source_chains import load_chains

    found = []
    names = (await session.scalars(select(RawSourceChain.data_type).distinct())).all()
    for data_type in sorted(names):
        chains = await load_chains(session, data_type)
        for old, new in sorted(chains.following.items()):
            if chains.review(old, new) == "suggested" and chains.usable(old, new):
                found.append(
                    {
                        "data_type": data_type,
                        "from": chains.spans[old].label if old in chains.spans else old[:12],
                        "to": chains.spans[new].label if new in chains.spans else new[:12],
                        "fingerprints": [old[:12], new[:12]],
                        "decided_by": chains.decided_by.get(old),
                    }
                )
    return found


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
    """Check archive guards, payload and body hashes, version history, and sync errors."""

    async def command():
        from app.parsers.common import payload_hash
        from app.services.identity import identity_tables_missing, verify_identities
        from app.services.raw_history import verify_guards, verify_versions
        from app.services.responses import verify_responses

        errors = []
        count = 0
        async with SessionLocal() as session:
            if await identity_tables_missing(session):
                # An older schema: say so instead of failing on the first missing table.
                _, identity_errors, notes = await verify_identities(session)
                return 0, 0, 0, 0, identity_errors, notes, []
            # First: a missing or disabled guard means deletes could go through unnoticed.
            errors.extend(await verify_guards(session))
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
            responses, response_errors = await verify_responses(session)
            errors.extend(response_errors)
            aliases, identity_errors, notes = await verify_identities(session)
            errors.extend(identity_errors)
            failed = (
                await session.scalars(select(SyncState).where(SyncState.last_error.is_not(None)))
            ).all()
        return count, versions, responses, aliases, errors, notes, failed

    count, versions, responses, aliases, errors, notes, failed = run(command())
    typer.echo(
        f"Verified {count} raw records, {versions} earlier versions, "
        f"{responses} stored Google responses and {aliases} identity aliases"
    )
    for note in notes:
        typer.echo(f"note: {note}", err=True)
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


FROM_DB_OPTION = typer.Option(
    ...,
    "--from-db",
    help=(
        "Restored archive: a database name on the live server, or a full URL. "
        "Repeat for several dumps; they are imported newest first."
    ),
)


@app.command()
def import_versions(
    from_db: list[str] = FROM_DB_OPTION,
    batch_size: int = typer.Option(2_000, min=1, max=50_000),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count only; write nothing."),
) -> None:
    """Add payload versions (and whole records) that only survive in old dumps."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.services.raw_history import import_versions as run_import
    from app.services.raw_history import newest_sighting, source_database_url

    urls = [source_database_url(item, get_settings().database_url) for item in from_db]

    async def command():
        engines = [create_async_engine(url) for url in urls]
        try:
            # Newest first: each older sighting then lands at the old end of a record's
            # timeline, so a payload that left and came back (A, B, A) stays three versions.
            newest = [await newest_sighting(engine) for engine in engines]
            order = sorted(
                range(len(engines)),
                key=lambda i: newest[i] or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
            results = []
            for index in order:
                stats = await run_import(
                    SessionLocal, engines[index], batch_size=batch_size, dry_run=dry_run
                )
                results.append((make_url(urls[index]).database, stats))
            return results
        finally:
            for engine in engines:
                await engine.dispose()

    results = run(command())
    report = [{"database": name, **stats.as_dict()} for name, stats in results]
    typer.echo(json.dumps({"dry_run": dry_run, "sources": report}, indent=2))
    if any(stats.source_hash_mismatches or stats.unrepresentable for _, stats in results):
        typer.echo(
            "some dump rows were not imported (listed above); keep those dump files",
            err=True,
        )
        raise typer.Exit(1)


DATA_TYPES_OPTION = typer.Option(
    None, "--data-type", help="Only this data type (repeatable); default: every archived type."
)
AFTER_OPTION = typer.Option(
    None,
    "--after",
    formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
    help="Only rows starting at/after this UTC time (resume a stopped run of --data-type).",
)


@app.command()
def identity_backfill(
    batch_size: int = typer.Option(5_000, min=100, max=100_000),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only; write nothing."),
    data_type: list[str] | None = DATA_TYPES_OPTION,
    after: datetime | None = AFTER_OPTION,
    incremental: bool = typer.Option(
        False,
        "--incremental",
        help="Only rows changed since each data type's checkpoint (what the nightly job runs).",
    ),
    statement_timeout: str = typer.Option(
        "120s", help="PostgreSQL statement_timeout for each batch (e.g. 120s, 5min)."
    ),
) -> None:
    """Decide source chains, add v2 aliases and link duplicate rows of a record (idempotent).

    Decides, from the sightings, that two sources are one renamed device (the owner decides
    the rest with identity-decide). The report lists, per data type, every chain (from -> to
    source, gap in seconds, review state), the chains to review, the sources the rule left
    open (``ambiguous``, with why), refuted chains, and the pairs of sources that share a class
    but are kept apart; review them after a dry run (docs/RUNBOOK.md).
    """
    import re

    from app.services.identity_backfill import BackfillStats, LockUnavailable
    from app.services.identity_backfill import identity_backfill as run_backfill
    from app.services.source_chains import BackfillRunning

    if not re.fullmatch(r"[1-9][0-9]*(ms|s|min)", statement_timeout):
        raise typer.BadParameter(
            "use a value like 120s, 500ms or 5min", param_hint="--statement-timeout"
        )

    def progress(name: str, stats: BackfillStats, reached: datetime) -> None:
        resume = (
            ""
            if incremental
            else f" (resume: --data-type {name} --after {reached.strftime('%Y-%m-%dT%H:%M:%S')})"
        )
        typer.echo(
            f"{name}: {stats.rows} rows, {sum(stats.linked.values())} superseded, "
            f"up to {reached.isoformat()}{resume}",
            err=True,
        )

    try:
        results = run(
            run_backfill(
                SessionLocal,
                data_types=data_type,
                batch_size=batch_size,
                dry_run=dry_run,
                after=after.replace(tzinfo=UTC) if after else None,
                incremental=incremental,
                statement_timeout=statement_timeout,
                progress=progress,
                notice=lambda message: typer.echo(message, err=True),
            )
        )
    except (BackfillRunning, LockUnavailable) as exc:
        typer.echo(f"identity-backfill refused: {exc}", err=True)
        raise typer.Exit(1) from exc
    total = BackfillStats()
    for stats in results.values():
        total.add(stats)
    report = {
        "dry_run": dry_run,
        "data_types": {name: stats.as_dict() for name, stats in results.items()},
        "total": total.as_dict(),
    }
    typer.echo(json.dumps(report, indent=2))


@app.command()
def identity_decide(
    decision: str = typer.Argument(
        ..., help="link, reject or confirm (a chain FROM -> TO); distinct or not-distinct."
    ),
    data_type: str = typer.Option(..., "--data-type"),
    first: str = typer.Option(
        ..., "--from", help="Fingerprint (prefix, as reports print it) of the earlier form."
    ),
    second: str = typer.Option(..., "--to", help="Fingerprint (prefix) of the later form."),
    note: str | None = typer.Option(None, "--note", help="Why (kept with the decision)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the effect; write nothing."),
    batch_size: int = typer.Option(5_000, min=100, max=100_000),
) -> None:
    """Decide what the reconciler left open: two sources are one device, or two devices.

    Recorded append-only (raw_source_decisions) and applied at once: after ``link`` and
    ``not-distinct`` the data type is settled again (its rows are linked where they hold the
    same reading). ``reject`` stops using a chain (the rows it linked stay linked: they held
    equal readings), ``confirm`` takes it off the review list, ``distinct`` declares two
    devices. Run with --dry-run first (docs/RUNBOOK.md).
    """
    from app.services.identity_backfill import LockUnavailable
    from app.services.identity_decisions import DecisionRefused, decide_sources
    from app.services.source_chains import BackfillRunning

    def progress(name, stats, reached: datetime) -> None:
        typer.echo(
            f"{name}: {stats.rows} rows, {sum(stats.linked.values())} superseded, "
            f"up to {reached.isoformat()}",
            err=True,
        )

    try:
        outcome = run(
            decide_sources(
                SessionLocal,
                data_type,
                decision,
                first,
                second,
                note=note,
                dry_run=dry_run,
                batch_size=batch_size,
                progress=progress,
                notice=lambda message: typer.echo(message, err=True),
            )
        )
    except (DecisionRefused, BackfillRunning, LockUnavailable) as exc:
        typer.echo(f"identity-decide refused: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(outcome.as_dict(), indent=2, default=str))


@app.command()
def identity_sources(data_type: str = typer.Option(..., "--data-type")) -> None:
    """List a data type's source fingerprints: spans, devices, chains (with review state),
    pairs kept apart, the owner's decisions, and what the chain rule leaves open."""
    from app.services.identity_decisions import list_sources

    typer.echo(json.dumps(run(list_sources(SessionLocal, data_type)), indent=2, default=str))


FETCHED_SINCE_OPTION = typer.Option(
    None, "--since", formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"], help="Fetched at/after (UTC)."
)
OUTPUT_OPTION = typer.Option(
    None, "--output", "-o", help="Write the body here instead of standard output."
)


@app.command()
def responses(
    data_type: str | None = typer.Option(None, "--data-type"),
    context_id: str | None = typer.Option(
        None, "--context", help="A run id, e.g. webhook:<element hash> or backfill:<job id>."
    ),
    since: datetime | None = FETCHED_SINCE_OPTION,
    limit: int = typer.Option(20, min=1, max=10_000),
) -> None:
    """List stored Google responses (newest first, metadata only) as JSON."""
    from app.services.responses import list_responses

    async def command():
        async with SessionLocal() as session:
            return await list_responses(
                session,
                data_type=data_type,
                context_id=context_id,
                since=since.replace(tzinfo=UTC) if since else None,
                limit=limit,
            )

    typer.echo(json.dumps(run(command()), indent=2, ensure_ascii=False))


@app.command()
def export_response(
    response_id: int = typer.Argument(..., help="raw_api_responses.id"),
    output: Path | None = OUTPUT_OPTION,
) -> None:
    """Write one stored Google response body byte for byte (metadata goes to stderr)."""
    import hashlib

    from app.services.responses import describe, load_response

    async def command():
        async with SessionLocal() as session:
            return await load_response(session, response_id)

    response = run(command())
    if response is None:
        typer.echo(f"no stored response {response_id}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(describe(response), ensure_ascii=False), err=True)
    if hashlib.sha256(response.body).hexdigest() != response.body_sha256:
        typer.echo("the stored body no longer matches its SHA-256", err=True)
        raise typer.Exit(1)
    if output is not None:
        output.write_bytes(response.body)
    else:
        sys.stdout.buffer.write(response.body)
        sys.stdout.buffer.flush()


@app.command()
def probe(days: int = typer.Option(30, min=1, max=365)) -> None:
    """Probe accessible Google Health types without archiving records or a backfill.

    The probe's Google responses are kept in raw_api_responses (source "probe").
    """
    runtime = build_runtime(get_settings(), SessionLocal)

    async def command():
        try:
            return await runtime.discovery.probe(days)
        finally:
            await runtime.shutdown()

    typer.echo(json.dumps(run(command()), indent=2, default=str))


if __name__ == "__main__":
    app()
