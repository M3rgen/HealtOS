# Runbook

Routine operations for the deployment in [DEPLOYMENT.md](DEPLOYMENT.md). Commands run on the VM in
`/opt/healthos` unless stated otherwise. API calls need the UI token:

```sh
TOKEN="$(sed -n 's/^APP_UI_TOKEN=//p' .env)"
api() { curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000$1"; }
```

Before anything that rebuilds or migrates: take a backup (`make backup`) and note the current
Alembic revision (`docker compose exec app alembic current`).

## Update the application

```sh
make backup
git pull --ff-only
docker compose up -d --build        # rebuilds the image; the app migrates on start
docker compose ps
api /health
```

Recreating the app interrupts the webhook for a few seconds; Google retries failed deliveries.

## Apply migrations

Migrations live in `migrations/versions/` and run automatically: the container command is
`alembic upgrade head && uvicorn ...`, so every app start applies pending revisions. To see or run
them by hand:

```sh
docker compose exec app alembic current
docker compose exec app alembic heads
docker compose exec app alembic upgrade head
```

If a migration fails the app does not start; `docker compose logs app` shows the Alembic error.
Migrations are additive (no destructive `DROP` outside `downgrade()`); some downgrades cannot fully
undo PostgreSQL enum additions, so roll back with a restore rather than `alembic downgrade` on the
live archive.

### `20261020_0010` (raw_json json → jsonb)

This one rewrites `raw_health_data`, `webhook_events` and `webhook_notifications` (and rebuilds
their indexes) under an exclusive lock. It runs before the app serves, so the app is down until it
finishes: expect seconds to a few minutes for a few hundred thousand raw rows, during which
webhooks fail and Google retries them. The rewrite needs free disk of roughly the size of those
tables. It first reads every row both ways and aborts, converting nothing, if any payload would
read back differently as jsonb (`-0.0`, a float written as `1e+16`, a `\u0000` escape).

```sh
docker compose exec app alembic current          # expect 20261020_0009
docker compose exec postgres psql -U health -d health \
  -c "SELECT pg_size_pretty(pg_total_relation_size('raw_health_data'))"
df -h /var/lib/docker
make backup                                       # mandatory: the only exact rollback
git pull --ff-only
docker compose up -d --build
docker compose logs -f app                        # wait for "Running upgrade ... 20261020_0010"
docker compose exec app alembic current          # expect 20261020_0010 (head)
docker compose exec app health-mirror verify     # payload hashes still match
api /health
```

If the log shows `raw_json -> jsonb would change ...` or `unsupported Unicode escape sequence`,
nothing was converted and the database is still on `20261020_0009`; check out the previous commit,
rebuild, and report the listed row keys. `alembic downgrade 20261020_0009` converts back to `json`
with every value kept, but the stored text is jsonb's spelling, not the original; restore the
backup if the original text is needed.

## Update images and cloudflared

Base images are pinned by tag and digest; follow [IMAGE_UPDATES.md](IMAGE_UPDATES.md) and change one
component at a time. Never change the PostgreSQL major version by editing the tag.

`cloudflared` runs with `--no-autoupdate`. To update: download the new release for the VM's
architecture, verify its checksum, replace `/usr/local/bin/cloudflared`, then
`sudo systemctl restart cloudflared` and repeat the public-surface checks in
[CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md#verify-the-public-surface).

## Rotate secrets

| Secret | Supported? | Procedure |
| --- | --- | --- |
| `APP_UI_TOKEN` | Yes | Put a new value in `.env`, then `docker compose up -d --force-recreate app`. The restart ends every login session (sessions are kept in memory); log in again. |
| `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` | Yes, with a gap | The app accepts exactly one value; there is no overlap period. Update the subscriber's authorization with Google and `.env` back to back, then recreate the app. Deliveries in between get 401; Google retries them and reconciliation covers anything lost. Run `health-mirror webhook-readiness` afterwards. |
| `GOOGLE_TOKEN_ENCRYPTION_KEY` | Only by re-linking | There is a single Fernet key and no re-encryption command. Take a backup, set the new key, recreate the app, then open `/auth/google/start` through the SSH tunnel and consent again; the callback stores the refresh token encrypted with the new key. Until then syncs fail with `Stored OAuth token cannot be decrypted`. Older backups still need the old key; backups are kept forever, so keep the old key forever as well. |
| `CODEX_BRIDGE_TOKEN` | Yes | Stop the bridge (below), delete `~/.codex/healthos-bridge/bridge.token`, start the launcher again (it generates a token and updates `.env`), then recreate the app. |
| `POSTGRES_PASSWORD` | Manual | The image only reads it when the data directory is first created. Change the role first, `docker compose exec postgres psql -U health -d health -c "ALTER ROLE health PASSWORD '<new>'"`, then update `.env` and run `docker compose up -d --force-recreate app`. |
| Backup key | Yes | Point `BACKUP_AGE_RECIPIENT` (or `/etc/healthos/backup.key`) at the new key. New backups use it. Backups are kept forever, so keep the old identity (in the password manager) for as long as any backup made with it exists. |

## Backup, verify and restore drill

```sh
make backup                          # prints backups/health-<stamp>.dump.age
ls -l backups/
cat backups/last-backup-status.json  # "result": "success", recent "timestamp", "free_bytes"
systemctl list-timers healthos-backup.timer
journalctl -u healthos-backup.service -n 20
journalctl -p crit -t healthos-backup   # failures reported by healthos-backup-failure.service
```

Check the status file weekly: `result` must be `success` and `timestamp` from the last day. A
`failure` carries the reason in `message`; a stale timestamp means the timer is not running.

### Disk space for backups

Backups are kept forever and nothing deletes them automatically. When free space falls below the
guard (twice the newest dump, at least 1 GiB; or `BACKUP_MIN_FREE_MB`), the backup refuses to start
and the unit fails; it never deletes old backups to make room. Fix it by adding space:

- **Grow the disk.** Enlarge the VM disk in the hypervisor, then grow the partition and filesystem
  (e.g. `sudo growpart /dev/sda 1 && sudo resize2fs /dev/sda1`; check names with `lsblk`).
- **Add a disk for backups.** Format and mount a new disk (e.g. at `/srv/healthos-backups`, listed
  in `/etc/fstab`), copy the existing backups there (`sudo rsync -a backups/ /srv/healthos-backups/`),
  set `BACKUP_HOST_DIR=/srv/healthos-backups` in `.env`, and run `make backup` once.
- **Move old backups to other storage, by hand.** They are encrypted, so any storage will do. Copy,
  verify, and only then remove the local copy:

  ```sh
  rsync -a --checksum backups/health-2026*.dump.age /mnt/archive/healthos/
  (cd backups && sha256sum health-2026*.dump.age) > /tmp/healthos-moved.sha256
  (cd /mnt/archive/healthos && sha256sum -c /tmp/healthos-moved.sha256)   # all OK before deleting
  ```

  Keep at least the newest backups on the host, and keep the backup key with the archive.

Restore drill, monthly and after every change to backups or keys. It never touches the live
`health` database: it drops and recreates a scratch database, restores into it, and prints row
counts next to the live counts plus the restored Alembic revision.

```sh
BACKUP_AGE_IDENTITY=/path/to/healthos-backup.age \
  make restore FILE=backups/health-<stamp>.dump.age          # default target: health_restore
docker compose exec -T postgres dropdb -U health health_restore
docker compose exec app health-mirror verify                 # live archive hash check
```

If the age identity lives only off the VM (recommended), copy it in for the drill and remove it
afterwards. Replacing the live database is a separate, destructive procedure; see the
[backup reference](REFERENCE.md#database-backup-and-restore).

## Restart the Umay bridge

The bridge is a host process, not a container. Its state is in `~/.codex/healthos-bridge/` (or under
`CODEX_HOME`): `bridge.pid`, `bridge.token`, `bridge.log`.

```sh
kill "$(cat ~/.codex/healthos-bridge/bridge.pid)"
uv run --frozen --no-dev python scripts/start_codex_bridge.py
docker compose up -d --force-recreate app      # only if the token or URL in .env changed
ss -ltn | grep 8766                            # must show the docker0 address or 127.0.0.1
```

A bridge started by an older launcher may still listen on all interfaces (`0.0.0.0`, port 8766). The current launcher
reuses any healthy bridge, so it will not replace that one by itself (it prints a warning when the
bridge also answers on loopback). Stop it through `bridge.pid` as above and launch again. After a
reboot the bridge is not restarted automatically; run the launcher again.

## The webhook went quiet

Symptoms: the dashboard's newest records lag by hours, or `/health` reports `stale`/`degraded`.

1. **App state.**

   ```sh
   api /health | python3 -m json.tool
   docker compose exec -T app health-mirror webhook-status
   ```

   Look at `last_webhook_received`, `last_webhook_processed`, `pending_notifications`,
   `failed_notifications`, `stale_streams` and `reasons`. `stale` means every frequent stream
   (heart rate, steps, activity, energy) is older than `PIPELINE_STALE_THRESHOLD_MINUTES`.
   Received but not processed points at the app or Google API; not received points at the path
   from Google.
2. **Logs.**

   ```sh
   docker compose logs --since 24h app | grep -E 'WEBHOOK_|WATCHDOG_|RECONCILIATION_|TOKEN_REFRESHED|RATE_LIMITED'
   ```

   `WEBHOOK_SIGNATURE_INVALID` with `reason=authorization` means the subscriber's secret and `.env`
   differ. `WEBHOOK_REPLAY_IGNORED` means a signed body older than 24 hours arrived again and was
   deliberately not reprocessed.
3. **Tunnel.** `systemctl status cloudflared`, `journalctl -u cloudflared --since '6 hours ago'`,
   then the external checks in [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md#verify-the-public-surface):
   an unauthenticated POST must return 401.
4. **Google side.** Check that the subscriber still exists and points at the same URI. Testing-mode
   OAuth refresh tokens expire after seven days, which stops both webhook follow-up reads and syncs.
5. **Recover.** Retry stored but unprocessed notifications, then fill the gap from Google:

   ```sh
   docker compose exec -T app health-mirror webhook-replay-pending
   docker compose exec -T app health-mirror reconcile --days 7
   ```

   The watchdog (every `WATCHDOG_INTERVAL_SECONDS`, default 15 minutes) also starts one bounded
   reconciliation by itself when all frequent streams are stale, at most once per
   `RECOVERY_COOLDOWN_MINUTES`. Reconciliation runs daily (7 days back) and weekly (90 days back)
   regardless.

## Abandoned backfill windows

Backfill downloads history in windows (`BACKFILL_WINDOW_DAYS`, default 31) per data type. A window
that fails five times is retried only after every other window; on its tenth failure it is marked
`abandoned` and the app logs `BACKFILL_WINDOW_ABANDONED`. An abandoned window is skipped from then
on, so it no longer blocks newer windows of the same type, but **its date range is missing from the
archive** until you act.

```sh
api /backfill/status | python3 -m json.tool    # rows with "status": "abandoned" and last_error
docker compose exec app health-mirror backfill  # also lists abandoned windows and exits 1
```

`last_error` holds a short error class and code (for example a 403 for one range). Fix the cause
first, then requeue the abandoned windows with a fresh retry count and run the backfill again in
one step (leave out `--data-type` to requeue every type's abandoned windows):

```sh
docker compose exec app health-mirror backfill --reset-abandoned --data-type steps
```

A requeued window resumes from its saved page token; if it keeps failing it is abandoned again
after ten more attempts.

If Google simply has no accessible data for that range, leaving the window abandoned is harmless.
