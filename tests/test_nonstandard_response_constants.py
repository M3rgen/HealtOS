"""Non-JSON constants cannot authorize records or completion evidence."""

import json

import httpx
import pytest
from raw_response_records import client
from rejected_page_records import ENCODINGS, KINDS, LANES, client_rejects, rejected_page
from retrieval_records import END, START, point

from app.google_health.client import NonStandardResponseConstantError
from app.google_health.data_types import REGISTRY
from app.sync.errors import error_summary, is_transient


def constant_body(constant, location, full, kind="steps"):
    value = "constant-placeholder"
    if location == "object":
        value = {"private-marker": value}
    elif location == "array":
        value = [{"private-marker": [value]}]
    records = [point(kind="steps")] if full else []
    if full and kind in REGISTRY:
        fields = records[0].pop("steps")
        fields["kcal" if kind == "total-calories" else "count"] = fields.pop("count")
        records[0][REGISTRY[kind].json_key] = fields
    key = "rollupDataPoints" if kind == "total-calories" else "dataPoints"
    payload = {key: records, "private-marker": value}
    if full and location != "top":
        records[0]["metadata"] = payload.pop("private-marker")
    return ("  " + json.dumps(payload).replace('"constant-placeholder"', constant) + "\n").encode()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("location", ["top", "object", "array"])
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "endpoint", ["get_identity", "list_paired_devices", "steps", "floors", "total-calories"]
)
async def test_client_rejects_nonstandard_constants(constant, location, full, endpoint, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        wire = constant_body(constant, location, full, endpoint)
        return httpx.Response(200, stream=httpx.ByteStream(wire))

    google = client(handler, google_api_max_retries=4)
    args = (REGISTRY[endpoint], START, END) if endpoint in REGISTRY else ()
    async with google.http:
        with pytest.raises(NonStandardResponseConstantError) as caught:
            await getattr(google, "list_data_points" if args else endpoint)(*args)
        assert error_summary(caught.value) == "NonStandardResponseConstantError (invalid_response)"
        assert not is_transient(caught.value) and len(calls) == 1
        assert caught.value.__cause__ is caught.value.__context__ is None
        assert all(s not in str(caught.value) + caplog.text for s in [constant, "private-marker"])


CONSTANTS = ["NaN", "Infinity", "-Infinity"]
LOCATIONS = ["top", "object", "array"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("constant", CONSTANTS)
@pytest.mark.parametrize("location", LOCATIONS)
@pytest.mark.parametrize("full", [False, True])
async def test_every_encoding_of_constants_is_rejected(kind, constant, location, full, caplog):
    raw = constant_body(constant, location, full, kind)
    await client_rejects(kind, raw, NonStandardResponseConstantError, caplog)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("first_page", [False, True])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", range(4))
async def test_constants_preserve_durable_progress(sessions, caplog, lane, first_page, kind, case):
    # The client matrices cover every constant, location and encoding; rotate them here.
    raw = constant_body(CONSTANTS[case % 3], LOCATIONS[case % 3], case % 2 == 1, kind)
    response = (raw, ENCODINGS[case], NonStandardResponseConstantError)
    await rejected_page(sessions, lane, first_page, 200, None, None, caplog, kind, response)


@pytest.mark.parametrize("kind", KINDS)
async def test_strings_and_standard_number_syntax_keep_existing_decoding(kind):
    values = '["NaN","Infinity","-Infinity",0,-12,1.25,1e3,1e400]'
    key = "rollupDataPoints" if kind == "total-calories" else "dataPoints"
    wire = ('{"' + key + '":[{"metadata":' + values + "}]}").encode()
    google = client(lambda _: httpx.Response(200, stream=httpx.ByteStream(wire)))
    async with google.http:
        page = await google.list_data_points(REGISTRY[kind], START, END)
        assert page.data_points == [{"metadata": json.loads(values)}]
        assert page.next_page_token is None
