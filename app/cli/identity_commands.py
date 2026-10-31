"""Identity backfill, owner decisions and source reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import typer

from app.cli._context import context

DATA_TYPES_OPTION = typer.Option(
    None, "--data-type", help="Only this data type (repeatable); default: every archived type."
)


AFTER_OPTION = typer.Option(
    None,
    "--after",
    formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
    help="Only rows starting at/after this UTC time (resume a stopped run of --data-type).",
)


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
        results = context.run(
            run_backfill(
                context.sessions,
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
    failed = {name: stats.error for name, stats in results.items() if stats.error}
    if failed:
        for name, error in sorted(failed.items()):
            typer.echo(f"identity-backfill: {name} failed (the others ran): {error}", err=True)
        raise typer.Exit(1)


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

    Recorded append-only (raw_source_decisions) and applied at once; the rows whose devices
    change are settled again. ``link`` joins two sources into one device (their rows are
    linked where they hold the same reading); ``reject`` stops using a chain: its links no
    longer hide anything, both sources count on their own (each record once per device), and
    ``confirm`` undoes that; ``confirm`` also takes a chain off the review list; ``distinct``
    declares two devices (a row whose reading then comes back only under the other source is
    marked deleted by the next complete fetch: permanent) and ``not-distinct`` undoes it. Run
    with --dry-run first (docs/RUNBOOK.md).
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
        outcome = context.run(
            decide_sources(
                context.sessions,
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


def identity_sources(data_type: str = typer.Option(..., "--data-type")) -> None:
    """List a data type's source fingerprints: spans, devices, chains (with review state),
    pairs kept apart, the owner's decisions, and what the chain rule leaves open."""
    from app.services.identity_decisions import list_sources

    typer.echo(
        json.dumps(context.run(list_sources(context.sessions, data_type)), indent=2, default=str)
    )
