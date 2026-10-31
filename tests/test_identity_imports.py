"""Legacy identity imports and caller-owned transactions survive the module split."""

import importlib
import subprocess
import sys

import pytest
from identity_records import HEART, T0, heart, legacy
from sqlalchemy import func, select

from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.models.raw_versions import RawHealthDataEvent
from app.models.state import RawHealthData
from app.parsers.common import payload_hash, source_id
from app.parsers.identity import record_identity
from app.services import identity


@pytest.mark.parametrize(
    ("module", "names"),
    [
        (
            "identity_common",
            "aliases among rows_by_key StoredRow PlannedLink Resolution MAX_CHAIN SPARE_BLOCK",
        ),
        ("identity_presence", "present_records Presence deferred_summary"),
        ("identity_writes", "add_aliases add_links"),
        ("identity_verification", "verify_identities"),
    ],
)
def test_legacy_exports_share_the_implementation(module, names):
    implementation = importlib.import_module(f"app.services.{module}")
    for name in names.split():
        assert getattr(identity, name) is getattr(implementation, name)


@pytest.mark.parametrize(
    "module",
    [
        "identity_common",
        "identity_resolution",
        "identity_presence",
        "identity_writes",
        "identity_verification",
    ],
)
def test_extracted_modules_import_without_entry_point_or_callers(module):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import app.services.{module}; import sys; "
            "assert 'app.services.identity' not in sys.modules; "
            "assert 'app.services.storage' not in sys.modules; "
            "assert 'app.services.identity_backfill' not in sys.modules; "
            "import app.services.storage; import app.sync.deletions; "
            "import app.services.identity_backfill; import app.cli.main",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_aliases_links_and_merge_events_use_the_callers_transaction(sessions):
    old, new = heart(60), heart(60, civil=True)
    old_id = await legacy(sessions, HEART, old, T0)
    new_id = await legacy(sessions, HEART, new, T0)
    old_key, new_key = source_id(HEART, old), source_id(HEART, new)
    form = record_identity(HEART, old)
    assert form is not None
    planned = identity.PlannedLink(
        old_key, new_key, "interval-shape-change", form.source, form.source, payload_hash(new)
    )
    raw_ids = {old_key: old_id, new_key: new_id}

    async with sessions() as session:
        before = await identity.verify_identities(session)
        assert before[1] == []
        assert await identity.rows_by_key(session, HEART.name, raw_ids) == raw_ids
        assert await identity.add_aliases(session, HEART.name, {form.specific_key: new_key}) == 1
        assert await identity.add_links(session, HEART.name, [planned], raw_ids, T0) == 1
        assert await identity.add_links(session, HEART.name, [planned], raw_ids, T0) == 0
        assert await identity.aliases(session, HEART.name, [form.specific_key]) == {
            form.specific_key: new_id
        }
        assert await session.scalar(select(func.count()).select_from(RawHealthDataEvent)) == 1
        await session.rollback()

    async with sessions() as session:
        for table in (RawHealthDataIdentity, RawHealthDataSupersession, RawHealthDataEvent):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
        rows = (await session.scalars(select(RawHealthData).order_by(RawHealthData.id))).all()
        assert {row.id: row.raw_json for row in rows} == {old_id: old, new_id: new}
        assert await identity.verify_identities(session) == before
