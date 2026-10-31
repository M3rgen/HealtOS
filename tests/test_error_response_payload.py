"""HTTP success cannot turn Google's error envelope into completion evidence."""

import json

import httpx
import pytest
from raw_response_records import client
from rejected_page_records import ENCODINGS, KINDS, LANES, client_rejects, rejected_page
from retrieval_records import END, START

from app.google_health.client import ErrorResponsePayloadError
from app.google_health.data_types import REGISTRY
from app.sync.errors import error_summary, is_transient

ERRORS = json.loads(
    '[{"code":503,"status":"UNAVAILABLE","message":"private-marker INVALID_PAGE_TOKEN"},'
    '{},null,false,true,0,1.5,"","private-marker HTTP 429",[],[0]]'
)


@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "endpoint", ["get_identity", "list_paired_devices", "steps", "floors", "total-calories"]
)
async def test_error_field_rejects_every_endpoint(error, full, endpoint, caplog):
    page = {"error": error}
    if full:
        page.update(dataPoints=[], rollupDataPoints=[], nextPageToken="saved")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=page)

    google = client(handler, google_api_max_retries=4)
    args = (REGISTRY[endpoint], START, END) if endpoint in REGISTRY else ()
    async with google.http:
        with pytest.raises(ErrorResponsePayloadError) as caught:
            await getattr(google, "list_data_points" if args else endpoint)(*args)
        assert error_summary(caught.value) == "ErrorResponsePayloadError (invalid_response)"
        assert not is_transient(caught.value) and len(calls) == 1
        assert caught.value.__cause__ is caught.value.__context__ is None
        assert "private-marker" not in str(caught.value) + caplog.text


def error_page(error, full):
    page = {"error": error}
    if full:
        page.update(dataPoints=[{"private-marker": 42}], rollupDataPoints=[], nextPageToken="saved")
    return ("  " + json.dumps(page) + "\n").encode()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("full", [False, True])
async def test_every_encoding_of_an_error_field_is_rejected(kind, error, full, caplog):
    await client_rejects(kind, error_page(error, full), ErrorResponsePayloadError, caplog)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("first_page", [False, True])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", range(4))
async def test_error_field_preserves_durable_progress(
    sessions, caplog, lane, first_page, kind, case
):
    # The client matrices cover every error value, shape and encoding; rotate them here.
    raw = error_page(ERRORS[case * 3], case % 2 == 1)
    response = (raw, ENCODINGS[case], ErrorResponsePayloadError)
    await rejected_page(sessions, lane, first_page, 200, None, None, caplog, kind, response)
