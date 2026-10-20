# Windows-to-Linux migration runbook (legacy)

> **Legacy document.** The supported deployment is the Linux VM in [DEPLOYMENT.md](../DEPLOYMENT.md)
> and day-to-day procedures are in [RUNBOOK.md](../RUNBOOK.md). This staged cutover plan is kept for
> anyone moving an existing Windows/Docker Desktop installation; see also [WINDOWS.md](WINDOWS.md).
> Backups are now taken with `scripts/backup.sh` (encrypted) and checked with `scripts/restore.sh`.

This is a planning document, not authorization to migrate. Replace every placeholder only after the
target host is known. Preserve the existing database volume and public webhook URL throughout.

## Operating rules

- Assign one cutover owner and one rollback owner. Record timestamps and verification evidence.
- Exactly one host may run reconciliation/watchdog schedulers, and exactly one tunnel may forward the
  webhook, at any time. On the passive host set both `RECONCILIATION_ENABLED=false` and
  `WATCHDOG_ENABLED=false`.
- Every checkpoint below is a stop/go decision. If evidence fails, leave or return traffic and
  scheduling to Windows. Never improvise against the original PostgreSQL volume.
- Do not run `docker compose down -v`, volume prune, system prune, or a restore against production.

## 1. Preconditions

Freeze unrelated releases; obtain an approved maintenance window; name owners; verify access to
Windows, Linux, backups, Google configuration, and the existing Cloudflare tunnel. Confirm the
latest CI and isolated Compose smoke gates pass.

**Rollback checkpoint:** no external state has changed. Stop if any owner, credential, or gate is
missing.

## 2. Current Windows inventory

Record Docker/Compose/cloudflared versions, running service names, app loopback port, source Alembic
head, Compose project name, named volume name, backup destination, environment-file location, current
image digests, scheduler flags, and tunnel service status. Record secret names, never values. Do not
inspect dump contents or service command lines that may embed tokens.

**Rollback checkpoint:** compare the inventory with your private migration readiness record; resolve drift
before continuing.

## 3. Required Linux software

Install a supported Docker Engine and Compose plugin, Git or an approved artifact-transfer tool,
the pinned cloudflared release, checksum/encryption tools, and sufficient monitoring. Do not assume a
distribution or package manager in this runbook.

**Rollback checkpoint:** installation affects only the unused target; rebuild it if validation fails.

## 4. CPU architecture compatibility

**Linux command:** run `uname -m`, then verify every pinned manifest lists the corresponding
Linux architecture with `docker buildx imagetools inspect <reference>`. Obtain the matching
cloudflared binary and checksum.

**Rollback checkpoint:** stop before transferring data if any image or binary lacks support.

## 5. Ports and firewall

Allow outbound HTTPS/DNS required by Docker, Google, and Cloudflare. Do not allow inbound PostgreSQL
5432. Bind the administrative API to `127.0.0.1` only. No inbound public web port is required for a
host-level outbound tunnel; allow SSH/management only from the approved management network.

**Rollback checkpoint:** independently review firewall rules before enabling Docker services.

## 6. Docker and Compose

Install/start Docker using the selected distribution's official procedure. Copy the repository at
the tested revision. Validate `docker compose version` and the secret-safe Compose model. Do not start
the application yet and do not guess storage paths.

**Rollback checkpoint:** remove/rebuild only the new unused host if its Engine configuration is wrong.

## 7. Secure environment transfer

Provision a protected deployment directory and environment file with owner-only permissions. Transfer
the real file through an encrypted authenticated channel; never via Git, chat, shell history, or CI.
Run `python scripts/verify_configuration.py --env-file <protected-path>`; it prints names/errors only.

**Rollback checkpoint:** securely delete only the rejected target copy if validation fails; Windows
remains authoritative.

## 8. PostgreSQL backup creation

Create a fresh custom-format dump on Windows using the current backup profile. This is non-destructive
but may consume I/O. Record timestamp, byte size, and SHA-256 without opening the dump.

**Rollback checkpoint:** if backup creation affects service health, stop it and retain the previous
verified generation.

## 9. Backup integrity verification

Restore the fresh dump into an isolated temporary project/database, run Alembic current/check and
application integrity/status checks, then remove only the explicitly named temporary project and its
temporary volume. Never attach or modify `googlefitbitair_postgres_data`.

**Rollback checkpoint:** a failed restore blocks migration; keep Windows active and create a new dump.

## 10. Encrypted backup transfer

Encrypt the already verified dump with an approved authenticated-encryption mechanism, transfer it,
verify the encrypted transport hash, decrypt on protected target storage, and verify the original
SHA-256. Limit file permissions. Do not inspect dump contents.

**Rollback checkpoint:** delete only corrupt target copies and repeat; the source dump stays intact.

## 11. Restore into a new PostgreSQL volume

Create a uniquely named new target Compose project/volume. Start only target PostgreSQL. Restore into
its empty `health` database using the matching PostgreSQL 17 client. A restore with `--clean` is
destructive to its target: confirm container, project, database, and volume names twice.

**Rollback checkpoint:** before restore, record the empty target identifiers. On failure, discard only
that new target volume after explicit confirmation and retry from the verified dump.

## 12. Alembic verification

Run `alembic current`, `alembic heads`, and `alembic check` against the restored target. Upgrade to the
tested source head only if the backup predates it and the release procedure explicitly approves the
migration.

**Rollback checkpoint:** migration failure means discard/recreate only the target database from the
dump; do not downgrade or alter Windows.

## 13. Start privately, without public traffic

Keep target cloudflared disabled. Set both scheduler flags false. Start one target app container and
confirm it publishes only to target loopback. Do not start Caddy's public profile.

**Rollback checkpoint:** stop the target app if its bindings, health, or hardening differ; Windows is
still the active scheduler and ingress.

## 14. Local health and API verification

From the Linux host, test `/health`, CLI status, database connectivity, read-only status endpoints,
container UID, read-only root filesystem, dropped capabilities, and absence of a PostgreSQL host bind.
Do not trigger sync/backfill or write synthetic production records.

**Rollback checkpoint:** any failure blocks cutover and the target app is stopped.

## 15. Google OAuth token verification

Confirm the encryption key is present and the stored refresh token can make the project's existing
read-only credential/sample readiness call. Avoid a broad sync and do not log tokens.

**Rollback checkpoint:** leave Windows active; correct secret transfer or OAuth configuration without
rotating credentials unless separately approved.

## 16. Scheduler single-owner verification

Before cutover, Windows alone remains scheduler-enabled. Confirm the target reports schedulers
disabled. Immediately before enabling them on Linux, stop the Windows app (not PostgreSQL), confirm it
is stopped, then enable exactly one target app process with the intended scheduler flags.

**Rollback checkpoint:** if target scheduler startup fails, disable/stop the target app before
restarting the Windows app. Never overlap scheduler-enabled processes.

## 17. cloudflared installation/configuration

Install the pinned architecture-specific binary and checksum-verify it. Securely transfer the existing
tunnel credential and render the deny-by-default config from `deploy/cloudflare/config.example.yml`.
Validate ingress offline. Keep the Linux service disabled/stopped.

**Rollback checkpoint:** remove only the target credential copy if validation fails; the Windows
service remains unchanged.

## 18. Public webhook cutover, same URL

After scheduler ownership is on Linux and the target app is healthy, stop the Windows cloudflared
service and confirm it stopped. Start the Linux cloudflared service using the same tunnel and hostname.
Do not create another tunnel or alter DNS/Google endpoint registration.

**Rollback checkpoint:** this is externally visible. If Linux fails to connect promptly, stop Linux
cloudflared first, then restart Windows cloudflared and, if needed, reverse scheduler ownership using
the order in step 16.

## 19. End-to-end signed webhook verification

Use the existing Google verification mechanism or wait for a genuine signed event. Confirm only the
webhook path is reachable, signature/authorization checks succeed, and a non-webhook path returns 404.
Do not print headers or send synthetic health records.

**Rollback checkpoint:** on failure, stop Linux cloudflared and restore Windows tunnel ownership.

## 20. Freshness and missing-data verification

Compare source/target pre-cutover checkpoints, latest timestamps by enabled data type, webhook batch
status, failed/pending windows, pipeline health, and expected reconciliation timing. Prefer metadata
counts and hashes; do not export health payloads.

**Rollback checkpoint:** unexplained gaps trigger tunnel rollback and controlled scheduler rollback;
retain both databases for analysis.

## 21. Rollback to Windows

Stop Linux cloudflared, confirm stopped, disable/stop the Linux scheduler-enabled app, then restart the
Windows app/scheduler and finally Windows cloudflared. Verify health and freshness. Do not copy a
partially active target database back over the original volume; reconcile later through a separately
designed data procedure if the target accepted unique events.

## 22. Post-migration monitoring

Monitor service restarts, disk capacity, database health, scheduler state, webhook receipts, failed
jobs, API freshness, backups, and cloudflared logs closely through the agreed observation window.
Create and isolated-restore a new Linux backup before considering retirement.

**Rollback checkpoint:** keep Windows intact, stopped/passive, and ready through the whole observation
window.

## 23. Retiring Windows

Retire only after the observation window, multiple verified target backups, successful restore test,
OAuth and signed-webhook proof, no unexplained data gap, and formal rollback-owner approval. Archive
the protected Windows environment/tunnel material according to policy. Deleting the old volume or
credentials is a separate destructive task requiring explicit confirmation; it is not part of this
runbook.
