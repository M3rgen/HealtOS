"""Non-object collection items cannot authorize records or completion evidence."""

import json

import httpx
import pytest
from raw_response_records import client
from rejected_page_records import ENCODINGS, KINDS, LANES, client_rejects, rejected_page
from retrieval_records import END, START

from app.google_health.client import InvalidCollectionItemError
from app.google_health.data_types import REGISTRY

ITEMS = [None, False, True, 0, 1.5, "", "private-marker HTTP 503 INVALID_PAGE_TOKEN", [], [0]]


def payload(kind, value, position, token):
    points = [] if position is None else [{}, {"private-marker": "HTTP 429"}]
    points.insert(position or 0, value)
    key = "rollupDataPoints" if kind == "total-calories" else "dataPoints"
    return ("  " + json.dumps({key: points, "nextPageToken": token}) + "\n").encode()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("value", ITEMS)
@pytest.mark.parametrize("position", [None, 0, 1, 2])
@pytest.mark.parametrize("token", [None, "next"])
async def test_client_rejects_entire_collection(kind, value, position, token, caplog):
    raw = payload(kind, value, position, token)
    await client_rejects(kind, raw, InvalidCollectionItemError, caplog)
    assert str(InvalidCollectionItemError()) == (
        "Google Health response has a non-object collection item"
    )


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("first_page", [False, True])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", range(4))
async def test_items_preserve_durable_progress(sessions, caplog, lane, first_page, kind, case):
    # The client matrix covers every item, position, token and encoding; rotate them here.
    value, position, token = ITEMS[case * 2], (None, 0, 1, 2)[case], (None, "next")[case % 2]
    response = (payload(kind, value, position, token), ENCODINGS[case], InvalidCollectionItemError)
    await rejected_page(sessions, lane, first_page, 200, None, None, caplog, kind, response)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("points", [None, [], [{}, {"metadata": [None, False, 42, [], {}]}]])
@pytest.mark.parametrize("both", [False, True])
async def test_objects_keep_legacy_empty_and_field_precedence(kind, points, both):
    key, other = "dataPoints", "rollupDataPoints"
    if kind == "total-calories":
        key, other = other, key
    body = {} if points is None else {key: points}
    if both and points is not None:
        body[other] = ITEMS
    google = client(lambda _: httpx.Response(200, json=body))
    async with google.http:
        page = await google.list_data_points(REGISTRY[kind], START, END)
        assert page.data_points == (points or []) and page.next_page_token is None
