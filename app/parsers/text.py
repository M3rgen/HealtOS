"""Optional source labels, prepared against the destination's SQL text contract."""

from functools import cache

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase


def optional_text(value: object, *, max_length: int | None = None) -> str | None:
    """Keep text verbatim, including empty text; never stringify or truncate metadata.

    Selection/fallback belongs to the caller and happens before validation. A bad
    selected label therefore becomes NULL, rather than exposing an older/alternate one.
    PostgreSQL String lengths count characters, not the UTF-8 bytes sent by the driver.
    """
    if not isinstance(value, str):
        return None
    if max_length is not None and len(value) > max_length:
        return None
    return value


def prepare_optional_text(model: type[DeclarativeBase], values: dict) -> None:
    """Validate only supplied, nullable String columns just before a write.

    Required identity/type strings and JSON attributes have separate contracts and
    must remain untouched. Read limits from the mapped model so raw index columns
    and normalized tables each enforce their own schema. Do not catch SQL errors:
    a connection failure or a real database constraint still rolls back the page.
    """
    for name, limit in _text_columns(model):
        if name in values:
            values[name] = optional_text(values[name], max_length=limit)


def optional_column_text(model: type[DeclarativeBase], name: str, value: object) -> str | None:
    """Validate one selected label for readers, as prepare_optional_text does for writes."""
    return optional_text(value, max_length=dict(_text_columns(model))[name])


@cache
def _text_columns(model: type[DeclarativeBase]) -> tuple[tuple[str, int | None], ...]:
    """Mapped contracts are fixed; inspect each model once, rather than per data point.

    Values and payloads are never cached. Only the small set of mapped column names
    and length limits is shared between records and ingestion pages.
    """
    return tuple(
        (column.name, column.type.length)
        for column in model.__table__.columns
        if column.nullable and isinstance(column.type, String)
    )
