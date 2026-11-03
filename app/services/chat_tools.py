"""Bounded archive queries owned by one chat connection, with no background sends."""

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from app.services.codex_chat import JsonObject
from app.services.health_transfer import HealthTransfer

MAX_TOOL_TASKS = 4
# Keep exact RPC deduplication bounded too. Exhaustion closes the ephemeral connection
# rather than evicting ids and potentially answering an old request twice.
MAX_TOOL_REQUESTS = 1024
# A query that suppresses cancellation must not hold the chat's shutdown open forever.
CLOSE_GRACE_SECONDS = 10.0
ToolResult = tuple[object, bool]


@dataclass
class ToolCall:
    request_id: str | int
    params: JsonObject
    transfer: HealthTransfer
    valid: bool = True


class HealthQueries:
    """Retain cancelled tasks until reaped, so slow cleanup still occupies a slot.

    Queries only return data. The socket pump alone decides whether to send it after
    processing ready cancellation/completion messages. A coroutine that suppresses
    cancellation cannot make its result valid again or write into a later turn.
    """

    def __init__(self) -> None:
        self.tasks: dict[asyncio.Task[ToolResult], ToolCall] = {}
        self.seen: set[str | int] = set()

    def claim(self, request_id: str | int) -> bool:
        if request_id in self.seen:
            return False
        if len(self.seen) >= MAX_TOOL_REQUESTS:
            raise RuntimeError("Umay tool request limit reached")
        self.seen.add(request_id)
        return True

    @property
    def full(self) -> bool:
        return len(self.tasks) >= MAX_TOOL_TASKS

    def start(self, call: ToolCall, query: Coroutine[Any, Any, ToolResult]) -> None:
        self.tasks[asyncio.create_task(query)] = call

    def cancel(self) -> None:
        for task, call in self.tasks.items():
            if call.valid:
                call.valid = False
                task.cancel()

    async def close(self) -> None:
        """Wait a bounded time for cancelled queries; never for a stubborn one.

        Like KEEP_ON_CANCEL_SECONDS, asyncio.wait does not cancel again on timeout.
        """
        self.cancel()
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=CLOSE_GRACE_SECONDS)
        for task in self.tasks:
            if task.done():
                _consume(task)
            else:
                # Invalid queries cannot send; keep a reference so the loop never
                # destroys a pending task, and consume its outcome when it ends.
                _ABANDONED.add(task)
                task.add_done_callback(_consume)
        self.tasks.clear()
        self.seen.clear()


_ABANDONED: set[asyncio.Task[ToolResult]] = set()


def _consume(task: asyncio.Task[ToolResult]) -> None:
    _ABANDONED.discard(task)
    if not task.cancelled():
        task.exception()  # an invalidated query's failure is never reported
