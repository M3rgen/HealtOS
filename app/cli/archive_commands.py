"""Archive verification, record history, dump imports and response exports."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from sqlalchemy import select

from app.cli._context import context
from app.models.state import RawHealthData, SyncState


def verify() -> None:
    """Check archive guards, payload and body hashes, version history, and sync errors."""

    async def command():
        from app.parsers.common import payload_hash
        from app.services.identity import identity_tables_missing, verify_identities
        from app.services.manual_flags import flag_schema_errors
        from app.services.raw_history import verify_guards, verify_versions
        from app.services.responses import verify_responses

        errors = []
        count = 0
        async with context.sessions() as session:
            if await identity_tables_missing(session):
                # An older schema: say so instead of failing on the first missing table.
                _, identity_errors, notes = await verify_identities(session)
                return 0, 0, 0, 0, identity_errors, notes, []
            if old_schema := await flag_schema_errors(session):  # not at 20261024_0014 yet
                return 0, 0, 0, 0, old_schema, [], []
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

    count, versions, responses, aliases, errors, notes, failed = context.run(command())
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
        async with context.sessions() as session:
            return await record_history(
                session,
                data_type,
                google_id=google_id,
                since=since.replace(tzinfo=UTC) if since else None,
                limit=limit,
                include_payload=payload,
            )

    records = context.run(command())
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

    urls = [source_database_url(item, context.settings().database_url) for item in from_db]

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
                    context.sessions, engines[index], batch_size=batch_size, dry_run=dry_run
                )
                results.append((make_url(urls[index]).database, stats))
            return results
        finally:
            for engine in engines:
                await engine.dispose()

    results = context.run(command())
    report = [{"database": name, **stats.as_dict()} for name, stats in results]
    typer.echo(json.dumps({"dry_run": dry_run, "sources": report}, indent=2))
    if any(stats.source_hash_mismatches or stats.unrepresentable for _, stats in results):
        typer.echo(
            "some dump rows were not imported (listed above); keep those dump files",
            err=True,
        )
        raise typer.Exit(1)


FETCHED_SINCE_OPTION = typer.Option(
    None, "--since", formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"], help="Fetched at/after (UTC)."
)


OUTPUT_OPTION = typer.Option(
    None, "--output", "-o", help="Write the body here instead of standard output."
)


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
        async with context.sessions() as session:
            return await list_responses(
                session,
                data_type=data_type,
                context_id=context_id,
                since=since.replace(tzinfo=UTC) if since else None,
                limit=limit,
            )

    typer.echo(json.dumps(context.run(command()), indent=2, ensure_ascii=False))


def export_response(
    response_id: int = typer.Argument(..., help="raw_api_responses.id"),
    output: Path | None = OUTPUT_OPTION,
) -> None:
    """Write one stored Google response body byte for byte (metadata goes to stderr)."""
    import hashlib

    from app.services.responses import describe, load_response

    async def command():
        async with context.sessions() as session:
            return await load_response(session, response_id)

    response = context.run(command())
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
