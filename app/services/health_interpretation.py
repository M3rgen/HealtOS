"""Production entry point for bounded interpretation actions, including bound v2 cursors."""

import base64
import json

from app.parsers.common import payload_hash
from app.services.derived import BUILD_FAILED, derived_range
from app.services.health_windows import interpretation_window
from app.services.interpretation_inputs import BudgetError, load_inputs
from app.services.interpretation_intraday import intraday_summaries, sleep_summaries
from app.services.interpretation_measures import (
    PHYSIOLOGY,
    QUANTITIES,
    measurement_summaries,
    session_summaries,
)
from app.services.interpretation_stats import (
    DAILY_KINDS,
    baselines,
    candidates,
    daily_series,
    relationships,
)
from app.services.interpretation_types import Interpretation, Refusal

RESPONSE_BUDGET_BYTES = 64 * 1024
MAX_SUMMARIES = 1000
MAX_RELATIONSHIP_REGIMES = 16
# Fixed whole-query allocation ceilings, independent of page size and cursor.
# References count both pair sides and every reuse across summaries/lags.
MAX_RELATIONSHIP_PAIRS = 20_000
MAX_RELATIONSHIP_REFERENCES = 40_000
DERIVED_ACTIONS = frozenset({"baseline", "analyze", "relationship", "sleep"})


# Whole-query work budgets: the same ceiling holds for every page size and cursor.
SCOPE_BUDGETS = ("baseline_", "intraday_", "overlap_", "relationship_", "summary_")
SCOPE_ERROR = "Yorum sorgusu tamamlanamadı; aralığı ya da metriği daralt."


def refusal(code):
    result = Refusal(code=code, reason_codes=[code])
    if code.startswith(SCOPE_BUDGETS) and code.endswith("_budget_exceeded"):
        result.error = SCOPE_ERROR
    return result.model_dump(mode="json")


def encode_cursor(query_hash, snapshot, offset):
    return base64.urlsafe_b64encode(
        json.dumps(["interpretation/2", query_hash, snapshot, offset]).encode()
    ).decode()


def cursor_offset(cursor, query_hash, snapshot):
    if cursor is None:
        return 0
    try:
        version, bound, state, offset = json.loads(
            base64.b64decode(cursor, altchars=b"-_", validate=True)
        )
        if version != "interpretation/2" or bound != query_hash:
            raise BudgetError("cursor_query_mismatch")
        if state != snapshot:
            raise BudgetError("stale_cursor")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, UnicodeDecodeError):
        raise BudgetError("invalid_cursor") from None


def derived_window(query):
    """The derived days an action reads, so they can be built before its snapshot."""
    if query.action not in DERIVED_ACTIONS or query.data_type:
        return None  # a data_type is refused by these actions before any read
    first, last = query.date_range()
    return interpretation_window(query.action, first, last, query.lookback_days)


async def interpret(session, query):
    first, last = query.date_range()
    if query.data_type:
        accepted = (
            PHYSIOLOGY
            if query.action == "physiology"
            else {*QUANTITIES, "muscle"}
            if query.action == "longitudinal"
            else {"steps", "heart-rate"}
            if query.action == "intraday"
            else set()
        )
        if query.data_type not in accepted:
            return refusal("unsupported_data_type")
    read_first, read_last = interpretation_window(query.action, first, last, query.lookback_days)
    kinds = (
        set(DAILY_KINDS.values())
        if query.action in {"baseline", "analyze", "sleep", "relationship"}
        else {query.data_type}
        if query.data_type
        else {"steps", "heart-rate"}
        if query.action == "intraday"
        else {"exercise"}
        if query.action == "sessions"
        else set(PHYSIOLOGY)
        if query.action == "physiology"
        else set(QUANTITIES)
    )

    async def inputs_now():
        return await load_inputs(
            session,
            read_first,
            read_last,
            query.timezone,
            kinds,
            manual=query.action == "longitudinal",
            workouts=query.action == "sessions",
        )

    try:
        inputs = await inputs_now()
        recommendations = []
        derived = None
        if query.action in DERIVED_ACTIONS:
            # Days are built beforehand; in a PostgreSQL REPEATABLE READ snapshot an
            # unbuilt day is only reported, never built from that snapshot.
            derived = await derived_range(session, read_first, read_last, query.timezone)
            if any(day["state"] == "error" for day in derived["days"]):
                return refusal(BUILD_FAILED)
            if any(day["state"] != "current" for day in derived["days"]):
                return refusal("inputs_changed_retry")
        if query.action in {"baseline", "analyze", "relationship"}:
            series = daily_series(derived, inputs)
            if query.action == "relationship":
                if len(series) > MAX_RELATIONSHIP_REGIMES:
                    return refusal("relationship_regime_budget_exceeded")
                summaries = relationships(
                    series,
                    query,
                    max_summaries=MAX_SUMMARIES,
                    max_pairs=MAX_RELATIONSHIP_PAIRS,
                    max_references=MAX_RELATIONSHIP_REFERENCES,
                )
            else:
                summaries = baselines(
                    series,
                    first,
                    last,
                    query.lookback_days,
                    metric=query.metric,
                    source=query.source,
                    definition=query.definition,
                    max_summaries=MAX_SUMMARIES,
                )
                if query.action == "analyze":
                    recommendations = candidates(summaries)
        elif query.action == "sleep":
            summaries = sleep_summaries(derived, first, last)
        elif query.action == "intraday":
            summaries = intraday_summaries(inputs, query, max_summaries=MAX_SUMMARIES)
        elif query.action == "sessions":
            summaries = session_summaries(inputs, query, max_summaries=MAX_SUMMARIES)
        else:
            summaries = measurement_summaries(inputs, query, max_summaries=MAX_SUMMARIES)
        if query.action not in {"baseline", "analyze", "relationship"}:
            summaries = [
                row
                for row in summaries
                if (not query.source or row.source == query.source)
                and (not query.definition or row.definition == query.definition)
            ]
        if len(summaries) > MAX_SUMMARIES:
            return refusal("summary_budget_exceeded")
        if (await inputs_now()).fingerprint != inputs.fingerprint:
            return refusal("stale_cursor" if query.cursor else "inputs_changed_retry")
        # Limit may vary between pages. All scientific inputs and action parameters are bound.
        query_hash = payload_hash(query.model_dump(mode="json", exclude={"cursor", "limit"}))
        snapshot = payload_hash(
            [inputs.fingerprint, [s.model_dump(mode="json") for s in summaries]]
        )
        offset = cursor_offset(query.cursor, query_hash, snapshot)
        if offset > len(summaries):
            return refusal("invalid_cursor")
        stop = min(offset + query.limit, len(summaries))
        # Candidate references use absolute indices into the full, stable summary sequence.
        result = Interpretation(
            action=query.action,
            timezone=query.timezone,
            start_date=str(first),
            end_date=str(last),
            summaries=summaries[offset:stop],
            candidates=recommendations,
            total_summaries=len(summaries),
            summary_offset=offset,
            next_cursor=encode_cursor(query_hash, snapshot, stop)
            if stop < len(summaries)
            else None,
            reason_codes=[] if summaries else ["no_eligible_evidence_in_window"],
        )
        encoded = result.model_dump(mode="json")
        if (
            len(json.dumps(encoded, ensure_ascii=False, allow_nan=False).encode())
            > RESPONSE_BUDGET_BYTES
        ):
            return refusal("response_budget_exceeded")
        return encoded
    except BudgetError as exc:
        return refusal(str(exc))
