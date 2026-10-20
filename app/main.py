from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import auth, backfill, codex, dashboard, google_health, health, login, sync, webhooks
from app.core.auth import UITokenMiddleware
from app.core.config import get_settings
from app.core.host_validation import HostValidationMiddleware
from app.core.logging import configure_logging, event
from app.core.security_headers import SecurityHeadersMiddleware
from app.db.session import SessionLocal
from app.services.runtime import build_runtime
from app.services.scheduler import build_scheduler
from app.sync.incremental import STALE_RUNNING_AFTER

logger = logging.getLogger(__name__)
# body.d736859b.glb: a content-hashed name never changes meaning, so it can be cached
# forever; anything else under /models must be revalidated.
HASHED_NAME = re.compile(r"[^/]+[.-][0-9a-f]{8,64}\.[A-Za-z0-9]+")


class ModelFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        hashed = HASHED_NAME.fullmatch(Path(full_path).name)
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if hashed else "no-cache"
        )
        return response


async def recover_stale_running(runtime, delay: timedelta = timedelta(0)) -> None:
    if delay:
        # A little past the threshold, so a row last touched just before startup is stale.
        await asyncio.sleep(delay.total_seconds() + 60)
    await runtime.incremental.recover_interrupted()
    await runtime.webhook_processor.recover_running()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings, SessionLocal)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        event(logger, "APPLICATION_STARTING")
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        event(logger, "DATABASE_READY")
        # A crash leaves SyncState.status=running, which blocks watchdog recovery.
        # Likewise a webhook element left `running` is never claimed again. Rows with a
        # recent heartbeat may be a live CLI run elsewhere, so they are re-checked once
        # the grace period has passed instead of being failed now.
        await recover_stale_running(runtime)
        if settings.app_env != "test":
            runtime.spawn(recover_stale_running(runtime, delay=STALE_RUNNING_AFTER))
        scheduler = None
        scheduler_needed = settings.reconciliation_enabled or settings.watchdog_enabled
        if scheduler_needed and settings.app_env != "test":
            scheduler = build_scheduler(
                settings,
                runtime.reconciliation,
                runtime.watchdog,
                runtime.pipeline_health,
            )
            scheduler.start()
            runtime.scheduler = scheduler
            event(
                logger,
                "SCHEDULER_STARTED",
                reconciliation_enabled=settings.reconciliation_enabled,
                watchdog_enabled=settings.watchdog_enabled,
            )
            if settings.watchdog_enabled:
                runtime.spawn(runtime.watchdog.startup_check())
        event(logger, "APPLICATION_READY")
        yield
        event(logger, "APPLICATION_STOPPING")
        if scheduler:
            scheduler.shutdown(wait=False)
            runtime.scheduler = None
        await runtime.shutdown()

    # The interactive docs need inline scripts and the jsDelivr CDN (see
    # security_headers.DOCS_POLICY); they are a development tool, so production, where
    # the login cookie lives, serves none of them.
    docs = not settings.production
    application = FastAPI(
        title="Personal Health Data Mirror",
        version="0.5.0",
        description="Read-only, raw-first archive of Google Health API data",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    application.state.settings = settings
    application.state.runtime = runtime
    # Starlette runs the last-added middleware first: headers, host check, UI token.
    application.add_middleware(UITokenMiddleware, token=settings.app_ui_token)
    application.add_middleware(
        HostValidationMiddleware,
        webhook_endpoint_uri=settings.google_health_webhook_endpoint_uri,
    )
    application.add_middleware(SecurityHeadersMiddleware, docs_enabled=docs)
    application.include_router(health.router)
    application.include_router(login.router)
    application.include_router(auth.router)
    application.include_router(sync.router)
    application.include_router(backfill.router)
    application.include_router(google_health.router)
    application.include_router(webhooks.router)
    application.include_router(dashboard.router)
    application.include_router(codex.router)
    static = Path(__file__).parent / "static"
    if static.is_dir():
        application.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")
        if (static / "models").is_dir():
            application.mount("/models", ModelFiles(directory=static / "models"), name="models")

        @application.get("/", include_in_schema=False)
        async def index():
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    return application


app = create_app()
