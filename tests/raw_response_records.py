"""Synthetic Google response fixtures shared by archive regressions."""

import asyncio
import json

import httpx
from sqlalchemy import select

from app.core.config import Settings
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import REGISTRY
from app.models.raw_versions import RawApiResponse
from app.services.responses import DatabaseResponseArchive

SPEC = REGISTRY["steps"]
NAME = "users/me/dataTypes/steps/dataPoints/a"


def steps(count: int, name: str = NAME) -> dict:
    return {
        "name": name,
        "steps": {
            "interval": {
                "startTime": "2026-08-17T10:00:00Z",
                "endTime": "2026-08-17T10:01:00Z",
            },
            "count": str(count),
        },
    }


def body(*records: dict, token: str | None = None) -> bytes:
    # Odd spacing and key order on purpose: only the stored bytes keep them.
    payload = json.dumps(
        {"dataPoints": list(records), **({"nextPageToken": token} if token else {})}
    )
    return ("  " + payload.replace(": ", ":   ") + "\n").encode()


def duplicate_body(key: str, mode: str) -> bytes:
    """Build wire JSON without passing duplicate members through a dictionary."""
    value = '"private-marker"' if key == "nextPageToken" else '[{"private-marker": 42}]'
    second = value if mode == "same" else ("null" if key == "nextPageToken" else "[]")
    repeated = json.dumps(key)
    if mode == "escaped":
        repeated = repeated[:-2] + f'\\u{ord(key[-1]):04x}"'
    pairs = f"{{{json.dumps(key)}: {value}, {repeated}: {second}}}"
    if mode == "nested":
        pairs = '{"dataPoints": [{"private-marker": [' + pairs + "]}]}"
    return ("  " + pairs + "\n").encode()


class Tokens:
    async def get(self):
        return "secret-access-token"

    def invalidate(self):
        pass


def client(handler, sessions=None, **settings) -> GoogleHealthClient:
    async def no_sleep(_delay):
        return None

    return GoogleHealthClient(
        Settings(_env_file=None, google_api_min_request_interval_seconds=0, **settings),
        Tokens(),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=no_sleep,
        archive=DatabaseResponseArchive(sessions) if sessions else None,
    )


async def stored(sessions) -> list[RawApiResponse]:
    async with sessions() as session:
        return list(
            (await session.scalars(select(RawApiResponse).order_by(RawApiResponse.id))).all()
        )


def no_secret(response: RawApiResponse) -> None:
    for value in (response.query, response.request_body, response.headers, response.path):
        assert "secret-access-token" not in str(value)


class PausedBody(httpx.AsyncByteStream):
    def __init__(
        self, wire: bytes, phase: str, *, read_error=None, close_error=None, fragment_size=1
    ):
        self.wire, self.phase = wire, phase
        self.fragment_size = fragment_size
        self.read_error, self.close_error = read_error, close_error
        self.reading = asyncio.Event()
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0

    def chunks(self, wire):
        if self.fragment_size is None:
            yield wire[:5]
            yield wire[5:]
        else:
            for start in range(0, len(wire), self.fragment_size):
                yield b""
                yield wire[start : start + self.fragment_size]

    async def __aiter__(self):
        if self.phase != "before":
            for chunk in self.chunks(self.wire):
                yield chunk
        self.reading.set()
        if self.read_error:
            raise self.read_error("private-body secret-access-token HTTP 503")
        if self.phase == "read-error":
            raise httpx.ReadTimeout("synthetic")
        if self.phase != "close":
            await asyncio.Event().wait()

    async def aclose(self):
        self.close_calls += 1
        self.closing.set()
        try:
            await self.release.wait()
            if self.close_error:
                raise self.close_error("private-body secret-access-token HTTP 503")
        finally:
            self.closed.set()


class FlowingBody(PausedBody):
    """Keep sending after a valid JSON prefix, recording every delivered byte."""

    def __init__(self, payload: bytes, *, buffered=False, finish=False, fragment_size=None):
        super().__init__(b"", "close", fragment_size=fragment_size)
        self.payload, self.buffered, self.finish = payload, buffered, finish

    async def __aiter__(self):
        for chunk in self.chunks(self.payload):
            if not self.buffered:
                await asyncio.sleep(0.001)
            self.wire += chunk
            yield chunk
        self.reading.set()
        while not self.finish:
            if not self.buffered:
                await asyncio.sleep(0.001)
            chunk = b"" if self.buffered == "empty" else b" "
            self.wire += chunk
            yield chunk
