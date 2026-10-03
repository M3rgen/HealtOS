from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api import webhooks
from app.core.config import Settings


@pytest.fixture
def application():
    application = FastAPI()
    application.state.settings = Settings(
        _env_file=None,
        google_health_webhook_authorization="Bearer local-test",
        google_health_webhook_max_body_bytes=1024,
    )
    application.state.runtime = SimpleNamespace()
    application.include_router(webhooks.router)
    return application


async def post_stream(application, chunks, headers):
    read = []

    async def body():
        for chunk in chunks:
            read.append(len(chunk))
            yield chunk

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        response = await client.post("/webhooks/google-health", content=body(), headers=headers)
    return response, read


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
async def test_unauthorized_webhook_never_reads_body(application, headers):
    response, read = await post_stream(application, [b"x" * 2048], headers)
    assert response.status_code == 401
    assert read == []


async def test_unconfigured_webhook_never_reads_body(application):
    application.state.settings.google_health_webhook_authorization = "CHANGE_ME"
    response, read = await post_stream(application, [b"x" * 2048], {})
    assert response.status_code == 503
    assert read == []


async def test_oversized_declared_body_is_rejected_before_read(application):
    response, read = await post_stream(
        application,
        [b"x" * 2048],
        {"Authorization": "Bearer local-test", "Content-Length": "2048"},
    )
    assert response.status_code == 413
    assert read == []


@pytest.mark.parametrize("length", [None, "1"])
async def test_chunked_or_understated_body_cannot_exceed_limit(application, length):
    headers = {"Authorization": "Bearer local-test"}
    if length is not None:
        headers["Content-Length"] = length
    response, read = await post_stream(application, [b"a" * 600, b"b" * 600, b"c" * 600], headers)
    assert response.status_code == 413
    assert read == [600, 600]


async def test_exact_limit_verification_is_accepted(application):
    body = b'{"type":"verification"}'
    body += b" " * (1024 - len(body))
    response, read = await post_stream(
        application, [body[:500], body[500:]], {"Authorization": "Bearer local-test"}
    )
    assert response.status_code == 201
    assert read == [500, 524]


@pytest.mark.parametrize("length", ["-1", "abc", "9" * 100, "1,2"])
async def test_invalid_lengths_are_rejected_without_reading(application, length):
    response, read = await post_stream(
        application, [b"{}"], {"Authorization": "Bearer local-test", "Content-Length": length}
    )
    assert response.status_code == 400
    assert read == []


async def test_duplicate_content_length_rejected(application):
    response, read = await post_stream(
        application,
        [b"{}"],
        [("Authorization", "Bearer local-test"), ("Content-Length", "2"), ("Content-Length", "2")],
    )
    assert response.status_code == 400
    assert read == []


async def test_exact_limit_signed_body_preserves_bytes(application):
    body = b'{ "data": {"label": "\\u00e7"} }'
    body += b" " * (1024 - len(body))
    result = SimpleNamespace(
        batch_hash="test", element_count=1, inserted=True, should_process=False
    )
    verifier = AsyncMock()
    ingest = AsyncMock(return_value=result)
    application.state.runtime = SimpleNamespace(
        webhook_verifier=SimpleNamespace(verify=verifier),
        webhook_processor=SimpleNamespace(ingest_verified=ingest),
    )
    response, _ = await post_stream(
        application,
        [body[:17], body[17:]],
        {"Authorization": "Bearer local-test", "GOOGLE-HEALTH-API-SIGNATURE": "signed"},
    )
    assert response.status_code == 204
    verifier.assert_awaited_once_with(body, "signed")
    ingest.assert_awaited_once_with(body, {"data": {"label": "ç"}})
