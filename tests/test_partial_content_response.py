"""HTTP completeness does not make a partial collection a successful retrieval."""

from itertools import product

import pytest
from rejected_page_records import KINDS, LANES, rejected_page

CASES = list(product([None, "private-marker"], [None, "bytes 0-99/200", "private-marker"]))


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("first_page", [False, True])
@pytest.mark.parametrize("case", range(len(CASES)))
async def test_partial_content_preserves_durable_progress(sessions, caplog, lane, first_page, case):
    # Every token/Content-Range pair runs in both lanes; the kinds rotate across them.
    token, content_range = CASES[case]
    await rejected_page(
        sessions, lane, first_page, 206, token, content_range, caplog, KINDS[case % 3]
    )
