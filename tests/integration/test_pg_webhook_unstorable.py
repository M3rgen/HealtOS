"""Signed bodies jsonb cannot hold, against real PostgreSQL (see test_webhook_unstorable)."""

import pytest
from test_webhook_unstorable import KINDS
from test_webhook_unstorable import (
    test_body_only_batch_is_prepared_from_its_raw_body as check_body_only,
)
from test_webhook_unstorable import (
    test_clean_sibling_is_processed_and_redeliveries_deduplicate as check_sibling,
)
from test_webhook_unstorable import (
    test_guards_freeze_null_payloads_and_their_bodies as check_guards,
)
from test_webhook_unstorable import (
    test_signed_body_is_archived_verbatim_without_raw_json as check_archived,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", KINDS)
async def test_postgres_archives_unstorable_signed_body(pg_sessions, kind):
    await check_archived(pg_sessions, kind)


async def test_postgres_processes_clean_sibling_and_deduplicates(pg_sessions):
    await check_sibling(pg_sessions)


async def test_postgres_prepares_body_only_batch(pg_sessions):
    await check_body_only(pg_sessions)


async def test_postgres_guards_freeze_null_payloads(pg_sessions):
    await check_guards(pg_sessions)
