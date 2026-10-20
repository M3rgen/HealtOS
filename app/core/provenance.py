"""Which write path and which run a fetch or a stored record belongs to.

``source`` is the path (``sync``, ``webhook``, ``reconciliation``, ``backfill``,
``gap-repair``, ...): it ends up in ``raw_health_data.payload_source``,
``raw_health_data_versions.source`` and ``raw_api_responses.source``. ``context_id``
names the run (``sync:<uuid>``, ``webhook:<element hash>``, ``backfill:<job id>``, ...)
so every stored Google response can be traced to what asked for it. Both are context
variables, so concurrent tasks (one per data type) never see each other's values.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_source: ContextVar[str] = ContextVar("healthos_source", default="sync")
_context_id: ContextVar[str | None] = ContextVar("healthos_context_id", default=None)


def current_source() -> str:
    return _source.get()


def current_context_id() -> str | None:
    return _context_id.get()


def new_context_id(kind: str) -> str:
    return f"{kind}:{uuid.uuid4()}"


@contextmanager
def provenance(source: str | None = None, context_id: str | None = None) -> Iterator[None]:
    """Set the source and/or run id for the enclosed work (``None`` keeps the outer one)."""
    tokens = []
    if source is not None:
        tokens.append((_source, _source.set(source)))
    if context_id is not None:
        tokens.append((_context_id, _context_id.set(context_id)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
