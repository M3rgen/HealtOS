"""Past-only robust personal references and calendar-aligned, non-causal relationships."""

from datetime import date
from fractions import Fraction
from math import sqrt
from statistics import median

from app.services.finite_math import finite_calculation
from app.services.interpretation_baseline_index import baseline_windows
from app.services.interpretation_relationship_plan import relationship_plans
from app.services.interpretation_series import (
    COMPARABILITY_BLOCKERS as COMPARABILITY_BLOCKERS,
)
from app.services.interpretation_series import (
    DAILY_KINDS as DAILY_KINDS,
)
from app.services.interpretation_series import (
    daily_series as daily_series,
)
from app.services.interpretation_series import (
    definition_groups as definition_groups,
)
from app.services.interpretation_types import Baseline, Candidate, Relationship, Statistics

LOOKBACK_DAYS = 56
MIN_BASELINE_DAYS = 14
COLD_START_DAYS = 7
MIN_SUPPORT_FRACTION = 0.5
MIN_PAIRS = 20
MIN_SPLIT_PAIRS = 8
MAX_SPLIT_DIFFERENCE = 0.35


def quantile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    a, b, weight = ordered[lower], ordered[upper], position - lower
    return finite_calculation(
        a + (b - a) * weight,
        lambda: Fraction(a) + (Fraction(b) - Fraction(a)) * Fraction(weight),
    )


def statistics(values):
    if not values:
        return Statistics(count=0)
    center = finite_calculation(median(values), lambda: median(list(map(Fraction, values))))
    return Statistics(
        count=len(values),
        median=center,
        mad=finite_calculation(
            median([abs(value - center) for value in values]),
            lambda: median([abs(Fraction(value) - Fraction(center)) for value in values]),
        )
        if center is not None
        else None,
        minimum=min(values),
        maximum=max(values),
        p10=quantile(values, 0.1),
        p90=quantile(values, 0.9),
    )


def statistics_reasons(stats):
    return [
        "numeric_overflow:" + field
        for field in ("median", "mad", "p10", "p90")
        if stats.count and getattr(stats, field) is None
    ]


def frame(key):
    metric, source, definition, unit, method, basis = key
    return dict(
        metric=metric,
        source=source,
        definition=definition,
        unit=unit,
        method_version=method,
        time_basis=basis,
    )


def baselines(
    series, first, last, lookback, *, metric=None, source=None, definition=None, max_summaries
):
    results = []
    windows = baseline_windows(
        series,
        first,
        last,
        lookback,
        metric=metric,
        source=source,
        definition=definition,
        max_summaries=max_summaries,
    )
    for key, day, current, prior, method_break in windows:
        values = [row["value"] for _, row in prior]
        stats = statistics(values)
        reasons = list(current["reasons"]) + statistics_reasons(stats)
        if len(prior) < COLD_START_DAYS:
            reasons.append("cold_start")
        if len(prior) < MIN_BASELINE_DAYS:
            reasons.append("insufficient_baseline_days")
        if len(prior) / lookback < MIN_SUPPORT_FRACTION:
            reasons.append("insufficient_calendar_support")
        if len(prior) < lookback:
            reasons.append("missing_comparable_days")
        if method_break:
            reasons.append("definition_or_method_break")
        if current["value"] is None:
            reasons.append("evaluated_value_unavailable")
        eligible = (
            len(prior) >= MIN_BASELINE_DAYS
            and len(prior) / lookback >= MIN_SUPPORT_FRACTION
            and current["value"] is not None
            and current["comparable"]
            and not statistics_reasons(stats)
        )
        percentile = z = None
        if stats.mad == 0:
            reasons.append("zero_mad")
        if eligible:
            value = current["value"]
            percentile = (
                100 * (sum(v < value for v in values) + 0.5 * values.count(value)) / len(values)
            )
            center, spread = stats.median, stats.mad
            if spread and center is not None:
                z = finite_calculation(
                    (value - center) / (1.4826 * spread),
                    lambda value=value, center=center, spread=spread: (
                        (Fraction(value) - Fraction(center)) / (Fraction(1.4826) * Fraction(spread))
                    ),
                )
                if z is None:
                    reasons.append("numeric_overflow:robust_z")
                    eligible = False
                    percentile = None
        results.append(
            Baseline(
                **frame(key),
                method_details=current["method_details"],
                date=str(day),
                value=current["value"],
                eligible=eligible,
                support_days=len(prior),
                expected_days=lookback,
                minimum_support=MIN_BASELINE_DAYS,
                cold_start_support=COLD_START_DAYS,
                comparison_dates=[str(d) for d, _ in prior],
                statistics=stats,
                percentile=percentile,
                robust_z=z,
                reason_codes=sorted(set(reasons)),
                evidence=[e for _, row in prior for e in row["evidence"]] + current["evidence"],
            )
        )
    return results


def candidates(summaries):
    choices = []
    for index, item in enumerate(summaries):
        if not item.eligible or item.percentile is None:
            continue
        # Observation candidates describe evidence, not medical targets or treatment.
        score = abs(item.percentile - 50)
        if score < 40:
            continue
        choices.append((score, index, item))
    choices.sort(key=lambda row: (-row[0], row[1]))
    return [
        Candidate(
            rank=rank + 1,
            code="review_personal_deviation",
            summary_index=index,
            evidence_dates=[item.date, *item.comparison_dates],
            priority_score=score,
            suggestion=(
                "Kişisel aralıktan ayrılan ölçümü bağlamıyla gözden geçir; "
                "aynı yöntemle tekrarını izle."
            ),
        )
        for rank, (score, index, item) in enumerate(choices[:5])
    ]


def ranks(values):
    ordered = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        stop = start + 1
        while stop < len(ordered) and values[ordered[stop]] == values[ordered[start]]:
            stop += 1
        midrank = (start + 1 + stop) / 2
        for position in range(start, stop):
            result[ordered[position]] = midrank
        start = stop
    return result


def correlation(pairs):
    if len(pairs) < 2:
        return None
    x, y = ranks([p["x"] for p in pairs]), ranks([p["y"] for p in pairs])
    ax, ay = sum(x) / len(x), sum(y) / len(y)
    denominator = sqrt(sum((v - ax) ** 2 for v in x) * sum((v - ay) ** 2 for v in y))
    return (
        sum((a - ax) * (b - ay) for a, b in zip(x, y, strict=True)) / denominator
        if denominator
        else None
    )


def relationships(series, query, *, max_summaries, max_pairs, max_references):
    first, last = query.date_range()
    plans = relationship_plans(
        series,
        query,
        max_summaries=max_summaries,
        max_pairs=max_pairs,
        max_references=max_references,
    )
    results = []
    for plan in plans:
        xkey, ykey, lag = plan.x.key, plan.y.key, plan.lag
        pairs: list = [
            dict(
                x_date=str(date.fromordinal(day)),
                y_date=str(date.fromordinal(target)),
                x=row["value"],
                y=other["value"],
                x_records=[e["record"] for e in row["evidence"]],
                y_records=[e["record"] for e in other["evidence"]],
            )
            for day, target, row, other in plan.matching_days()
        ]
        expected = max(0, (last - first).days + 1 - abs(lag))
        supported = len(pairs) >= MIN_PAIRS and len(pairs) >= expected * MIN_SUPPORT_FRACTION
        midpoint = first + (last - first) // 2
        early = [pair for pair in pairs if pair["x_date"] <= str(midpoint)]
        late = [pair for pair in pairs if pair["x_date"] > str(midpoint)]
        left_corr, right_corr = correlation(early), correlation(late)
        splits = [left_corr, right_corr]
        stable = (
            min(len(early), len(late)) >= MIN_SPLIT_PAIRS
            and left_corr is not None
            and right_corr is not None
            and left_corr * right_corr > 0
            and abs(left_corr - right_corr) <= MAX_SPLIT_DIFFERENCE
        )
        reasons = []
        if not supported:
            reasons.append("insufficient_pairs_or_support")
        if not stable:
            reasons.append("unstable_or_constant_relationship")
        results.append(
            Relationship(
                metric=xkey[0],
                other_metric=ykey[0],
                source=xkey[1],
                other_source=ykey[1],
                definition=xkey[2],
                other_definition=ykey[2],
                unit=xkey[3],
                other_unit=ykey[3],
                lag_days=lag,
                eligible=supported and stable,
                support_pairs=len(pairs),
                expected_pairs=expected,
                minimum_pairs=MIN_PAIRS,
                correlation=correlation(pairs) if supported else None,
                split_correlations=splits,
                stable=stable,
                pairs=pairs,
                reason_codes=reasons,
                limits=[
                    "association_not_causation",
                    "preselected_lags_only",
                    "missing_days_not_shifted",
                    "serial_dependence_and_confounding_uncontrolled",
                    "exploratory_no_p_value_claim",
                ],
            )
        )
    return results
