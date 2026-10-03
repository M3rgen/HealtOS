from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import auth, backfill, codex, dashboard, google_health, health, sync, webhooks
from app.core.config import get_settings
from app.core.host_validation import HostValidationMiddleware
from app.core.logging import configure_logging, event
from app.db.session import SessionLocal
from app.services.runtime import build_runtime
from app.services.scheduler import build_scheduler

logger = logging.getLogger(__name__)


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
        scheduler = None
        scheduler_needed = settings.reconciliation_enabled or settings.watchdog_enabled
        if scheduler_needed and settings.app_env != "test":
            scheduler = build_scheduler(settings, runtime.reconciliation, runtime.watchdog)
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

    application = FastAPI(
        title="Personal Health Data Mirror",
        version="0.1.0",
        description="Read-only, raw-first archive of Google Health API data",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.runtime = runtime
    application.add_middleware(
        HostValidationMiddleware,
        webhook_endpoint_uri=settings.google_health_webhook_endpoint_uri,
    )
    application.include_router(health.router)
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
            application.mount("/models", StaticFiles(directory=static / "models"), name="models")

        @application.get("/", include_in_schema=False)
        async def index():
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    return application


app = create_app()
