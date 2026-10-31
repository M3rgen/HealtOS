"""Read-only HTTP history, pagination and persistence through a new app and DB engine."""

import copy
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.encoders import jsonable_encoder
from reconciliation_records import END, START, Frozen, execute, point, service
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_session
from app.google_health.client import Page
from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.services import reconciliation_reports as reports
from app.services.storage import store_records
from app.sync import reconciliation

PATH = "/api/sync/reconciliation"
AUTH = {"Authorization": "Bearer synthetic-reconciliation-test-authentication"}


@asynccontextmanager
async def client_for(sessions, monkeypatch):
    settings = Settings(_env_file=None, app_env="test", app_ui_token=AUTH["Authorization"][7:])
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    # A GET must work without any runnable runtime or Google client.
    monkeypatch.setattr(main, "build_runtime", lambda *_: SimpleNamespace())
    app = main.create_app()

    async def session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost", headers=AUTH
    ) as client:
        yield client


@pytest.fixture
async def api(sessions, monkeypatch):
    async with client_for(sessions, monkeypatch) as client:
        yield client


async def test_empty_history_is_valid_and_reads_start_no_work(api, sessions):
    expected = {"runs": [], "total": 0, "offset": 0, "limit": 20}
    for _ in range(2):
        response = await api.get(PATH)
        assert response.status_code == 200 and response.json() == expected
    async with sessions() as session:
        assert await reports.list_runs(session) == expected


async def test_persisted_differences_equal_http_results_including_unchanged(
    api, sessions, monkeypatch
):
    monkeypatch.setattr(reconciliation, "datetime", Frozen)
    a, b, missing, new = [point(at=f"2026-09-01T{hour}:00:00Z") for hour in (10, 11, 12, 13)]
    runner = service(sessions)
    async with sessions.begin() as session:
        await store_records(session, runner.incremental.specs["heart-rate"], [a, b, missing])
    changed = copy.deepcopy(b)
    changed["heartRate"]["beatsPerMinute"] = 73
    persisted, _, _ = await execute(sessions, Page([a, changed], "next"), Page([new], None))
    response = await api.get(PATH)
    assert response.status_code == 200
    assert response.json()["runs"] == [jsonable_encoder(persisted)]
    assert {
        name: response.json()["runs"][0]["types"][0][name]
        for name in ("inserted", "updated", "unchanged", "source_deleted")
    } == {"inserted": 1, "updated": 1, "unchanged": 1, "source_deleted": 1}
    repeated, _, _ = await execute(sessions, Page([a, changed, new], None))
    response = await api.get(PATH)
    result = next(r for r in response.json()["runs"] if r["id"] == str(repeated["id"]))
    assert [
        result["types"][0][n] for n in ("inserted", "updated", "unchanged", "source_deleted")
    ] == [0, 0, 3, 0]
    assert new["name"] not in response.text and "beatsPerMinute" not in response.text


async def test_stable_tie_order_paginates_runs_before_loading_all_their_types(api, sessions):
    ids = [uuid.UUID(int=i) for i in range(1, 7)]
    for run_id in reversed(ids):
        await reports.begin_run(sessions, run_id, START, END, START, ["steps", "heart-rate"])
        for kind in ("steps", "heart-rate"):
            await reports.start_type(sessions, run_id, kind)
            await reports.finish_type(sessions, run_id, kind)
        await reports.finish_run(sessions, run_id)
    pages = []
    for offset in (0, 2, 4):
        response = await api.get(PATH, params={"offset": offset, "limit": 2})
        data = response.json()
        assert (data["total"], data["offset"], data["limit"]) == (6, offset, 2)
        assert all(
            [r["data_type"] for r in run["types"]] == ["heart-rate", "steps"]
            for run in data["runs"]
        )
        assert (await api.get(PATH, params={"offset": offset, "limit": 2})).json() == data
        pages.extend(data["runs"])
    assert [run["id"] for run in pages] == [str(i) for i in reversed(ids)]
    assert len({run["id"] for run in pages}) == 6
    assert (await api.get(PATH, params={"offset": 6})).json()["runs"] == []
    assert (await api.get(PATH, params={"offset": 10000, "limit": 100})).status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 101},
        {"limit": "oops"},
        {"limit": "1.5"},
        {"offset": -1},
        {"offset": 10001},
        {"offset": "oops"},
        {"offset": "0.5"},
    ],
)
async def test_invalid_pagination_is_rejected(api, params):
    assert (await api.get(PATH, params=params)).status_code == 422


async def test_history_requires_authentication(api):
    response = await api.get(PATH, headers={"Authorization": ""})
    assert response.status_code == 401 and response.json() == {"detail": "auth_required"}


async def test_failed_report_never_exposes_upstream_exception_details(api, sessions, monkeypatch):
    monkeypatch.setattr(reconciliation, "datetime", Frozen)
    private_message = "synthetic-health-value-and-access-credential"
    row, _, _ = await execute(sessions, RuntimeError(private_message))
    response = await api.get(PATH)
    assert response.json()["runs"] == [jsonable_encoder(row)]
    assert response.json()["runs"][0]["error_type"] == "RuntimeError"
    assert response.json()["runs"][0]["types"][0]["error_type"] == "RuntimeError"
    assert private_message not in response.text


async def test_read_only_history_survives_new_engine_service_and_application(tmp_path, monkeypatch):
    monkeypatch.setattr(reconciliation, "datetime", Frozen)
    url = f"sqlite+aiosqlite:///{tmp_path / 'history.sqlite'}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    completed, _, _ = await execute(sessions, Page([point()], None))
    incomplete_id = uuid.uuid4()
    await reports.begin_run(
        sessions, incomplete_id, START, END, END + timedelta(seconds=1), ["steps"]
    )
    async with client_for(sessions, monkeypatch) as client:
        before = (await client.get(PATH)).json()
    await engine.dispose()
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        restored = service(sessions)
        assert restored.incremental.sequence == 0
        async with client_for(sessions, monkeypatch) as client:
            after = (await client.get(PATH)).json()
            assert after == before
            assert after["runs"][0]["id"] == str(incomplete_id)
            assert after["runs"][0]["status"] == "running"
            assert after["runs"][0]["finished_at"] is None
            assert after["runs"][1] == jsonable_encoder(completed)
        async with sessions() as session:
            assert await session.get(ReconciliationRun, incomplete_id) is not None
            assert (
                await session.get(ReconciliationResult, (incomplete_id, "steps"))
            ).status == "pending"
    finally:
        await engine.dispose()
