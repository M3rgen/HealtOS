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
        """Store responses outside a page transaction (failures, probes, identity).

        An archive error is logged, never raised. If the calling task is cancelled while
        the responses are being stored, the store still gets up to
        ``KEEP_ON_CANCEL_SECONDS`` to finish before the cancellation propagates.
        """
        if not responses or self.archive is None:
            return
        store = asyncio.ensure_future(self._store(responses))
        try:
            await asyncio.shield(store)
        except asyncio.CancelledError:
            await _finish(store, len(responses))
            raise

    async def keep_after(
        self, responses: Sequence[CapturedResponse], failure: BaseException
    ) -> None:
        """Keep responses whose page or request was cut short by ``failure``.

        Also on cancellation (a restart or deploy between fetching and storing): the bytes
        already received are stored (shielded, at most ``KEEP_ON_CANCEL_SECONDS``); the
        caller re-raises ``failure`` either way, so a cancellation is never swallowed.
        """
        if isinstance(failure, Exception):
            await self.keep(responses)
        elif responses and self.archive is not None:
            await _finish(asyncio.ensure_future(self._store(responses)), len(responses))

    async def _store(self, responses: Sequence[CapturedResponse]) -> None:
        assert self.archive is not None
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
        except BaseException as failure:
            await self.keep_after(captured, failure)
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
                # Every response is kept, also an error status or a body cut off midway:
                # the bytes as they came off the wire (still compressed if the server
                # ignored Accept-Encoding: identity); `body` is the decoded entity.
                wire, cut_off = await _read_body(response)
                body, failure = _decode(wire, response.headers, cut_off)
                captured.append(
                    _capture(request, response, wire, cut_off, failure, len(captured), data_type)
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


# How long a cancelled task (restart, deploy) waits for responses it already received to
# reach the database before it lets the cancellation through.
KEEP_ON_CANCEL_SECONDS = 10.0


async def _finish(store: asyncio.Future, count: int) -> None:
    """Wait (bounded, shielded) for a store started on behalf of a cancelled task."""
    try:
        await asyncio.wait_for(asyncio.shield(store), KEEP_ON_CANCEL_SECONDS)
    except (TimeoutError, asyncio.CancelledError):
        logger.error(
            "Google Health responses may not have been archived before cancellation",
            extra={"event": "RESPONSE_ARCHIVE_INTERRUPTED", "count": count},
        )


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
    """The body bytes as received on the wire, before any Content-Encoding is undone.

    A connection that fails mid-body still yields what arrived.
    """
    chunks: list[bytes] = []
    failure: Exception | None = None
    try:
        try:
            async for chunk in response.aiter_raw():
                chunks.append(chunk)
        except httpx.StreamConsumed:
            # A response built in memory (httpx.MockTransport) read itself on creation;
            # its stream still holds the bytes exactly as given.
            chunks = [chunk async for chunk in response.stream]  # type: ignore[union-attr]
    except httpx.TransportError as exc:
        failure = exc
    finally:
        await response.aclose()
    return b"".join(chunks), failure


def _decode(
    wire: bytes, headers: httpx.Headers, failure: Exception | None
) -> tuple[bytes, Exception | None]:
    """The entity body with the response's Content-Encoding (gzip, deflate, ...) undone."""
    encoding = headers.get("content-encoding")
    if failure is not None or not encoding or encoding.strip().lower() == "identity":
        return wire, failure
    try:
        return httpx.Response(
            200, headers={"content-encoding": encoding}, content=wire
        ).content, None
    except httpx.DecodingError as exc:
        return b"", exc


def _capture(
    request: httpx.Request,
    response: httpx.Response,
    wire: bytes,
    cut_off: Exception | None,
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
        body=wire,
        # The wire bytes are complete unless the connection failed mid-body; `error` also
        # names a body that arrived whole but could not be decoded.
        complete=cut_off is None,
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
