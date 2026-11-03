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

Updating a deployment that predates the backup metadata mount (`BACKUP_STATUS_HOST_DIR`,
default `./backup-status`): create the directory once before `docker compose up`, so the
backup account owns it:

```sh
install -d -m 755 backup-status   # or the absolute BACKUP_STATUS_HOST_DIR shared with the timer
```

If it is missing, Compose creates it owned by root (`create_host_path`). The app still
starts and the root backup timer can publish into it; a timer running as another account
cannot, until `sudo chown` gives the directory to that account. `GET /health/backup`
reports `missing` until the next backup run.

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
docker compose exec app alembic current          # expect 20261021_0012 (or a later head)
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
- A device field Google adds to a source's records (`formFactor`, `manufacturer`: e.g. a band
  sent until then by its name alone, now with `formFactor: "FITNESS_BAND"`, sometimes without the
  name) makes a new form of that source, like a rename: it appears under `chains`/`review` (or
  `ambiguous` when Google sends both forms side by side), and is decided the same way. Two
  different values of such a field (a phone and a watch) are never one device, through whatever
  forms: `identity-decide` refuses a link that would make them one (`ambiguous` says so: "its
  earlier form belongs to a device with another value of a device field").
  Two forms are only taken for one source when every other `dataSource` field is byte for byte
  the same (platform, recording method, the whole `application`, any field Google adds outside
  `device`). If a dry run (`identity-backfill --dry-run`) after Google added a device field shows
  the old form's rows under `deletions_kept` instead of a chain, `ambiguous` or
  `deletions_suspicious`, Google changed another field as well, and the two forms are treated
  as two sources (`identity-decide` refuses to link them, and their deletions are marked like
  any other's): compare the two forms' `dataSource` in what Google sent
  ([Audit what Google sent](#audit-what-google-sent)) and report the difference (its shape, no
  values) so the rule can be extended.
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
  count); `distinct_sources` the records (match keys) held by two devices; `conflicts` (rows of
  one device left double because linking them would close a cycle of links, or would join two
  values of a device field; listed in `status`) should be 0;
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

### `20261024_0013` (analytics views count what every reader counts)

Replaces the `analytics_*` views (`CREATE OR REPLACE`, same columns; no table, row or index
changes): a normalized row now also needs a live, unquarantined raw row with its key, as the
dashboard, its history, Umay's tools and the workout list require (docs/REFERENCE.md, Derived
tables). It waits at most 100 ms per attempt for a reader of a view and retries until
`lock_timeout` (30 s, `-x lock-timeout=...`); when no attempt succeeds it fails with
`20261024_0013 could not replace the analytics_* views`, changing nothing: retry once the reader
(a long analytics query) has finished.

```sh
make backup
git pull --ff-only
docker compose up -d --build
docker compose exec app alembic current          # expect 20261024_0013 (head)
docker compose exec app health-mirror verify     # exit 0: the views are the ones of 0013
```

`alembic downgrade 20261021_0012` restores the views of 0012 the same way; the previous image
then runs against them unchanged.

### `20261024_0014` (test-entry flags on manual measurements)

Adds `manual_measurement_flag_events`: one row per decision to mark a manual measurement as a test
entry (`mark`) or to clear that (`unmark`); the latest row per measurement is its state. The
measurement rows are never changed (`manual_measurements` stays append-only). The table refuses
`UPDATE`, `DELETE` and `TRUNCATE` like the 0011 tables, and `health-mirror verify` checks its
triggers. Fast and lock-free for existing tables: an empty table, its index and its triggers (no
foreign key, so `manual_measurements` is not locked).

Marking an entry: on the Ölçümler page, "Test olarak işaretle" on its row ("İşareti kaldır" clears
it), or on the host (loopback, with the UI token):

```sh
curl -sS -X POST -H "Authorization: Bearer $APP_UI_TOKEN" -H 'X-HealthOS-Request: 1' \
  http://127.0.0.1:8000/api/dashboard/measurements/<id>/flag     # .../unflag clears it
```

`GET /api/dashboard/measurements` lists the ids with their `flags`. A flagged entry stays listed
with a "Test kaydı" badge; the profile card, the 3D body and Umay's daily data use the entry before
it. `alembic downgrade 20261024_0013` refuses while the table has rows (`-x allow-flag-loss=1`
would drop the decisions, never on the live archive).

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
| `CODEX_BRIDGE_TOKEN` | Yes | Stop the bridge (below). For systemd, change the private service environment and app `.env` to the same new value, then restart the service and recreate the app. Manual mode: remove the old token file and configured token, rerun the launcher, then recreate the app. |
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

Each run streams ciphertext into its own private temporary file. A stdlib Python helper
publishes the complete dump with an atomic hard link that cannot replace an existing name;
same-second collisions add a unique suffix before `.dump.age` or `.dump.gpg`. The backup
filesystem must support hard links, otherwise publication fails and old backups stay intact.
Discovery, restore and explicitly enabled retention accept both naming forms.

Check the status file weekly: `result` must be `success` and `timestamp` from the last day. A
`failure` carries the reason in `message`; a stale timestamp means the timer is not running.

The protected `GET /health/backup` reads only status metadata, without database access.
Its `status` is `available`, `legacy`, `missing`, `invalid`, `unreadable` or `unconfigured`.
`last_attempt` contains `result` (`success`/`failure`) and UTC `timestamp`;
`last_success_at` is UTC or `null` when there is no evidence. A failure preserves the
previous success across app recreation. Success is published only after encryption
and final artifact placement. No paths, keys or raw error messages enter this response.
The old detailed `last-backup-status.json` remains compatible. On first publication,
only a valid legacy **success** proves an earlier success; a legacy failure cannot.
Back up the metadata directory along with operational state to retain that history.

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
counts across the union of tables plus the restored Alembic revision. Each table is
`equal`, `different`, `restored_only` or `live_only`; query/invalid-output errors and a
restored copy without tables fail the command instead of appearing equal. Count differences are informational: an old
snapshot and a changing live archive normally differ, and equal counts alone do not
prove content integrity. The isolated Compose drill freezes synthetic data and fails
unless every table matches. No comparison restores into the live database.

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

The bridge runs on the host. Linux production can use
[`deploy/systemd/healthos-bridge.service`](../deploy/systemd/healthos-bridge.service).
The paths below assume a checkout in `/opt/healthos`, with dependencies installed using
`uv sync --frozen --no-dev`, and the existing CLI installed at `/usr/local/bin/codex`.
Adjust the unit's explicit paths if your installation differs; no dependency upgrade is needed.

1. Stop any manually launched bridge using its recorded `bridge.pid` before enabling the unit.
   Provision a dedicated account and private home (once):

   ```sh
   sudo useradd --system --user-group --home-dir /var/lib/healthos-bridge --shell /usr/sbin/nologin healthos-bridge
   sudo install -d -m 700 -o healthos-bridge -g healthos-bridge /var/lib/healthos-bridge
   sudo install -d -m 700 /etc/healthos
   ```

2. Sign in as this account, with the same home used by the unit. An existing login can instead
   be provisioned privately as `/var/lib/healthos-bridge/auth.json`, owner `healthos-bridge`,
   mode 0600; never put credentials in the checkout or the unit:

   ```sh
   sudo -u healthos-bridge env HOME=/var/lib/healthos-bridge CODEX_HOME=/var/lib/healthos-bridge /usr/local/bin/codex login
   sudoedit /etc/healthos/bridge.env
   sudo chmod 600 /etc/healthos/bridge.env
   ```

   In that root-owned environment file, set `CODEX_BRIDGE_TOKEN` to the backend's existing
   private token (or provision one random value in both places). Do not paste it into shell
   arguments or logs. The service also reuses an existing private `bridge.token` when no
   environment token is supplied; if neither exists, startup fails instead of rotating it.
   Set `CODEX_BRIDGE_BIND_HOST` only to override address selection. The default remains the
   `docker0` gateway on Linux Docker Engine, loopback elsewhere. The app's existing
   `host.docker.internal:host-gateway` mapping needs no change. Keep the backend's explicit
   `CODEX_BRIDGE_URL`; for a native backend use `ws://127.0.0.1:8766` and a loopback bind.
   Service mode never writes `.env` and does not need to read it: the checkout's mode 600
   `.env` stays unreadable to this account, so service settings belong in `bridge.env`.
   Recreate the app only after a token or URL change.

3. Apply the existing [`deploy/firewall/`](../deploy/firewall/) nftables or UFW instructions
   before starting. Only loopback and Docker bridges may reach port 8766. UI/API stays on
   `127.0.0.1`; the public tunnel still allows only `POST /webhooks/google-health`.
   The account needs read/execute access to the checkout and Python environment, not write access.
   The virtualenv's Python interpreter and CLI must live outside `/home` (`ProtectHome=true`).

   ```sh
   sudo install -m 644 deploy/systemd/healthos-bridge.service /etc/systemd/system/healthos-bridge.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now healthos-bridge
   ```

Routine operations and diagnostics:

```sh
sudo systemctl stop healthos-bridge
sudo systemctl start healthos-bridge
sudo systemctl restart healthos-bridge
sudo systemctl status healthos-bridge --no-pager
sudo journalctl -u healthos-bridge -n 80 --no-pager
ss -ltn | grep 8766
```

`Type=simple` tracks process lifetime, not application readiness: check the listener and open
Umay on the local dashboard. The supervisor stops the process group, waits up to five seconds,
then kills leftovers; systemd also owns the entire cgroup with a 15-second stop limit.
Unexpected exits fail the service, including a spontaneous exit 0. Restarts wait 10 seconds,
with at most five starts per 300 seconds. After fixing a login, executable, port conflict or
configuration problem, use `sudo systemctl reset-failed healthos-bridge`, then start it again.
Identity stays in `/var/lib/healthos-bridge/healthos-bridge/`, directory 0700 and private files
0600. Service output goes to the journal; no detached PID is adopted. Enablement survives reboot.
The first service start copies the source login; later starts keep credentials refreshed inside
the isolated home. To renew that login, stop the service and repeat the login command above with
`CODEX_HOME=/var/lib/healthos-bridge/healthos-bridge`, then start the service.

Without systemd, the manual launcher remains available; its files are under
`~/.codex/healthos-bridge/` (or `CODEX_HOME`), including `bridge.pid` and `bridge.log`:

```sh
kill "$(cat ~/.codex/healthos-bridge/bridge.pid)"
uv run --frozen --no-dev python scripts/start_codex_bridge.py
docker compose up -d --force-recreate app      # only if token or URL changed
```

Older bridges might still listen on `0.0.0.0`: stop them explicitly before relaunching, since
manual mode reuses a healthy instance. Manual mode alone does not restart after reboot.
In the panel, missing settings, refusal, timeout and disconnect show safe Umay messages;
five retries use increasing waits, then **Yeniden bağlan** resets the budget. Sending is disabled
while waiting. Reconnecting starts a new ephemeral conversation; there is no persistent memory.
Requested health records still go to OpenAI through the signed-in account for interpretation.
Synthetic process/WebSocket tests do not validate VM systemd, Docker routing or firewall rules;
the maintainer must verify start/stop/reboot, allowed Docker access and denied LAN access there.

## The webhook went quiet

Sistem → Google bildirimleri → Bildirim ayrıntıları, aşağıdaki salt okunur uçla aynı
tanılamayı gösterir (UI kimlik doğrulaması gerekir; genel webhook hostundan erişilemez):

```sh
api /api/webhooks/status | python3 -m json.tool
```

- `last_notification_at`: geçerli imza ve en az bir geçerli bildirim öğesiyle alınmış,
  eski tekrar teslimi sayılmayan son teslim. İşleme başarısı ya da son veri yazımı değildir.
  Eski arşivde yalnız ham baytları ve geçerli öğesi bulunan doğrulama zamanı kanıt sayılır.
- `silence`: servis yedi günlük eşiği hesaplar. İlk yapılandırılmış uygulama başlangıcı
  bekleme süresini başlatır; yeni kurulum hemen sessiz sayılmaz. Yapılandırılmamış alıcı,
  henüz teslim alınmaması, eşik aşılması ve durum ucuna erişilememesi panelde ayrıdır.
  Challenge, bozuk trafik, tüm öğeleri reddedilen gövde ve eski tekrar süreyi sıfırlamaz.
- `counters.scope=persistent_diagnostic_events`: `20261025_0017` ile eklenen ayrı
  `webhook_status_events` tablosuna yalnız olay eklenir; webhook arşivi değiştirilmez.
  `since` ilk tanılama gözleminin zamanıdır; sayaçlar süreçler arasında ortaktır ve yeniden
  başlatmada korunur. Önceki arşivden sayaç türetilmez; veritabanı kesintisinde gözlem eksilebilir.
- `reasons` yetkilendirme, eksik/bozuk/geçersiz imza, anahtar indirme hatası, challenge,
  yinelenen teslim, eski tekrar ve işleme hatalarını ayırır. `units` sayım birimlerini belirtir.
  İstek başına birden fazla olay olabilir; `valid` yalnız kriptografik başarıdır.
  `invalid` eksik/bozuk/geçersiz imzaları toplar; yetkilendirme ve anahtar hatası ayrı kalır.
  Google'ın anahtar seti alınamazsa (`key_download_failed`) yanıt `401` değil `503`'tür:
  imza hakkında hüküm değildir, Google teslimi sonra yeniden dener.
- Yetkilendirme öncesi retler (`authorization_rejected`, `receiver_unconfigured`) herkesçe
  tetiklenebilir ve tanılama satırı silinmez; bu yüzden neden başına dakikada en fazla bir
  satır kalıcı yazılır. Bu iki nedenin `counters` değeri istek sayısından az olabilir.
  Yetkili aşamadaki nedenler ve teslimler eksiksiz yazılır.
- `process_local` yalnız bu sürecin başlangıcından (`since`) beri bellekte tutulur; yeniden
  başlatmada sıfırlanır, worker'lar arasında ortak değildir. `unpersisted` örneklemeyle
  atlanan yetki öncesi ret satırlarını neden başına sayar. `rate_limited.authorized` ve
  `rate_limited.unauthorized` iki webhook bütçesinin `429` ile reddettiği istekleri sayar;
  hız retleri hiç kalıcı yazılmaz ([sınırlar](#webhook-ve-manuel-yazma-sınırları)).
- `expired`, imzanın süre sonu değildir: biçimde böyle bir alan yoktur. Yerel
  `SIGNED_REPLAY_MAX_AGE` kuralı, ilk doğrulamasından **24 saatten fazla** geçen arşivlenmiş
  gövdenin yeniden işlenmesini engeller. Bu istek hem `valid` hem `expired` sayılabilir;
  yanıt 204 kalır. Tam 24 saatlik teslim mevcut kurala göre kabul edilir.
- Abonelik ve yenileme kanıtı/istemcisi bulunmadığından `subscription.status` ve
  `renewal_status` daima `unknown`; yapılandırma hazırlığı yenileme başarısı değildir.
  GET çağrısı Google'a gitmez, abonelik oluşturmaz veya yenilemez. `last_error` yalnız sabit
  kod, doğrulama/işleme aşaması ve zaman içerir; imza, gövde ve sağlık değeri içermez.

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
   differ. `WEBHOOK_VERIFICATION_UNAVAILABLE` means Google's public keyset could not be fetched:
   the delivery got 503 and Google retries it, so check the app's outbound network rather than the
   secret. A rising `process_local.rate_limited.authorized` means real deliveries are hitting 429
   (`WEBHOOK_REQUESTS_PER_MINUTE`). `WEBHOOK_REPLAY_IGNORED` means a signed body older than 24 hours arrived again and was
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

### Re-fetching a period whose windows are already completed

A plain `health-mirror backfill` never re-reads a range whose windows are `completed`; it only
plans before the first and after the last window. To fetch such a period again with retrieval
receipts, add new `pending` windows for it and run `backfill --data-type`. A one-off backfill of a
past period (prerequisites, window SQL, monitoring, stop/resume, verification) follows a separate
maintainer runbook that is not part of the published tree.

## Kapsama ve çekim fişleri — 20261025_0015

Önce `alembic upgrade head` çalıştırılır. Migration `retrieval_receipts`, `retrieval_events`
ve operasyonel `retrieval_checkpoints` tablolarını, indekslerini ve fiş korumalarını ekler;
mevcut ham ve manuel kayıtlara dokunmaz. Fiş/olay tablolarında UPDATE/DELETE ve PostgreSQL
TRUNCATE reddedilir; korumalar
`health-mirror verify` ile denetlenir. Fiş varken downgrade veri kaybını reddeder.
Geçmişe fiş doldurulmaz; eski başarılı işler ve `fetch_rounds` eksiksizlik kanıtı değildir.

Incremental (webhook ve uzlaştırma dahil) ve backfill çekimleri artık her denemeyi kaydeder.
Sayfa olayı kayıtlar ve checkpoint ile aynı işlemde kalıcılaşır. Operasyonel fiş
işaretçisi değişebilir ve tarihsel kanıt değildir; tokenı belirli bir sayfa olayına bağlar.
Aynı tokenla eski fiş tahmin edilmez. Geri alınan sayfanın dönüş sayısı hata olayında kalır; bu olay kayıtların kalıcılaştığını kanıtlamaz. Son sayfadan sonra sonuç
olayı eklenir; fiş güncellenmez. Devam, kaydedilmiş sayfanın token özetine bağlanır; eski
fişsiz token son sayfaya ulaşsa bile eksiksizlik kanıtlamaz. Token sıfırlanınca ayrı bir ilk
sayfa denemesi açılır. Hata, iptal veya süreç kesintisi yarım denemeyi başarıya dönüştürmez.
Google aynı denemede daha önce verdiği bir `nextPageToken`'ı tekrar verirse (döngüsel sayfa),
o sayfanın kayıtları ve yanıtları normal sayfa gibi saklanır, ama sayfa olayı yazılmaz,
checkpoint ve `last_successful_sync` ilerlemez; çekim `RepeatedPageTokenError
(repeated_page_token)` ile başarısız olur ve dönüş sayısı hata olayında kalır. Sonraki çekim
aynı tokendan devam eder; tekrar gelen kayıtlar yinelenmez. Google düzelince zincir son
sayfaya ulaşıp günü kanıtlayabilir.

Sistem → Kapsama → Ayrıntıları aç üzerinden dönem ve veri türü seçilir. Uzun içerik pencere
içinde kayar. `GET /api/sync/coverage` mevcut oturum/bearer kimlik doğrulamasını kullanır:

```text
/api/sync/coverage?start_date=2026-09-01&end_date=2026-09-07&timezone=Europe/Istanbul&data_type=steps
```

Tarihler dahil, en çok 31 gün; `timezone` zorunludur. `data_type` isteğe bağlıdır. Türler
alfabetik sayfalanır: `limit` 1–4, `offset` 0–1000; yanıttaki `next_offset` izlenir. Sunucu
sayfayı en çok 62 tür/gün hücresine indirir. Özet yalnız o sayfanın kapalı tür/günlerini
sayar. Yanıt başına 12 KiB; en çok 2000 fiş, 5000 olay üstverisi, dönemle örtüşen sayfalardan 8 MiB zaman verisi ve tür başına
100000 dönem kaydı okunur. Limit aşımı kırpılmış başarı yerine yapısal 413 döndürür.

`has_value` mevcut uygunluk/dışlama kurallarını kullanır; kaynaklar ayrı satırlarda kalır.
`presence_basis=eligible_record` olan türlerde bu, dışlanmamış kayıt varlığıdır; skalerin
veya ölçüm sıklığının doğruluğunu kanıtlamaz. `retrieval` bundan bağımsızdır:
`completed_with_records`, `completed_empty`, `incomplete`, `not_attempted`, `unknown`. Dönen kayıtlar dışlanan
kayıtları da içerir; eklenen/güncellenen satır sayısı değildir. `returned_records` günün
kanıtlanmış dönüş gözlemlerini sayar (yeniden çekimler dahil); gün belirlenemiyorsa null.
Pencere toplamı günlere dağıtılmaz. Fişsiz geçmişe “hiç çekilmedi” denmez: `not_attempted`
yalnız hiçbir denemenin dokunmadığı ve (civil saat farkı payı dahil) ilk fişten
(`receipts_since`) sonra başlayan kapalı gündür; o andan beri her Google okuması fiş bırakır.
Daha eski fişsiz gün `unknown` kalır.

Günlük filtreler civil tarihe, uyku bitiş zamanına, diğer oturumlar civil başlangıç için
saat farkı sınırlarına göre değerlendirilir. Fiziksel günler seçilen saat diliminde 23/25
saat olabilir; saniye hassasiyeti ve rollup dakika hizalaması korunur. Bugün kapalı gün
sayılmaz; kanıt ufku ilk sayfa denemesinin başlangıcıdır. Gece yarısını geçen veya sonraki
gün devam eden sorgu, ilk sayfa başlarken gelecekte olan günü tamamlanmış göstermez. Zaman filtresi
olmayan kataloglarda günlük değer/çekim kanıtı yoktur (`no_time_filter`). Umay'ın kapsama
kartı (şema 2, 12 KiB) aynı fiş okuyucusunu kullanır: her metrikte kapalı günleri çekim
durumuna göre sayar ve değersiz günleri `without_value` altında `empty_response`,
`returned_without_value`, `not_retrieved`, `incomplete`, `unknown` diye ayırır; tek metrik
istenince ardışık boşluk dönemlerini (`gaps`, en çok 31) listeler. Fiş bütçesini aşan dönem
küçük parçalarla okunur; tek gün yine aşarsa `unknown` ve `retrieval_evidence_limit` olur.

## Uzlaştırma fark geçmişi — 20261025_0016

Migration yalnız `reconciliation_runs` ve `reconciliation_results` tablolarını ekler.
Günlük/haftalık görev, `health-mirror reconcile` ve watchdog aynı raporlama yolunu kullanır.
Fişsiz veya raporsuz geçmişten tahmini fark raporu üretilmez. Rapor bulunan veritabanında
downgrade reddedilir; ham arşiv, sürümler ve değiştirilemez çekim fişleri korunur.

`GET /api/sync/reconciliation?offset=0&limit=20` mevcut oturum/bearer kimlik doğrulamasıyla
okunur; yeni iş başlatmaz. `limit` 1–100, `offset` 0–10000; geçersiz değerler 422 döndürür.
Yanıt `runs`, `total`, `offset`, `limit` içerir; boş geçmişte `runs=[]`, `total=0` olur.
Çalıştırmalar `started_at DESC, id DESC`, her çalıştırmanın `types` sonuçları `data_type ASC`
sırasındadır. Sayfalar çalışma bazındadır; tür sonuçları sayfalar arasında bölünmez.

Her çalıştırma istenen aralığı, kimliğini, başlangıç/bitişi, durumu ve güvenli hata türünü
tutar. Tür sonuçlarında `inserted`, `updated`, `unchanged`, `source_deleted` ve
`pages_committed` bulunur. Bunlar o çalıştırma sırasında commit edilmiş işlemlerdir;
ham değer, token veya istisna mesajı rapora kopyalanmaz. Sayaçlar kayıt/sayfa/checkpoint
veya silinme işlemiyle aynı transaction'dadır. Rollback sayılmaz; sonraki sayfa/pencere
başarısız olsa da önceki commit'ler raporda kalır. Tekrar gelen aynı içerik yeni/değişmiş
sayılmaz. Önceki başka pencerenin token devamı bu isteğin farklarına eklenmez.

`completed` bütün türlerin çekimleri tamamlandığında, `partial` hata öncesinde en az bir
istenen sayfa veya tür tamamlandığında, `failed` böyle bir ilerleme olmadan hata olduğunda
yazılır. İptal `cancelled` olur. Türler başlamadan `pending`, çalışırken `running` kalır.
Süreç sonuç yazamadan kesilirse rapor `running` ve bitişi null kalır; okuma bunu başarıya
dönüştürmez. Watchdog yalnız kendi kimliğine ait bitmiş, bütün türleri tamamlanmış kalıcı
raporu başarılı kurtarma kabul eder; cooldown, canlı eşitleme ve çakışma kilitleri korunur.
Başarılı token devamı bütün aralığın yeniden ilk sayfadan çekildiği anlamına gelmez;
eksiksizlik kanıtı yine çekim fişlerindedir. Kaynak silinmesi yalnız mevcut tam çekim
kapsamında `source_deleted_at` ile işaretlenir; kimlik ertelemeleri değişmez.

### Başarısız türetilmiş günleri yeniden deneme

Hesaplama hatası düzeltildikten sonra `METHOD` veya kaynak kayıtlarını değiştirmeden:

```sh
python -m app.cli.derived_retry 2026-09-01 2026-09-30 Europe/Istanbul --dry-run
python -m app.cli.derived_retry 2026-09-01 2026-09-30 Europe/Istanbul
```

İki tarih dahil, aralık 1–366 gündür; saat dilimi açıkça verilmelidir. Yalnız güncel
yöntemin bu aralık/saat dilimindeki mevcut `build_error` günleri adaydır; soğuk gün
oluşturulmaz. Kaynak değişmişse kilit altında güncel revizyon hesaplanır. Her gün ayrı
transaction'da commit edilir, kilit günler arasında bırakılır. Kesilme yalnız etkin günü
geri alır; yeniden çalıştırma başarılı günleri atlar. Normal okumalar ve zamanlayıcı,
kaynak revizyonu değişmedikçe başarısız günleri kendiliğinden yeniden denemez.

Rapor aday, işlenen, başarılı, başarısız, kaynağı değişen ve atlanan gün sayılarını içerir;
sağlık değeri içermez. Atlanan, aday olmayan (eksik/sağlıklı) günlerle kilit beklenirken başka
bir hesaplayıcının düzelttiği adaylardır. Kaynağı değişen gün yeniden hesaplanırken kaynağı
değişmiştir; yenileme kuyruğu onu yeniden kurar ve başarısız sayılmaz. `--dry-run` yalnız
okur, işlenen/başarılı/başarısız sıfır olur; aday sayısı yapılacak işi gösterir. Yeniden
denemede hata kaydı kalan gün varsa çıkış kodu 1, geçersiz girdi için 2, başarı veya salt
okunur deneme için 0'dır. Veritabanı hatasında o günün
önceki hata kaydı korunur; sonraki günler denenir. Ham, manuel ve geçmiş kayıtlar değişmez.

### Webhook ve manuel yazma sınırları

- Manuel ölçüm/antrenman oluşturma ve ölçüm işaretleme/kaldırma POST gövdeleri
  `MANUAL_WRITE_MAX_BODY_BYTES` ile sınırlıdır (varsayılan 4096 bayt, 1024–65536).
  Başlık olmasa veya az bildirilse de gerçek baytlar JSON ayrıştırılmadan sayılır;
  aşım `413`, bozuk/çoklu `Content-Length` ise `400` döndürür.
- Webhook mevcut `GOOGLE_HEALTH_WEBHOOK_MAX_BODY_BYTES` sınırını ve imzalanan özgün
  baytları korur; yetkisiz webhook gövdesi okunmaz.
- Uygulama başına üç ayrı, sabit 60 saniyelik pencereli bütçe vardır: manuel yazma
  (`MANUAL_WRITE_REQUESTS_PER_MINUTE=60`) ve iki webhook bütçesi; ikisinin de kapasitesi
  `WEBHOOK_REQUESTS_PER_MINUTE=120`'dir (her ayar 1–10000). `Authorization` değeri sabit
  zamanlı karşılaştırmayla eşleşen istekler (challenge dahil) yetkili bütçeyi, eşleşmeyenler
  ve yapılandırılmamış alıcıya gelenler yetkisiz bütçeyi harcar. Yetkili bütçe bu
  karşılaştırmadan sonra, gövde okunmadan ve imza doğrulanmadan önce harcanır; yetkisiz sel
  Google teslimlerini `429`'a düşüremez. İstemci adresi, UUID ve proxy başlıkları bütçeyi
  değiştirmez. GET, sohbet, sağlık kontrolü ve statik dosyalar bu bütçeleri tüketmez.
- Dolan bütçe gövdeyi okumadan `429` ve pozitif saniye cinsinden `Retry-After`
  döndürür. Hız reddi kalıcı tanılama yazmaz; son doğrulanmış teslimi ilerletmez.
  Sayaçlar yalnız süreç belleğindedir, yeniden başlatmada sıfırlanır; birden çok
  worker birbirinden bağımsızdır. Ret sayıları `/api/webhooks/status` içinde
  `process_local.rate_limited` altında görünür. Pencere sınırının iki yanında kısa
  kümeler mümkündür.

## jsonb'nin tutamadığı imzalı webhook gövdeleri — 20261031_0021

PostgreSQL jsonb, Python'un kabul ettiği U+0000'ı (`\u0000`), eşsiz vekil karakteri
(`\ud800`) ve sonlu olmayan sayıları (`NaN`, `Infinity`, `1e400`) tutamaz. Böyle bir imzalı
gövde önceden `webhook_events.raw_json` yazımında hata verip bütün alımı geri alıyordu; ham
baytlar hiç saklanmıyordu. Bu migration iki sütunu NULL olabilir yapar ve iki kısıt ekler:
satırda `raw_json` ya da `raw_body` bulunur; `raw_json` NULL olan öğe reddedilmiştir.
Mevcut satırlar okunmaz, değiştirilmez, silinmez. NOT NULL'ı kaldırmak iki tabloda
ACCESS EXCLUSIVE kilidi ister; 0012–0020'nin aksine çalışan bir `pg_dump` bunu engeller.
Migration kilidi en çok 30 sn bekler (`-x lock-timeout=2min` ile değişir), alamazsa
`20261031_0021 could not lock ... retry` hatasıyla hiçbir şeyi değiştirmeden durur; yedek
bitince yeniden çalıştırılır.

Böyle bir gövde `raw_body`'de bayt bayt saklanır (`payload_hash` yine bu baytların SHA-256'sı),
`raw_json` NULL kalır. Tutulamayan öğe `raw_json` NULL ve türetilmiş sütunları boş olarak
reddedilir; aslı batch'in `raw_body`'sinde, `webhook_batch_items` sırasındadır. Temiz kardeş
öğeler işlenir. Tutulamayan öğeler, `json.loads`'un aynı değere indirdiği farklı yazımlarda
aynı `element_hash`'i alabilir (`1e400`, `2e999` ve `Infinity` hepsi inf olur). Veri kaybolmaz:
her öğenin baytı kendi batch'inin `raw_body`'sinde ve `webhook_batch_items` sırasındadır, öğe
de zaten reddedilir. `raw_json` NULL satır varken `alembic downgrade 20261025_0020` hiçbir şeyi
değiştirmeden reddedilir.

## jsonb'nin tutamadığı Google kayıtları — 20261102_0023

Webhook gövdelerindeki sorunun (0021) eşitleme yanı: Google'ın döndürdüğü tek bir kayıt jsonb'nin
tutamayacağı bir değer taşıyorsa (`\u0000`, eşsiz vekil, `NaN`/`Infinity`/`1e400`) sayfa her
denemede geri alınıyor, o veri türü hiç ilerlemiyordu. Artık böyle kayıt sayfa saklanmadan önce,
aynı transaction içinde `raw_health_data_rejections` tablosuna ayrılır; sayfanın geri kalanı
saklanır, sonraki sayfa istenir, tür ilerler. Hiçbir değer temizlenmez ya da değiştirilmez,
arşivdeki hiçbir satır ezilmez. Google'ın gönderdiği baytlar her zamanki gibi sayfanın
`raw_api_responses` satırındadır (her alışverişe bir satır; yanıt tekilleştirmesi yoktur);
reddetme satırı ona `response_id` ve `ordinal` (sayfadaki sıra) ile bağlanır, ayrıca kaydın
ayrıştırılmış kopyasını ASCII kaçışlarıyla `element_json` içinde tutar.

Migration yalnız üç yeni tablo, indeksleri ve koruma tetikleyicilerini ekler; mevcut bir tabloya
ALTER ya da yabancı anahtar yoktur, bu yüzden mevcut tablolarda kilit almaz ve gece yedeği
(`pg_dump`) sırasında da çalışır. Üç tablo da yalnız-eklemedir (UPDATE/DELETE/TRUNCATE reddedilir;
`health-mirror verify` denetler). Tablolardan birinde satır varken `alembic downgrade 20261102_0022`
hiçbir şeyi değiştirmeden reddedilir.

Reddetme olan bir tur kaynakta silinme işaretlemez (reddedilen kayıt arşivdeki bir kaydın yeni
hali olabilir; tur onu gösteremez). İşaretleme reddetmesiz ilk tura kalır; kayıt Google'da
düzelince ya da pencereden çıkınca bu kendiliğinden olur. Çekim fişinde reddedilen kayıt
`returned_count` ve `returned_times` içinde kalır (Google onu döndürdü; kapsama o günü boş saymaz),
`unplaced_count`'a eklenmez; sayısı `retrieval_event_rejections` içinde ayrıdır.

`/health` onaylanmamış reddedilen kayıt varken `degraded` ve `rejected_google_records` gösterir,
`rejected_records` onaylanmamış farklı kayıt sayısıdır. Bakım:

```sh
docker compose exec app health-mirror rejections                 # son 20 kayıt, yalnız üst veri (JSON)
docker compose exec -T app health-mirror rejections --element 7 > kayit.json   # 7 numaralı kaydın kendisi
docker compose exec -T app health-mirror export-response <response_id> > page.json   # geldiği yanıt, bayt bayt
docker compose exec app health-mirror rejections --ack 7         # 7'ye kadar (dahil) bakıldı
```

Onay `raw_rejection_acks` tablosuna eklenir (o da yalnız-ekleme), geri alınmaz ve yalnız ileri
gider: daha küçük bir numara bir şey yazmaz. Onaydan sonra aynı kayıt (aynı veri türü ve
`element_sha256`) yeniden reddedilirse sağlık yeşil kalır; yeni bir kayıt reddedilince yine
`degraded` olur. Onay kayıtları arşivden çıkarmaz ya da arşive almaz: reddedilen kayıt
`raw_health_data`'ya girmez, panel ve Umay onu görmez. Kaydı kurtarmak için kaynağı
`export-response` ile alın; değerin nasıl temsil edileceği (ör. jsonb dışı bir sütun) ayrı bir
bakımcı kararıdır.
