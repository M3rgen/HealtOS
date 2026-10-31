"""Test-entry flags on manual measurements: append-only events, read by every profile reader
(migration 20261024_0014; SQLite, synthetic values only)."""

from __future__ import annotations

import asyncio
import importlib.util
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from app.cli import main as cli
from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.models import manual_flags as flag_models
from app.models.manual import ManualMeasurement
from app.models.manual_flags import ManualMeasurementFlagEvent
from app.services import codex_health
from app.services.codex_health import read_health
from app.services.manual_flags import excluded_measurement_ids, flag_schema_errors
from app.services.raw_history import PROTECTIVE_TRIGGERS, SQLITE_TRIGGER_SQL, verify_guards

EARLIER = datetime(2026, 9, 20, 7, 30, tzinfo=UTC)
LATER = datetime(2026, 9, 28, 7, 30, tzinfo=UTC)
END_DATE = "2026-09-29"
TABLE = "manual_measurement_flag_events"


async def refused(sessions, statement: str) -> None:
    with pytest.raises((IntegrityError, OperationalError), match="refused"):
        async with sessions.begin() as session:
            await session.execute(text(statement))


async def raw_row(sessions, measurement_id: uuid.UUID) -> tuple:
    """The measurement row exactly as stored (every column, as SQLite holds it)."""
    async with sessions() as session:
        row = await session.execute(
            text("SELECT * FROM manual_measurements WHERE id = :id"),
            {"id": measurement_id.hex},
        )
        return tuple(row.one())


async def events(sessions) -> list[tuple]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                ManualMeasurementFlagEvent.measurement_id,
                ManualMeasurementFlagEvent.flag,
                ManualMeasurementFlagEvent.action,
                ManualMeasurementFlagEvent.reason,
            ).order_by(ManualMeasurementFlagEvent.id)
        )
        return list(rows.tuples())


@pytest.fixture
async def client(sessions):
    application = create_app()

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://localhost",
        headers={"X-HealthOS-Request": "1"},
    ) as http:
        yield http
    await application.state.runtime.shutdown()


async def add_weights(client) -> tuple[uuid.UUID, uuid.UUID]:
    ids = []
    for value, at in (("80.0", EARLIER), ("99.0", LATER)):
        response = await client.post(
            "/api/dashboard/measurements",
            json={"metric": "weight", "value": value, "measured_at": at.isoformat()},
        )
        assert response.status_code == 201
        ids.append(uuid.UUID(response.json()["id"]))
    return ids[0], ids[1]


async def profile_weight(client) -> float:
    response = await client.get(f"/api/dashboard?end_date={END_DATE}")
    return response.json()["profile"]["weight"]["value"]


async def umay_weight(sessions, monkeypatch) -> float:
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    result = await read_health(
        {"action": "daily", "start_date": "2026-09-23", "end_date": END_DATE}
    )
    return result["profile"]["weight"]["value"]


async def test_a_test_entry_leaves_the_profile_and_comes_back_when_unflagged(
    sessions, client, monkeypatch
) -> None:
    earlier, later = await add_weights(client)
    original = await raw_row(sessions, later)
    assert await profile_weight(client) == 99.0
    assert await umay_weight(sessions, monkeypatch) == 99.0

    response = await client.post(
        f"/api/dashboard/measurements/{later}/flag", json={"flag": "test", "reason": " deneme "}
    )
    assert response.status_code == 201
    assert response.json() == {"id": str(later), "flags": ["test"], "changed": True}
    # The profile card, the 3D body (both read data.profile) and Umay fall back to the
    # measurement before it.
    assert await profile_weight(client) == 80.0
    assert await umay_weight(sessions, monkeypatch) == 80.0
    async with sessions() as session:
        assert await excluded_measurement_ids(session) == {later}

    response = await client.post(f"/api/dashboard/measurements/{later}/unflag")
    assert response.status_code == 201
    assert response.json() == {"id": str(later), "flags": [], "changed": True}
    assert await profile_weight(client) == 99.0
    assert await umay_weight(sessions, monkeypatch) == 99.0
    async with sessions() as session:
        assert await excluded_measurement_ids(session) == set()

    # Both decisions were appended; the measurement itself is exactly as stored.
    assert await events(sessions) == [
        (later, "test", "mark", "deneme"),
        (later, "test", "unmark", None),
    ]
    assert await raw_row(sessions, later) == original
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ManualMeasurement)) == 2
    assert earlier != later


async def test_a_metric_with_only_test_entries_has_no_profile_value(sessions, client) -> None:
    response = await client.post(
        "/api/dashboard/measurements",
        json={"metric": "muscle", "value": "42.5", "measured_at": LATER.isoformat()},
    )
    measurement = response.json()["id"]
    await client.post(f"/api/dashboard/measurements/{measurement}/flag")
    profile = (await client.get(f"/api/dashboard?end_date={END_DATE}")).json()["profile"]
    # Missing stays missing: no value, never a zero.
    assert "muscle" not in profile


async def test_the_measurement_list_keeps_flagged_entries_with_their_flag(client) -> None:
    earlier, later = await add_weights(client)
    await client.post(f"/api/dashboard/measurements/{later}/flag")
    rows = (await client.get("/api/dashboard/measurements")).json()
    assert [(row["id"], row["flags"]) for row in rows] == [
        (str(later), ["test"]),
        (str(earlier), []),
    ]
    # The fields it had are unchanged.
    assert set(rows[0]) == {"id", "metric", "value", "measured_at", "source", "flags"}
    assert rows[0]["source"] == "Manuel"


async def test_flag_api_is_idempotent_and_checks_the_measurement(sessions, client) -> None:
    _, later = await add_weights(client)
    missing = uuid.uuid4()
    assert (await client.post(f"/api/dashboard/measurements/{missing}/flag")).status_code == 404
    assert (await client.post(f"/api/dashboard/measurements/{missing}/unflag")).status_code == 404
    assert (await client.post("/api/dashboard/measurements/not-an-id/flag")).status_code == 422
    # Clearing a flag that is not set adds nothing.
    response = await client.post(f"/api/dashboard/measurements/{later}/unflag")
    assert (response.status_code, response.json()["changed"]) == (200, False)
    assert await events(sessions) == []

    assert (await client.post(f"/api/dashboard/measurements/{later}/flag")).status_code == 201
    again = await client.post(f"/api/dashboard/measurements/{later}/flag")
    assert again.status_code == 200
    assert again.json() == {"id": str(later), "flags": ["test"], "changed": False}
    assert len(await events(sessions)) == 1

    for invalid in ({"flag": "other"}, {"reason": "x" * 201}):
        response = await client.post(f"/api/dashboard/measurements/{later}/flag", json=invalid)
        assert response.status_code == 422
    # Like every manual write: a deliberate same-origin request only.
    response = await client.post(
        f"/api/dashboard/measurements/{later}/unflag", headers={"X-HealthOS-Request": "0"}
    )
    assert response.status_code == 403
    assert len(await events(sessions)) == 1


async def test_the_latest_event_wins(sessions) -> None:
    async with sessions.begin() as session:
        measurement = ManualMeasurement(metric="weight", value=Decimal("70"), measured_at=EARLIER)
        tie = ManualMeasurement(metric="weight", value=Decimal("71"), measured_at=EARLIER)
        session.add_all([measurement, tie])
    base = datetime(2026, 10, 1, tzinfo=UTC)

    async def append(measurement_id, action, at) -> None:
        async with sessions.begin() as session:
            await session.execute(
                insert(ManualMeasurementFlagEvent).values(
                    measurement_id=measurement_id, flag="test", action=action, created_at=at
                )
            )

    async def excluded() -> set[uuid.UUID]:
        async with sessions() as session:
            return await excluded_measurement_ids(session)

    await append(measurement.id, "mark", base)
    assert await excluded() == {measurement.id}
    # Inserted later but decided earlier: the newer decision stands.
    await append(measurement.id, "unmark", base - timedelta(hours=1))
    assert await excluded() == {measurement.id}
    await append(measurement.id, "unmark", base + timedelta(hours=1))
    assert await excluded() == set()
    await append(measurement.id, "mark", base + timedelta(hours=2))
    assert await excluded() == {measurement.id}
    # Same time: the later row wins.
    await append(tie.id, "mark", base)
    await append(tie.id, "unmark", base)
    assert await excluded() == {measurement.id}


async def test_flag_events_and_measurements_refuse_update_and_delete(sessions, client) -> None:
    _, later = await add_weights(client)
    await client.post(f"/api/dashboard/measurements/{later}/flag")
    for statement in (
        f"UPDATE {TABLE} SET action = 'unmark'",
        f"UPDATE {TABLE} SET reason = 'x'",
        f"DELETE FROM {TABLE}",
        "UPDATE manual_measurements SET value = 1",
        "UPDATE manual_measurements SET updated_at = CURRENT_TIMESTAMP",
        "DELETE FROM manual_measurements",
    ):
        await refused(sessions, statement)
    assert [event[2] for event in await events(sessions)] == ["mark"]
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(
                ManualMeasurementFlagEvent(measurement_id=later, flag="test", action="remove")
            )


async def test_verify_checks_the_flag_guards(sessions) -> None:
    names = {f"{TABLE}_no_update", f"{TABLE}_no_delete"}
    assert names <= set(SQLITE_TRIGGER_SQL)
    assert {(TABLE, f"{TABLE}_append_only"), (TABLE, f"{TABLE}_no_truncate")} <= {
        (table, trigger) for table, trigger, _ in PROTECTIVE_TRIGGERS
    }
    async with sessions() as session:
        assert await verify_guards(session) == []
    async with sessions.begin() as session:
        await session.execute(text(f"DROP TRIGGER {TABLE}_no_delete"))
    async with sessions() as session:
        assert await verify_guards(session) == [
            f"protective trigger missing: {TABLE}.{TABLE}_no_delete"
        ]


def test_model_and_migration_ddl_are_the_same() -> None:
    path = Path("migrations/versions/20261024_0014_manual_measurement_flags.py")
    spec = importlib.util.spec_from_file_location("migration_0014", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def normalize(statements) -> list[str]:
        return [re.sub(r"\s+", " ", statement).strip() for statement in statements]

    assert normalize(migration.POSTGRESQL_DDL) == normalize(flag_models.POSTGRESQL_DDL)
    assert normalize(migration.SQLITE_DDL) == normalize(flag_models.SQLITE_DDL)
    assert migration.FLAG_TABLES == flag_models.FLAG_TABLES
    assert migration.PG_TRIGGER_SQL == flag_models.PG_TRIGGER_SQL
    assert migration.revision == "20261024_0014"


def test_the_migration_installs_the_guards_and_its_downgrade_keeps_flags(
    tmp_path, monkeypatch
) -> None:
    database = (tmp_path / "flags.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(f"sqlite:///{database}")
    try:
        command.upgrade(config, "head")
        with engine.begin() as connection:
            stored = {
                name: sql
                for name, sql in connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
                ).tuples()
            }
            for name, (_, sql) in flag_models.SQLITE_TRIGGER_SQL.items():
                assert " ".join(stored[name].split()) == " ".join(sql.split())
            connection.execute(
                text(
                    f"INSERT INTO {TABLE} (measurement_id, flag, action) "
                    "VALUES (:id, 'test', 'mark')"
                ),
                {"id": uuid.uuid4().hex},
            )
        with pytest.raises(RuntimeError, match=f"{TABLE} holds 1 row"):
            command.downgrade(config, "20261024_0013")
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT count(*) FROM {TABLE}")) == 1
    finally:
        engine.dispose()
        get_settings.cache_clear()


async def test_the_flag_schema_gate_passes_a_current_database(sessions) -> None:
    async with sessions() as session:
        assert await flag_schema_errors(session) == []


def test_verify_on_a_database_before_0014_names_the_revision(tmp_path, monkeypatch) -> None:
    """verify used to fail on SQL (its guard check copies every guarded table) against a
    database not upgraded to 0014 yet; it names the revision and the upgrade instead."""
    database = (tmp_path / "before-flags.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "20261024_0013")
    finally:
        get_settings.cache_clear()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def gate() -> list[str]:
        async with factory() as session:
            return await flag_schema_errors(session)

    expected = (
        "the database is at revision 20261024_0013, without the manual measurement flags of "
        f"20261024_0014 ({TABLE}): run `alembic upgrade head`"
    )
    try:
        assert [error[: len(expected)] for error in asyncio.run(gate())] == [expected]
        monkeypatch.setattr(cli, "SessionLocal", factory)
        result = CliRunner().invoke(cli.app, ["verify"])
    finally:
        asyncio.run(engine.dispose())
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert expected in result.output
