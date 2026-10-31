"""Ambiguous JSON never becomes a page or completion evidence."""

import httpx
import pytest
from raw_response_records import client, duplicate_body
from rejected_page_records import ENCODINGS, KINDS, LANES, client_rejects, rejected_page
from retrieval_records import END, START

from app.google_health.client import DuplicateResponseKeyError
from app.google_health.data_types import REGISTRY
from app.sync.errors import error_summary, is_transient


@pytest.mark.parametrize(
    "key", ["dataPoints", "rollupDataPoints", "nextPageToken", "private-marker"]
)
@pytest.mark.parametrize("mode", ["same", "different", "nested", "escaped"])
@pytest.mark.parametrize("endpoint", ["list_data_points", "get_identity", "list_paired_devices"])
async def test_client_rejects_duplicate_keys(key, mode, endpoint, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(duplicate_body(key, mode)))

    google = client(handler, google_api_max_retries=4)
    async with google.http:
        args = (REGISTRY["steps"], START, END) if endpoint == "list_data_points" else ()
        with pytest.raises(DuplicateResponseKeyError) as caught:
            await getattr(google, endpoint)(*args)
        assert error_summary(caught.value) == "DuplicateResponseKeyError (invalid_response)"
        assert not is_transient(caught.value) and len(calls) == 1
        assert caught.value.__cause__ is caught.value.__context__ is None
        assert "private-marker" not in str(caught.value) + caplog.text


KEYS = ["dataPoints", "rollupDataPoints", "nextPageToken"]
MODES = ["same", "different", "nested", "escaped"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("mode", MODES)
async def test_every_encoding_of_duplicate_keys_is_rejected(kind, key, mode, caplog):
    await client_rejects(kind, duplicate_body(key, mode), DuplicateResponseKeyError, caplog)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("first_page", [False, True])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", range(4))
async def test_duplicate_keys_preserve_durable_progress(
    sessions, caplog, lane, first_page, kind, case
):
    # The client matrices cover every key, mode and encoding; rotate them here.
    raw = duplicate_body(KEYS[case % 3], MODES[case])
    response = (raw, ENCODINGS[case], DuplicateResponseKeyError)
    await rejected_page(sessions, lane, first_page, 200, None, None, caplog, kind, response)


async def test_repeated_keys_in_separate_objects_are_valid():
    wire = b'{"dataPoints":[{"metadata":{"a":1}},{"metadata":{"a":2}}]}'
    google = client(lambda _: httpx.Response(200, stream=httpx.ByteStream(wire)))
    async with google.http:
        page = await google.list_data_points(REGISTRY["steps"], START, END)
        assert page.data_points == [{"metadata": {"a": 1}}, {"metadata": {"a": 2}}]
        assert page.next_page_token is None
