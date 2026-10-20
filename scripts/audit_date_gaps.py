"""Audit missing civil days and optionally re-fetch only those source windows.

Run inside the configured application container; output contains counts and dates only.
"""

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.google_health.pagination import iter_pages
from app.services.runtime import build_runtime
from app.services.storage import StoreStats, store_records

AUDIT_SQL = """
WITH daily AS (
 SELECT data_type,
 (CASE WHEN data_type='sleep' THEN source_end_time ELSE source_start_time END
 AT TIME ZONE CASE WHEN data_type LIKE 'daily-%' THEN 'UTC' ELSE :zone END)::date AS day
 FROM raw_health_data
 WHERE NOT is_quarantined AND source_deleted_at IS NULL AND source_start_time IS NOT NULL
 AND data_type IN ('heart-rate','steps','sleep','daily-resting-heart-rate',
 'daily-heart-rate-variability','daily-oxygen-saturation','daily-respiratory-rate',
 'daily-sleep-temperature-derivations','activity-level','distance',
 'active-energy-burned','total-calories','heart-rate-variability','oxygen-saturation',
 'active-minutes','sedentary-period','time-in-heart-rate-zone','calories-in-heart-rate-zone')
 GROUP BY 1,2
), bounds AS (SELECT data_type,min(day) AS first FROM daily GROUP BY 1),
missing AS (
 SELECT b.data_type,d::date AS day FROM bounds b
 CROSS JOIN LATERAL generate_series(b.first,CAST(:end AS date),interval '1 day') d
 LEFT JOIN daily a ON a.data_type=b.data_type AND a.day=d::date WHERE a.day IS NULL
), islands AS (
 SELECT *,day-(row_number() OVER (PARTITION BY data_type ORDER BY day))::int AS grp
 FROM missing
)
SELECT data_type,min(day) AS missing_from,max(day) AS missing_to,count(*) AS days
FROM islands GROUP BY data_type,grp ORDER BY data_type,missing_from
"""


async def audit(zone, end):
    async with SessionLocal() as session:
        result = await session.execute(text(AUDIT_SQL), {"zone": zone, "end": end})
        return [dict(row) for row in result.mappings()]


async def main(args):
    end = datetime.now(ZoneInfo(args.timezone)).date() - timedelta(days=1)
    before = await audit(args.timezone, end)
    print(
        json.dumps(
            {"event": "before", "timezone": args.timezone, "through": end, "gaps": before},
            default=str,
        ),
        flush=True,
    )
    if not args.repair:
        return
    runtime = build_runtime(get_settings(), SessionLocal)
    totals = StoreStats()
    failures = []
    try:
        for gap in before:
            spec = runtime.incremental.specs[gap["data_type"]]
            zone = ZoneInfo("UTC" if spec.name.startswith("daily-") else args.timezone)
            # Include adjacent days to capture sessions and intervals near midnight.
            start = datetime.combine(gap["missing_from"] - timedelta(days=1), time.min, zone)
            stop = datetime.combine(gap["missing_to"] + timedelta(days=2), time.min, zone)
            start, stop = start.astimezone(UTC), stop.astimezone(UTC)
            stats = StoreStats()
            try:
                cursor = start
                while cursor < stop:
                    window_end = min(cursor + timedelta(days=spec.max_window_days or 7), stop)
                    async for page in iter_pages(runtime.google, spec, cursor, window_end):
                        async with SessionLocal.begin() as session:
                            stats.add(
                                await store_records(
                                    session, spec, page.data_points, source="gap-repair"
                                )
                            )
                    cursor = window_end
                totals.add(stats)
                print(
                    json.dumps(
                        {"event": "repaired_window", **gap, "stats": asdict(stats)}, default=str
                    ),
                    flush=True,
                )
            except Exception as exc:
                failures.append({**gap, "error_type": type(exc).__name__})
                print(
                    json.dumps({"event": "failed_window", **failures[-1]}, default=str), flush=True
                )
        after = await audit(args.timezone, end)
        print(
            json.dumps(
                {
                    "event": "after",
                    "through": end,
                    "gaps": after,
                    "totals": asdict(totals),
                    "failures": failures,
                },
                default=str,
            ),
            flush=True,
        )
    finally:
        await runtime.shutdown()
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--timezone", default="UTC")
    asyncio.run(main(parser.parse_args()))
