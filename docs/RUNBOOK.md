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

### `20261020_0011` (raw versions, verbatim responses, delete guards)

Adds `raw_health_data_versions`, `raw_api_responses` (exact Google response bodies),
`raw_health_data_events` (source-deletion timeline), three nullable columns on `raw_health_data`
(`payload_received_at`, `payload_source`, `response_id` with a `NOT VALID` foreign key), the
version, event and guard triggers (no `DELETE`/`TRUNCATE`/identity change on `raw_health_data`,
append-only history, responses and events) and the `raw_health_data_history` view (see
[What is preserved where](REFERENCE.md#what-is-preserved-where)). It copies and rewrites no rows:
existing records become version 1 implicitly, and the foreign key is added `NOT VALID`, so the
upgrade is catalog-only: measured 0.02 s on a synthetic 500,000-row, 393 MB `raw_health_data`
(PostgreSQL 16), independent of table size; no extra disk; the exclusive lock on
`raw_health_data` lasts only that moment. The app restarts as for any update (a few seconds;
Google retries webhooks). From then on `raw_api_responses` grows by every page fetched (see the
size query in the reference).

**Do not deploy while the nightly backup runs** (the timer starts it at 03:30; it takes as long as
the dump). `pg_dump` holds a lock on every table until it finishes, and the upgrade needs an
`ACCESS EXCLUSIVE` lock on `raw_health_data` (and locks on the webhook and manual tables). The
upgrade sets `lock_timeout = 30s`: if it cannot get a lock within 30 seconds it stops with
`20261020_0011 could not lock its tables within 30s ... retry`, changes nothing (the database stays
on `20261020_0010`), and readers were blocked for at most those 30 seconds instead of until the dump
ends. The app container then exits and `restart: unless-stopped` retries the upgrade by itself;
once the backup has finished one of those retries succeeds. To wait instead, check first:

```sh
systemctl is-active healthos-backup.service      # "inactive" (not "activating"): no backup running
systemctl list-timers healthos-backup.timer      # next run not within the next minutes
```

```sh
make backup
docker compose exec app alembic current          # expect 20261020_0010
git pull --ff-only
docker compose up -d --build
docker compose logs app | grep 20261020_0011     # "Running upgrade 20261020_0010 -> 20261020_0011"
docker compose exec app alembic current          # expect 20261020_0011 (head)
docker compose exec app health-mirror verify     # exit 0: guards in place, hashes match
api /health
# After the next scheduled sync (or `docker compose exec app health-mirror sync heart-rate`):
docker compose exec app health-mirror responses --limit 3   # pages are being kept
```

#### Rolling back the application after `20261020_0011`

Do **not** downgrade the schema. `alembic downgrade 20261020_0010` refuses as soon as
`raw_api_responses` (or the history or event tables) holds a row, which is minutes after the first
sync, by design: downgrading would drop exactly the data this revision keeps. (`-x
allow-version-loss=1` would force it; never use it on the live archive. An offline `alembic
downgrade --sql` script contains the same check as a `DO` block.) The previous image cannot simply
be started either: its container command is `alembic upgrade head && uvicorn ...`, and its
`alembic upgrade head` fails with `Can't locate revision identified by '20261020_0011'`, so the app
never starts.

Roll back by running the previous image **with only the uvicorn command** (no Alembic), against the
0011 schema as it is:

```sh
cat > /tmp/healthos-no-migrate.yml <<'YAML'
services:
  app:
    command: ["sh", "-c", "exec uvicorn app.main:app --host $${APP_HOST:-127.0.0.1} --port $${APP_PORT:-8000} --no-access-log"]
YAML
git checkout <previous commit>                   # the one running before the update
docker compose -f docker-compose.yml -f /tmp/healthos-no-migrate.yml up -d --build app   # plus your usual -f overrides
docker compose exec app alembic current          # still 20261020_0011: nothing was dropped
api /health
```

The previous code works against the 0011 schema: the new columns are nullable, nothing it does is
refused by the guards (it never deletes or truncates raw, webhook or manual rows), and the version
trigger still keeps every replaced payload. Verified on 2026-10-20 (PostgreSQL 16) with the release
before 0011 (commit `0b08a27`) on a database migrated to `20261020_0011`: its `alembic upgrade
head` fails as described, `uvicorn` alone starts and serves `/health/live`, its PostgreSQL
integration tests for storage, sync, dashboard and freshness pass (only its migration tests fail,
because they expect `20261020_0010` as head), and its `health-mirror verify` passes. While it runs,
Google responses are not stored in `raw_api_responses` and new payloads carry no `payload_source`
or `response_id`, so roll forward soon: check out the fixed release and run a plain `docker compose
up -d --build` (without the override; the 0011 migration is already applied, so Alembic has nothing
to do). Keep using the override file for every restart until then; a plain `up` of the old code
fails at Alembic.

#### Guards and the database superuser

Production's `health` role (`POSTGRES_USER` of the official image) is a **superuser** and owns
every table. Triggers stop `DELETE`, `TRUNCATE`, `UPDATE` and `session_replication_role = replica`
from any role, but no trigger can stop the owner's or a superuser's DDL: `DROP TABLE`, `ALTER TABLE
... DISABLE TRIGGER`, `DROP TRIGGER`, `CREATE OR REPLACE FUNCTION`, or a trigger recreated with
`WHEN (false)`. `health-mirror verify` detects every change to the guards (missing, disabled,
redefined, extra triggers, replaced functions) and the backups recover dropped data. Follow-up (not
done yet): run the app as a separate non-superuser role that does not own the tables, with only
`SELECT`/`INSERT`/`UPDATE` grants, so the application itself could not issue that DDL.

If `verify` reports a guard as `disabled` or `enabled (not ALWAYS)` (for example after a
`pg_restore --disable-triggers` data-only restore), re-enable it with the `ALTER TABLE ... ENABLE
ALWAYS TRIGGER ...` statement it prints. A *missing* trigger, a *redefined* one (`was redefined`:
other events, timing, level, columns or `WHEN` than migration 0011 created, e.g. `WHEN (false)`),
an *unexpected trigger on a guarded table* or a *changed* function means the schema was altered by
hand: restore from a backup rather than patching it.

### `20261021_0012` (record identity, source chains, superseded duplicates)

Adds `raw_health_data_identity` (v2 identity key → raw row), `raw_health_data_supersessions`
(duplicate row → the row holding its record now) and `raw_source_chains` (source fingerprint →
the same device after a rename or a dropped package suffix), all three append-only with
`ENABLE ALWAYS` guards, plus the bookkeeping table `raw_source_sightings`; it replaces the
`analytics_*` views so they count what the dashboard counts (no superseded, no source-deleted
rows). See [Record identity](REFERENCE.md#record-identity). It copies no rows: empty tables,
triggers and view definitions; measured 0.02 s on a synthetic 515,151-row `raw_health_data`
(PostgreSQL 16). The foreign keys need a SHARE ROW EXCLUSIVE lock on `raw_health_data`, which a
running `pg_dump` does **not** block (tested), but a writing transaction does: with the app
container restarting nothing else should write, but stop a CLI `backfill`/`import-versions`
first. The upgrade sets `lock_timeout = 30s` and otherwise fails with `20261021_0012 could not lock
its tables within 30s`, changing nothing.

From this revision on, records without a Google id are written onto the row that holds their
identity. Whether two `dataSource` forms are one renamed device is decided per source from their
sighting timelines, never per record; that needs the history, which only
`health-mirror identity-backfill` reads. **Until it has run**, a rename (or suffix change) that
happens meanwhile creates new rows (as before the deploy) and a complete fetch marks the old ones;
the readings stay visible under the new name and the backfill then supersedes the old rows.
Growing intervals and added civil times merge from the first fetch on. Rows duplicated before the
deploy (on 21 Oct about 17,000 rows, most of them heart rate, then heart-rate zones, activity level,
energy, swim lengths, sedentary periods, steps and distance) are linked by the backfill, which also
writes the aliases of every existing row.

Order: **backup → deploy → dry run → review the chains and the sources kept apart → backfill →
verify**. The scheduler, webhooks and manual syncs can keep running throughout: a page being stored
and a deletion check hold a shared lock per data type, each backfill batch the exclusive one (a
sync of that data type waits for one batch, seconds, then goes on), and a second backfill refuses
at once with `identity-backfill refused: another identity-backfill is running`.

```sh
make backup
docker compose exec app alembic current          # expect 20261020_0011
git pull --ff-only
docker compose up -d --build
docker compose exec app alembic current          # expect 20261021_0012 (head)
df -h /var/lib/docker                            # the aliases need ~2 GB (≈ 310 bytes per row)
docker compose exec -T app health-mirror identity-backfill --dry-run > /tmp/identity-dry.json
```

Review the dry run before writing anything. Per data type:

- `chains`: every link it would record, `from` → `to` (labels: platform / device or package /
  recording method) with `gap_seconds` between the old form's last and the new form's first
  sighting. Expect the Fitbit rename of 19 Oct (old name → new name, gap ≈ 6 h) in each Fitbit data
  type and the Apple package (suffixed → plain) in the Apple types; nothing else. A chain between two
  devices you know are different (two watches) is wrong: stop and look at their sightings before
  running the backfill.
- `kept_apart`: pairs of sources that share records but stay separate, with the overlap
  (`gap_seconds` ≤ 300: seen together) or why. Your second watch belongs here, against both names
  of the band. A rename listed here (both forms returned within 5 minutes) stays double counted;
  that is the safe side (nothing hidden), and nothing is linked for it.
- `ambiguous`: a new form with more than one possible predecessor: nothing linked (expect none).
- `linked` (rows that would be superseded, by reason) should be in the order of the numbers above;
  `superseded_marked` of those carried a deletion mark (it stays, as history); `deletions_kept` is
  the number of genuinely deleted records that stay marked (a handful); `distinct_sources` the keys
  held by two devices; `conflicts` and `start_mismatches` should be 0.

Then:

```sh
docker compose exec -T app health-mirror identity-backfill > /tmp/identity.json
docker compose exec app health-mirror verify     # exit 0: guards, keys, links and chains
api /health
```

Duration: on the synthetic review archive (258,097 rows in 9 data types, PostgreSQL 16, developer
laptop) the dry run took 11 s, the backfill 29 s and a repeated run 23 s, also while syncs of the
same data types ran side by side (they waited for single batches; 103 sync windows, no error). The
first pass (timelines) reads every row once, the second links in batches of 5,000 rows, each its own
transaction with `statement_timeout` 120 s. The 5.8 million rows of production are therefore about
5 minutes (dry run) and 12 minutes (backfill) at that speed; plan for 30–60 minutes on the 2-vCPU
VM (one Python core plus index inserts). It prints its progress per batch on stderr and is
idempotent: if it is interrupted (or a batch times out), run it again, or resume the second pass
where it stopped with the `--data-type ... --after ...` that the last progress line names (the
first pass always reads the whole data type: a chain is decided over a source's whole history),
then once more without them. `--batch-size` (default 5,000) and `--statement-timeout` tune it.

After the backfill, renames are linked by the fetch that first sees them, under the same rule (the
old form not seen in that fetch nor within 5 minutes before it, the only candidate). Running the
backfill again later is harmless and settles whatever a fetch could not (an ambiguous case that
resolved itself, a rename seen before the backfill ever ran).

Rolling back the application: as for 0011, never downgrade the schema (`alembic downgrade
20261020_0011` refuses while the new tables hold rows; `-x allow-identity-loss=1` would drop the
links and chains, never on the live archive; a re-upgrade and backfill afterwards links the same
rows again without repeating their `identity-merged` events). The previous image runs against the
0012 schema with the no-migrate override above; it ignores aliases, chains and links, so its
dashboard counts the duplicates again and new renames would duplicate again until you roll forward.

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

## Audit what Google sent

Every Google response is in `raw_api_responses` (see
[Verbatim Google responses](REFERENCE.md#verbatim-google-responses)). To check a record against
the exact page it came from:

```sh
docker compose exec app health-mirror history --data-type steps --id 'users/me/dataTypes/steps/dataPoints/…' --no-payload
# each version shows response_id; export that page byte for byte:
docker compose exec -T app health-mirror export-response <response_id> > page.json
sha256sum page.json            # equals body_sha256 in the metadata printed on stderr
docker compose exec app health-mirror responses --context webhook:<element hash>   # all pages of one run
```

`health-mirror verify` recomputes every stored body's SHA-256; run it after restores and in the
backup drill.

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

### Watch the disk

Nothing here ever deletes data to save space: every Google response (`raw_api_responses`), every
payload version and every backup dump is kept forever, so the disk only fills. There is no
`health-mirror` command for disk usage; check monthly, and before imports or migrations:

```sh
df -h /var/lib/docker backups/                    # database volume; backups (or BACKUP_HOST_DIR)
docker compose exec -T postgres psql -U health -d health -c "SELECT pg_size_pretty(pg_database_size('health'))"
docker compose exec -T postgres psql -U health -d health -c \
  "SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) FROM pg_class
   WHERE relname IN ('raw_api_responses', 'raw_health_data', 'raw_health_data_versions')"
du -sh backups/                                   # or BACKUP_HOST_DIR
cat backups/last-backup-status.json               # size_bytes, free_bytes, required_free_bytes
```

When free space trends towards the backup guard (`required_free_bytes`, twice the newest dump),
grow the disk or move old dumps as below; never delete data or responses to make room.

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
