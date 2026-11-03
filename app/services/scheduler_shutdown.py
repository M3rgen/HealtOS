"""Await APScheduler's cancelled coroutine jobs before closing their HTTP clients."""

from __future__ import annotations

import asyncio

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler


class DrainingExecutor(AsyncIOExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.stopped = asyncio.Event()
        self.cancelled_jobs: list[asyncio.Future] = []

    def shutdown(self, wait=True):
        # APScheduler 3.x cancels these futures and immediately clears its set, even
        # with wait=True. Keep them alive until transaction/outcome cleanup finishes.
        self.cancelled_jobs = list(self._pending_futures)
        super().shutdown(wait=wait)
        self.stopped.set()


class AppScheduler(AsyncIOScheduler):
    def __init__(self, **kwargs):
        self.draining_executor = DrainingExecutor()
        super().__init__(executors={"default": self.draining_executor}, **kwargs)

    async def shutdown_and_wait(self) -> None:
        self.shutdown(wait=False)
        # AsyncIOScheduler queues shutdown on the event loop. Wait for cancellation
        # to be dispatched before joining; never cancel a job twice during cleanup.
        await self.draining_executor.stopped.wait()
        await asyncio.gather(*self.draining_executor.cancelled_jobs, return_exceptions=True)
        self.draining_executor.cancelled_jobs.clear()
