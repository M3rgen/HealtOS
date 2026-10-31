"""Token grants enforce actual wire/inflation budgets and settle owned cleanup."""

import asyncio
import traceback
import zlib
from itertools import repeat
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from compressed_records import compressed
from oauth_streams import LIMIT, PAYLOAD, TokenStream

from app.core.config import Settings
from app.google_health import oauth, response_close, response_decode, strict_json
from app.google_health.oauth import OAuthClient, OAuthError, TokenProvider


@pytest.mark.parametrize("grant", ["refresh", "exchange_code"])
@pytest.mark.parametrize("size", [LIMIT - 1, LIMIT, LIMIT + 1])
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("length", [None, "1", "999999999"])
@pytest.mark.parametrize(
    "encoding",
    [
        "identity",
        "gzip-padding",
        "gzip",
        "deflate",
        "raw-deflate",
        "gzip-members",
        "stacked",
        "intermediate",
    ],
)
async def test_actual_boundaries_precede_json(monkeypatch, grant, size, split, length, encoding):
    body = PAYLOAD + b" " * (size - len(PAYLOAD))
    header = encoding.removeprefix("raw-")
    if encoding in {"gzip-padding", "intermediate"}:
        wire = compressed(PAYLOAD, "gzip")
        wire += b"\0" * (size - len(wire))
        header = "gzip"
        if encoding == "intermediate":
            wire = compressed(wire, "gzip")
            header = "gzip, gzip"
    elif encoding == "gzip-members":
        wire = compressed(body[:100], "gzip") + compressed(body[100:], "gzip")
        header = "gzip"
    elif encoding == "stacked":
        wire = compressed(compressed(body, "deflate"), "gzip")
        header = "deflate, gzip"
    else:
        wire = compressed(body, encoding)
    chunks = [wire[: len(wire) // 2], wire[len(wire) // 2 :]] if split else [wire]
    # An extra chunk must never be requested after the raw limit is exceeded.
    raw_excess = size > LIMIT and encoding in {"identity", "gzip-padding"}
    stream = TokenStream(chunks + ([b"unreachable-private-chunk"] if raw_excess else []))
    headers = {"content-encoding": header}
    if length is not None:
        headers["content-length"] = length
    handler = AsyncMock(return_value=httpx.Response(200, stream=stream, headers=headers))
    parse = Mock(wraps=strict_json.loads_object)
    monkeypatch.setattr(strict_json, "loads_object", parse)
    sleep = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        invoke = getattr(OAuthClient(Settings(_env_file=None), http, sleep=sleep), grant)
        if size > LIMIT:
            with pytest.raises(OAuthError) as caught:
                await invoke("synthetic-private-input")
            assert str(caught.value) == "OAuth token response exceeded its byte budget"
            assert not caught.value.transient
            assert caught.value.__cause__ is caught.value.__context__ is None
            parse.assert_not_called()
        else:
            result = await invoke("synthetic-private-input")
            assert (result.value if grant == "refresh" else result["access_token"]) == (
                "synthetic-private-access"
            )
            parse.assert_called_once()
    assert handler.await_count == 1 and stream.pulled == len(chunks)
    assert stream.closed.is_set() and stream.close_calls == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
async def test_real_bomb_never_inflates_beyond_limit_plus_one(monkeypatch, encoding):
    wire = compressed(PAYLOAD + b" " * (4 * LIMIT), encoding)
    factory, produced = zlib.decompressobj, []

    class ObservedInflater:
        def __init__(self, *args):
            self.inner = factory(*args)

        def decompress(self, data, max_length):
            assert 0 < max_length <= 64 * 1024
            result = self.inner.decompress(data, max_length)
            produced.append(len(result))
            return result

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(zlib, "decompressobj", ObservedInflater)
    stream = TokenStream([wire])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=stream, headers={"content-encoding": encoding.removeprefix("raw-")}
            )
        )
    ) as http:
        with pytest.raises(OAuthError, match="exceeded its byte budget"):
            await OAuthClient(Settings(_env_file=None), http).refresh("synthetic")
    assert sum(produced) == LIMIT + 1 and stream.closed.is_set()
    assert response_decode.MAX_DECODED_BYTES == 32 * LIMIT


@pytest.mark.parametrize(
    "encoding,damage",
    [
        ("gzip", "truncated"),
        ("gzip", "checksum"),
        ("deflate", "truncated"),
        ("deflate", "junk"),
        ("raw-deflate", "zero"),
        ("br", "valid"),
        ("zstd", "valid"),
        ("synthetic-private-encoding", "valid"),
        ("identity", "json"),
    ],
)
@pytest.mark.parametrize("grant", ["refresh", "exchange_code"])
async def test_bad_encoding_and_json_have_safe_errors(grant, encoding, damage, caplog):
    wire = (
        b'{"synthetic-private-json"' if damage == "json" else compressed(PAYLOAD, encoding, damage)
    )
    stream = TokenStream([wire])
    handler, sleep = (
        AsyncMock(
            return_value=httpx.Response(
                200, stream=stream, headers={"content-encoding": encoding.removeprefix("raw-")}
            )
        ),
        AsyncMock(),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthError) as caught:
            await getattr(OAuthClient(Settings(_env_file=None), http, sleep=sleep), grant)(
                "synthetic"
            )
    expected = "invalid JSON" if damage == "json" else "content decoding failed"
    assert (
        str(caught.value)
        == f"OAuth token {'endpoint returned' if damage == 'json' else 'response'} {expected}"
    )
    assert not caught.value.transient and handler.await_count == 1
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert (
        "synthetic-private" not in "".join(traceback.format_exception(caught.value)) + caplog.text
    )
    assert stream.closed.is_set() and stream.close_calls == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize("phase", ["read", "close", "status", "partial", "oversize"])
@pytest.mark.parametrize("grant", ["refresh", "exchange_code"])
@pytest.mark.parametrize("stop", ["timeout", "cancel", "twice", "error", "unexpected"])
async def test_waiting_and_failing_streams_settle_and_release_lock(
    monkeypatch, phase, stop, grant, caplog
):
    monkeypatch.setattr(response_close, "RESPONSE_CLOSE_SECONDS", 0.02)
    monkeypatch.setattr(oauth, "TOKEN_REQUEST_TIMEOUT_SECONDS", 0.05)
    fails = stop in {"error", "unexpected"}
    error_type = RuntimeError if stop == "unexpected" else httpx.ReadError
    stream = TokenStream(
        [PAYLOAD + (b" " * LIMIT if phase == "oversize" else b"")],
        wait_read=phase == "read" and not fails,
        wait_close=phase != "read" and not fails,
        read_error=error_type("synthetic-private-read") if phase == "read" and fails else None,
        close_error=error_type("synthetic-private-close") if phase != "read" and fails else None,
    )
    recovered = TokenStream([PAYLOAD])
    handler = AsyncMock(
        side_effect=[
            httpx.Response(
                307 if phase == "status" else 206 if phase == "partial" else 200,
                stream=stream,
                headers={"location": "https://example.test:bad/synthetic-private-location"},
            ),
            httpx.Response(200, stream=recovered),
        ]
    )
    sleep = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        provider = TokenProvider(
            OAuthClient(Settings(_env_file=None), http, sleep=sleep),
            AsyncMock(return_value="synthetic-private-refresh"),
            AsyncMock(),
        )
        baseline = asyncio.all_tasks()

        async def invoke():
            if grant == "refresh":
                return await provider.get()
            return (await provider.oauth.exchange_code("synthetic"))["access_token"]

        task = asyncio.create_task(invoke())
        if stop in {"cancel", "twice"}:
            await asyncio.wait_for(
                (stream.reading if phase == "read" else stream.closing).wait(), 1
            )
            task.cancel()
            if stop == "twice":
                await asyncio.sleep(0)
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        elif (
            phase in {"status", "partial", "oversize"}
            or (phase == "read" and stop == "timeout")
            or grant == "exchange_code"  # A single-use code is never retried.
        ):
            with pytest.raises(OAuthError) as caught:
                await asyncio.wait_for(task, 1)
            assert caught.value.transient == (phase in {"read", "close"})
            assert "synthetic-private" not in "".join(traceback.format_exception(caught.value))
        else:
            assert await asyncio.wait_for(task, 1) == "synthetic-private-access"
        assert stream.close_calls == 1 and stream.closed.is_set()
        assert not (asyncio.all_tasks() - baseline)
        if task.cancelled() or task.exception() is not None:
            assert handler.await_count == 1
            provider.refresh_token_saver.assert_not_awaited()
            sleep.assert_not_awaited()
        if handler.await_count == 1 or grant == "refresh":
            assert await asyncio.wait_for(invoke(), 1) == "synthetic-private-access"
        assert handler.await_count == 2
    assert "synthetic-private" not in caplog.text


@pytest.mark.parametrize("grant", ["refresh", "exchange_code"])
@pytest.mark.parametrize("content_range", [None, "bytes 0-9/100", "synthetic-private-range"])
async def test_partial_credentials_rejected_before_read_or_parse(monkeypatch, grant, content_range):
    stream = TokenStream([PAYLOAD])
    headers = {} if content_range is None else {"Content-Range": content_range}
    handler = AsyncMock(return_value=httpx.Response(206, stream=stream, headers=headers))
    parse, sleep = Mock(wraps=strict_json.loads_object), AsyncMock()
    monkeypatch.setattr(strict_json, "loads_object", parse)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthError) as caught:
            await getattr(OAuthClient(Settings(_env_file=None), http, sleep=sleep), grant)(
                "synthetic"
            )
    assert str(caught.value) == "OAuth token endpoint returned HTTP 206"
    assert not caught.value.transient and handler.await_count == 1
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert stream.pulled == 0 and stream.closed.is_set() and stream.close_calls == 1
    parse.assert_not_called()
    sleep.assert_not_awaited()


@pytest.mark.parametrize("chunk", [b"", b"x" * (64 * 1024)])
@pytest.mark.parametrize("grant", ["refresh", "exchange_code"])
async def test_continuously_ready_stream_cannot_starve_deadline_or_size_limit(
    monkeypatch, chunk, grant
):
    monkeypatch.setattr(oauth, "TOKEN_REQUEST_TIMEOUT_SECONDS", 0.02)
    stream = TokenStream(repeat(chunk))
    handler = AsyncMock(return_value=httpx.Response(200, stream=stream))
    sleep = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthError) as caught:
            await asyncio.wait_for(
                getattr(OAuthClient(Settings(_env_file=None), http, sleep=sleep), grant)(
                    "synthetic"
                ),
                1,
            )
    assert caught.value.transient == (not chunk)
    assert handler.await_count == 1 and stream.close_calls == 1 and stream.closed.is_set()
    if chunk:
        assert stream.pulled == LIMIT // len(chunk) + 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize("grant", ["refresh", "exchange_code"])
@pytest.mark.parametrize("phase", ["read", "close"])
@pytest.mark.parametrize("error_type", [httpx.ReadError, RuntimeError])
async def test_exhausted_stream_errors_do_not_expose_credentials(grant, phase, error_type, caplog):
    streams = []

    def respond(request):
        stream = TokenStream(
            [PAYLOAD], **{f"{phase}_error": error_type("synthetic-private-detail")}
        )
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    sleep = AsyncMock()
    # Pass the credential by name: Python 3.13 tracebacks print the whole failing
    # statement, so a literal argument would appear there without any real leak.
    credential = "synthetic-private-input"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        settings = Settings(_env_file=None, google_client_secret="synthetic-private-client")
        with pytest.raises(OAuthError) as caught:
            await getattr(OAuthClient(settings, http, sleep=sleep), grant)(credential)
    assert str(caught.value) == "OAuth token endpoint connection failed" and caught.value.transient
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert (
        "synthetic-private" not in "".join(traceback.format_exception(caught.value)) + caplog.text
    )
    assert len(streams) == (4 if grant == "refresh" else 1)
    assert all(stream.closed.is_set() and stream.close_calls == 1 for stream in streams)
    delays = [call.args[0] for call in sleep.await_args_list]
    assert delays == ([1, 2, 4] if grant == "refresh" else [])
