"""Whether PostgreSQL text/jsonb can hold an unmodified parsed Google value.

Shared by the webhook ingest and the record write path (app/services/raw_rejections.py),
so both refuse exactly the same values and neither sanitizes one to make it fit.
"""

import math
import re

# PostgreSQL text and jsonb hold neither U+0000 nor an unpaired surrogate.
_UNSTORABLE_TEXT = re.compile("[\x00\ud800-\udfff]")
# Distinct from JSON null: no parsed value or element boundaries are available.
UNPARSED_BODY = object()


def storable_text(value: str) -> bool:
    return _UNSTORABLE_TEXT.search(value) is None


def jsonb_storable(value: object) -> bool:
    """Whether jsonb can hold a parsed value; json.loads also accepts NaN and 1e400.

    An unparsed body (UNPARSED_BODY) has no value at all and is never storable.
    Iterative so it sets no depth limit of its own: json.loads and canonical_json (C, one
    recursion level per nesting) already accept depths where a recursive Python walk, at
    two frames per level, raises RecursionError and would fail the ingest.
    """
    if value is UNPARSED_BODY:
        return False
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if not storable_text(item):
                return False
        elif isinstance(item, float):
            if not math.isfinite(item):
                return False
        elif isinstance(item, dict):
            if not all(storable_text(key) for key in item):
                return False
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return True
