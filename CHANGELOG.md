# Changelog

All notable changes to HealtOS are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases before 0.2.1 were not recorded
in this file.

## 0.2.1 — 2026-10-31

Covers everything merged since 0.2 (`4828135`), mainly Gitea #31 and #32.

### Upgrade notes

- Database migrations `20261024_0013` to `20261031_0021` run automatically when the app starts
  (`alembic upgrade head`). They are additive; no record is deleted or rewritten. Per-revision
  notes are in [docs/RUNBOOK.md](docs/RUNBOOK.md).
- `20261031_0021` needs a short `ACCESS EXCLUSIVE` lock on the two webhook tables and cannot run
  while a `pg_dump` is in progress. Update outside the nightly backup window (around 03:30); if
  they overlap, the migration stops after about 30 seconds without changing anything and succeeds
  on the next start ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).
- Take a backup before updating, as always. Downgrades refuse when they would lose evidence
  written by the new revisions; roll back with a restore instead.
- Existing deployments: create the backup status directory (`install -d -m 755 backup-status`,
  or the `BACKUP_STATUS_HOST_DIR` you use) before `docker compose up`, so the backup account owns
  it. The backup script now needs Python 3.11+ on the host (standard library only).
- The PostgreSQL container now runs with a read-only root filesystem and tmpfs mounts for
  `/tmp` and `/var/run/postgresql`; data stays in the `postgres_data` volume.

### Security

- A signed webhook body that PostgreSQL `jsonb` cannot hold (`\u0000`, unpaired surrogates,
  `NaN`/`Infinity`, numbers that overflow) is no longer lost: the ingest used to roll back and
  the signed bytes were never stored. The body is now kept byte for byte in `raw_body` with
  `raw_json` left NULL, the affected item is rejected, and clean items in the same delivery are
  still processed. Existing rows are untouched (migration `20261031_0021`).
- Unauthorized webhook requests no longer use up the budget of authorized Google deliveries:
  authorized and unauthorized requests have separate rate budgets, a missing key set answers 503
  instead of 401, and pre-authorization rejections write at most one diagnostic row per reason per
  minute.
- Webhook and manual-entry endpoints enforce body size and rate limits
  (`MANUAL_WRITE_MAX_BODY_BYTES`, `MANUAL_WRITE_REQUESTS_PER_MINUTE`,
  `WEBHOOK_REQUESTS_PER_MINUTE`).
- OAuth token handling is stricter: redirects, HTTP 206, error envelopes, duplicate JSON keys,
  non-JSON numeric constants, invalid refresh-token types and access tokens that cannot travel in
  an HTTP header are refused instead of being cached or activated; token responses are read with
  memory limits.

### Added

- Umay interpretation layer: tool-based interpretation, a per-turn view of the health data sent
  to Umay, a source-separated derived daily data foundation with a 64 KiB budget per daily card,
  and a coverage action for `health_archive`.
- Umay persona v7: voice-focused persona, no scripted opening lines, reasoning effort `high`, and
  the real local date and time in every turn.
- Coverage report built on append-only retrieval receipts, with an API (`GET /api/sync/coverage`)
  and a panel under System › Coverage (migration `20261025_0015`).
- Durable reconciliation run reports, linked to the watchdog (`GET /api/sync/reconciliation`,
  migration `20261025_0016`).
- Webhook diagnostics in the service, API and panel, kept immutable in the database (migrations
  `20261025_0017`, `20261025_0019`).
- Test-entry flags on manual measurements as append-only events; a flagged entry is left out of
  the profile, the 3D body and Umay's daily data (migration `20261024_0014`).
- Bounded retry for failed derived days (`python -m app.cli.derived_retry`).
- Backup status exposed through the API (`GET /health/backup`) and a completed restore
  comparison; a restored copy without tables now fails the drill.
- Monthly timeline in the sports view; keyboard and accessibility behavior of the single-screen
  interface; an enforced first-load budget, with the body viewer loaded when it becomes visible.
- Linux service lifecycle for the Umay bridge (`deploy/systemd/healthos-bridge.service`).

### Changed

- Every analytics reader counts the same set of records (migration `20261024_0013`).
- Versioned local daily read models with transactional invalidation (migrations
  `20261025_0018`, `20261025_0020`).
- Large modules were split into smaller ones with unchanged behavior: identity resolution and
  backfill, source chains, raw history, identity models, the CLI, the body view and the
  stylesheet.
- Sleep totals: invalid sessions no longer count as a measured zero, restless time is subtracted
  and assumptions are shown.

### Fixed

- Google sync no longer reports a false completion or hangs on malformed responses: error
  objects, wrong or non-object collection items, duplicate JSON keys, non-JSON numeric constants,
  HTTP 206 and redirects, malformed page tokens, endless pagination, stalled bodies and
  `Retry-After` headers, truncated compressed bodies and trailing bytes after a deflate stream.
  Bytes already received are kept in the archive when a request fails or is cancelled.
- Memory use is bounded while reading and decompressing Google and OAuth responses.
- A malformed field (dates, numbers, heart-rate or text metadata, exercise type) no longer rolls
  back a whole retrieval page.
- Invalid step, sleep, heart-rate, weight and exercise corrections no longer bring back the old
  value.
- Backfill jobs and same-type syncs cannot run twice across processes; the watchdog recovery lock
  no longer blocks the database pool; live webhook work is not reclaimed during recovery.
- Webhook date boundaries no longer cause an endless retry loop, and broken webhook metadata no
  longer rolls back valid notifications.
- Numeric overflows in dashboard calculations and Umay's derived data are handled; dashboard date
  windows that overflow answer 422.
- Quadratic scans in Umay's calculations and sleep summaries were removed and their output is
  bounded.
- Backups made in the same second or concurrently no longer overwrite each other.
- The grain texture and the background chosen under System › Appearance show on the home page
  again.
