"""Compressed responses remain verbatim through storage and CLI export."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from compressed_records import compressed
from raw_response_records import SPEC, body, client, steps, stored
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from app.cli import main as cli
from app.db.base import Base
from app.google_health import client as client_module
from app.google_health.client import ResponseStreamError
from app.models.raw_versions import RawApiResponse
from app.services.responses import verify_responses
from app.services.storage import store_page


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate", "identity"])
def test_a_compressed_response_is_kept_as_it_came_off_the_wire(
    tmp_path, monkeypatch, encoding
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wire.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    decoded = body(steps(7))
    wire = compressed(decoded, encoding)
    encoding = encoding.removeprefix("raw-")

    def handler(request: httpx.Request) -> httpx.Response:
        # Sent compressed although the client asked for identity; a stream, as on a socket.
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": encoding},
            stream=httpx.ByteStream(wire),
        )

    async def fetch() -> RawApiResponse:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        start = datetime(2026, 8, 17, tzinfo=UTC)
        page = await client(handler).list_data_points(SPEC, start, start + timedelta(hours=1))
        assert page.data_points == [steps(7)]  # parsed from the decoded body
        async with sessions.begin() as session:
            await store_page(session, SPEC, page)
        (row,) = await stored(sessions)
        async with sessions() as session:
            assert await verify_responses(session) == (1, [])
        return row

    row = asyncio.run(fetch())
    assert row.body == wire and (row.body == decoded) == (encoding == "identity")
    assert row.body_sha256 == hashlib.sha256(wire).hexdigest()
    assert row.body_size == len(wire)
    assert row.headers["content-encoding"] == encoding and row.complete and row.error is None
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    target = tmp_path / "page.bin"
    exported = CliRunner().invoke(cli.app, ["export-response", "1", "--output", str(target)])
    assert exported.exit_code == 0, exported.output
    assert target.read_bytes() == wire
    assert f'"content_encoding": "{encoding}"' in exported.output
    asyncio.run(engine.dispose())


@pytest.mark.parametrize("damage", ["valid", "truncated", "junk", "zero", "stream"])
@pytest.mark.parametrize("encoding", ["deflate", "raw-deflate"])
@pytest.mark.parametrize("position", ["inner", "outer"])
def test_each_stacked_compression_layer_must_finish(monkeypatch, encoding, position, damage):
    from app.google_health import response_decode as decoder
    from app.google_health.response_decode import decode

    original = decoder._inflate

    def inflate(wire, wbits, body, limit):
        assert encoding == "raw-deflate" or wbits > 0  # Wrapped suffixes cannot fall back.
        assert limit == 32 * 1024 * 1024  # Health archives keep their existing ceiling.
        return original(wire, wbits, body, limit)

    monkeypatch.setattr(decoder, "_inflate", inflate)
    formats = [encoding, "gzip"] if position == "inner" else ["gzip", encoding]
    wire = compressed(
        compressed(b"{}", formats[0], damage if position == "inner" else "valid"),
        formats[1],
        damage if position == "outer" else "valid",
    )
    encodings = [e.removeprefix("raw-") for e in formats]
    result, error = decode(wire, httpx.Headers({"content-encoding": ", ".join(encodings)}), None)
    assert result == (b"{}" if damage == "valid" else b"")
    assert (error is None) == (damage == "valid")


async def test_unexpected_decode_failure_still_archives_the_wire_bytes(sessions, monkeypatch):
    def broken_decode(*_args):
        raise RuntimeError("private-marker")

    monkeypatch.setattr(client_module, "_decode", broken_decode)
    wire = compressed(body(steps(7)), "gzip")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=wire, headers={"content-encoding": "gzip"})

    google = client(handler, sessions, google_api_max_retries=3)
    start = datetime(2026, 8, 17, tzinfo=UTC)
    async with google.http:
        with pytest.raises(ResponseStreamError) as caught:
            await google.list_data_points(SPEC, start, start + timedelta(hours=1))
    assert "private-marker" not in str(caught.value) and len(calls) == 1
    (row,) = await stored(sessions)
    assert row.body == wire and row.complete and row.error == "RuntimeError"
