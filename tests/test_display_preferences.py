from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from app.db.session import get_session
from app.main import create_app
from app.models.settings import AppearanceSettings, DisplayPreferences

URL = "/api/settings/display"
JOB = {"X-HealthOS-Request": "1"}
EMPTY = {"schema_version": 1, "preferences": {}, "updated_at": None}


@pytest.fixture
def application(sessions):
    application = create_app()

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    return application


def client(application, **kwargs):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost", **kwargs
    )


def body(**preferences):
    return {"schema_version": 1, "preferences": preferences}


async def test_default_is_empty_and_versioned(application):
    async with client(application) as http:
        response = await http.get(URL)
    assert response.status_code == 200
    assert response.json() == EMPTY


async def test_put_then_get_roundtrip_last_write_wins(application, sessions):
    async with client(application, headers=JOB) as http:
        first = await http.put(URL, json=body(timezone="Europe/Istanbul", motion=False))
        assert first.status_code == 200
        assert first.json()["preferences"] == {"timezone": "Europe/Istanbul", "motion": False}
        second = await http.put(URL, json=body(timezone="auto"))
        assert second.status_code == 200
        stored = (await http.get(URL)).json()
    # A full replace, not a merge: `motion` from the first write is gone.
    assert stored["preferences"] == {"timezone": "auto"}
    assert stored["schema_version"] == 1
    assert datetime.fromisoformat(stored["updated_at"]) == datetime.fromisoformat(
        second.json()["updated_at"]
    )
    async with sessions() as session:
        rows = (await session.scalars(select(DisplayPreferences))).all()
        assert [(row.id, row.schema_version) for row in rows] == [(1, 1)]
        # The appearance row is a separate record and stays untouched.
        assert await session.get(AppearanceSettings, 1) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"preferences": {"motion": True}},  # No schema version.
        {"schema_version": 2, "preferences": {"motion": True}},  # Unknown version.
        {"schema_version": "1", "preferences": {}},
        body(motion="true"),  # Booleans only, no strings.
        body(motion=1),
        body(timezone="Europe/Istanbul; rm"),
        body(timezone="../etc/passwd"),
        body(timezone="A" * 65),
        body(theme="dark"),  # Unknown preference.
        {**body(), "extra": 1},
        ["auto"],
    ],
)
async def test_invalid_payload_is_rejected(application, payload):
    async with client(application, headers=JOB) as http:
        response = await http.put(URL, json=payload)
        assert response.status_code == 422
        assert (await http.get(URL)).json() == EMPTY


async def test_oversized_body_is_rejected(application):
    async with client(application, headers=JOB) as http:
        response = await http.put(URL, json={**body(), "pad": "x" * 10_000})
    assert response.status_code == 413


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-HealthOS-Request": "1", "Origin": "http://attacker.example"}],
)
async def test_put_requires_job_request_header(application, headers):
    async with client(application, headers=headers) as http:
        response = await http.put(URL, json=body(motion=False))
        assert response.status_code == 403
        assert (await http.get(URL)).json() == EMPTY


async def test_newer_schema_row_is_neither_misread_nor_overwritten(application, sessions):
    newer = {"timezone": {"name": "Europe/Istanbul"}, "motion": "slow"}
    async with sessions() as session:
        session.add(DisplayPreferences(id=1, schema_version=2, preferences=newer))
        await session.commit()
    async with client(application, headers=JOB) as http:
        assert (await http.get(URL)).json() == EMPTY
        response = await http.put(URL, json=body(motion=True))
        assert response.status_code == 409
    async with sessions() as session:
        row = await session.get(DisplayPreferences, 1)
        assert (row.schema_version, row.preferences) == (2, newer)


async def test_hand_edited_row_reads_as_empty(application, sessions):
    async with sessions() as session:
        session.add(DisplayPreferences(id=1, schema_version=1, preferences={"motion": "yes"}))
        await session.commit()
    async with client(application, headers=JOB) as http:
        assert (await http.get(URL)).json() == EMPTY
        # A valid write repairs it.
        assert (await http.put(URL, json=body(motion=True))).status_code == 200
        assert (await http.get(URL)).json()["preferences"] == {"motion": True}


def test_openapi_documents_body_and_job_header(application):
    put = application.openapi()["paths"][URL]["put"]
    schema = put["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"schema_version", "preferences"}
    assert schema["additionalProperties"] is False
    assert any(item["name"] == "X-HealthOS-Request" for item in put["parameters"])
