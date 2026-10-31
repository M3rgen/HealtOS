"""Read a step measurement from its current payload, never a retained normalized row."""

from datetime import datetime

from app.google_health.data_types import REGISTRY
from app.parsers.common import first_number, payload_for, temporal
from app.services.finite_math import finite_number


def current_steps(record: dict) -> tuple[datetime, datetime, float] | None:
    """Use normalization's field aliases and time parser, preserving measured zero.

    A correction that cannot normalize deliberately leaves the old Steps row in place.
    Its count and interval are historical evidence, so readers must validate both against
    the current raw payload. Eligibility (identity, quarantine, deletion) stays with the
    caller's query; this function only establishes whether a measurement exists.
    """
    spec = REGISTRY["steps"]
    count = finite_number(first_number(payload_for(spec, record), ("count", "countSum", "steps")))
    left, right, _ = temporal(spec, record)
    if count is None or left is None or right is None or right <= left:
        return None
    return left, right, count
