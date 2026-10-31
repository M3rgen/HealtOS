from concurrent.futures import ThreadPoolExecutor

from app.core.request_limits import RequestBudget


def test_budget_is_atomic_across_threads_and_uses_monotonic_clock():
    current = [1000.0]
    budget = RequestBudget(7, lambda: current[0])
    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(lambda _: budget.retry_after(), range(100)))
    assert results.count(None) == 7
    assert results.count(60) == 93
    current[0] += 59.25
    assert budget.retry_after() == 1
    current[0] += 0.75
    assert budget.retry_after() is None
