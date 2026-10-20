from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.db.session import get_session
from app.main import create_app
from app.models.health import ExerciseSession
from app.models.manual import ManualWorkout
from app.models.state import RawHealthData


async def test_workouts_sources_month_timezone_and_validation(sessions):
    application = create_app()

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    start = datetime(2026, 8, 31, 22, 30, tzinfo=UTC)
    end = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)
    async with sessions() as session:
        for identifier in ("valid", "deleted", "quarantined"):
            session.add(
                ExerciseSession(
                    google_data_point_id=identifier,
                    exercise_type="RUNNING",
                    start_time=start,
                    end_time=end,
                    duration_seconds=3500,
                )
            )
            session.add(
                RawHealthData(
                    data_type="exercise",
                    google_data_point_id=identifier,
                    raw_json={},
                    payload_hash="test",
                    first_seen_at=start,
                    last_seen_at=start,
                    source_start_time=start,
                    source_end_time=end,
                    source_deleted_at=end if identifier == "deleted" else None,
                    is_quarantined=identifier == "quarantined",
                )
            )
        await session.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://localhost",
        headers={"X-HealthOS-Request": "1"},
    ) as client:
        body = {
            "exercise_type": " Yürüyüş ",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "calories_kcal": 0,
            "distance_meters": 2000,
        }
        response = await client.post("/api/dashboard/workouts", json=body)
        assert response.status_code == 201
        assert response.json()["exercise_type"] == "Yürüyüş"
        assert response.json()["duration_seconds"] == 3600
        rows = (
            await client.get("/api/dashboard/workouts?month=2026-09-01&timezone=Europe/Paris")
        ).json()
        assert {row["source"] for row in rows} == {"Fitbit", "Manuel"}
        assert len(rows) == 2
        assert next(row for row in rows if row["source"] == "Fitbit")["duration_seconds"] == 3500
        assert (
            await client.get("/api/dashboard/workouts?month=2026-08-01&timezone=Europe/Paris")
        ).json() == []
        assert (
            len((await client.get("/api/dashboard/workouts?month=2026-08-01&timezone=UTC")).json())
            == 2
        )
        for change in (
            {"exercise_type": " "},
            {"exercise_type": "x" * 129},
            {"end_time": start.isoformat()},
            {"end_time": "2099-01-01T00:00:00Z"},
            {"start_time": "2026-08-31T22:30:00"},
            {"calories_kcal": -1},
            {"distance_meters": "NaN"},
        ):
            assert (
                await client.post("/api/dashboard/workouts", json={**body, **change})
            ).status_code == 422
        assert (await client.get("/api/dashboard/workouts?timezone=wrong")).status_code == 422
        assert (await client.get("/api/dashboard/workouts?month=2099-01-01")).status_code == 422
        february = await client.get("/api/dashboard?days=29&end_date=2024-02-29")
        assert february.json()["start_date"] == "2024-02-01"
    async with sessions() as session:
        assert len(list(await session.scalars(select(ManualWorkout)))) == 1
    await application.state.runtime.shutdown()
