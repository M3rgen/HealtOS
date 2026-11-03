"""Coverage HTTP contract and daily evidence; SQLite, synthetic records only."""

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from coverage_records import assert_sources
from coverage_records import source_archive as source_archive
from retrieval_records import END, START, Client, Runner, point
from sqlalchemy import Text, cast, func, insert, select
from weight_records import INVALID_FIELDS

from app import main
from app.core.config import Settings
from app.db.session import get_session
from app.google_health.client import Page
from app.google_health.data_types import CIVIL_AFTER, CIVIL_BEFORE, REGISTRY
from app.models.health import HeartRate
from app.models.retrieval import RetrievalEvent as Event
from app.models.retrieval import RetrievalReceipt as Receipt
from app.models.state import BackfillJob, JobStatus, SyncState
from app.services import retrieval_coverage
from app.services.storage import store_records

QUERY = {
    "start_date": "2026-09-01",
    "end_date": "2026-09-01",
    "timezone": "UTC",
    "data_type": "heart-rate",
}
PATH = "/api/sync/coverage"
AUTH = {"Authorization": "Bearer synthetic-coverage-test-authentication"}


@pytest.mark.parametrize("kind", ["steps", "daily-resting-heart-rate", "heart-rate"])
async def test_source_labels_preserve_stored_measurements(api, source_archive, kind):
    response = await api.get(PATH, params=QUERY | {"data_type": kind, "end_date": "2026-09-02"})
    assert response.status_code == 200, response.text
    report = response.json()["types"][0]
    assert [row["has_value"] for row in report["days"]] == [True, False]
    assert all(row["retrieval"] == "unknown" for row in report["days"])
    assert_sources(report["source_regimes"], source_archive)


@pytest.fixture
async def api(sessions, monkeypatch):
    settings = Settings(_env_file=None, app_env="test", app_ui_token=AUTH["Authorization"][7:])
    monkeypatch.setattr(main, "get_settings", lambda: settings)
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


async def day(api, **overrides):
    response = await api.get(PATH, params=QUERY | overrides)
    assert response.status_code == 200, response.text
    assert len(response.content) <= 12 * 1024
    return response.json()["types"][0]["days"][0]


async def fetch(sessions, start=START, end=END, kind="heart-rate", points=()):
    await Runner(sessions, "sync", kind, start, end).run(Client(Page(list(points), None)))


async def test_legacy_records_and_successful_job_state_are_not_retrieval_evidence(api, sessions):
    async with sessions.begin() as session:
        await store_records(session, REGISTRY["heart-rate"], [point()])
        session.add(
            SyncState(data_type="heart-rate", status=JobStatus.completed, last_successful_sync=END)
        )
        session.add(
            BackfillJob(
                data_type="heart-rate",
                status=JobStatus.completed,
                window_start=START,
                window_end=END,
                completed_at=END,
            )
        )
    result = await day(api)
    assert result["has_value"] is True
    assert result["retrieval"] == "unknown" and result["returned_records"] is None
    response = await api.get("/sync/coverage", params=QUERY)
    assert response.status_code in {404, 405}


async def test_excluded_return_is_not_an_empty_google_response(api, sessions):
    record = point()
    await fetch(sessions, points=[record])
    async with sessions.begin() as session:
        normalized = await session.scalar(select(HeartRate))
        normalized.is_quarantined = True
    result = await day(api)
    assert result["has_value"] is False
    assert result["retrieval"] == "completed_with_records"
    assert result["returned_records"] == 1
    report = (await api.get(PATH, params=QUERY)).json()
    assert report["types"][0]["exclusions"]["quarantined"] == 1


async def test_window_return_total_is_not_distributed_over_empty_days(api, sessions):
    await fetch(sessions, end=END + timedelta(days=2), points=[point()])
    response = await api.get(PATH, params=QUERY | {"end_date": "2026-09-03"})
    days = response.json()["types"][0]["days"]
    assert [d["retrieval"] for d in days] == [
        "completed_with_records",
        "completed_empty",
        "completed_empty",
    ]
    assert [d["returned_records"] for d in days] == [1, 0, 0]


@pytest.mark.parametrize("gap", [0, 1])
async def test_partial_adjacent_and_overlapping_windows_preserve_gaps(api, sessions, gap):
    noon = START + timedelta(hours=12)
    await fetch(sessions, end=noon)
    assert (await day(api))["retrieval"] == "incomplete"
    await fetch(sessions, start=noon + timedelta(seconds=gap))
    assert (await day(api))["retrieval"] == ("incomplete" if gap else "completed_empty")
    # Overlap joins the ranges, without counting a partial fetch as a whole day.
    await fetch(sessions, start=noon - timedelta(minutes=5), end=noon + timedelta(minutes=5))
    assert (await day(api))["retrieval"] == "completed_empty"


async def test_second_precision_rounds_out_to_cover_the_full_day(api, sessions):
    await fetch(
        sessions, start=START + timedelta(microseconds=999999), end=END - timedelta(microseconds=1)
    )
    assert (await day(api))["retrieval"] == "completed_empty"


@pytest.mark.parametrize(("date_string", "hours"), [("2026-03-29", 23), ("2026-10-25", 25)])
async def test_local_day_uses_23_and_25_hour_bounds(api, sessions, date_string, hours, monkeypatch):
    # A future reader after both DST days has closed them; no wall-clock dependency.
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 2, tzinfo=UTC).astimezone(tz or UTC)

    monkeypatch.setattr(retrieval_coverage, "datetime", Later)
    # Receipt timestamps must also be after the requested window, for the autumn day.
    from app.services import retrieval

    monkeypatch.setattr(retrieval, "datetime", Later)
    local = datetime.combine(
        date.fromisoformat(date_string), datetime.min.time(), ZoneInfo("Europe/Brussels")
    )
    start, stop = local.astimezone(UTC), (local + timedelta(days=1)).astimezone(UTC)
    assert (stop - start).total_seconds() == hours * 3600
    await fetch(sessions, start=start, end=stop - timedelta(seconds=1))
    query = {"start_date": date_string, "end_date": date_string, "timezone": "Europe/Brussels"}
    assert (await day(api, **query))["retrieval"] == "incomplete"
    await fetch(sessions, start=stop - timedelta(seconds=1), end=stop)
    assert (await day(api, **query))["retrieval"] == "completed_empty"


async def test_daily_filter_ceil_is_proof_of_the_civil_date(api, sessions):
    kind = "daily-resting-heart-rate"
    await fetch(
        sessions, start=START + timedelta(hours=12), end=START + timedelta(hours=13), kind=kind
    )
    result = await day(api, data_type=kind, timezone="America/New_York")
    assert result["retrieval"] == "completed_empty"
    async with sessions() as session:
        from app.models.retrieval import RetrievalReceipt

        receipt = await session.scalar(select(RetrievalReceipt))
    assert receipt.query_filter.endswith('date < "2026-09-02"')
    assert receipt.queried_from.replace(tzinfo=UTC) == START


async def test_sleep_uses_wake_day_and_civil_sessions_need_the_offset_envelope(api, sessions):
    sleep = {
        "sleep": {
            "interval": {"startTime": "2026-08-31T22:00:00Z", "endTime": "2026-09-01T06:00:00Z"}
        }
    }
    await fetch(sessions, kind="sleep", points=[sleep])
    result = await day(api, data_type="sleep")
    assert result["retrieval"] == "completed_with_records" and result["has_value"] is True
    await fetch(sessions, kind="exercise")
    assert (await day(api, data_type="exercise"))["retrieval"] == "incomplete"
    await fetch(sessions, start=START - CIVIL_BEFORE, end=END + CIVIL_AFTER, kind="exercise")
    assert (await day(api, data_type="exercise"))["retrieval"] == "completed_empty"


@pytest.mark.parametrize("missing", ["startTime", "endTime"])
async def test_sleep_budget_and_exclusions_follow_current_wake_candidates(
    api, sessions, monkeypatch, missing
):
    from derived_records import sleep, store

    from app.models.health import SleepSession

    await store(sessions, "sleep", sleep(start=START, end=START + timedelta(hours=2)))
    correction = sleep(start=END, end=END + timedelta(hours=2))
    correction["sleep"]["interval"].pop(missing)
    await store(sessions, "sleep", correction)
    monkeypatch.setattr(retrieval_coverage, "MAX_RECORDS", 0)
    assert (await day(api, data_type="sleep"))["has_value"] is False
    moved = QUERY | {"data_type": "sleep", "start_date": "2026-09-02", "end_date": "2026-09-02"}
    response = await api.get(PATH, params=moved)
    assert response.status_code == (413 if missing == "startTime" else 200)
    monkeypatch.setattr(retrieval_coverage, "MAX_RECORDS", 100)
    # Exclusion counts must not revive the old date either; an end-less correction
    # cannot be assigned to either day, while a start-less one has a wake candidate.
    async with sessions.begin() as session:
        row = await session.scalar(select(SleepSession))
        row.is_quarantined = True
    for query, count in ((QUERY | {"data_type": "sleep"}, 0), (moved, int(missing == "startTime"))):
        result = (await api.get(PATH, params=query)).json()["types"][0]
        assert result["exclusions"]["quarantined"] == count
        assert result["days"][0]["has_value"] is False


async def test_rollup_uses_client_minute_alignment(api, sessions):
    await fetch(
        sessions,
        start=START + timedelta(seconds=30),
        end=END - timedelta(seconds=30),
        kind="total-calories",
    )
    assert (await day(api, data_type="total-calories"))["retrieval"] == "completed_empty"


async def test_unplaceable_return_never_becomes_a_daily_empty_claim(api, sessions):
    await fetch(sessions, points=[{"unrecognized": "synthetic"}])
    result = await day(api)
    assert result["retrieval"] == "unknown" and result["reason"] == "returned_dates_unknown"


async def test_no_time_filter_reports_the_limit(api, sessions):
    await fetch(sessions, kind="food")
    result = await day(api, data_type="food")
    assert result["retrieval"] == "unknown" and result["reason"] == "no_time_filter"
    assert result["has_value"] is None


async def test_today_and_future_range_never_turn_into_closed_day_proof(api, sessions, monkeypatch):
    from app.services import retrieval

    now = datetime(2026, 9, 1, 12, tzinfo=UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or UTC)

    monkeypatch.setattr(retrieval, "datetime", Frozen)
    monkeypatch.setattr(retrieval_coverage, "datetime", Frozen)
    await fetch(sessions, end=END + timedelta(days=2))
    result = await day(api)
    assert result["closed"] is False and result["retrieval"] == "incomplete"
    now += timedelta(days=5)
    # Fetching through a future timestamp yesterday does not prove that future day today.
    assert (await day(api))["retrieval"] == "incomplete"
    assert (await day(api, start_date="2026-09-02", end_date="2026-09-02"))[
        "retrieval"
    ] == "incomplete"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"start_date": "2026-09-01"},
        QUERY | {"start_date": "bad"},
        QUERY | {"start_date": "2026-09-02"},
        QUERY | {"end_date": "2026-12-01"},
        QUERY | {"timezone": "Invalid/Timezone"},
        QUERY | {"timezone": "../UTC"},
        QUERY | {"data_type": "not-a-type"},
        QUERY | {"limit": 0},
        QUERY | {"limit": 5},
        QUERY | {"offset": -1},
        QUERY | {"timezone": ""},
        QUERY | {"start_date": "0001-01-01", "end_date": "0001-01-01"},
        QUERY | {"start_date": "9999-12-31", "end_date": "9999-12-31"},
    ],
)
async def test_invalid_parameters_are_structured_422(api, params):
    response = await api.get(PATH, params=params)
    assert response.status_code == 422
    assert "detail" in response.json()


async def test_authentication_is_required(api):
    api.headers.clear()
    response = await api.get(PATH, params=QUERY)
    assert response.status_code == 401 and response.json() == {"detail": "auth_required"}


async def test_all_supported_types_are_paged_and_every_response_fits_budget(api):
    seen = []
    offset = 0
    while offset is not None:
        query = {k: v for k, v in QUERY.items() if k != "data_type"} | {"offset": offset}
        response = await api.get(PATH, params=query)
        assert response.status_code == 200 and len(response.content) <= 12 * 1024
        data = response.json()
        seen.extend(t["data_type"] for t in data["types"])
        assert all(t["days"][0]["retrieval"] == "unknown" for t in data["types"])
        offset = data["next_offset"]
    assert seen == sorted(REGISTRY)


@pytest.mark.parametrize(
    ("limit", "code"),
    [
        ("MAX_RECEIPTS", "coverage_receipt_limit"),
        ("MAX_EVENTS", "coverage_evidence_limit"),
        ("MAX_EVIDENCE_BYTES", "coverage_evidence_limit"),
        ("MAX_RECORDS", "coverage_record_limit"),
        ("COVERAGE_BUDGET_BYTES", "coverage_response_budget"),
    ],
)
async def test_read_and_response_budgets_refuse_without_truncating(
    api, sessions, monkeypatch, limit, code
):
    await fetch(sessions, points=[point()])
    monkeypatch.setattr(retrieval_coverage, limit, 0)
    response = await api.get(PATH, params=QUERY)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == code


@pytest.mark.parametrize("count", [2, 1100])
@pytest.mark.parametrize("budget", ["MAX_RECEIPTS", "MAX_EVENTS", "MAX_EVIDENCE_BYTES"])
async def test_evidence_budgets_are_per_type(api, sessions, monkeypatch, budget, count):
    kinds = ("heart-rate", "heart-rate-variability")
    for kind in kinds:
        await fetch(sessions, kind=kind, points=[point(kind=kind)])
    async with sessions.begin() as session:
        template = (await session.execute(select(Receipt.__table__).limit(1))).mappings().one()
        # Unfinished attempts consume budget too; keep one complete attempt per type.
        for kind in kinds:
            await session.execute(
                insert(Receipt),
                [dict(template, id=uuid.uuid4(), data_type=kind) for _ in range(count - 1)],
            )
        size = await session.scalar(select(func.max(func.length(cast(Event.returned_times, Text)))))
    ceilings = {"MAX_RECEIPTS": min(2000, count * 2 - 1), "MAX_EVENTS": 2}
    ceiling = ceilings.get(budget, size)
    monkeypatch.setattr(retrieval_coverage, budget, ceiling)
    separate = [(await api.get(PATH, params=QUERY | {"data_type": kind})).json() for kind in kinds]
    query = {k: v for k, v in QUERY.items() if k != "data_type"}
    query |= {"offset": sorted(REGISTRY).index(kinds[0]), "limit": 2}
    response = await api.get(PATH, params=query)
    assert response.status_code == 200 and len(response.content) <= 12 * 1024
    combined = response.json()
    assert combined["types"] == [report["types"][0] for report in separate]
    assert combined["summary"]["basis"] == "type_days_in_page"
    for key in ("days_without_value", "unknown_days", "incomplete_days"):
        assert combined["summary"][key] == sum(report["summary"][key] for report in separate)
    monkeypatch.setattr(
        retrieval_coverage, budget, (count if budget == "MAX_RECEIPTS" else ceiling) - 1
    )
    for params in (QUERY, query):
        response = await api.get(PATH, params=params)
        assert response.status_code == 413
        code = "coverage_receipt_limit" if budget == "MAX_RECEIPTS" else "coverage_evidence_limit"
        assert response.json()["detail"]["code"] == code


async def test_missing_receipt_migration_is_reported_by_archive_verification(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings
    from app.services.raw_history import verify_guards

    database = tmp_path / "old-schema.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "20261024_0014")
    finally:
        get_settings.cache_clear()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with async_sessionmaker(engine)() as session:
            errors = await verify_guards(session)
        assert len(errors) == 1 and "20261025_0015" in errors[0]
        assert "alembic upgrade head" in errors[0]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["heart-rate", "daily-resting-heart-rate"])
@pytest.mark.parametrize("resume", [False, True])
async def test_first_page_snapshot_cannot_cover_future_time_by_finishing_later(
    api, sessions, monkeypatch, kind, resume
):
    from app.services import retrieval

    now = START + timedelta(hours=12)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or UTC)

    monkeypatch.setattr(retrieval, "datetime", Frozen)
    monkeypatch.setattr(retrieval_coverage, "datetime", Frozen)
    runner = Runner(sessions, "sync", kind)
    if resume:
        with pytest.raises(RuntimeError):
            await runner.run(Client(Page([], "second"), RuntimeError("synthetic interruption")))
        now = END + timedelta(hours=1)
        await runner.run(Client(Page([], None)))
    else:

        class Crossing(Client):
            async def list_data_points(self, *args):
                nonlocal now
                now = END + timedelta(hours=1)
                return Page([], None)

        await runner.run(Crossing())
    assert (await day(api, data_type=kind))["retrieval"] == "incomplete"


async def test_dense_backfill_only_reads_timestamps_of_pages_overlapping_requested_day(
    api, sessions, monkeypatch
):
    runner = Runner(sessions, "sync", end=END + timedelta(days=2))
    await runner.run(Client(Page([point()], "second"), Page([point("2026-09-03T12:00:00Z")], None)))
    monkeypatch.setattr(retrieval_coverage, "MAX_EVIDENCE_BYTES", 1)
    result = await day(api, start_date="2026-09-02", end_date="2026-09-02")
    assert result["retrieval"] == "completed_empty" and result["returned_records"] == 0


async def test_record_read_budget_counts_the_requested_day_not_neighbouring_days(
    api, sessions, monkeypatch
):
    await fetch(
        sessions,
        end=END + timedelta(days=2),
        points=[point(), point("2026-09-02T12:00:00Z"), point("2026-09-03T12:00:00Z")],
    )
    monkeypatch.setattr(retrieval_coverage, "MAX_RECORDS", 1)
    result = await day(api, start_date="2026-09-02", end_date="2026-09-02")
    assert result["has_value"] is True and result["returned_records"] == 1


async def test_daily_return_is_attributed_by_date_not_an_unrelated_physical_timestamp(
    api, sessions
):
    kind = "daily-resting-heart-rate"
    await fetch(
        sessions,
        kind=kind,
        end=END + timedelta(days=1),
        points=[point("2026-09-02T12:00:00Z", kind=kind, date="2026-09-01")],
    )
    assert (await day(api, data_type=kind))["returned_records"] == 1
    next_day = await day(api, data_type=kind, start_date="2026-09-02", end_date="2026-09-02")
    assert next_day["retrieval"] == "completed_empty" and next_day["returned_records"] == 0


@pytest.mark.parametrize("fields", INVALID_FIELDS)
async def test_weight_budget_and_retrieval_proof_follow_current_candidates(
    api, sessions, monkeypatch, fields
):
    from derived_records import store
    from weight_records import correction, weight

    await fetch(sessions, kind="weight", points=[weight(at=START)])
    moved = QUERY | {"data_type": "weight", "start_date": "2026-09-02", "end_date": "2026-09-02"}
    await store(sessions, "weight", correction(END, fields))
    result = await day(api, data_type="weight")
    assert result["has_value"] is False
    assert result["retrieval"] == "completed_with_records" and result["returned_records"] == 1
    monkeypatch.setattr(retrieval_coverage, "MAX_RECORDS", 0)
    assert (await day(api, data_type="weight"))["has_value"] is False
    response = await api.get(PATH, params=moved)
    placeable = "sampleTime" not in fields
    assert response.status_code == (413 if placeable else 200)
    if placeable:
        assert response.json()["detail"]["code"] == "coverage_record_limit"
    monkeypatch.setattr(retrieval_coverage, "MAX_RECORDS", 1)
    report = (await api.get(PATH, params=moved)).json()["types"][0]
    assert report["days"][0]["has_value"] is False
    assert report["days"][0]["retrieval"] == "unknown"
    assert report["exclusions"]["invalid_shape"] == int(placeable)
    assert report["source_regimes"] == []
    await store(sessions, "weight", weight(grams=74000, at=END))
    report = (await api.get(PATH, params=moved)).json()["types"][0]
    assert report["days"][0]["has_value"] is True
    assert report["days"][0]["retrieval"] == "unknown"
    assert report["source_regimes"][0]["first_date"] == "2026-09-02"


@pytest.mark.parametrize("flag", ["raw", "normalized", "deleted", "superseded", "orphan"])
@pytest.mark.parametrize("lost_time", [False, True])
async def test_weight_exclusions_use_current_source_date(api, sessions, flag, lost_time):
    from derived_records import store
    from weight_records import correction, weight

    from app.models.health import Weight
    from app.models.identity import RawHealthDataSupersession
    from app.models.state import RawHealthData as Raw

    await store(sessions, "weight", weight(at=START))
    await store(sessions, "weight", correction(END, {"sampleTime": None} if lost_time else {}))
    async with sessions.begin() as session:
        raw = await session.scalar(select(Raw))
        normalized = await session.scalar(select(Weight))
        if flag == "raw":
            raw.is_quarantined = True
        elif flag == "normalized":
            normalized.is_quarantined = True
        elif flag == "deleted":
            raw.source_deleted_at = END
        elif flag == "orphan":
            # A synthetic orphan has no current raw time; its own archive date applies.
            session.add(Weight(google_data_point_id="orphan", timestamp=END, kilograms=99))
        else:
            await store_records(session, REGISTRY["weight"], [weight("holder", at=START)])
            holder = await session.scalar(select(Raw).where(Raw.id != raw.id))
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=raw.id,
                    new_raw_id=holder.id,
                    data_type="weight",
                    old_google_data_point_id=raw.google_data_point_id,
                    new_google_data_point_id=holder.google_data_point_id,
                    reason="duplicate",
                    old_fingerprint="a" * 64,
                    new_fingerprint="a" * 64,
                    same_reading=True,
                    new_payload_hash=holder.payload_hash,
                )
            )
    key = {
        "raw": "quarantined",
        "normalized": "quarantined",
        "deleted": "source_deleted",
        "superseded": "superseded",
        "orphan": "without_raw_record",
    }[flag]
    for day_text, expected in (("2026-09-01", 0), ("2026-09-02", 1)):
        report = (
            await api.get(
                PATH,
                params=QUERY
                | {
                    "data_type": "weight",
                    "start_date": day_text,
                    "end_date": day_text,
                },
            )
        ).json()["types"][0]
        assert report["exclusions"][key] == expected * int(not lost_time or flag == "orphan")
        assert report["days"][0]["has_value"] == (flag == "superseded" and day_text == "2026-09-01")
    await store(sessions, "weight", weight(grams=74000, at=END))
    async with sessions() as session:
        from app.services.dashboard import dashboard_data

        profile = (await dashboard_data(session, END.date(), 1, "UTC"))["profile"]
    assert profile.get("weight", {}).get("value") == {
        "superseded": 70,
        "orphan": 74,
        "deleted": 74,  # a new Google payload revives a source-deleted record
    }.get(flag)
