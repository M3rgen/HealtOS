"""Calendar pairs, preselected lags, support and stability through health_archive."""

from datetime import date, timedelta
from math import log2, sqrt
from types import SimpleNamespace

import pytest
from derived_records import DAY, store, summary

from app.services import codex_health
from app.services.codex_health import read_health
from app.services.interpretation_stats import correlation, ranks, relationships


@pytest.mark.parametrize(
    "values",
    [[], [7], [4] * 8, [8, 7, 6, 5, 4], [2, -0.0, 2, 0.0, -1e308, 1e308, 1]],
)
def test_midranks_and_spearman_preserve_ties_and_input_order(values):
    def reference(items):
        return [
            sum(other < value for other in items) + (items.count(value) + 1) / 2 for value in items
        ]

    original = values.copy()
    assert ranks(values) == reference(values)
    assert values == original
    for other in (values, values[::-1], sorted(values), [1] * len(values)):
        x, y = reference(values), reference(other)
        ax = sum(x) / len(x) if x else 0
        ay = sum(y) / len(y) if y else 0
        denominator = sqrt(sum((v - ax) ** 2 for v in x) * sum((v - ay) ** 2 for v in y))
        expected = (
            sum((a - ax) * (b - ay) for a, b in zip(x, y, strict=True)) / denominator
            if denominator
            else None
        )
        result = correlation([dict(x=a, y=b) for a, b in zip(values, other, strict=True)])
        assert result == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize("count", [128, 1024, 4096])
@pytest.mark.parametrize("shape", ["constant", "reverse", "mixed"])
def test_rank_comparison_count_is_n_log_n(count, shape):
    comparisons = 0
    ceiling = 2 * count * (int(log2(count)) + 1)

    class Number(float):
        def __lt__(self, other):
            nonlocal comparisons
            comparisons += 1
            assert comparisons <= ceiling
            return super().__lt__(other)

        def __eq__(self, other):
            nonlocal comparisons
            comparisons += 1
            assert comparisons <= ceiling
            return super().__eq__(other)

    numbers = [
        1 if shape == "constant" else count - i if shape == "reverse" else (i * 131) % 97
        for i in range(count)
    ]
    result = ranks(list(map(Number, numbers)))
    assert 0 < comparisons <= ceiling
    assert result == ranks(numbers)


@pytest.mark.parametrize("first", [date.min, date.max - timedelta(days=6)])
def test_lags_at_calendar_boundaries_keep_missing_and_incomparable_days(first):
    last = first + timedelta(days=6)
    xkey = ("resting_bpm", "x", "definition", "bpm", "method", "civil_day")
    ykey = ("hrv_ms", "y", "definition", "ms", "method", "civil_day")

    def days(offsets):
        return {
            first + timedelta(days=i): dict(
                value=None if i == 2 else i,
                comparable=i != 3,
                evidence=[{"record": str(i)}, {"record": str(i)}],
            )
            for i in offsets
        }

    series = {xkey: days([0, 2, 3, 5, 6]), ykey: days([0, 1, 2, 3, 6])}
    # Isolate the calculator's date arithmetic, including date.max, from the
    # public query's separate future-date validation.
    request = SimpleNamespace(
        metric="resting_bpm",
        other_metric="hrv_ms",
        source=None,
        other_source=None,
        definition=None,
        other_definition=None,
        lags=[-14, -1, 0, 1, 14],
        date_range=lambda: (first, last),
    )
    rows = relationships(series, request, max_summaries=5, max_pairs=20, max_references=80)
    for row in rows:
        expected = []
        for day, x in sorted(series[xkey].items()):
            target = day.toordinal() + row.lag_days
            if not first.toordinal() <= target <= last.toordinal():
                continue
            y = series[ykey].get(date.fromordinal(target))
            if (
                y is not None
                and x["comparable"]
                and y["comparable"]
                and x["value"] is not None
                and y["value"] is not None
            ):
                expected.append(
                    dict(
                        x_date=str(day),
                        y_date=str(date.fromordinal(target)),
                        x=x["value"],
                        y=y["value"],
                        x_records=[str(x["value"])] * 2,
                        y_records=[str(y["value"])] * 2,
                    )
                )
        assert [p.model_dump() for p in row.pairs] == expected
        assert row.support_pairs == len(expected)
        assert row.expected_pairs == max(0, 7 - abs(row.lag_days))
        assert not row.eligible and "insufficient_pairs_or_support" in row.reason_codes


async def relationship(sessions, monkeypatch, size=40, lags=None):
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    return await read_health(
        dict(
            action="relationship",
            metric="resting_bpm",
            other_metric="hrv_ms",
            lags=lags or [1],
            start_date=str(DAY - timedelta(days=size)),
            end_date=str(DAY),
            limit=100,
        )
    )


async def paired(sessions, size=40, missing=(), reverse=False, constant=False):
    first = DAY - timedelta(days=size)
    await store(
        sessions,
        "daily-resting-heart-rate",
        *[
            summary(
                "daily-resting-heart-rate",
                str(i),
                day=first + timedelta(days=i),
                beatsPerMinute=i + 50,
            )
            for i in range(size)
        ],
    )
    await store(
        sessions,
        "daily-heart-rate-variability",
        *[
            summary(
                "daily-heart-rate-variability",
                str(i),
                day=first + timedelta(days=i + 1),
                rmssd=10 if constant else 100 - i if reverse and i >= size // 2 else i + 10,
            )
            for i in range(size)
            if i not in missing
        ],
    )


async def test_lag_pairs_align_calendar_and_do_not_slide_missing_days(sessions, monkeypatch):
    await paired(sessions, missing=(5, 12, 19))
    result = await relationship(sessions, monkeypatch, lags=[0, 1, -1])
    assert [r["lag_days"] for r in result["summaries"]] == [0, 1, -1]
    row = result["summaries"][1]
    assert row["eligible"] and row["stable"] and row["correlation"] == pytest.approx(1)
    assert row["support_pairs"] == 37 and row["expected_pairs"] == 40
    assert row["split_correlations"] == pytest.approx([1, 1])
    assert all(p["y"] == p["x"] - 40 for p in row["pairs"])
    assert {p["x_date"] for p in row["pairs"]}.isdisjoint(
        {str(DAY - timedelta(days=40 - i)) for i in (5, 12, 19)}
    )
    assert "association_not_causation" in row["limits"] and "p_value" not in row


@pytest.mark.parametrize(
    "size,missing,reason",
    [
        (19, (), "insufficient_pairs_or_support"),
        (60, tuple(range(40)), "insufficient_pairs_or_support"),
    ],
)
async def test_minimum_pair_and_calendar_support_thresholds(
    sessions, monkeypatch, size, missing, reason
):
    await paired(sessions, size=size, missing=missing)
    row = (await relationship(sessions, monkeypatch, size=size))["summaries"][0]
    assert not row["eligible"] and reason in row["reason_codes"]
    assert row["correlation"] is None


@pytest.mark.parametrize("constant", [False, True])
async def test_split_instability_and_constant_series_are_not_supported_findings(
    sessions, monkeypatch, constant
):
    await paired(sessions, reverse=True, constant=constant)
    row = (await relationship(sessions, monkeypatch))["summaries"][0]
    assert not row["eligible"] and not row["stable"]
    assert "unstable_or_constant_relationship" in row["reason_codes"]
    if constant:
        assert row["correlation"] is None and row["split_correlations"] == [None, None]


async def test_relationship_statistics_do_not_depend_on_page_size(sessions, monkeypatch):
    await paired(sessions)
    result = await relationship(sessions, monkeypatch, lags=[0, 1])
    args = dict(
        action="relationship",
        metric="resting_bpm",
        other_metric="hrv_ms",
        lags=[0, 1],
        start_date=str(DAY - timedelta(days=40)),
        end_date=str(DAY),
        limit=1,
    )
    first = await read_health(args)
    second = await read_health(dict(args, cursor=first["next_cursor"], limit=200))
    assert first["summaries"] + second["summaries"] == result["summaries"]


async def test_stability_requires_support_in_both_calendar_halves(sessions, monkeypatch):
    await paired(sessions, missing=tuple(range(20, 40)))
    row = (await relationship(sessions, monkeypatch))["summaries"][0]
    assert row["support_pairs"] == 20 and row["correlation"] == pytest.approx(1)
    assert not row["stable"] and not row["eligible"]
    assert "unstable_or_constant_relationship" in row["reason_codes"]
