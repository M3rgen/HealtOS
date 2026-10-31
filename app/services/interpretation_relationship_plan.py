"""Whole-query relationship preflight without allocating pairs or record references."""

from app.services.interpretation_inputs import BudgetError


def matches(key, metric, source, definition):
    return (
        key[0] == metric
        and (not source or key[1] == source)
        and (not definition or key[2] == definition)
    )


class RegimeDays:
    def __init__(self, key, days, dates, first, last):
        self.key = key
        # Ordinals avoid overflowing date.min/date.max when applying a lag.
        # Sort once per regime; both query sides reuse the same ordered index.
        self.rows = {
            day.toordinal(): days[day]
            for day in dates
            if first <= day <= last and days[day]["value"] is not None and days[day]["comparable"]
        }


class RelationshipPlan:
    def __init__(self, x, y, lag):
        self.x, self.y, self.lag = x, y, lag

    def matching_days(self):
        for day, row in self.x.rows.items():
            target = day + self.lag
            other = self.y.rows.get(target)
            # The indices contain only comparable days inside the query window:
            # missing days are never shifted to another observation.
            if other is not None:
                yield day, target, row, other


def relationship_plans(series, query, *, max_summaries, max_pairs, max_references):
    """Check every summary and repeated evidence use before any output is built.

    Selection retains regimes whose date spans intersect the requested calendar,
    even if gaps or comparability leave them with no usable pairs. Only lengths
    of evidence lists are read: no reference list is traversed or copied here.
    Pagination and cursors do not participate in these fixed query-wide budgets.
    """
    first, last = query.date_range()
    xs, ys = [], []
    for key, days in sorted(series.items()):
        is_x = matches(key, query.metric, query.source, query.definition)
        is_y = matches(key, query.other_metric, query.other_source, query.other_definition)
        if not (is_x or is_y):
            continue
        dates = sorted(days)
        if not dates or first > dates[-1] or last < dates[0]:
            continue
        index = RegimeDays(key, days, dates, first, last)
        if is_x:
            xs.append(index)
        if is_y:
            ys.append(index)
    if len(xs) * len(ys) * len(query.lags) > max_summaries:
        raise BudgetError("summary_budget_exceeded")

    pair_count = reference_count = 0
    plans = []
    for x in xs:
        for y in ys:
            for lag in query.lags:
                plan = RelationshipPlan(x, y, lag)
                for _, _, row, other in plan.matching_days():
                    pair_count += 1
                    if pair_count > max_pairs:
                        raise BudgetError("relationship_pair_budget_exceeded")
                    # Count both sides, including self-pairs, duplicate records
                    # and reuse of the same record in other summaries or lags.
                    reference_count += len(row["evidence"]) + len(other["evidence"])
                    if reference_count > max_references:
                        raise BudgetError("relationship_reference_budget_exceeded")
                plans.append(plan)
    return plans
