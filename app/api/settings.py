"""Appearance choices and display preferences shared by every browser that opens the dashboard.

Only preset ids are stored for appearance. Uploaded images (logo, Umay portrait,
background) stay in the browser that uploaded them (IndexedDB); the server keeps just the
`upload` marker, and a browser without that upload falls back to the default preset.

Display preferences (time zone choice, body model rotation) carry a `schema_version`, so a
row written in an older shape is never misread and a newer row is never overwritten.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import bounded_body, require_job_request
from app.core.timeutil import as_utc
from app.db.session import get_session
from app.models.settings import AppearanceSettings, DisplayPreferences

router = APIRouter(prefix="/api/settings", tags=["settings"])

# The payload is three short ids; anything near this size is not a real request.
MAX_BODY_BYTES = 4 * 1024
SINGLETON_ID = 1
# Same rule as the preset ids in frontend/src/appearance.js; also matches `upload`.
PresetId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")]
# Shape of `display_preferences.preferences`. Bump it when the shape changes and upgrade
# older rows in _display_response(); a row newer than this server is left untouched.
DISPLAY_SCHEMA_VERSION = 1
# `auto` (each device's own zone) or an IANA-style name. Each browser checks the name with
# Intl and falls back to `auto`, so the server does not need the same zone database.
TimeZoneChoice = Annotated[
    str,
    StringConstraints(
        pattern=r"^(auto|[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2})$", max_length=64
    ),
]


class AppearanceSelection(BaseModel):
    """Chosen preset per appearance kind; a missing kind means the default preset."""

    model_config = ConfigDict(extra="forbid")

    logo: PresetId | None = None
    umay: PresetId | None = None
    background: PresetId | None = None


class DisplayPreferenceValues(BaseModel):
    """Display preferences; a missing one means the browser's default."""

    model_config = ConfigDict(extra="forbid", strict=True)

    timezone: TimeZoneChoice | None = None
    motion: bool | None = None


class DisplayPreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    preferences: DisplayPreferenceValues


async def _parse[T: BaseModel](request: Request, model: type[T], message: str) -> T:
    raw = await bounded_body(request, MAX_BODY_BYTES, message)
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        raise HTTPException(
            422, jsonable_encoder(error.errors(include_url=False, include_context=False))
        ) from error


async def _store(
    session: AsyncSession, model: type[AppearanceSettings | DisplayPreferences], values: dict
) -> None:
    """Insert or replace the single row (last write wins)."""
    for attempt in range(2):
        row = await session.get(model, SINGLETON_ID)
        if row is None:
            session.add(model(id=SINGLETON_ID, **values))
        else:
            for name, value in values.items():
                setattr(row, name, value)
        try:
            await session.commit()
            return
        except IntegrityError:
            # Two first writes raced to insert the row; update the winner's row instead.
            await session.rollback()
            if attempt:
                raise


def _response(row: AppearanceSettings | None) -> dict:
    if row is None:
        return {"selection": {}, "updated_at": None}
    try:
        selection = AppearanceSelection.model_validate(row.selection).model_dump(exclude_none=True)
    except ValidationError:
        selection = {}  # A hand-edited row must not break every browser's dashboard.
    return {"selection": selection, "updated_at": as_utc(row.updated_at)}


@router.get("/appearance")
async def get_appearance(session: AsyncSession = Depends(get_session)) -> dict:
    return _response(await session.get(AppearanceSettings, SINGLETON_ID))


@router.put(
    "/appearance",
    dependencies=[Depends(require_job_request)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": AppearanceSelection.model_json_schema()}},
        }
    },
)
async def put_appearance(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    """Replace the shared selection (last write wins)."""
    body = await _parse(request, AppearanceSelection, "Appearance payload is too large")
    selection = body.model_dump(exclude_none=True)
    now = datetime.now(UTC)
    await _store(session, AppearanceSettings, {"selection": selection, "updated_at": now})
    return {"selection": selection, "updated_at": now}


def _display_response(row: DisplayPreferences | None) -> dict:
    empty = {"schema_version": DISPLAY_SCHEMA_VERSION, "preferences": {}, "updated_at": None}
    if row is None or row.schema_version != DISPLAY_SCHEMA_VERSION:
        return empty  # Nothing stored, or a newer shape this server cannot read.
    try:
        values = DisplayPreferenceValues.model_validate(row.preferences)
    except ValidationError:
        return empty  # A hand-edited row must not break every browser's dashboard.
    return {
        "schema_version": DISPLAY_SCHEMA_VERSION,
        "preferences": values.model_dump(exclude_none=True),
        "updated_at": as_utc(row.updated_at),
    }


@router.get("/display")
async def get_display(session: AsyncSession = Depends(get_session)) -> dict:
    return _display_response(await session.get(DisplayPreferences, SINGLETON_ID))


@router.put(
    "/display",
    dependencies=[Depends(require_job_request)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": DisplayPreferencesBody.model_json_schema()}},
        }
    },
)
async def put_display(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    """Replace the shared display preferences (last write wins)."""
    body = await _parse(request, DisplayPreferencesBody, "Display payload is too large")
    row = await session.get(DisplayPreferences, SINGLETON_ID)
    if row is not None and row.schema_version > DISPLAY_SCHEMA_VERSION:
        raise HTTPException(409, "Görünüm tercihleri daha yeni bir sürümle kaydedilmiş.")
    preferences = body.preferences.model_dump(exclude_none=True)
    now = datetime.now(UTC)
    values = {"schema_version": DISPLAY_SCHEMA_VERSION, "preferences": preferences}
    await _store(session, DisplayPreferences, {**values, "updated_at": now})
    return {**values, "updated_at": now}
