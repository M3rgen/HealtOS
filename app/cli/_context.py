"""Shared CLI dependencies, resolved at invocation time for legacy overrides.

Command modules can be imported without the entry point. The entry point supplies
late-bound getters so callers can still replace main.SessionLocal, get_settings,
build_runtime and run without command modules importing main back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.services.runtime import Runtime, build_runtime


@dataclass
class CommandContext:
    session_factory: Callable[[], async_sessionmaker] = lambda: SessionLocal
    settings: Callable[[], Settings] = get_settings
    runtime_factory: Callable[[Settings, async_sessionmaker], Runtime] = build_runtime
    run: Callable[[Coroutine[Any, Any, Any]], Any] = asyncio.run

    @property
    def sessions(self) -> async_sessionmaker:
        return self.session_factory()

    def runtime(self) -> Runtime:
        return self.runtime_factory(self.settings(), self.sessions)


context = CommandContext()
