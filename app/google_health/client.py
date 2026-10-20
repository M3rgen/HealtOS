from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from app.core.config import Settings
from app.core.logging import event
from app.google_health.data_types import AccessMethod, DataTypeSpec
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


class GoogleHealthClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenProvider,
        http: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
    ):
        self.settings = settings
        self.tokens = tokens
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20))
        self.sleep = sleep
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
            payload = await self._request(
                "POST",
                f"/users/me/dataTypes/{spec.name}/dataPoints:rollUp",
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
            payload = await self._request(
                "GET",
                f"/users/me/dataTypes/{spec.name}/dataPoints{suffix}",
                params=params,
            )
            points = payload.get("dataPoints", [])
        if not isinstance(points, list):
            raise GoogleHealthError("Google Health response has an invalid data point list")
        token = payload.get("nextPageToken") or None
        return Page(points, token)

    async def get_identity(self, access_token: str | None = None) -> dict:
        return await self._request("GET", "/users/me/identity", access_token=access_token)

    async def list_paired_devices(self) -> dict:
        return await self._request("GET", "/users/me/pairedDevices")

    async def _request(
        self, method: str, path: str, *, access_token: str | None = None, **kwargs
    ) -> dict:
        await self._pace_request()
        refreshed = False
        last_status: int | None = None
        attempt = 0
        while True:
            token = access_token if access_token is not None else await self.tokens.get()
            try:
                response = await self.http.request(
                    method,
                    f"{self.settings.google_health_base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    **kwargs,
                )
            except httpx.TransportError as exc:
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
                if response.status_code == 400 and _has_error_code(response, "INVALID_PAGE_TOKEN"):
                    raise InvalidPageTokenError("Google Health pagination token expired")
                raise GoogleHealthError(
                    "Google Health API returned HTTP "
                    f"{response.status_code}: {_safe_error(response)}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise GoogleHealthError("Google Health API returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise GoogleHealthError("Google Health API returned a non-object JSON response")
            return payload
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


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _safe_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", {})
        return str(error.get("status") or error.get("message") or "request rejected")[:300]
    except ValueError:
        return "request rejected"


def _has_error_code(response: httpx.Response, expected: str) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False

    def contains(value: object) -> bool:
        if isinstance(value, str):
            return value == expected or expected in value
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return False

    return contains(payload)
