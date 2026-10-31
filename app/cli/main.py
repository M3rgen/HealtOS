"""Stable health-mirror entry point and compatibility exports for CLI commands."""

from __future__ import annotations

import asyncio

import typer

from app.cli._context import context
from app.cli.archive_commands import FETCHED_SINCE_OPTION as FETCHED_SINCE_OPTION
from app.cli.archive_commands import FROM_DB_OPTION as FROM_DB_OPTION
from app.cli.archive_commands import OUTPUT_OPTION as OUTPUT_OPTION
from app.cli.archive_commands import SINCE_OPTION as SINCE_OPTION
from app.cli.archive_commands import export_response as export_response
from app.cli.archive_commands import history as history
from app.cli.archive_commands import import_versions as import_versions
from app.cli.archive_commands import responses as responses
from app.cli.archive_commands import verify as verify
from app.cli.identity_commands import AFTER_OPTION as AFTER_OPTION
from app.cli.identity_commands import DATA_TYPES_OPTION as DATA_TYPES_OPTION
from app.cli.identity_commands import identity_backfill as identity_backfill
from app.cli.identity_commands import identity_decide as identity_decide
from app.cli.identity_commands import identity_sources as identity_sources
from app.cli.sync_commands import _chains_to_review as _chains_to_review
from app.cli.sync_commands import backfill as backfill
from app.cli.sync_commands import probe as probe
from app.cli.sync_commands import reconcile as reconcile
from app.cli.sync_commands import status as status
from app.cli.sync_commands import sync as sync
from app.cli.webhook_commands import webhook_readiness as webhook_readiness
from app.cli.webhook_commands import webhook_replay_pending as webhook_replay_pending
from app.cli.webhook_commands import webhook_status as webhook_status
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.runtime import build_runtime

app = typer.Typer(help="Operate the Personal Health Data Mirror.", no_args_is_help=True)


def run(coroutine):
    return asyncio.run(coroutine)


# Resolve the public entry point's dependencies when a command runs, not on import.
context.session_factory = lambda: SessionLocal
context.settings = lambda: get_settings()
context.runtime_factory = lambda settings, sessions: build_runtime(settings, sessions)
context.run = lambda coroutine: run(coroutine)

# Keep the original flat tree and registration order. Implementations have no app
# decorators, so importing them cannot register a command a second time.
for _command in (
    status,
    backfill,
    sync,
    reconcile,
    webhook_readiness,
    webhook_status,
    webhook_replay_pending,
    verify,
    history,
    import_versions,
    identity_backfill,
    identity_decide,
    identity_sources,
    responses,
    export_response,
    probe,
):
    app.command()(_command)


if __name__ == "__main__":
    app()
