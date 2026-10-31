"""Indexed, query-wide planning before personal reference output is allocated."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta

from app.services.interpretation_inputs import BudgetError

# Count every output reference, including the current day and history repeated in
# later summaries: each summary materializes its own copy. Pagination cannot reduce
# this fixed whole-query work ceiling. It admits the largest query with one record a
# day: five daily metrics, 90 days, each with 90 days of history plus the day itself.
MAX_BASELINE_EVIDENCE = 5 * 90 * (90 + 1)


class DayIndex:
    def __init__(self, days, first, last):
        self.dates = sorted(days)
        self.rows = [days[day] for day in self.dates]
        self.first = bisect_left(self.dates, first)
        self.stop = bisect_right(self.dates, last)
        self.history = []
        self.history_dates = []
        self.evidence_counts = [0]

    def index_history(self):
        for day, row in zip(self.dates, self.rows, strict=True):
            if row["value"] is not None and row["comparable"]:
                self.history.append((day, row))
                self.history_dates.append(day)
                self.evidence_counts.append(self.evidence_counts[-1] + len(row["evidence"]))

    def window(self, start, day):
        return bisect_left(self.history_dates, start), bisect_left(self.history_dates, day)


class MethodIndex:
    """Count all regimes' dates minus this regime's dates in [start, day).

    Duplicate dates across regimes must remain: a different method on the same
    historical day is a break too. Values and comparability do not affect this
    evidence. Include unselected definitions, but never unrelated metric/sources.
    """

    def __init__(self, series, selected):
        pairs = {key[:2] for key in selected}
        dates = defaultdict(list)
        for key, days in series.items():
            if key[:2] in pairs:
                dates[key[:2]].extend(days)
        self.dates = {pair: sorted(days) for pair, days in dates.items()}

    def has_break(self, key, own_dates, start, day):
        dates = self.dates[key[:2]]
        all_count = bisect_left(dates, day) - bisect_left(dates, start)
        own_count = bisect_left(own_dates, day) - bisect_left(own_dates, start)
        return all_count > own_count


def baseline_windows(series, first, last, lookback, *, metric, source, definition, max_summaries):
    """Preflight the complete filtered query, then yield bounded history slices.

    Days are sorted once per selected series. Prefix counts and binary searches
    bound planning to O(n log n + s log n), without traversing repeated evidence
    or materializing per-summary histories until both budgets have passed.
    """
    selected = {
        key: DayIndex(days, first, last)
        for key, days in series.items()
        if (not metric or key[0] == metric)
        and (not source or key[1] == source)
        and (not definition or key[2] == definition)
    }
    if sum(index.stop - index.first for index in selected.values()) > max_summaries:
        raise BudgetError("summary_budget_exceeded")
    evidence_count = 0
    windows = []
    for key, index in sorted(selected.items()):
        if index.first == index.stop:
            continue
        index.index_history()
        for position in range(index.first, index.stop):
            day, current = index.dates[position], index.rows[position]
            start = day - timedelta(days=lookback)
            left, stop = index.window(start, day)
            evidence_count += (
                index.evidence_counts[stop] - index.evidence_counts[left] + len(current["evidence"])
            )
            if evidence_count > MAX_BASELINE_EVIDENCE:
                raise BudgetError("baseline_evidence_budget_exceeded")
            windows.append((key, index, position, start, left, stop))
    methods = MethodIndex(series, selected)
    for key, index, position, start, left, stop in windows:
        day, current = index.dates[position], index.rows[position]
        yield (
            key,
            day,
            current,
            index.history[left:stop],
            methods.has_break(key, index.dates, start, day),
        )
