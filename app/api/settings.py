"""Appearance choices shared by every browser that opens the dashboard.

Only preset ids are stored here. Uploaded images (logo, Umay portrait, background) stay
in the browser that uploaded them (IndexedDB); the server keeps just the `upload`
marker, and a browser without that upload falls back to the default preset.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import bounded_body, require_job_request
from app.core.timeutil import as_utc
from app.db.session import get_session
from app.models.settings import AppearanceSettings

router = APIRouter(prefix="/api/settings", tags=["settings"])

# The payload is three short ids; anything near this size is not a real request.
MAX_BODY_BYTES = 4 * 1024
SINGLETON_ID = 1
# Same rule as the preset ids in frontend/src/appearance.js; also matches `upload`.
PresetId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")]


class AppearanceSelection(BaseModel):
    """Chosen preset per appearance kind; a missing kind means the default preset."""

    model_config = ConfigDict(extra="forbid")

    logo: PresetId | None = None
    umay: PresetId | None = None
    background: PresetId | None = None


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
    raw = await bounded_body(request, MAX_BODY_BYTES, "Appearance payload is too large")
    try:
        body = AppearanceSelection.model_validate_json(raw)
    except ValidationError as error:
        raise HTTPException(
            422, jsonable_encoder(error.errors(include_url=False, include_context=False))
        ) from error
    selection = body.model_dump(exclude_none=True)
    now = datetime.now(UTC)
    for attempt in range(2):
        row = await session.get(AppearanceSettings, SINGLETON_ID)
        if row is None:
            row = AppearanceSettings(id=SINGLETON_ID, selection=selection, updated_at=now)
            session.add(row)
        else:
            row.selection = selection
            row.updated_at = now
        try:
            await session.commit()
            break
        except IntegrityError:
            # Two first writes raced to insert the row; update the winner's row instead.
            await session.rollback()
            if attempt:
                raise
    return {"selection": selection, "updated_at": now}
