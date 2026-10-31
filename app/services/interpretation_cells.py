"""Query-wide intraday work limits and half-open interval event sweep."""

from collections import OrderedDict
from dataclasses import dataclass
from itertools import pairwise

from app.services.interpretation_inputs import BudgetError

# Fixed limits cover all selected sources and days, regardless of pagination.
# Keep plans bounded before allocating evidence dictionaries or Pydantic models.
MAX_INTRADAY_CELLS = 20_000
MAX_INTRADAY_EVIDENCE = 20_000


@dataclass
class IntradayBudget:
    max_summaries: int
    cells: int = 0
    evidence: int = 0
    summaries: int = 0

    def charge(self, *, cells=0, evidence=0, summaries=0):
        if self.summaries + summaries > self.max_summaries:
            raise BudgetError("summary_budget_exceeded")
        if self.evidence + evidence > MAX_INTRADAY_EVIDENCE:
            raise BudgetError("intraday_evidence_budget_exceeded")
        if self.cells + cells > MAX_INTRADAY_CELLS:
            raise BudgetError("intraday_cell_budget_exceeded")
        self.cells += cells
        self.evidence += evidence
        self.summaries += summaries


class CellPlan:
    def __init__(self, left, stop, budget):
        self.budget = budget
        self.events = {left: [], stop: []}
        self.observations = []
        self.span_count = 0

    def observe(self, raw, day, value, field, reasons=()):
        # Every produced evidence reference counts, including duplicates across days
        # and samples just outside a day that can support clipped interpolation.
        first = not self.observations
        self.budget.charge(evidence=1, summaries=int(first), cells=int(first))
        self.observations.append((raw, day, value, field, reasons))

    def add_span(self, start, end, value):
        for stamp in (start, end):
            if stamp not in self.events:
                self.budget.charge(cells=1)
                self.events[stamp] = []
        index = self.span_count
        self.span_count += 1
        self.events[start].append((index, True, value))
        self.events[end].append((index, False, value))

    def cells(self):
        # OrderedDict retains O(1) access to the sole active value even after many
        # insertions/deletions (ordinary dict iteration can scan deleted slots).
        active = OrderedDict()
        for start, end in pairwise(sorted(self.events)):
            # Apply every event at the boundary before emitting [start, end).
            # Touching intervals never conflict, regardless of event insertion order.
            for index, entering, value in self.events[start]:
                if entering:
                    active[index] = value
                else:
                    del active[index]
            state = "conflict" if len(active) > 1 else "gap"
            value = next(iter(active.values())) if len(active) == 1 else None
            if len(active) == 1 and value is not None:
                state = "supported"
            yield start, end, state, value
