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

Adds `raw_health_data_identity` (fingerprint-specific v2 key → raw row), `raw_health_data_supersessions`
(duplicate row → the row holding its record now), `raw_source_chains` (source fingerprint → the
same device after a rename or a dropped package suffix), `raw_source_chain_events` (a chain that
turned out to join two devices), `raw_source_pairs` (evidence of two devices: one response
returned a key under two sources with two values), `raw_source_decisions` (your decisions,
`identity-decide`) and `raw_source_devices` (each chained source's device, what readers count
duplicates by), all append-only with `ENABLE ALWAYS` guards; `raw_source_sightings`
(first/last sighting per source, widen-only guard); the bookkeeping tables
`identity_reconcile_state` and `fetch_rounds`; and one partial index on `raw_health_data` (its
source-deleted rows). It replaces the `analytics_*` views so they count what the dashboard counts
(no source-deleted rows, no rows superseded by a live row through a link in force). See
[Record identity](REFERENCE.md#record-identity). It copies no rows and alters no existing table.
The partial index of source-deleted rows is built first, `CONCURRENTLY` and outside any
transaction: writers are never blocked by it, however large the table. A concurrent build that
fails (a timeout, a cancel) leaves an invalid index behind, so the upgrade starts it only while no
other session of the database holds a transaction or snapshot open (however briefly: a `pg_dump`
just begun counts) or a lock on `raw_health_data` beyond reading it, and gives it no timeout (no
lock or statement timeout, whatever the role or database sets: a dump started during the build is
waited for; writers are not). Otherwise it prints
`ix_raw_health_data_source_deleted is left for later` with the statement that builds it, and goes
on without it (the `analytics_*` views are only slower); `health-mirror verify` notes it until you
run that statement (`CREATE INDEX CONCURRENTLY IF NOT EXISTS ...`, also with the app running, when
no dump runs). An index left invalid by an interrupted build is not touched by the upgrade; verify
names it with `REINDEX INDEX CONCURRENTLY ix_raw_health_data_source_deleted`, and names any other
invalid index on `raw_health_data` (a `..._ccnew` copy of an interrupted rebuild: `DROP INDEX
CONCURRENTLY` it, it holds no data) - run those yourself when nothing else runs.
The foreign keys need a SHARE ROW EXCLUSIVE lock on `raw_health_data`, which a running `pg_dump`
does **not** block (tested), but a writing transaction does, and replacing the `analytics_*` views
needs each view for a moment (a long reader of a view holds it). The upgrade creates its tables
first, then in one attempt replaces the views (waiting at most 100 ms for a reader of each) and
takes the lock with `NOWAIT`; a failed attempt releases everything and is retried every few hundred
milliseconds for up to `lock_timeout` (30 s, `-x lock-timeout=...`), so no writer queues behind the
upgrade; once both are held, the foreign keys are added and it commits (writers wait
milliseconds). When no attempt succeeds it fails with `20261021_0012 could not lock its tables
within 30s`, changing nothing: stop a CLI `backfill`/`import-versions` first.

**The model.** The live write path never decides that two `dataSource` forms are one renamed
device, and it writes a record only onto a row of **its own** fingerprint (a changed payload is a
version): no row ever holds another source's payload. A record of a fingerprint joined to another
by a decided chain gets a row of its own that is linked at once to the device's row showing the
same reading (counted once); any other fingerprint gets rows of its own (`synthetic:v2s:…`). So a
rename nobody decided yet is **counted twice** (both rows visible) until the reconciler links the
rows, and a genuinely distinct device is never hidden. Nothing Google currently returns is ever
hidden: a superseded row counts again when the row holding it is marked deleted, and a returned
value that differs from what the row hiding its own shows gets a row of its own. A complete fetch
does not mark a row as deleted while another source that may be its device renamed returns the
same reading (the mark is deferred, never guessed, and listed under `identity_deferred_deletions`
in `status`). A duplicate is hidden only while the two sources it was linked across are one device:
rejecting a wrong chain (or deciding two sources distinct) counts both sides again at once, each
device once, and the opposite decision undoes that exactly. The **reconciler** (`health-mirror identity-backfill`) decides
chains only when every
fingerprint's whole sighting history leaves no doubt, and links the duplicates; it runs once in
full after the deploy and then **nightly** at 02:30 UTC as the scheduler job `identity-reconcile`
(incremental: rows changed since its last run; `IDENTITY_RECONCILE_ENABLED`,
`IDENTITY_RECONCILE_HOUR_UTC`, `IDENTITY_RECONCILE_MINUTE`). Everything it leaves open, and every
chain it decided, is listed in its report and in `health-mirror status`; **you** decide those with
`health-mirror identity-decide` (below). Double counting after a rename therefore lasts until the
next night, or until you decide a case the rule leaves open.

Order: **backup → deploy → `identity-backfill --dry-run` → review → `identity-backfill` → verify →
review the chains and open cases, decide them (`identity-decide`) → check that the nightly job is
active**. The scheduler, webhooks and manual syncs can keep running throughout: a page being
stored and a deletion check hold a shared lock per data type, each reconciler batch the exclusive
one (it tries for it without queueing writers behind it, waits in the queue at most 1 s, and once
it holds it at most 2 s for any row another transaction holds, then backs off and retries, saying
so on stderr, so no sync waits behind it for long), and a second run refuses at once with
`identity-backfill refused: another identity-backfill is running`. A `--dry-run` takes no lock.

```sh
make backup
docker compose exec app alembic current          # expect 20261020_0011
git pull --ff-only
docker compose up -d --build
docker compose exec app alembic current          # expect 20261021_0012 (head)
df -h /var/lib/docker                            # room for the aliases (below)
docker compose exec -T app health-mirror identity-backfill --dry-run > /tmp/identity-dry.json
```

The alias table holds about one row per raw row without a Google id, a few hundred bytes each
with its index: check that the volume has room for that beside the archive before the full run.

Review the dry run before writing anything. Per data type:

- `chains`: every link it would record, `from` → `to` (labels: platform / device or package /
  recording method; `fingerprints`: the first 12 characters of each source's fingerprint) with
  `gap_seconds` between the old form's last and the new form's first sighting. Expect one chain
  per device rename or package-suffix change you know of, in each data type that device writes.
  A chain between two devices you know are different (a band replaced by a watch of the same
  platform, worn one after the other) is wrong: reject it after the run
  (`identity-decide reject`, below); it only ever merges rows with equal readings, so nothing is
  lost meanwhile.
- `review`: the chains decided by the reconciler that you have not confirmed or rejected yet
  (all of them, after the first run). `health-mirror status` lists them too
  (`identity_review_suggested`) until you decide.
- `ambiguous`: sources the rule left open, each with `why` and the `candidates`: `a source of its
  class ended less than five minutes before` (a rename in the middle of a fetch), `more than one
  source of its class ended before it`, `another source of its class appeared in between`,
  `its predecessor has more than one successor candidate`, `another source of its class was seen
  alongside` / `seen alongside another source of its class` (a second device of the same platform
  and recording method around the rename, not yet known to be another device), `history
  incomplete`. **Nothing is linked for them**: both sources keep their rows, the readings are
  visible (counted twice where they are the same records), and deletion marks between them stay
  deferred. That is the safe side; decide each with `identity-decide`.
- `kept_apart`: pairs of sources of one class that stay separate; `established: true` when
  Google returned different values for the same record under both, for three records in at least
  two fetches, the later one with records newer than the first (two devices recording side by
  side: your second watch belongs here, against both names of the band), or you decided them
  distinct; `false` when nothing decided them (see `ambiguous`).
- `review_distinct`: pairs established distinct by that evidence alone although it looks like a
  rename: one source began as the other ended (and no form of one was worn alongside a form of the
  other for more than a day), or most records both hold came back with the same reading (Google
  reprocessed old records under the new name, a few with other values). Nothing is merged and no
  deletion is marked because of them; confirm with `identity-decide distinct`, or, if it was a
  rename, decide `not-distinct` and `link`.
- `deferred_deletions`: rows complete fetches did not mark because another source, that may be
  their device renamed, returned the same reading (`rows_of`, `returned_by`, how many, since
  when; one row each in `identity_deferred_deletions` until a later complete fetch of their
  window decides them).
  Decide that pair: `distinct` (two devices: the next complete fetch marks them) or `not-distinct`
  and `link` (one device: they stay, counted once; the pair leaves this list at once, and the next
  complete fetch of the data type resolves its rows).
- `refuted`: chains whose two forms turned out to be two devices (no longer used: the rows they
  linked count on their own again); a chain in state `lapsed` was decided by the reconciler only
  because another chain made a rival source continue elsewhere, and that chain is no longer live
  (it is live again when that one is); `returned`: chains whose old form was seen again (a name
  changed back: still one device, its records get rows of their own, linked to the device's).
- `linked` (rows that would be superseded, by reason) should be in the order of the chains above;
  `superseded_marked` of those carried a deletion mark (it stays, as history); `deletions_kept`
  the deleted records with no other source at their instant (genuine deletions; expect a few per
  data type), `deletions_suspicious` deleted records whose instant another, undecided source still
  holds live (possibly a rename nobody decided: look at `ambiguous`); `kept_differing` rows of a
  chained source that measured something else than the row holding the record (kept, both
  count); `distinct_sources` the keys held by two devices; `conflicts` (rows of one device left
  double because linking them would close a cycle of links; listed in `status`) should be 0;
  `start_mismatches` rows with a stale stored start (settled last); `agreement` per pair of
  sources with evidence of two devices, how many records this run settled that both hold with
  the same reading and how many with two (the counts over every record both hold, kept per
  record in `raw_source_agreements`, decide whether the pair is two devices; a chain is refuted
  only when all of them were counted).

Then:

```sh
docker compose exec -T app health-mirror identity-backfill > /tmp/identity.json
docker compose exec app health-mirror verify     # exit 0: guards, keys, links, chains, views
docker compose exec app health-mirror status     # identity_reconcile per data type; review list
docker compose logs app | grep SCHEDULER_STARTED # identity_reconcile_enabled: true
api /health
```

**Deciding what the reconciler left open** (`health-mirror identity-decide`). Each decision is
recorded append-only in `raw_source_decisions` (with the time and your `--note`), applied at once,
and `verify` checks that every chain you linked names its decision. Always run it with
`--dry-run` first: it writes nothing and prints the effect (links it would make, chains, open
cases afterwards). Fingerprints are given as the reports print them (12 characters; any unique
prefix of at least 6); `health-mirror identity-sources --data-type <type>` lists every source of a
data type with its fingerprint, sightings, device, the sources it is established distinct from,
the chains with their review state and what the rule leaves open.

```sh
# A rename the rule left open (e.g. within five minutes): FROM is the earlier form.
docker compose exec app health-mirror identity-decide link --data-type <type> \
    --from <fingerprint> --to <fingerprint> --note "renamed in the app" --dry-run
# A chain you checked: off the review list. A wrong one: no longer used.
docker compose exec app health-mirror identity-decide confirm --data-type <type> --from … --to …
docker compose exec app health-mirror identity-decide reject  --data-type <type> --from … --to …
# Two devices whatever the evidence, or a stray duplicate that is not a second device.
docker compose exec app health-mirror identity-decide distinct     --data-type <type> --from … --to …
docker compose exec app health-mirror identity-decide not-distinct --data-type <type> --from … --to …
```

Every decision settles again exactly the rows whose devices it changes: after `link`,
`not-distinct` or a `confirm` that makes a rejected chain live again, every row written since the
later of the two sources was first seen (rows of both forms are linked where they hold the same
reading); after `reject` or `distinct`, every row of a link between the two sides (also rows
archived before 20261020_0011). The links made across the two sources stop hiding anything at once
(both count again in the dashboard, Umay and the `analytics_*` views, each device once; `verify`
notes the links as no longer in force), and both sources are free for another chain - so after a
wrong `link`, `reject` it and `link` the right pair. The latest decision per question wins, so
every decision is undone by the opposite one, exactly in what is counted (`link` or `confirm`
after `reject` uses the chain again, and a chain the reconciler decided because of it is live again
with it; `not-distinct` after `distinct`). There is no decision that forgets one: return to "what
the evidence says" by deciding what it says. **`distinct` has a permanent consequence**: from then
on a row of one source whose reading comes back only under the other is marked deleted by the next
complete fetch, and a mark is never removed by a decision (the outcome and `--dry-run` say so in
capitals); undoing it with `not-distinct` restores what is counted, not the marks. Decide it only
for two devices. An owner's `distinct` reaches the other forms of a device only through chains you
linked or confirmed (a chain only the reconciler suggested may be wrong). A decision that would
make one source continue as two (or close a cycle) is refused, naming the chain to reject first.
A deletion left undecided between two sources is decided by the next complete fetch of its window
after your decision (the daily deletion check, or a `backfill` of that window); the decision itself
fetches nothing. A decision records only itself: a chain it makes possible (say `distinct` leaves
a retired watch the only candidate before a new name) is listed under `left_to_reconciler` and
decided by the next nightly run, listed for review like any other. For a chain the reconciler
suggested, `confirm` after `reject` restores what is counted but marks the chain reviewed
(`confirmed`); if the reconciler linked one of the freed sources elsewhere meanwhile, `confirm` is
refused naming that chain (reject it first). A decision waits up to a minute while a reconciler
batch run holds the single-run lock, then is refused naming it; run it again after
`health-mirror status` shows the run finished. If a decision or a nightly run fails after devices
changed, the rows still to settle are kept (`pending_settle`) and the next run settles them (a
`--dry-run` shows them among what it would settle).

The next morning, `health-mirror status` shows `last_mode: incremental` with a fresh
`last_finished_at` per data type, its `last_result` with the counts and an `open` section
(chains to review, ambiguous sources, pairs kept apart that nothing decided, pairs that look like
a rename although evidence made them two devices, refuted chains, deferred deletions), and
`identity_deferred_deletions` lists the deletions waiting for a decision; the log has
`IDENTITY_RECONCILE_COMPLETED` (and one `IDENTITY_RECONCILE_DATA_TYPE` per data type). Check
`open` after every rename you notice; anything listed there is counted twice (never hidden) until
you decide it. A data type that never had a full run gets its first full pass from the nightly
job (`skipped: no full identity-backfill of this data type yet: ran its first full pass`).
`IDENTITY_RECONCILE_LOCK_BUSY` (and `retrying in … s` on stderr) means a batch met a long sync, a
deletion check or an import and backed off; `error: … could not get the identity lock` in that
data type's `last_error` (and `IDENTITY_RECONCILE_DATA_TYPE_FAILED` in the log) that it gave up
after 8 attempts: nothing of that batch was written, the other data types still ran, and the next
run resumes. By hand, `identity-backfill` then exits 1 after printing the report. Run the incremental pass by hand with `identity-backfill --incremental [--dry-run]`.

Duration: the first pass (timelines) reads every row once, the second links in batches of 5,000
rows, each its own transaction with `statement_timeout` 120 s. On a synthetic archive of 1.4
million rows (PostgreSQL 16, developer laptop) a full run took 2 min 19 s (≈ 10,000 rows/s); an
incremental run right after under a second. Scale by your row count (`health-mirror status`
prints `raw_records`) and allow several times longer on a small server. It prints its progress per
batch on stderr and is idempotent: if it is interrupted (or a batch times out), run it again, or
resume the second pass where it stopped with the `--data-type ... --after ...` that the last
progress line names (the first pass always reads the whole data type, and rows with a stale start
before `--after` are still settled), then once more without them. `--batch-size` (default 5,000;
inserts are chunked for any size) and `--statement-timeout` tune it.

Rolling back the application: as for 0011, never downgrade the schema (`alembic downgrade
20261020_0011` refuses while the append-only tables or the sightings hold rows;
`-x allow-identity-loss=1` would drop the links, chains, decisions and sightings, never on the
live archive; a re-upgrade and backfill afterwards links the same rows again without repeating
their `identity-merged` events, while your decisions would have to be made again). The previous
image runs against the 0012 schema with the no-migrate override above; it ignores aliases, chains
and links (and does not run the nightly job), so its dashboard counts the duplicates again and new
renames duplicate again until you roll forward.

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
