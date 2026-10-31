"""Bounded maintenance recovers errors without modifying the source archive."""

import asyncio
import os
import subprocess
import sys
from datetime import timedelta

import pytest
from derived_records import DAY, cache, fail_derived_range, manual, read, snapshot, steps, store
from sqlalchemy import select, update
from sqlalchemy.exc import DataError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cli import derived_retry
from app.db.base import Base
from app.models.derived import DerivedDay
from app.services import derived


async def derived_snapshot(sessions):
    async with sessions() as session:
        return (await session.execute(select(DerivedDay.__table__))).all()


async def test_real_module_recovers_once_and_preserves_all_source_tables(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'synthetic.db'}"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await store(sessions, "steps", steps(count=100))
        await store(sessions, "steps", steps(count=120))  # archive history too
        await manual(sessions, 80)
        await fail_derived_range(sessions, monkeypatch)
        failed = await cache(sessions)
        async with sessions() as session:
            before = await snapshot(session)
        env = os.environ | {"DATABASE_URL": url}
        command = [sys.executable, "-m", "app.cli.derived_retry", str(DAY), str(DAY), "UTC"]
        cached = await derived_snapshot(sessions)
        preview = await asyncio.to_thread(
            subprocess.run,
            command + ["--dry-run"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert preview.returncode == 0 and "aday: 1, işlenen: 0" in preview.stdout
        assert await derived_snapshot(sessions) == cached
        for expected in ("işlenen: 1, başarılı: 1", "işlenen: 0, başarılı: 0"):
            result = await asyncio.to_thread(
                subprocess.run, command, env=env, capture_output=True, text=True, timeout=30
            )
            assert result.returncode == 0, result.stderr
            assert expected in result.stdout
            assert "998877" not in result.stdout + result.stderr
        current = await cache(sessions)
        assert current.build_count == failed.build_count + 1
        assert current.revision == current.built_revision == failed.revision
        assert (await read(sessions))["days"][0]["state"] == "current"
        async with sessions() as session:
            assert await snapshot(session) == before
    finally:
        await engine.dispose()


async def test_selection_and_dry_run_leave_other_days_zones_methods_untouched(
    sessions, monkeypatch
):
    first, last = DAY - timedelta(days=1), DAY + timedelta(days=2)
    await fail_derived_range(sessions, monkeypatch, first, last)
    await fail_derived_range(sessions, monkeypatch, timezone="Europe/Paris")
    async with sessions() as session, session.begin():
        await session.execute(
            update(DerivedDay).where(DerivedDay.day == last).values(method="old/0")
        )
    await derived.build_day(sessions, DAY + timedelta(days=1), "UTC")  # healthy day
    before = await derived_snapshot(sessions)
    report = await derived.retry_failed_range(sessions, DAY, last, "UTC", dry_run=True)
    # A dry run skips only the days that are not candidates.
    assert report == dict(candidates=1, processed=0, succeeded=0, failed=0, stale=0, skipped=2)
    assert await derived_snapshot(sessions) == before
    report = await derived.retry_failed_range(sessions, DAY, last, "UTC")
    assert report == dict(candidates=1, processed=1, succeeded=1, failed=0, stale=0, skipped=2)
    after = await derived_snapshot(sessions)
    assert [row for row in before if row.day != DAY or row.timezone != "UTC"] == [
        row for row in after if row.day != DAY or row.timezone != "UTC"
    ]
    assert await derived.retry_failed_range(sessions, last, last, "UTC") == dict(
        candidates=0, processed=0, succeeded=0, failed=0, stale=0, skipped=1
    )
    assert await cache(sessions, last) is None  # old method never creates a current-method day


@pytest.mark.parametrize("days,zone", [(0, "UTC"), (367, "UTC"), (1, "Bad/Zone"), (1, "../UTC")])
async def test_invalid_request_opens_no_session(days, zone):
    def forbidden():
        pytest.fail("invalid request touched database")

    with pytest.raises(ValueError):
        await derived.retry_failed_range(forbidden, DAY, DAY + timedelta(days=days - 1), zone)


async def test_maximum_window_only_reads_registered_rows(sessions):
    report = await derived.retry_failed_range(sessions, DAY, DAY + timedelta(days=365), "UTC")
    assert report == dict(candidates=0, processed=0, succeeded=0, failed=0, stale=0, skipped=366)
    assert await derived_snapshot(sessions) == []


@pytest.mark.parametrize("error", [ValueError, DataError, OperationalError])
async def test_repeated_failure_is_isolated_and_safe(sessions, monkeypatch, caplog, error):
    first = DAY - timedelta(days=1)
    await fail_derived_range(sessions, monkeypatch, first, DAY)
    original = derived.canonical_day

    async def fail_first(session, day, timezone):
        if day == first:
            if error is ValueError:
                raise ValueError("998877")
            raise error("998877", {"health": 998877}, Exception("998877"))
        return await original(session, day, timezone)

    monkeypatch.setattr(derived, "canonical_day", fail_first)
    report = await derived.retry_failed_range(sessions, first, DAY, "UTC")
    assert report == dict(candidates=2, processed=2, succeeded=1, failed=1, stale=0, skipped=0)
    assert derived.build_failed((await cache(sessions, first)).result)
    assert (await read(sessions))["days"][0]["state"] == "current"
    assert await derived.build_range(sessions, first, DAY, "UTC") == 0
    assert await derived.DerivedRefresh(sessions).run() == 0
    assert "998877" not in caplog.text
    assert "998877" not in repr(report)


async def test_cancel_rolls_back_active_day_and_keeps_previous_commits(sessions, monkeypatch):
    first = DAY - timedelta(days=1)
    await fail_derived_range(sessions, monkeypatch, first, DAY)
    before = await cache(sessions)
    original = derived.rebuild_day

    async def interrupt_after_update(session, day, timezone, **kwargs):
        result = await original(session, day, timezone, **kwargs)
        if day == DAY:
            raise asyncio.CancelledError
        return result

    with monkeypatch.context() as patch:
        patch.setattr(derived, "rebuild_day", interrupt_after_update)
        with pytest.raises(asyncio.CancelledError):
            await derived.retry_failed_range(sessions, first, DAY, "UTC")
    assert not derived.build_failed((await cache(sessions, first)).result)
    after = await cache(sessions)
    assert (after.result, after.build_count, after.computed_at) == (
        before.result,
        before.build_count,
        before.computed_at,
    )
    assert (await derived.retry_failed_range(sessions, first, DAY, "UTC"))["processed"] == 1


async def test_source_revision_changed_during_retry_stays_pending(sessions, monkeypatch):
    await store(sessions, "steps", steps())
    await fail_derived_range(sessions, monkeypatch)
    original = derived.canonical_day

    async def mutate(session, day, timezone):
        from app.google_health.data_types import REGISTRY
        from app.services.storage import store_records

        result = await original(session, day, timezone)
        await store_records(session, REGISTRY["steps"], [steps(count=999)])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(derived, "canonical_day", mutate)
        report = await derived.retry_failed_range(sessions, DAY, DAY, "UTC")
    # The refresh queue rebuilds the changed day; it is neither a success nor a failure.
    assert report == dict(candidates=1, processed=1, succeeded=0, failed=0, stale=1, skipped=0)
    row = await cache(sessions)
    assert row.revision > row.built_revision
    assert await derived.DerivedRefresh(sessions).run() == 1


@pytest.mark.parametrize(
    "dry_run,failed,stale,exit_code",
    [(False, 0, 0, 0), (False, 1, 0, 1), (False, 0, 1, 0), (True, 0, 0, 0)],
)
def test_cli_report_and_exit_code(monkeypatch, capsys, dry_run, failed, stale, exit_code):
    async def run(first, last, timezone, preview):
        assert (first, last, timezone, preview) == (DAY, DAY, "UTC", dry_run)
        return dict(
            candidates=1,
            processed=int(not dry_run),
            succeeded=0,
            failed=failed,
            stale=stale,
            skipped=0,
        )

    monkeypatch.setattr(derived_retry, "_run", run)
    args = [str(DAY), str(DAY), "UTC"] + (["--dry-run"] if dry_run else [])
    assert derived_retry.main(args) == exit_code
    output = capsys.readouterr().out
    assert f"başarısız: {failed}, kaynağı değişen: {stale}" in output


@pytest.mark.parametrize("args", [[], ["bad", str(DAY), "UTC"], [str(DAY), str(DAY), "bad"]])
def test_cli_rejects_invalid_input_before_running(monkeypatch, args):
    def forbidden(*args):
        pytest.fail("invalid CLI request ran maintenance")

    monkeypatch.setattr(derived_retry, "_run", forbidden)
    with pytest.raises(SystemExit) as exc:
        derived_retry.main(args)
    assert exc.value.code == 2


def test_cli_does_not_print_database_exception_values(monkeypatch, capsys):
    async def fail(*args):
        raise DataError("998877", {}, Exception("998877"))

    monkeypatch.setattr(derived_retry, "_run", fail)
    assert derived_retry.main([str(DAY), str(DAY), "UTC"]) == 1
    output = capsys.readouterr()
    assert "DataError" in output.out and "998877" not in output.out + output.err
