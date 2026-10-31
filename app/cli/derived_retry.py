"""Explicit, bounded maintenance for failed derived days; no automatic retry policy."""

import argparse
import asyncio
from datetime import date

from app.services.derived import RetryReport, check_retry_range, retry_failed_range


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Tarih YYYY-MM-DD biçiminde olmalı.") from exc


async def _run(first: date, last: date, timezone: str, dry_run: bool) -> RetryReport:
    from app.db.session import SessionLocal, engine

    try:
        return await retry_failed_range(SessionLocal, first, last, timezone, dry_run=dry_run)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Başarısız türetilmiş günleri yeniden dene.")
    parser.add_argument("first", type=_date, help="Başlangıç tarihi (dahil, YYYY-MM-DD)")
    parser.add_argument("last", type=_date, help="Bitiş tarihi (dahil, en çok 366 gün)")
    parser.add_argument("timezone", help="IANA saat dilimi")
    parser.add_argument("--dry-run", action="store_true", help="Yalnız adayları say; yazma.")
    args = parser.parse_args(argv)
    try:
        check_retry_range(args.first, args.last, args.timezone)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        report = asyncio.run(_run(args.first, args.last, args.timezone, args.dry_run))
    except Exception as exc:
        # Database exceptions may contain SQL parameters or source values.
        print(f"Bakım tamamlanamadı: {type(exc).__name__}")
        return 1
    prefix = "Salt okunur deneme; " if args.dry_run else ""
    print(
        f"{prefix}aday: {report['candidates']}, işlenen: {report['processed']}, "
        f"başarılı: {report['succeeded']}, başarısız: {report['failed']}, "
        f"kaynağı değişen: {report['stale']}, atlanan: {report['skipped']}"
    )
    return int(report["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
