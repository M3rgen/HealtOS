"""Parse one JSON object strictly, without letting parser errors carry its content."""

from __future__ import annotations

import json

INVALID = "invalid"
DUPLICATE_KEY = "duplicate_key"
CONSTANT = "constant"
NOT_OBJECT = "not_object"
ERROR_FIELD = "error_field"


class StrictJsonError(Exception):
    """A rejection naming only its kind; no key, value or parser input is retained."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # Called for every object at every depth with decoded keys; equal values are
    # still ambiguous, because last-value-wins would discard evidence.
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(DUPLICATE_KEY)
        result[key] = value
    return result


def _constant(_value: str) -> None:
    # NaN, Infinity and -Infinity are Python extensions, not JSON numbers.
    raise StrictJsonError(CONSTANT)


def loads_object(body: bytes, *, allow_error: bool = False) -> dict:
    """The decoded top-level object, or StrictJsonError.

    Without ``allow_error``, a top-level ``error`` field rejects the body as a success
    envelope: its presence is decisive, even for null or empty values.
    """
    try:
        value = json.loads(body, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, RecursionError):
        # Deep nesting exhausts the C scanner's recursion limit; neither is content.
        # Raise outside the handler: JSONDecodeError keeps the whole document.
        value = _Invalid
    if value is _Invalid:
        raise StrictJsonError(INVALID)
    if not isinstance(value, dict):
        raise StrictJsonError(NOT_OBJECT)
    if not allow_error and "error" in value:
        raise StrictJsonError(ERROR_FIELD)
    return value


class _Invalid:
    pass
