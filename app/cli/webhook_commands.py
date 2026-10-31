"""Webhook readiness, receipt status and explicit pending replay."""

from __future__ import annotations

import json

import typer
from sqlalchemy import func, select

from app.cli._context import context
from app.models.state import WebhookEvent, WebhookNotification


def webhook_readiness() -> None:
    """Report secret-safe Google Health subscriber prerequisites."""
    typer.echo(json.dumps(context.settings().webhook_readiness, indent=2))


def webhook_status() -> None:
    """Show secret-safe local webhook receipt and processing status."""

    async def command():
        async with context.sessions() as session:
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
            "readiness": context.settings().webhook_readiness,
            "received_events": event_count,
            "pending_events": pending_count,
            "last_received_at": str(last_received) if last_received else None,
            "last_processed_at": str(last_processed) if last_processed else None,
            "notification_elements": notification_count,
            "notification_states": {row[0]: row[1] for row in notification_states},
            "incremental_sync_attempts": sync_attempts,
            "incremental_sync_successes": sync_successes,
        }

    typer.echo(json.dumps(context.run(command()), indent=2))


def webhook_replay_pending() -> None:
    """Retry durable pending webhook elements without asking Google to resend them."""

    runtime = context.runtime()

    async def command():
        try:
            return await runtime.webhook_processor.process_pending()
        finally:
            await runtime.shutdown()

    result = context.run(command())
    typer.echo(json.dumps(result.__dict__, indent=2))
    if result.failed_elements:
        raise typer.Exit(1)
