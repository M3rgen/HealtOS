"""Only explicit Google error reasons may discard a durable continuation."""

import httpx
import pytest
from raw_response_records import client
from rejected_page_records import ENCODINGS, KINDS, LANES, rejected_page
from retrieval_records import END, START

from app.google_health.client import GoogleHealthError, InvalidPageTokenError
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import iter_pages

CODE = b'"INVALID_PAGE_TOKEN"'
MISLEADING = [
    b'{"unrelated":' + CODE + b"}",
    b'{"error":{"message":"NOT_INVALID_PAGE_TOKEN"}}',
    b'{"error":{"message":"INVALID_PAGE_TOKEN_SUFFIX"}}',
    b'{"error":{"message":"INVALID_PAGE_TOKENS"}}',
    '{"error":{"message":"ÇINVALID_PAGE_TOKEN"}}'.encode(),
    *[b'{"error":' + value + b"}" for value in [b"null", b"[]", CODE, b"false", b"400"]],
    b'{"error":{"unrelated":' + CODE + b"}}",
    b'{"error":{"status":' + CODE + b"}}",
    b'{"error":{"details":{"reason":' + CODE + b"}}}",
    b'{"error":{"details":[' + CODE + b',null,{"reason":["INVALID_PAGE_TOKEN"]}]}}',
    b'{"error":{"details":[{"reason":"NOT_INVALID_PAGE_TOKEN"}]}}',
    b'{"error":{"details":[{"metadata":{"reason":' + CODE + b"}}]}}",
    b'{"error":{"fieldViolations":[{"reason":' + CODE + b"}]}}",
    b'{"error":{"details":[{"fieldViolations":{"reason":' + CODE + b"}}]}}",
    b'{"error":{"details":[{"fieldViolations":[[{"reason":' + CODE + b"}]]}]}}",
    b'{"error":{"details":[{"fieldViolations":[null,{"description":' + CODE + b"}]}]}}",
    b'{"error":{"details":[{"fieldViolations":[{"reason":"NOT_INVALID_PAGE_TOKEN"}]}]}}',
    b'{"error":{"details":[{"fieldViolations":[{"metadata":{"reason":' + CODE + b"}}]}]}}",
    b'{"error":{"message":' + CODE + b',"message":' + CODE + b"}}",
    b'{"error":{"message":' + CODE + b'},"private-marker":NaN}',
    b'{"error":{"message":' + CODE + b"}",
    *[b'{"error":{"message":' + b"[" * n + CODE + b"]" * n + b"}}" for n in [400, 100_000]],
]
# Shapes observed in Google Health 400 responses: ErrorInfo carries the reason,
# BadRequest carries fieldViolations; the message is free text.
ENVELOPE = (
    b'{"error":{"code":400,"message":"Request contains an invalid argument.",'
    b'"status":"INVALID_ARGUMENT",'
)
EXPLICIT = [
    b'{"error":{"message":' + CODE + b"}}",
    b'{"error":{"message":"private-marker HTTP 503 INVALID_PAGE_TOKEN"}}',
    b'{"error":{"message":"Page token expired (INVALID_PAGE_TOKEN)."}}',
    b'{"error":{"details":[null,{}, {"reason":' + CODE + b"}]}}",
    ENVELOPE
    + b'"details":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":'
    + CODE
    + b',"domain":"health.googleapis.com","metadata":{"detailedReasons":"private-marker"}}]}}',
    ENVELOPE
    + b'"details":[{"@type":"type.googleapis.com/google.rpc.BadRequest",'
    + b'"fieldViolations":['
    + b'{"field":"pageToken","description":"private-marker"},'
    + b'{"field":"pageToken","description":"expired","reason":'
    + CODE
    + b"}]}]}}",
]


@pytest.mark.parametrize("raw", MISLEADING + EXPLICIT, ids=lambda raw: f"{len(raw)}B")
@pytest.mark.parametrize("second_status", [200, 400])
async def test_client_reset_requires_explicit_evidence(raw, second_status, caplog):
    calls, resets = [], []

    def handler(request):
        calls.append(request.url.params.get("pageToken"))
        return httpx.Response(
            400 if len(calls) == 1 else second_status,
            content=raw if len(calls) == 1 or second_status == 400 else b"{}",
        )

    async def reset():
        resets.append(True)

    explicit = raw in EXPLICIT
    google = client(handler, google_api_max_retries=4)
    async with google.http:
        pages = iter_pages(google, REGISTRY["steps"], START, END, "saved", on_reset=reset)
        if explicit and second_status == 200:
            assert len([page async for page in pages]) == 1
        else:
            with pytest.raises(GoogleHealthError) as caught:
                _ = [page async for page in pages]
            assert type(caught.value) is (InvalidPageTokenError if explicit else GoogleHealthError)
            assert caught.value.__cause__ is caught.value.__context__ is None
    assert (calls, resets) == ((["saved", None], [True]) if explicit else (["saved"], []))
    assert "private-marker" not in caplog.text


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", range(len(MISLEADING)))
async def test_misleading_400_preserves_checkpoint(sessions, caplog, lane, kind, case):
    caplog.set_level("INFO")
    response = (MISLEADING[case], ENCODINGS[case % len(ENCODINGS)], GoogleHealthError)
    await rejected_page(sessions, lane, False, 400, None, None, caplog, kind, response)
