"""Fixed-size, process-local write budgets; restarting the app resets all of them."""

import math
import time
from collections.abc import Callable
from threading import Lock

from app.core.config import Settings


class RequestBudget:
    """A fixed 60-second window, shared by all callers (no untrusted client keys).

    The first request after an expired window starts a new one. Rejections neither
    consume additional capacity nor extend the window. A fixed window permits a
    burst on either side of its boundary; it is not a rolling per-minute quota.
    Only three scalars change with traffic, regardless of addresses, UUIDs or headers;
    ``rejected`` counts refusals since startup, for status displays only.
    """

    def __init__(self, capacity: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = capacity
        self.clock = clock
        self._started = clock()
        self._used = 0
        self.rejected = 0
        self._lock = Lock()

    def retry_after(self) -> int | None:
        # Also atomic when an application is called from multiple threads/event loops.
        with self._lock:
            now = self.clock()
            if now - self._started >= 60:
                self._started = now
                self._used = 0
            if self._used >= self.capacity:
                self.rejected += 1
                return max(1, math.ceil(60 - (now - self._started)))
            self._used += 1
            return None


class WriteLimits:
    """Owned by one FastAPI application, independent of login sessions and runtimes.

    Global budgets keep memory constant and avoid trusting proxy headers. Webhook
    requests without the configured Authorization value spend their own budget, so an
    unauthenticated flood cannot starve Google's deliveries; ``webhook`` is spent only
    after the constant-time comparison passed. Multiple workers have independent
    budgets; no state is persisted or shared.
    """

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self.webhook = RequestBudget(settings.webhook_requests_per_minute, clock)
        self.webhook_unauthorized = RequestBudget(settings.webhook_requests_per_minute, clock)
        self.manual = RequestBudget(settings.manual_write_requests_per_minute, clock)
