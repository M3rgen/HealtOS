"""One jsonb check for the webhook ingest and the record write path, #229's guard included."""

import json

import pytest

from app.google_health import jsonb
from app.services import webhook_metadata, webhook_processing


def test_webhook_modules_share_the_one_check_and_sentinel():
    for module in (webhook_metadata, webhook_processing):
        assert module.UNPARSED_BODY is jsonb.UNPARSED_BODY
        assert module.jsonb_storable is jsonb.jsonb_storable
    assert webhook_metadata.storable_text is jsonb.storable_text


def test_an_unparsed_body_is_never_storable():
    # #229: the sentinel is a plain object(); without the guard the walk would accept it.
    assert jsonb.jsonb_storable(jsonb.UNPARSED_BODY) is False
    assert webhook_metadata.parse_webhook_body(b'{"a": 1, "a": 2}') is jsonb.UNPARSED_BODY
    assert webhook_metadata.parse_webhook_body(b"\xff") is jsonb.UNPARSED_BODY


@pytest.mark.parametrize(
    "value",
    [
        "a\x00b",
        "\ud800",
        {"\udfff": 1},
        {"key\x00": 1},
        float("nan"),
        [1, [float("inf")]],
        json.loads('{"x": 1e400}'),
    ],
)
def test_values_postgresql_refuses(value):
    assert jsonb.jsonb_storable(value) is False


@pytest.mark.parametrize("value", [None, 0, 1.5, "ç\U0001f600", {"a": [True, None]}, []])
def test_ordinary_values_pass(value):
    assert jsonb.jsonb_storable(value) is True


def test_deep_nesting_has_no_recursion_limit_of_its_own():
    value: object = "x\x00"
    for _ in range(50_000):
        value = [value]
    assert jsonb.jsonb_storable(value) is False
