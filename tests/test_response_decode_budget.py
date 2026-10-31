"""Bound inflation before allocation, including members and intermediate layers."""

import asyncio
import gzip
import zlib

import httpx
import pytest
from compressed_records import compressed
from raw_response_records import client, stored
from retrieval_records import END, START

from app.google_health import response_decode as decoder
from app.google_health.client import GoogleHealthError
from app.google_health.data_types import REGISTRY
from app.sync.errors import error_summary, is_transient


def decode(wire, encoding, failure=None):
    return decoder.decode(wire, httpx.Headers({"content-encoding": encoding}), failure)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate", "gzip-members"])
@pytest.mark.parametrize("length", [30, 31, 32])
@pytest.mark.parametrize("chunk_size", [7, 64 * 1024])
@pytest.mark.parametrize("damage", ["valid", "junk", "zero", "stream"])
def test_decoded_boundary_is_shared_by_members(monkeypatch, encoding, length, chunk_size, damage):
    monkeypatch.setattr(decoder, "MAX_DECODED_BYTES", 31)
    monkeypatch.setattr(decoder, "_CHUNK_BYTES", chunk_size)
    payload = bytes(range(length))
    wire = (
        compressed(payload[:15], "gzip") + b"\0\0" + compressed(payload[15:], "gzip")
        if encoding == "gzip-members"
        else compressed(payload, encoding, damage if "deflate" in encoding else "valid")
    )
    body, error = decode(wire, encoding.removesuffix("-members").removeprefix("raw-"))
    accepted = length <= 31 and (damage == "valid" or "deflate" not in encoding)
    assert body == (payload if accepted else b"")
    assert (error is None) == accepted
    if error:
        assert isinstance(
            error,
            decoder.ResponseDecodeLimitExceeded
            if length > 31
            else decoder.TrailingEncodedBytes
            if damage != "valid"
            else httpx.DecodingError,
        )
        assert error.__traceback__ is error.__cause__ is error.__context__ is None


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
def test_bomb_stops_after_one_excess_byte(monkeypatch, encoding):
    wire = compressed(b"x" * (2 * 1024 * 1024), encoding)
    factory, produced = zlib.decompressobj, []

    class ObservedInflater:
        def __init__(self, *args):
            self.inner = factory(*args)

        def decompress(self, data, max_length):
            assert 0 < max_length <= 32  # no unbounded/default call, no flush API
            result = self.inner.decompress(data, max_length)
            produced.append(len(result))
            return result

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(decoder, "MAX_DECODED_BYTES", 31)
    monkeypatch.setattr(zlib, "decompressobj", ObservedInflater)
    result, error = decode(wire, encoding.removeprefix("raw-"))
    assert result == b"" and isinstance(error, decoder.ResponseDecodeLimitExceeded)
    assert sum(produced) == 32  # real zlib produced only budget + 1, not the 2 MiB


@pytest.mark.parametrize("inner", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("outer", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("stage", ["inner", "intermediate", "exact"])
def test_each_stacked_layer_has_a_budget(monkeypatch, inner, outer, stage):
    payload = bytes(range(200)) if stage == "intermediate" else b"x" * 200
    intermediate = compressed(payload, inner)
    budget = max(len(payload), len(intermediate)) if stage == "exact" else 200
    if stage == "inner":
        budget = len(intermediate)
        assert len(payload) > budget
    elif stage == "intermediate":
        assert len(intermediate) > budget >= len(payload)
    monkeypatch.setattr(decoder, "MAX_DECODED_BYTES", budget)
    headers = httpx.Headers(
        [
            ("content-encoding", inner.removeprefix("raw-") + ", identity"),
            ("content-encoding", outer.removeprefix("raw-").upper()),
        ]
    )
    result, error = decoder.decode(compressed(intermediate, outer), headers, None)
    assert result == (payload if stage == "exact" else b"")
    assert (error is None) == (stage == "exact")
    if error:
        assert isinstance(error, decoder.ResponseDecodeLimitExceeded)


@pytest.mark.parametrize("damage", ["valid", "truncated", "checksum", "junk"])
def test_exact_limit_still_checks_later_gzip_members(monkeypatch, damage):
    monkeypatch.setattr(decoder, "MAX_DECODED_BYTES", 31)
    tail = b"junk" if damage == "junk" else compressed(b"", "gzip", damage)
    wire = compressed(b"x" * 31, "gzip") + tail + b"\0\0"
    result, error = decode(wire, "gzip")
    assert result == (b"x" * 31 if damage == "valid" else b"")
    assert (error is None) == (damage == "valid")
    if error:
        assert type(error) is httpx.DecodingError


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), httpx.ReadError("synthetic")])
def test_existing_failure_wins_without_decoding(monkeypatch, failure):
    monkeypatch.setattr(decoder, "MAX_DECODED_BYTES", 0)
    wire = compressed(b"x" * 100, "gzip")
    assert decode(wire, "gzip", failure) == (wire, failure)


@pytest.mark.parametrize("encoding", ["br", "zstd", "unknown-private-marker"])
async def test_optional_codecs_never_enter_unbounded_httpx_path(
    sessions, monkeypatch, encoding, caplog
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, headers={"content-encoding": encoding}, stream=httpx.ByteStream(b"{}")
        )

    google = client(handler, sessions, google_api_max_retries=4)
    # If the decoder constructs a buffered HTTPX response, fail before inflation.
    monkeypatch.setattr(decoder.httpx.Response, "read", lambda _: pytest.fail("unbounded decode"))
    try:
        with pytest.raises(GoogleHealthError) as caught:
            await google.list_data_points(REGISTRY["steps"], START, END)
        assert str(caught.value) == "Google Health response has an unsupported content encoding"
        assert error_summary(caught.value) == "GoogleHealthError (unsupported_encoding)"
        assert not is_transient(caught.value) and len(calls) == 1
        (row,) = await stored(sessions)
        assert row.body == b"{}" and row.complete and row.error == "UnsupportedResponseEncoding"
        assert "private-marker" not in caplog.text
    finally:
        await google.http.aclose()


def test_no_inflation_keeps_raw_body_behavior(monkeypatch):
    monkeypatch.setattr(decoder, "MAX_DECODED_BYTES", 1)
    assert decode(b"{}", "identity") == (b"{}", None)
    assert decoder.decode(b"{}", httpx.Headers(), None) == (b"{}", None)
    assert decode(gzip.compress(b""), "gzip") == (b"", None)


@pytest.mark.parametrize("encoding", ["deflate", "raw-deflate"])
@pytest.mark.parametrize("damage", ["junk", "zero", "stream"])
async def test_bytes_after_deflate_stream_are_archived_and_not_retried(
    sessions, encoding, damage, caplog
):
    wire = compressed(b'{"dataPoints": []}', encoding, damage)
    calls = []

    def handler(request):
        calls.append(request)
        headers = {"content-encoding": encoding.removeprefix("raw-")}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(wire))

    google = client(handler, sessions, google_api_max_retries=2)
    async with google.http:
        with pytest.raises(GoogleHealthError) as caught:
            await google.list_data_points(REGISTRY["steps"], START, END)
    assert error_summary(caught.value) == "GoogleHealthError (invalid_encoding)"
    assert not is_transient(caught.value) and len(calls) == 1
    assert caught.value.__cause__ is caught.value.__context__ is None
    (row,) = await stored(sessions)
    assert row.body == wire and row.complete and row.error == "TrailingEncodedBytes"
    assert "private-marker" not in caplog.text
