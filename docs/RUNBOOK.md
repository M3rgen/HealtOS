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
docker compose exec app alembic current          # expect 20261020_0010 (or a later head)
docker compose exec app health-mirror verify     # payload hashes still match
api /health
```

If the log shows `raw_json -> jsonb would change ...` or `unsupported Unicode escape sequence`,
nothing was converted and the database is still on `20261020_0009`; check out the previous commit,
rebuild, and report the listed row keys. `alembic downgrade 20261020_0009` converts back to `json`
with every value kept, but the stored text is jsonb's spelling, not the original; restore the
backup if the original text is needed.

### `20261020_0011` (raw payload versions and delete guards)

Adds `raw_health_data_versions`, two nullable columns on `raw_health_data`, the version and guard
triggers (no `DELETE`/`TRUNCATE`/identity change on `raw_health_data`, append-only history) and the
`raw_health_data_history` view (see [Raw record versions](REFERENCE.md#raw-record-versions)). It
copies no rows: existing records become version 1 implicitly, so the upgrade is catalog-only and
takes well under a second regardless of table size, needs no extra disk, and holds its exclusive
lock on `raw_health_data` only for that moment. The app restarts as for any update (a few seconds;
Google retries webhooks).

```sh
make backup
docker compose exec app alembic current          # expect 20261020_0010
git pull --ff-only
docker compose up -d --build
docker compose logs app | grep 20261020_0011     # "Running upgrade 20261020_0010 -> 20261020_0011"
docker compose exec app alembic current          # expect 20261020_0011 (head)
docker compose exec app health-mirror verify     # exit 0: guards in place, hashes match
api /health
```

`alembic downgrade 20261020_0010` refuses while history rows exist (it would drop them); after a
backup, `alembic -x allow-version-loss=1 downgrade 20261020_0010` forces it. An offline script
(`alembic downgrade --sql`) contains the same check as a `DO` block that raises when it runs.
Prefer restoring the backup.

If `verify` reports a guard as `disabled` or `enabled (not ALWAYS)` (for example after a
`pg_restore --disable-triggers` data-only restore), re-enable it with the `ALTER TABLE ... ENABLE
ALWAYS TRIGGER ...` statement it prints. A *missing* trigger or a *changed* function means the
schema was altered by hand: restore from a backup rather than patching it.

## Import payload versions from archive dumps

Payload versions overwritten before `20261020_0011` survive only in older dumps. Import them one
dump at a time, **newest first** (that order keeps a payload that came back, A → B → A, as three
versions): restore into a scratch database, import, drop the scratch database. The live archive
keeps running; each batch of 2,000 records is locked for a moment while its versions are added. A
restored dump needs free disk of roughly the live database's size; check `df -h` first. (With disk
for several, restore them all and pass `--from-db` once per database: the command orders them
newest first itself.)

```sh
make backup                                          # a fresh backup of the live archive first
for dump in $(ls -r /path/to/encrypted/archive-*.dump.gpg); do   # newest first (timestamped names)
  bash scripts/restore.sh "$dump" health_archive     # decrypts (BACKUP_KEY_FILE) into a scratch DB
  docker compose exec -T app health-mirror import-versions --from-db health_archive --dry-run
  docker compose exec -T app health-mirror import-versions --from-db health_archive || break
  docker compose exec -T postgres dropdb -U health health_archive
done
docker compose exec app health-mirror verify
docker compose exec app health-mirror history --data-type heart-rate-variability --no-payload
```

`restore.sh` never touches `health` and decrypts `.gpg` files with the passphrase in
`BACKUP_KEY_FILE` (default `/etc/healthos/backup.key`) and `.age` files with
`BACKUP_AGE_IDENTITY`; the decrypted dump is streamed into `pg_restore` and never written to disk.
Each import prints JSON per source database: `imported` versions added, `already_present`,
`renumbered` existing history rows, `restored_records` (records only the dump had, now rows of
their own marked source-deleted), `source_hash_mismatches` and `unrepresentable` (skipped; the
command exits 1 and the loop stops, so keep that dump file and report the listed keys). A repeated
import adds nothing. Expect about 30 seconds per million dump rows for the scan, plus the
`pg_restore` time.

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
| `GOOGLE_TOKEN_ENCRYPTION_KEY` | Only by re-linking | There is a single Fernet key and no re-encryption command. Take a backup, set the new key, recreate the app, then open `/auth/google/start` through the SSH tunnel and consent again; the callback stores the refresh token encrypted with the new key. Until then syncs fail with `Stored OAuth token cannot be decrypted`. Older backups still need the old key, so keep it with them until they expire. |
| `CODEX_BRIDGE_TOKEN` | Yes | Stop the bridge (below), delete `~/.codex/healthos-bridge/bridge.token`, start the launcher again (it generates a token and updates `.env`), then recreate the app. |
| `POSTGRES_PASSWORD` | Manual | The image only reads it when the data directory is first created. Change the role first, `docker compose exec postgres psql -U health -d health -c "ALTER ROLE health PASSWORD '<new>'"`, then update `.env` and run `docker compose up -d --force-recreate app`. |
| Backup key | Yes | Point `BACKUP_AGE_RECIPIENT` (or `/etc/healthos/backup.key`) at the new key. New backups use it; keep the old identity until the last old backup has aged out (14 days by default). |

## Backup, verify and restore drill

```sh
make backup                          # prints backups/health-<stamp>.dump.age
ls -l backups/
systemctl list-timers healthos-backup.timer
journalctl -u healthos-backup.service -n 20
```

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
