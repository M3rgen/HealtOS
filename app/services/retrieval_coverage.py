"""Read bounded daily retrieval evidence, separately from present eligible values.

Unknown history stays unknown. A completed attempt must have a first-page root, a
contiguous token chain, a durable last page and an appended completion outcome. Mutable
job tables are not read. A day that no attempt touched is ``not_attempted`` only when it
begins after the first receipt was written (``receipt_horizon``): from then on every
Google read leaves a receipt, so the absence of one is evidence. Earlier days stay
``unknown``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.timeutil import as_utc
from app.google_health.data_types import (
    CIVIL_AFTER,
    CIVIL_BEFORE,
    WAKE_DATED_TYPES,
    DataTypeSpec,
    RecordKind,
)
from app.models.eligibility import raw_counted
from app.models.identity import plan_each_query
from app.models.retrieval import RetrievalEvent as Event
from app.models.retrieval import RetrievalReceipt as Receipt
from app.models.state import RawHealthData as Raw
from app.models.state import RawJSON
from app.parsers.common import parse_datetime
from app.services.coverage import (
    COVERAGE_BUDGET_BYTES,
    METRICS,
    NORMALIZED,
    Metric,
    Window,
    _exclusions,
    _google_streams,
    _regime,
    _stream_of,
)
from app.services.current_sleep import sleep_wake_window

MAX_RECEIPTS = 2000
MAX_EVENTS = 5000
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_DAYS = 31


class CoverageLimit(ValueError):
    pass


def merge_ranges(ranges):
    """Half-open union: touching intervals join, even a one-second gap remains a gap."""
    merged = []
    for start, stop in sorted(ranges):
        if stop <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
        else:
            merged.append((start, stop))
    return merged


def covers(ranges, start, stop):
    return any(left <= start and right >= stop for left, right in merge_ranges(ranges))


async def evidence(session: AsyncSession, name: str, window: Window):
    """Read one type's full receipt lineage within its own evidence budgets."""
    # Include civil filter bounds in their own coordinate system, plus daily UTC dates.
    lower = window.start - CIVIL_BEFORE - timedelta(days=1)
    upper = window.stop + CIVIL_AFTER + timedelta(days=1)
    rows = list(
        (
            await session.scalars(
                select(Receipt)
                .where(
                    Receipt.data_type == name,
                    or_(Receipt.queried_from.is_(None), Receipt.queried_from < upper),
                    or_(Receipt.queried_to.is_(None), Receipt.queried_to > lower),
                )
                .order_by(Receipt.started_at, Receipt.id)
                .limit(MAX_RECEIPTS + 1)
            )
        ).all()
    )
    if len(rows) > MAX_RECEIPTS:
        raise CoverageLimit("coverage_receipt_limit")
    if not rows:
        return [], {}
    ids = [row.id for row in rows]
    events = list(
        (
            await session.scalars(
                select(Event)
                .options(defer(Event.returned_times))
                .where(Event.receipt_id.in_(ids))
                .order_by(Event.id)
                .limit(MAX_EVENTS + 1)
            )
        ).all()
    )
    if len(events) > MAX_EVENTS:
        raise CoverageLimit("coverage_evidence_limit")
    # All metadata proves the token chain, but timestamps from other days are not read.
    # Include both civil-date UTC coordinates and the requested physical day bounds.
    read_start = min(window.start, datetime.combine(window.first, time.min, UTC))
    read_stop = max(window.stop, datetime.combine(window.end + timedelta(days=1), time.min, UTC))
    relevant = [
        e.id
        for e in events
        if e.kind == "page"
        and e.first_record_at is not None
        and e.last_record_at is not None
        and as_utc(e.first_record_at) < read_stop
        and as_utc(e.last_record_at) >= read_start
    ]
    times: dict[int, list] = {}
    if relevant:
        size = await session.scalar(
            select(func.coalesce(func.sum(func.length(cast(Event.returned_times, Text))), 0)).where(
                Event.id.in_(relevant)
            )
        )
        if (size or 0) > MAX_EVIDENCE_BYTES:
            raise CoverageLimit("coverage_evidence_limit")
        # Exact immutable ids: a writer appending new events cannot grow this read.
        times = {
            identifier: stamps
            for identifier, stamps in (
                await session.execute(
                    select(Event.id, Event.returned_times).where(Event.id.in_(relevant))
                )
            ).tuples()
        }
    by_receipt = defaultdict(list)
    by_event = {}
    for event in events:
        by_receipt[event.receipt_id].append(event)
        by_event[event.id] = event
    receipts = {row.id: row for row in rows}

    def chain(receipt, seen):
        if len(seen) >= 100:
            raise CoverageLimit("coverage_lineage_limit")
        if receipt.id in seen or any(e.kind == "reset" for e in by_receipt[receipt.id]):
            return None
        seen = seen | {receipt.id}
        pages = []
        expected = receipt.resume_token_hash
        if not receipt.starts_first:
            parent = by_event.get(receipt.parent_event_id)
            if parent is None:
                return None
            previous = receipts.get(parent.receipt_id)
            if (
                not previous
                or parent.kind != "page"
                or not expected
                or parent.next_token_hash != expected
                or (
                    previous.lane,
                    previous.owner,
                    previous.data_type,
                    previous.requested_from,
                    previous.requested_to,
                    previous.basis,
                    previous.access_method,
                    previous.query_filter,
                    previous.queried_from,
                    previous.queried_to,
                )
                != (
                    receipt.lane,
                    receipt.owner,
                    receipt.data_type,
                    receipt.requested_from,
                    receipt.requested_to,
                    receipt.basis,
                    receipt.access_method,
                    receipt.query_filter,
                    receipt.queried_from,
                    receipt.queried_to,
                )
            ):
                return None
            prefix = chain(previous, seen)
            if prefix is None or not prefix or prefix[-1].id != parent.id:
                return None
            pages.extend(prefix)
        elif expected is not None or receipt.parent_event_id is not None:
            return None
        seen_tokens = {page.next_token_hash for page in pages if page.next_token_hash}
        for event in by_receipt[receipt.id]:
            if event.kind == "page":
                if event.request_token_hash != expected or (pages and pages[-1].last_page):
                    return None
                if event.next_token_hash and event.next_token_hash in seen_tokens:
                    return None
                if event.next_token_hash:
                    seen_tokens.add(event.next_token_hash)
                pages.append(event)
                expected = event.next_token_hash
        return pages

    result = []
    for receipt in rows:
        outcomes = by_receipt[receipt.id]
        completed = next((e for e in reversed(outcomes) if e.kind == "completed"), None)
        pages = chain(receipt, set())
        valid = bool(
            completed
            and pages
            and pages[-1].last_page
            and pages[-1].next_token_hash is None
            and pages[-1].id < completed.id
            and outcomes[-1].kind == "completed"
        )
        # Pagination can cross midnight (or resume days later). Its initial snapshot
        # cannot prove time that was still in the future when the first page began.
        root = receipts[pages[0].receipt_id] if pages else receipt
        result.append((receipt, pages or [], completed if valid else None, as_utc(root.started_at)))
    # The recursive closure may outlive this call; do not retain its evidence lookups.
    by_receipt.clear()
    by_event.clear()
    receipts.clear()
    return result, times


async def receipt_horizon(session: AsyncSession) -> datetime | None:
    """When retrieval evidence began: the earliest receipt of any type, or None."""
    first = await session.scalar(select(func.min(Receipt.started_at)))
    return as_utc(first) if first is not None else None


def unattempted(day, window, horizon) -> str:
    """The state of a closed day no attempt touched: ``not_attempted`` only when every
    filter that could have reached it (the civil envelope included) lies after the
    horizon; otherwise nothing proves it was never read."""
    if horizon is None:
        return "unknown"
    earliest = min(
        datetime.combine(day, time.min, window.zone).astimezone(UTC),
        datetime.combine(day, time.min, UTC),
    )
    return "not_attempted" if earliest - CIVIL_BEFORE >= horizon else "unknown"


def daily_evidence(spec, day, window, attempts, times, receipts_since=None):
    physical_start = datetime.combine(day, time.min, window.zone).astimezone(UTC)
    physical_stop = datetime.combine(day + timedelta(days=1), time.min, window.zone).astimezone(UTC)
    daily = spec.kind == RecordKind.daily
    lower = datetime.combine(day, time.min, UTC) if daily else physical_start
    upper = lower + timedelta(days=1) if daily else physical_stop
    ranges, empty_ranges = [], []
    attempted, returned, ambiguous = False, 0, False
    counted_pages = set()
    reason = None
    for receipt, pages, completed, horizon in attempts:
        if receipt.queried_from is None:
            continue
        left = as_utc(receipt.queried_from)
        right = as_utc(receipt.queried_to) if receipt.queried_to else physical_stop
        if receipt.basis == "civil_start":
            # Need every possible civil start of this physical day. Merge query ranges
            # before shrinking, so adjacent requests can together cover the whole day.
            wanted_left, wanted_right = lower - CIVIL_BEFORE, upper + CIVIL_AFTER
        else:
            wanted_left, wanted_right = lower, upper
        if right <= wanted_left or left >= wanted_right:
            continue
        attempted = True
        if not completed:
            continue
        at = min(as_utc(completed.occurred_at), horizon)
        if daily:
            # Today's date filter is not proof of a closed day tomorrow.
            if at < physical_stop:
                continue
        elif receipt.basis == "civil_start":
            if at < physical_stop:
                continue
        else:
            right = min(right, at)
        ranges.append((left, right))
        unknown = any(page.unplaced_count for page in pages)
        if not unknown:
            empty_ranges.append((left, right))
        else:
            ambiguous = True
        for page in pages:
            if page.id in counted_pages:
                continue
            counted_pages.add(page.id)
            for stamp, count in times.get(page.id, ()):
                instant = parse_datetime(stamp)
                if instant is not None and lower <= instant < upper:
                    returned += count
    if not spec.time_filtered:
        return "unknown", None, "no_time_filter"
    if spec.name == "electrocardiogram":
        # The request has no upper bound. Daily *empty* proof is unsafe for malformed
        # timestamps; bounded days are still supported by a rooted last-page snapshot.
        reason = "lower_bound_filter"
    wanted_left, wanted_right = lower, upper
    if spec.kind == RecordKind.session and spec.name not in {"sleep", "electrocardiogram"}:
        wanted_left -= CIVIL_BEFORE
        wanted_right += CIVIL_AFTER
        reason = "civil_start_offset_envelope"
    if day >= window.today:
        return "incomplete" if attempted else "unknown", None, "open_day"
    if not attempted:
        return unattempted(day, window, receipts_since), None, reason
    if covers(ranges, wanted_left, wanted_right):
        if returned:
            return (
                "completed_with_records",
                None if ambiguous else returned,
                "returned_dates_unknown" if ambiguous else reason,
            )
        if not ambiguous and covers(empty_ranges, wanted_left, wanted_right):
            return "completed_empty", 0, reason
        if ambiguous:
            return "unknown", None, "returned_dates_unknown"
    return "incomplete", None, reason


async def presence(session, spec, window):
    # Guard all readers, including the existing metric adapters, before materializing rows.
    lower, upper = window.start, window.stop
    if spec.kind == RecordKind.daily:
        lower = datetime.combine(window.first, time.min, UTC)
        upper = datetime.combine(window.end + timedelta(days=1), time.min, UTC)
    # Sleep and its summaries count on the local day the sleep ended (WAKE_DATED_TYPES).
    column = Raw.source_end_time if spec.name in WAKE_DATED_TYPES else Raw.source_start_time
    condition = (Raw.data_type == spec.name, column >= lower, column < upper)
    metric = next((metric for metric in METRICS if metric.data_type == spec.name), None)
    model = NORMALIZED.get(metric.read) if metric else None
    if model is not None and metric is not None:
        if metric.read == "steps":
            # Step readers now select current raw intervals. Bound that same candidate
            # set, including corrections whose retained normalized interval moved away.
            model = Raw
            bounds = (
                Raw.data_type == spec.name,
                Raw.source_end_time > lower,
                Raw.source_start_time < upper,
            )
        elif metric.read == "sleep":
            # Match the wake-date candidates read by _google_streams, including a
            # missing-start correction and excluding a retained, obsolete wake date.
            model = Raw
            bounds = (
                Raw.data_type == spec.name,
                *sleep_wake_window(Raw.source_end_time, lower, upper),
            )
        else:
            # Weight presence reads current source timestamps, including invalid
            # corrections moved away from their preserved normalized timestamp.
            model = Raw
            bounds = condition
        count = await session.scalar(select(func.count()).select_from(model).where(*bounds))
    else:
        count = await session.scalar(select(func.count()).select_from(Raw).where(*condition))
    if (count or 0) > MAX_RECORDS:
        raise CoverageLimit("coverage_record_limit")
    if metric:
        streams, invalid = await _google_streams(session, metric, window)
        exclusions = await _exclusions(session, metric, window)
        exclusions["invalid_shape"] = invalid
    else:
        streams = {}
        exclusions = await _exclusions(
            session,
            Metric(
                spec.name, spec.name, "raw_daily" if spec.kind == RecordKind.daily else "raw_sample"
            ),
            window,
        )
        if spec.time_filtered:
            # Only the source member. JSON `->` (PostgreSQL, SQLite 3.38+) returns JSON,
            # so a boolean source keeps its own hash; scalar extraction would make it 0/1.
            source = Raw.raw_json.op("->", return_type=RawJSON)("dataSource")
            rows = await session.execute(
                select(source, column).where(*condition, raw_counted(data_types=(spec.name,)))
            )
            for data_source, stamp in rows:
                day = as_utc(stamp).date() if spec.kind == RecordKind.daily else window.local(stamp)
                if window.first <= day <= window.end:
                    stream = _stream_of(data_source, streams)
                    stream.records += 1
                    stream.days.add(day)
    values = set().union(*(stream.days for stream in streams.values()))
    regimes = [
        regime.model_dump(mode="json")
        for stream in streams.values()
        if (regime := _regime(stream, window)) is not None
    ]
    return values, regimes, exclusions


async def coverage_report(
    session,
    specs: dict[str, DataTypeSpec],
    start: date,
    end: date,
    timezone: str,
    data_type: str | None = None,
    offset=0,
    limit=4,
):
    await plan_each_query(session)
    zone = ZoneInfo(timezone)
    window = Window(start, end, datetime.now(zone).date(), zone)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    names = [data_type] if data_type else sorted(specs)
    # At most 62 type/day cells; each HTTP response stays within the card's 12 KiB budget.
    limit = min(limit, max(1, 62 // len(days)))
    selected = names[offset : offset + limit]
    types = []
    missing = unknown = incomplete = not_attempted = 0
    horizon = await receipt_horizon(session)
    for name in selected:
        spec = specs[name]
        attempts, times = await evidence(session, name, window)
        values, sources, exclusions = await presence(session, spec, window)
        entries = []
        for day in days:
            state, returned, reason = daily_evidence(spec, day, window, attempts, times, horizon)
            closed = day < window.today
            missing += closed and spec.time_filtered and day not in values
            unknown += closed and state == "unknown"
            incomplete += closed and state == "incomplete"
            not_attempted += closed and state == "not_attempted"
            entries.append(
                {
                    "date": day.isoformat(),
                    "has_value": day in values if spec.time_filtered else None,
                    "retrieval": state,
                    "returned_records": returned,
                    "reason": reason,
                    "closed": closed,
                }
            )
        # Keep only daily results before loading the next type's receipts and timestamps.
        del attempts, times
        types.append(
            {
                "data_type": name,
                "presence_basis": "metric_value"
                if any(m.data_type == name for m in METRICS)
                else "eligible_record",
                "days": entries,
                "source_regimes": sources,
                "exclusions": exclusions,
            }
        )
    report = {
        "schema_version": "1",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": timezone,
        "types": types,
        "supported_data_types": sorted(specs),
        "summary": {
            "days_without_value": missing,
            "unknown_days": unknown,
            "incomplete_days": incomplete,
            "not_attempted_days": not_attempted,
            "basis": "type_days_in_page",
        },
        "receipts_since": horizon.isoformat() if horizon else None,
        "next_offset": offset + len(selected) if offset + len(selected) < len(names) else None,
        "note": "Değer varlığı çekim kanıtından ayrıdır. Kaynaklar birleştirilmez. "
        "Fişsiz geçmiş bilinmiyor; fişler başladıktan sonra hiç dokunulmayan gün "
        "çekilmedi demektir. Bugün kapalı gün değildir. "
        "Dönen kayıtlar dışlananları da içerir; sayılar ölçüm sıklığı değildir.",
    }
    if (
        len(json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode())
        > COVERAGE_BUDGET_BYTES
    ):
        raise CoverageLimit("coverage_response_budget")
    return report
