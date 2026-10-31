import pytest
from retrieval_records import START, Client, Runner
from sqlalchemy import select
from text_records import BAD_TEXT, FALLBACKS, MISSING, TEXT_FIELDS, text_point

from app.google_health.capture import CapturedResponse
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.raw_versions import RawApiResponse, RawHealthDataVersion
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState
from app.parsers.common import canonical_json
from app.parsers.normalize import normalize
from app.services.storage import store_page


@pytest.mark.parametrize("kind,path,column", TEXT_FIELDS)
def test_optional_text_types_lengths_and_precedence(kind, path, column):
    model = normalize(REGISTRY[kind], text_point(kind, path, "valid")).model
    limit = model.__table__.c[column].type.length
    for fallback in [None, "fallback"] if path in FALLBACKS else [None]:
        for value in (MISSING, None, "", "valid", "ç" * limit, "ç" * (limit + 1), *BAD_TEXT):
            record = text_point(kind, path, value, fallback)
            original = canonical_json(record)
            selected = fallback if value is MISSING else value
            if path in FALLBACKS:
                selected = selected or fallback
            expected = selected if isinstance(selected, str) and len(selected) <= limit else None
            assert normalize(REGISTRY[kind], record).values[column] == expected
            assert canonical_json(record) == original


@pytest.mark.parametrize("kind,path,column", TEXT_FIELDS)
@pytest.mark.parametrize("bad", BAD_TEXT)
@pytest.mark.parametrize("lane", ["page", "sync", "backfill"])
async def test_text_corrections_keep_neighbors_and_archive(sessions, kind, path, column, bad, lane):
    spec = REGISTRY[kind]
    valid, neighbor = text_point(kind, path, "valid"), text_point(kind, path, "neighbor")
    broken = {**text_point(kind, path, bad), "name": valid["name"]}
    model = normalize(spec, valid).model
    history, ids = [], {}
    runner = Runner(sessions, lane, kind)
    for record, expected in [(valid, "valid"), (broken, None), (valid, "valid")]:
        # Explicitly requeue this fixture to exercise later corrections: completed
        # backfill jobs are terminal and ordinary repeated calls now leave them alone.
        if lane == "backfill" and history:
            async with sessions.begin() as session:
                (await session.get(BackfillJob, runner.job_id)).status = JobStatus.pending
        body = canonical_json({"dataPoints": [record, neighbor]}).encode()
        response = CapturedResponse(
            START, "sync", None, kind, "GET", "/synthetic", None, None, 1, 200, {}, body
        )
        page = Page([record, neighbor], "p2", responses=[response])
        if lane == "page":
            async with sessions.begin() as session:
                stats = await store_page(session, spec, page)
            assert stats.normalization_skipped == 0 and stats.normalized == (1 if history else 2)
        else:
            client = Client(page, Page([], None))
            await runner.run(client)
            assert client.tokens == [None, "p2"] and not client.kept
        async with sessions() as session:
            raw = {r.google_data_point_id: r for r in await session.scalars(select(RawHealthData))}
            ids = ids or {key: row.id for key, row in raw.items()}
            assert len(raw) == 2 and ids == {key: row.id for key, row in raw.items()}
            assert raw[valid["name"]].raw_json == record
            assert raw[neighbor["name"]].raw_json == neighbor
            versions = await session.scalars(
                select(RawHealthDataVersion).order_by(RawHealthDataVersion.version)
            )
            assert [(r.version, r.raw_json) for r in versions] == list(enumerate(history, 1))
            rows = {row.google_data_point_id: row for row in await session.scalars(select(model))}
            assert len(rows) == 2 and getattr(rows[valid["name"]], column) == expected
            assert getattr(rows[neighbor["name"]], column) == "neighbor"
            if column == "source_timezone":
                assert raw[valid["name"]].source_timezone == expected
            archived = (
                await session.scalars(select(RawApiResponse).order_by(RawApiResponse.id))
            ).all()
            assert archived[-1].body == body and len(archived) == len(history) + 1
            assert raw[valid["name"]].response_id == archived[-1].id
            if lane != "page":
                state = SyncState if lane == "sync" else BackfillJob
                assert (await session.scalar(select(state))).status == JobStatus.completed
        history.append(record)
