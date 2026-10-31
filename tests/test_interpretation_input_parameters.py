"""Production input reads at driver limits, without a PostgreSQL server."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from derived_records import BAND, DAY, T0, manual, point, snapshot, store
from interpretation_records import call
from interpretation_records import production_reader as production_reader
from sqlalchemy import event, insert
from sqlalchemy.dialects.postgresql.asyncpg import dialect

from app.models.identity import RawSourceDevice
from app.models.manual import ManualMeasurement
from app.models.manual_flags import ManualMeasurementFlagEvent
from app.models.state import RawHealthData
from app.parsers.common import canonical_json, payload_hash
from app.services import interpretation_input_state as state
from app.services import interpretation_inputs as inputs
from app.services.manual_flags import record_flag


def raw_rows(count):
    return [
        dict(
            id=UUID(int=i + 1),
            data_type="weight" if i % 2 == 0 else "height",
            google_data_point_id=f"synthetic/parameters/{i}",
            raw_json={"dataSource": {"device": {"displayName": str(i if i != 1 else 0)}}},
            payload_hash="a" * 64,
            source_start_time=T0,
            source_end_time=T0,
            source_timezone="UTC",
            source_deleted_at=None,
            is_quarantined=False,
            first_seen_at=T0,
            last_seen_at=T0,
        )
        for i in range(count)
    ]


def measurements(count):
    return [
        dict(id=UUID(int=i + 1), metric="weight", value=70, measured_at=T0)
        for i in reversed(range(count))
    ]


async def read(session, manual=True):
    return await inputs.load_inputs(session, DAY, DAY, "UTC", {"weight", "height"}, manual=manual)


@pytest.mark.parametrize("raw_count,manual_count", [(16_384, 0), (0, 32_768), (16_384, 32_768)])
async def test_postgres_expanded_parameters_through_load_inputs(raw_count, manual_count):
    # Together these fit the unchanged 50,000-record and 16-MiB budgets.
    raw, measured = raw_rows(raw_count), measurements(manual_count)
    size = sum(len(canonical_json(r["raw_json"]).encode()) for r in raw)
    assert len(raw) + len(measured) <= inputs.MAX_INPUT_RECORDS
    assert size < inputs.MAX_INPUT_BYTES
    session = Mock()
    session.get_bind.return_value.dialect = dialect()
    statements = []

    def compile_statement(statement):
        compiled = statement.compile(dialect=dialect(), compile_kwargs={"render_postcompile": True})
        assert len(compiled.positiontup) <= 32_767
        statements.append(compiled)

    async def execute(statement):
        compile_statement(statement)
        return Mock(one=lambda: (len(raw), size), all=lambda: [])

    results = iter(
        [
            [SimpleNamespace(**r) for r in raw],
            [r["id"] for r in raw],
            [SimpleNamespace(**r) for r in measured],
            [],
            [],
            [],
            [],
        ]
    )

    async def scalars(statement):
        compile_statement(statement)
        return next(results)

    session.execute, session.scalars = execute, scalars
    loaded = await read(session)
    assert len(loaded.rows) == raw_count and len(loaded.measurements) == manual_count
    decisions = [s for s in statements if str(s).startswith("SELECT raw_source_devices.id")]
    flags = [s for s in statements if str(s).startswith("SELECT manual_measurement_flag_events.id")]
    assert len(decisions) == (2 if raw_count else 0)
    assert all(len(s.positiontup) == 2 for s in decisions)
    assert all("ANY" in str(s) for s in decisions)
    assert len(flags) == 1 and len(flags[0].positiontup) < 10
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


async def test_sqlite_large_reads_keep_pairs_order_latest_and_global_flag_budget(
    sessions, monkeypatch
):
    raw, measured = raw_rows(16_384), measurements(32_768)
    decisions, expected = [], {}
    for i, row in enumerate(raw):
        key = row["data_type"], payload_hash(row["raw_json"]["dataSource"])
        if i % 5:
            expected[key] = str(i)
            decisions.append(
                dict(data_type=key[0], fingerprint=key[1], device=str(i), decided_at=T0)
            )
        if i > 1:  # Crossed keys must never enter the result, even if a key has no decision.
            decisions.append(
                dict(
                    data_type="height" if key[0] == "weight" else "weight",
                    fingerprint=key[1],
                    device="wrong",
                    decided_at=T0,
                )
            )
    key = next(iter(expected))
    decisions.append(dict(data_type=key[0], fingerprint=key[1], device="latest", decided_at=T0))
    expected[key] = "latest"
    flags = [
        dict(
            id=i + 1,
            measurement_id=UUID(int=i % 32_768 + 1),
            flag="test",
            action="mark" if i < 32_768 else "unmark",
            created_at=T0,
        )
        for i in reversed(range(inputs.MAX_INPUT_RECORDS))
    ]
    async with sessions.begin() as session:
        for model, rows in (
            (RawHealthData, raw),
            (ManualMeasurement, measured),
            (RawSourceDevice, decisions),
            (ManualMeasurementFlagEvent, flags),
        ):
            await session.execute(insert(model), rows)
    signatures, parameters = [], []
    original = inputs.payload_hash

    def capture(value):
        if isinstance(value, dict) and "flags" in value:
            signatures.append(value)
        return original(value)

    monkeypatch.setattr(inputs, "payload_hash", capture)
    async with sessions.begin() as session:

        def check_parameters(conn, cursor, statement, values, context, many):
            assert session.in_transaction()
            parameters.append(len(values))
            assert len(values) <= 999

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", check_parameters)
        try:
            loaded = await read(session)
        finally:
            event.remove(engine, "before_cursor_execute", check_parameters)
        assert len(loaded.rows) == len(raw) and loaded.devices == dict(sorted(expected.items()))
        assert list(loaded.devices) == sorted(expected)
        assert [r.id.int for r in loaded.measurements] == list(range(1, 17_233))
        assert signatures[-1]["flags"] == [
            [str(r["id"]), r["action"]] for r in sorted(flags, key=lambda r: r["id"])
        ]
        assert [(r[0], r[1]) for r in signatures[-1]["devices"]] == sorted(expected)
        assert max(parameters) <= 901
        monkeypatch.setattr(state, "_SQLITE_FINGERPRINTS", 137)
        assert (await read(session)).fingerprint == loaded.fingerprint
    # One more event exceeds the global event budget, regardless of page size.
    async with sessions.begin() as session:
        await session.execute(
            insert(ManualMeasurementFlagEvent),
            [dict(measurement_id=UUID(int=1), flag="test", action="mark", created_at=T0)],
        )
    for limit in (1, 200):
        assert (await call("longitudinal", limit=limit))["code"] == "input_budget_exceeded"


async def test_empty_and_disabled_manual_windows_ignore_unrelated_flags(sessions):
    left, stop = inputs.input_bounds(DAY, DAY, "UTC")
    at_stop = await manual(sessions, 70, at=stop)
    outside = await manual(sessions, 70, at=left - timedelta(seconds=1))
    async with sessions.begin() as session:
        for key in (at_stop, outside):
            await record_flag(session, key, "test", True)
        empty = await read(session)
        assert empty.rows == empty.measurements == [] and empty.devices == {}
        baseline = empty.fingerprint
    inside = await manual(sessions, 70)
    async with sessions.begin() as session:
        await record_flag(session, inside, "test", True)
        assert (await read(session, manual=False)).fingerprint == baseline
        assert (await read(session)).fingerprint != baseline


@pytest.mark.parametrize("mutation", ["flag", "device"])
async def test_round_trip_events_expire_cursor_without_changing_values(sessions, mutation):
    measured = await manual(sessions, 70)
    await store(
        sessions,
        "weight",
        point(
            "weight", "one", dict(sampleTime={"physicalTime": T0.isoformat()}, weightGrams=72000)
        ),
    )
    decision = dict(data_type="weight", fingerprint=payload_hash(BAND), decided_at=T0)
    async with sessions.begin() as session:
        session.add(RawSourceDevice(**decision, device="original"))
    before = await call("longitudinal", limit=1)
    assert before["next_cursor"]
    async with sessions() as session:
        archive = await snapshot(session)
        fingerprint = (await read(session)).fingerprint
    for changed in (True, False):
        async with sessions.begin() as session:
            if mutation == "flag":
                await record_flag(session, measured, "test", changed)
            else:
                session.add(
                    RawSourceDevice(**decision, device="changed" if changed else "original")
                )
        assert (await call("longitudinal", cursor=before["next_cursor"]))["code"] == "stale_cursor"
    assert (await call("longitudinal", limit=1))["summaries"] == before["summaries"]
    async with sessions() as session:
        assert (await read(session)).fingerprint != fingerprint
        after = await snapshot(session)
        for name in archive.keys() - {"raw_source_devices", "manual_measurement_flag_events"}:
            assert after[name] == archive[name]
