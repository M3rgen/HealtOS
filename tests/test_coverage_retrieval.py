"""Why a coverage day has no value: retrieval receipts in the card and the HTTP report.

SQLite, synthetic records only. Receipts written by the real sync path start "now"; an
older receipt of another type is inserted directly to set when receipts began.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from retrieval_records import Runner, point
from test_coverage import archive as archive
from test_coverage import coverage_of, query
from test_retrieval_coverage_api import PATH, QUERY
from test_retrieval_coverage_api import api as api

from app.google_health.client import Page
from app.models.retrieval import RetrievalReceipt as Receipt
from app.services import retrieval_coverage
from app.services.codex_health import read_health
from app.services.coverage import Window
from app.services.coverage_retrieval import gap_periods
from app.services.retrieval_coverage import unattempted

HORIZON = datetime(2026, 8, 31, tzinfo=UTC)


def day(n: int) -> datetime:
    return datetime(2026, 9, n, tzinfo=UTC)


class Client:
    def __init__(self, *points):
        self.points = list(points)

    async def warm_up(self):
        pass

    async def list_data_points(self, spec, start, end, page_token=None):
        return Page(self.points, None)

    async def keep_after(self, responses, failure):
        pass


async def fetch(sessions, start, end, kind="steps", points=()):
    await Runner(sessions, "sync", kind, start, end).run(Client(*points))


async def receipts_began(sessions, at=HORIZON):
    """An older receipt of an unrelated type; no event, nothing about steps."""
    async with sessions.begin() as session:
        session.add(
            Receipt(
                id=uuid.uuid4(),
                data_type="blood-glucose",
                lane="sync",
                owner="blood-glucose",
                requested_from=at - timedelta(hours=1),
                requested_to=at,
                queried_from=at - timedelta(hours=1),
                queried_to=at,
                basis="physical_start",
                access_method="list",
                started_at=at,
                starts_first=True,
            )
        )


async def steps_week(sessions):
    """09-01 value, 09-02 empty answer, 09-03 never asked, 09-04 half asked, 09-05 none."""
    await receipts_began(sessions)
    await fetch(sessions, day(1), day(2), points=[point("2026-09-01T12:00:00Z", "steps")])
    await fetch(sessions, day(2), day(3))
    await fetch(sessions, day(4), day(4) + timedelta(hours=12))


async def test_card_separates_value_empty_answer_and_never_asked(archive, sessions):
    await steps_week(sessions)
    card = await read_health(query("2026-09-01", "2026-09-05", data_type="steps"))
    assert card["schema_version"] == "2"
    assert "retrieval_receipts_unavailable" not in card["reason_codes"]
    steps = coverage_of(card, "steps")
    assert (steps["value_days"], steps["days_without_value"]) == (1, 4)
    retrieval = steps["retrieval"]
    assert retrieval["state"] == "receipts"
    assert datetime.fromisoformat(card["retrieval_receipts_since"]) == HORIZON
    counts = {
        key: retrieval[f"{key}_days"]
        for key in ("completed_with_records", "completed_empty", "incomplete", "not_attempted")
    }
    assert counts == {
        "completed_with_records": 1,
        "completed_empty": 1,
        "incomplete": 1,
        "not_attempted": 2,
    }
    assert retrieval["unknown_days"] == 0
    assert retrieval["without_value"] == {
        "empty_response": 1,
        "returned_without_value": 0,
        "not_retrieved": 2,
        "incomplete": 1,
        "unknown": 0,
    }
    assert [(g["start_date"], g["days"], g["state"]) for g in retrieval["gaps"]] == [
        ("2026-09-02", 1, "empty_response"),
        ("2026-09-03", 1, "not_retrieved"),
        ("2026-09-04", 1, "incomplete"),
        ("2026-09-05", 1, "not_retrieved"),
    ]
    assert retrieval["gaps_omitted"] == 0
    assert "days_not_retrieved" in steps["reason_codes"]
    assert "retrieval_unknown" not in steps["reason_codes"]


async def test_full_card_counts_states_without_gap_lists(archive, sessions):
    await steps_week(sessions)
    card = await read_health(query("2026-09-01", "2026-09-05"))
    assert "retrieval_gaps_need_data_type" in card["reason_codes"]
    steps = coverage_of(card, "steps")["retrieval"]
    assert "gaps" not in steps and steps["without_value"]["empty_response"] == 1
    # Weight was never requested after receipts began: no value is "not retrieved".
    weight = coverage_of(card, "weight")["retrieval"]
    assert weight["without_value"]["not_retrieved"] == 5
    # Muscle has no Google type: nothing can be said about retrieval.
    muscle = coverage_of(card, "muscle")
    assert muscle["retrieval"]["state"] == "unknown"
    assert muscle["retrieval"]["unknown_days"] == 5
    assert "retrieval_unknown" in muscle["reason_codes"]


async def test_days_before_receipts_began_stay_unknown(archive, sessions):
    await receipts_began(sessions, at=day(3))
    card = await read_health(query("2026-09-01", "2026-09-05", data_type="steps"))
    retrieval = coverage_of(card, "steps")["retrieval"]
    # 09-03 starts at the horizon itself but within the civil envelope; 09-04 is after it.
    assert (retrieval["unknown_days"], retrieval["not_attempted_days"]) == (3, 2)
    assert [g["state"] for g in retrieval["gaps"]] == ["unknown", "not_retrieved"]
    assert [g["days"] for g in retrieval["gaps"]] == [3, 2]


async def test_no_receipts_at_all_keeps_retrieval_unknown(archive, sessions):
    card = await read_health(query("2026-09-01", "2026-09-02", data_type="steps"))
    assert "retrieval_receipts_unavailable" in card["reason_codes"]
    assert card["retrieval_receipts_since"] is None
    retrieval = coverage_of(card, "steps")["retrieval"]
    assert retrieval == {
        "state": "unknown",
        "completed_with_records_days": None,
        "completed_empty_days": None,
        "incomplete_days": None,
        "not_attempted_days": None,
        "unknown_days": 2,
        "without_value": None,
    }


async def test_evidence_budget_is_read_in_pieces_not_refused(archive, sessions, monkeypatch):
    await receipts_began(sessions)
    await fetch(sessions, day(1), day(2))
    await fetch(sessions, day(20), day(21))
    monkeypatch.setattr(retrieval_coverage, "MAX_RECEIPTS", 1)
    card = await read_health(query("2026-09-01", "2026-09-20", data_type="steps"))
    steps = coverage_of(card, "steps")
    assert steps["retrieval"]["completed_empty_days"] == 2
    assert steps["retrieval"]["not_attempted_days"] == 18
    assert "retrieval_evidence_limit" not in steps["reason_codes"]


async def test_day_over_the_budget_is_unknown_and_flagged(archive, sessions, monkeypatch):
    await receipts_began(sessions)
    await fetch(sessions, day(1), day(2))
    monkeypatch.setattr(retrieval_coverage, "MAX_RECEIPTS", 0)
    card = await read_health(query("2026-09-01", "2026-09-01", data_type="steps"))
    steps = coverage_of(card, "steps")
    assert "error" not in card
    assert steps["retrieval"]["unknown_days"] == 1
    assert "retrieval_evidence_limit" in steps["reason_codes"]


async def test_records_without_a_value_are_not_an_empty_answer(archive, sessions):
    """A returned record that gives no counted value: Google was not silent."""
    await receipts_began(sessions)
    bad = point("2026-09-01T12:00:00Z", "weight", weightGrams=None)
    await fetch(sessions, day(1), day(2), kind="weight", points=[bad])
    card = await read_health(query("2026-09-01", "2026-09-01", data_type="weight"))
    retrieval = coverage_of(card, "weight")["retrieval"]
    assert retrieval["without_value"]["returned_without_value"] == 1
    assert retrieval["gaps"][0]["state"] == "returned_without_value"


async def test_http_report_marks_never_requested_days(api, sessions):
    await receipts_began(sessions)
    await fetch(sessions, day(1), day(2), kind="heart-rate", points=[point()])
    response = await api.get(PATH, params=QUERY | {"end_date": "2026-09-02"})
    assert response.status_code == 200, response.text
    report = response.json()
    states = [row["retrieval"] for row in report["types"][0]["days"]]
    assert states == ["completed_with_records", "not_attempted"]
    assert report["summary"]["not_attempted_days"] == 1
    assert report["summary"]["unknown_days"] == 0
    assert datetime.fromisoformat(report["receipts_since"]) == HORIZON


async def test_http_report_without_receipts_is_unchanged(api, sessions):
    response = await api.get(PATH, params=QUERY)
    report = response.json()
    assert report["types"][0]["days"][0]["retrieval"] == "unknown"
    assert report["receipts_since"] is None
    assert report["summary"]["not_attempted_days"] == 0


@pytest.mark.parametrize(
    ("zone", "horizon", "expected"),
    [
        ("UTC", datetime(2026, 9, 9, 12, tzinfo=UTC), "not_attempted"),
        ("UTC", datetime(2026, 9, 9, 12, 1, tzinfo=UTC), "unknown"),
        # A local day east of UTC starts earlier in UTC; the envelope follows it.
        ("Europe/Berlin", datetime(2026, 9, 9, 10, tzinfo=UTC), "not_attempted"),
        ("Europe/Berlin", datetime(2026, 9, 9, 11, tzinfo=UTC), "unknown"),
        # West of UTC the civil date (daily types) starts first.
        ("America/New_York", datetime(2026, 9, 9, 12, tzinfo=UTC), "not_attempted"),
        ("America/New_York", datetime(2026, 9, 9, 12, 1, tzinfo=UTC), "unknown"),
        ("UTC", None, "unknown"),
    ],
)
def test_never_requested_needs_receipts_before_every_reachable_filter(zone, horizon, expected):
    window = Window(date(2026, 9, 10), date(2026, 9, 10), date(2026, 11, 1), ZoneInfo(zone))
    assert unattempted(date(2026, 9, 10), window, horizon) == expected


def test_gap_periods_join_only_consecutive_days_of_one_state():
    days = [date(2026, 9, n) for n in range(1, 8)]
    states = {
        days[1]: "completed_empty",
        days[2]: "completed_empty",
        days[3]: "not_attempted",
        days[5]: "completed_empty",
    }
    runs = gap_periods(days, {days[0], days[4]}, states)
    assert [(r["start_date"].day, r["days"], r["state"]) for r in runs] == [
        (2, 2, "empty_response"),
        (4, 1, "not_retrieved"),
        (6, 1, "empty_response"),
        (7, 1, "unknown"),
    ]


def test_gap_dates_sent_to_the_model_are_counted_as_metadata():
    from app.services.health_transfer import describe_result

    arguments = query("2026-09-01", "2026-09-05", data_type="steps")
    card = {
        "metrics": [
            {
                "source_regimes": [{"first_date": "2026-09-02", "last_date": "2026-09-02"}],
                "retrieval": {
                    "gaps": [
                        {"start_date": "2026-09-01", "end_date": "2026-09-01"},
                        {"start_date": "2026-09-04", "end_date": "2026-09-05"},
                    ]
                },
            }
        ]
    }
    described = describe_result(arguments, card, True)
    assert described["metadata_dates"] == {"first": "2026-09-01", "last": "2026-09-05"}
    assert described["data_dates"] is None
