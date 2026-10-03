from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import mirror_data_types, supported_data_types
from app.google_health.oauth import OAuthClient, TokenProvider
from app.google_health.webhook import WebhookSignatureVerifier
from app.services.credentials import CredentialStore
from app.services.discovery import DiscoveryService
from app.services.pipeline_health import PipelineHealthService
from app.services.watchdog import PipelineWatchdog
from app.services.webhook_processing import WebhookBatchProcessor
from app.sync.backfill import BackfillService
from app.sync.incremental import IncrementalSync
from app.sync.reconciliation import ReconciliationService

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    credentials: CredentialStore
    oauth: OAuthClient
    tokens: TokenProvider
    google: GoogleHealthClient
    incremental: IncrementalSync
    backfill: BackfillService
    reconciliation: ReconciliationService
    webhook_verifier: WebhookSignatureVerifier
    webhook_processor: WebhookBatchProcessor
    discovery: DiscoveryService
    pipeline_health: PipelineHealthService
    watchdog: PipelineWatchdog
    tasks: set[asyncio.Task] = field(default_factory=set)
    scheduler: object | None = None

    @property
    def scheduler_running(self) -> bool:
        return bool(self.scheduler and getattr(self.scheduler, "running", False))

    def spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "Background job failed",
                extra={"event": "BACKGROUND_JOB_FAILED", "error_type": type(error).__name__},
            )

    async def shutdown(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.oauth.http.aclose()
        await self.google.http.aclose()
        await self.webhook_verifier.http.aclose()


def build_runtime(settings: Settings, sessions: async_sessionmaker) -> Runtime:
    credentials = CredentialStore(settings, sessions)
    oauth = OAuthClient(settings)
    tokens = TokenProvider(
        oauth,
        credentials.load_refresh_token,
        credentials.save_refresh_token,
    )
    google = GoogleHealthClient(settings, tokens)
    discovery_specs = supported_data_types(settings.extra_data_types, settings.enabled_data_types)
    mirror_specs = mirror_data_types(settings.extra_data_types, settings.enabled_data_types)
    incremental = IncrementalSync(settings, sessions, google, mirror_specs)
    backfill = BackfillService(settings, sessions, google, mirror_specs)
    reconciliation = ReconciliationService(incremental, sessions)
    frequent_types = {
        "heart-rate",
        "activity-level",
        "steps",
        "distance",
        "active-energy-burned",
        "total-calories",
        "active-minutes",
    }.intersection(mirror_specs)
    pipeline_health = PipelineHealthService(settings, sessions, frequent_types)
    watchdog = PipelineWatchdog(settings, sessions, pipeline_health, reconciliation)
    return Runtime(
        credentials=credentials,
        oauth=oauth,
        tokens=tokens,
        google=google,
        incremental=incremental,
        backfill=backfill,
        reconciliation=reconciliation,
        webhook_verifier=WebhookSignatureVerifier(settings.google_health_webhook_public_key_url),
        webhook_processor=WebhookBatchProcessor(sessions, incremental, set(mirror_specs)),
        discovery=DiscoveryService(google, discovery_specs, settings.sync_concurrency),
        pipeline_health=pipeline_health,
        watchdog=watchdog,
    )
