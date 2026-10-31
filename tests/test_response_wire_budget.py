"""Wire limits retain whole chunks and reject before decoding or parsing."""

import asyncio
import hashlib
import json
import tracemalloc

import httpx
import pytest
from compressed_records import compressed
from oauth_streams import TokenStream
from raw_response_records import FlowingBody, client, no_secret, stored
from retrieval_records import END, START

from app.google_health import client as client_module
from app.google_health import response_body, response_close, response_decode
from app.google_health.client import GoogleHealthError, ResponseBodyTooLargeError
from app.google_health.data_types import REGISTRY
from app.sync.errors import error_summary, is_transient


@pytest.mark.parametrize("encoding", [None, "identity", "gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("length", [None, "1", "999999"])
@pytest.mark.parametrize(
    "margin", [(m, f) for m in [-1, 0, 1, "first", "continuous"] for f in [None, 1, 7]]
)
async def test_wire_boundary_preserves_every_delivered_byte(
    sessions, monkeypatch, caplog, encoding, length, margin, endpoint="steps", status=200
):
    margin, fragment_size = margin
    assert response_body.MAX_WIRE_BYTES == 64 * 1024 * 1024
    payload = b' {"dataPoints": [], "private-body": "synthetic"}\n'
    wire = compressed(payload, encoding or "identity")
    continuous = margin == "continuous"
    limit = 4 if margin == "first" else len(wire) + (0 if continuous else margin)
    monkeypatch.setattr(response_body, "MAX_WIRE_BYTES", limit)
    # Neither the decoded ceiling nor an absent/lying Content-Length controls this limit.
    monkeypatch.setattr(response_decode, "MAX_DECODED_BYTES", len(payload))
    stream = FlowingBody(wire, buffered=True, finish=not continuous, fragment_size=fragment_size)
    stream.release.set()
    headers = {"content-encoding": encoding.removeprefix("raw-")} if encoding else {}
    if length is not None:
        headers["content-length"] = length
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers=headers, stream=stream)

    rejected = continuous or margin in {-1, "first"}
    if rejected:
        # A valid empty JSON prefix cannot bypass the receive limit. Neither codec
        # work nor JSON interpretation may start, even for HTTP retry/refresh statuses.
        monkeypatch.setattr(client_module, "_decode", lambda *a: pytest.fail("decoded rejection"))
        monkeypatch.setattr(
            client_module, "_success_payload", lambda *a: pytest.fail("parsed rejection")
        )
    google = client(handler, sessions, google_api_max_retries=4)
    async with google.http:
        args = (REGISTRY[endpoint], START, END) if endpoint in REGISTRY else ()
        method = getattr(google, "list_data_points" if args else endpoint)
        if rejected:
            with pytest.raises(ResponseBodyTooLargeError) as caught:
                await method(*args)
            assert (
                error_summary(caught.value) == "ResponseBodyTooLargeError (wire_response_too_large)"
            )
            assert not is_transient(caught.value)
            assert caught.value.__cause__ is caught.value.__context__ is None
            assert str(caught.value) == "Google Health response exceeded its wire byte budget"
        else:
            page = await method(*args)
            assert page.data_points == [] and page.next_page_token is None
            await google.keep(page.responses)
        assert len(calls) == 1 and stream.close_calls == 1 and stream.closed.is_set()
        width = fragment_size or 5
        expected = wire[: (limit // width + 1) * width] if margin == "first" else wire
        expected += b" " if continuous else b""
        assert stream.wire == expected
        # The fixture sets reading only if another chunk/EOF is requested after wire.
        assert stream.reading.is_set() == (margin not in {-1, "first"})
        (row,) = await stored(sessions)
        assert row.body == stream.wire and row.body_size == len(stream.wire)
        assert row.body_sha256 == hashlib.sha256(stream.wire).hexdigest()
        assert row.complete == (not rejected)
        assert row.error == ("ResponseBodyLimitExceeded" if rejected else None)
        no_secret(row)
        assert "private-body" not in caplog.text and "secret-access-token" not in caplog.text


@pytest.mark.parametrize(
    "endpoint", ["steps", "floors", "total-calories", "get_identity", "list_paired_devices"]
)
@pytest.mark.parametrize("status", [200, 401, 429, 503, 307])
async def test_wire_rejection_precedes_endpoint_status_handling(
    sessions, monkeypatch, caplog, endpoint, status
):
    await test_wire_boundary_preserves_every_delivered_byte(
        sessions, monkeypatch, caplog, "gzip", "1", (-1, None), endpoint, status
    )


async def test_wire_rejection_survives_close_timeout(sessions, monkeypatch):
    monkeypatch.setattr(response_body, "MAX_WIRE_BYTES", 5)
    monkeypatch.setattr(response_close, "RESPONSE_CLOSE_SECONDS", 0.01)
    stream = FlowingBody(b"{}" + b" " * 100, buffered=True)
    google = client(
        lambda _: httpx.Response(200, stream=stream), sessions, google_api_max_retries=4
    )
    async with google.http:
        with pytest.raises(ResponseBodyTooLargeError):
            await google.list_paired_devices()
        (row,) = await stored(sessions)
        assert row.error == "ResponseBodyLimitExceeded" and not row.complete
        assert row.body == stream.wire and stream.close_calls == 1 and stream.closed.is_set()


@pytest.mark.parametrize("size", [2, 1024 * 1024])
async def test_fragmentation_memory_depends_on_bytes_not_chunk_count(size):
    wire = bytes(range(256)) * (size // 256) if size > 2 else b"{}"

    async def measure(width, empty_count):
        retained = []

        def chunks():
            for offset in range(0, len(wire), width):
                yield wire[offset : offset + width]
            retained.append(tracemalloc.get_traced_memory()[0])
            for _ in range(empty_count):
                yield b""
            retained.append(tracemalloc.get_traced_memory()[0])

        # Generate chunks lazily; probe retained storage before EOF and peak
        # copies on return, without counting a fixture-owned list of chunks.
        response = httpx.Response(200, stream=TokenStream(chunks()))
        tracemalloc.start()
        try:
            body, cut_off, failure = await response_body.read_body(response, [])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert body == wire and cut_off is failure is None
        assert retained[1] - retained[0] < 4096
        return peak

    large = await measure(len(wire), 0)
    small = await measure(2, 100_000)
    assert small < large + size // 4 + 64 * 1024


class SteadyBody(httpx.AsyncByteStream):
    def __init__(self, wire: bytes, pieces: int, pause: float, stall: float = 0):
        self.wire, self.pieces, self.pause, self.stall = wire, pieces, pause, stall
        self.sent = 0

    async def __aiter__(self):
        step = len(self.wire) // self.pieces + 1
        for offset in range(0, len(self.wire), step):
            await asyncio.sleep(self.stall if offset and offset >= len(self.wire) // 2 else 0)
            await asyncio.sleep(self.pause)
            self.sent += 1
            yield self.wire[offset : offset + step]

    async def aclose(self):
        pass


@pytest.mark.parametrize("case", ["steady", "stall", "total"])
async def test_body_budget_follows_progress_not_total_time(sessions, monkeypatch, case):
    """A slow but steady large page is kept whole; a stall or the hard cap still ends it."""
    monkeypatch.setattr(response_body, "RESPONSE_BODY_SECONDS", 0.15)
    monkeypatch.setattr(response_body, "RESPONSE_PROGRESS_BYTES", 1024, raising=False)
    monkeypatch.setattr(
        response_body, "RESPONSE_BODY_MAX_SECONDS", 0.25 if case == "total" else 60, raising=False
    )
    record = {"name": "users/me/dataTypes/steps/dataPoints/a", "steps": {"count": "1"}}
    wire = json.dumps({"dataPoints": [record] * 400}).encode()  # 20 pieces of ~2 KiB
    streams = []

    def handler(_):
        streams.append(SteadyBody(wire, 20, 0.02, stall=0.3 if case == "stall" else 0))
        return httpx.Response(200, stream=streams[-1])

    google = client(handler, sessions, google_api_max_retries=1)
    async with google.http:
        if case == "steady":  # 0.4 s in total, beyond the 0.15 s window
            page = await google.list_data_points(REGISTRY["steps"], START, END)
            assert len(page.data_points) == 400 and len(streams) == 1
            await google.keep(page.responses)
        else:
            with pytest.raises(GoogleHealthError) as caught:
                await google.list_data_points(REGISTRY["steps"], START, END)
            assert isinstance(caught.value.__cause__, response_body.ResponseBodyTimeout)
            assert len(streams) == 2
    rows = await stored(sessions)
    assert all(row.complete == (case == "steady") for row in rows)
    assert all((row.body == wire) == (case == "steady") for row in rows)
