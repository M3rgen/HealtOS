from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from app.core.config import Settings
from app.core.logging import event
from app.core.provenance import current_context_id, current_source, provenance
from app.google_health.capture import CapturedResponse, ResponseArchive, kept_headers, safe_query
from app.google_health.data_types import AccessMethod, DataTypeSpec, query_iso
from app.google_health.oauth import TokenProvider

logger = logging.getLogger(__name__)


class GoogleHealthError(RuntimeError):
    pass


class InvalidPageTokenError(GoogleHealthError):
    pass


@dataclass(frozen=True)
class Page:
    data_points: list[dict]
    next_page_token: str | None
    # Every HTTP exchange behind this page (retries included), oldest first; the last one
    # is the response the data points were read from. Stored with the records
    # (app.services.storage.store_page), in the same transaction.
    responses: tuple[CapturedResponse, ...] = ()


class GoogleHealthClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenProvider,
        http: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
        archive: ResponseArchive | None = None,
    ):
        self.settings = settings
        self.tokens = tokens
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20))
        self.sleep = sleep
        # Where responses go that no page transaction stores (failures, identity, devices,
        # probes); None (tests) drops them.
        self.archive = archive
        self._request_lock = asyncio.Lock()
        self._last_request_started = 0.0

    async def list_data_points(
        self,
        spec: DataTypeSpec,
        start: datetime,
        end: datetime,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> Page:
        requested_page_size = page_size or self.settings.google_api_page_size
        effective_page_size = (
            min(requested_page_size, 25)
            if spec.name in {"sleep", "exercise"}
            else requested_page_size
        )
        if spec.access_method == AccessMethod.rollup:
            start, end = _minute_aligned_range(start, end)
            body = {
                "range": {"startTime": _iso(start), "endTime": _iso(end)},
                "windowSize": "60s",
                "pageSize": effective_page_size,
                "dataSourceFamily": "users/me/dataSourceFamilies/all-sources",
            }
            if page_token:
                body["pageToken"] = page_token
            payload, responses = await self._request(
                "POST",
                f"/users/me/dataTypes/{spec.name}/dataPoints:rollUp",
                data_type=spec.name,
                json=body,
            )
            points = payload.get("rollupDataPoints", [])
        else:
            params: dict[str, str | int] = {"pageSize": effective_page_size}
            if filter_expression := spec.filter_for(_iso(start), _iso(end)):
                params["filter"] = filter_expression
            if spec.access_method == AccessMethod.reconcile:
                params["dataSourceFamily"] = "users/me/dataSourceFamilies/all-sources"
            if page_token:
                params["pageToken"] = page_token
            suffix = ":reconcile" if spec.access_method == AccessMethod.reconcile else ""
            payload, responses = await self._request(
                "GET",
                f"/users/me/dataTypes/{spec.name}/dataPoints{suffix}",
                data_type=spec.name,
                params=params,
            )
            points = payload.get("dataPoints", [])
        if not isinstance(points, list):
            await self.keep(responses)
            raise GoogleHealthError("Google Health response has an invalid data point list")
        token = payload.get("nextPageToken") or None
        return Page(points, token, tuple(responses))

    async def get_identity(self, access_token: str | None = None) -> dict:
        with provenance(source="identity"):
            payload, responses = await self._request(
                "GET", "/users/me/identity", access_token=access_token
            )
        await self.keep(responses)
        return payload

    async def list_paired_devices(self) -> dict:
        payload, responses = await self._request("GET", "/users/me/pairedDevices")
        await self.keep(responses)
        return payload

    async def keep(self, responses: Sequence[CapturedResponse]) -> None:
        """Store responses outside a page transaction (failures, probes, identity)."""
        if not responses or self.archive is None:
            return
        try:
            await self.archive.keep(responses)
        except Exception:
            logger.exception(
                "Google Health responses could not be archived",
                extra={"event": "RESPONSE_ARCHIVE_FAILED", "count": len(responses)},
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        data_type: str | None = None,
        **kwargs,
    ) -> tuple[dict, list[CapturedResponse]]:
        """The decoded JSON object and every response received on the way to it.

        On failure the responses received so far are archived before the error propagates.
        """
        captured: list[CapturedResponse] = []
        try:
            return await self._exchange(method, path, access_token, data_type, captured, kwargs)
        except Exception:
            await self.keep(captured)
            raise

    async def _exchange(
        self,
        method: str,
        path: str,
        access_token: str | None,
        data_type: str | None,
        captured: list[CapturedResponse],
        kwargs: dict,
    ) -> tuple[dict, list[CapturedResponse]]:
        await self._pace_request()
        refreshed = False
        last_status: int | None = None
        attempt = 0
        while True:
            token = access_token if access_token is not None else await self.tokens.get()
            request = self.http.build_request(
                method,
                f"{self.settings.google_health_base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    # Uncompressed, so the stored body is the bytes on the wire.
                    "Accept-Encoding": "identity",
                },
                **kwargs,
            )
            response: httpx.Response | None = None
            body = b""
            try:
                response = await self.http.send(request, stream=True)
            except httpx.TransportError as exc:
                failure: Exception | None = exc
            else:
                # Every response is kept, also an error status or a body cut off midway.
                body, failure = await _read_body(response)
                captured.append(
                    _capture(request, response, body, failure, len(captured), data_type)
                )
            if failure is not None or response is None:
                exc = failure
                if attempt >= self.settings.google_api_max_retries:
                    raise GoogleHealthError(
                        "Google Health API connection failed after retries"
                    ) from exc
                delay = _backoff(attempt)
                event(logger, "RETRYING", attempt=attempt + 1, delay_seconds=delay)
                attempt += 1
                await self.sleep(delay)
                continue
            last_status = response.status_code
            if response.status_code == 401 and access_token is None and not refreshed:
                self.tokens.invalidate()
                refreshed = True
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt >= self.settings.google_api_max_retries:
                    break
                delay = _retry_delay(response, attempt)
                if response.status_code == 429:
                    event(logger, "RATE_LIMITED", retry_after_seconds=delay)
                event(logger, "RETRYING", attempt=attempt + 1, delay_seconds=delay)
                attempt += 1
                await self.sleep(delay)
                continue
            if response.is_error:
                if response.status_code == 400 and _has_error_code(body, "INVALID_PAGE_TOKEN"):
                    raise InvalidPageTokenError("Google Health pagination token expired")
                raise GoogleHealthError(
                    f"Google Health API returned HTTP {response.status_code}: {_safe_error(body)}"
                )
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise GoogleHealthError("Google Health API returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise GoogleHealthError("Google Health API returned a non-object JSON response")
            return payload, captured
        raise GoogleHealthError(
            f"Google Health API request failed after retries (HTTP {last_status})"
        )

    async def _pace_request(self) -> None:
        interval = self.settings.google_api_min_request_interval_seconds
        if interval <= 0:
            return
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            delay = interval - (loop.time() - self._last_request_started)
            if delay > 0:
                await self.sleep(delay)
            self._last_request_started = loop.time()


# Shared with deletion marking, which must use exactly the bounds Google was sent.
_iso = query_iso


def _minute_aligned_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Keep 60-second rollup buckets stable across overlapping requests."""
    aligned_start = start.replace(second=0, microsecond=0)
    aligned_end = end.replace(second=0, microsecond=0)
    if aligned_end < end:
        aligned_end += timedelta(minutes=1)
    if aligned_end <= aligned_start:
        aligned_end = aligned_start + timedelta(minutes=1)
    return aligned_start, aligned_end


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            try:
                return max(
                    0.0,
                    (parsedate_to_datetime(header) - datetime.now().astimezone()).total_seconds(),
                )
            except (TypeError, ValueError):
                pass
    return _backoff(attempt)


def _backoff(attempt: int) -> float:
    return min(60.0, (2**attempt) + random.random())


async def _read_body(response: httpx.Response) -> tuple[bytes, Exception | None]:
    """The body as received; a connection that fails mid-body still yields what arrived."""
    chunks: list[bytes] = []
    failure: Exception | None = None
    try:
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
    except (httpx.TransportError, httpx.DecodingError) as exc:
        failure = exc
    finally:
        await response.aclose()
    return b"".join(chunks), failure


def _capture(
    request: httpx.Request,
    response: httpx.Response,
    body: bytes,
    failure: Exception | None,
    attempt: int,
    data_type: str | None,
) -> CapturedResponse:
    content = request.content
    return CapturedResponse(
        fetched_at=datetime.now(UTC),
        source=current_source(),
        context_id=current_context_id(),
        data_type=data_type,
        method=request.method,
        path=request.url.path,
        query=safe_query(request.url.query),
        request_body=content or None,
        attempt=attempt,
        status_code=response.status_code,
        headers=kept_headers(response.headers),
        body=body,
        complete=failure is None,
        error=type(failure).__name__ if failure is not None else None,
    )


def _json(body: bytes) -> object:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _safe_error(body: bytes) -> str:
    payload = _json(body)
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        return "request rejected"
    return str(error.get("status") or error.get("message") or "request rejected")[:300]


def _has_error_code(body: bytes, expected: str) -> bool:
    payload = _json(body)

    def contains(value: object) -> bool:
        if isinstance(value, str):
            return value == expected or expected in value
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return False

    return contains(payload)
