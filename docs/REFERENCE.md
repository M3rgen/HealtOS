# HealtOS — Technical Reference

A fully self-hosted, subscription-free, read-only archive of health and fitness data exposed by the **Google Health API v4**. It preserves every source payload before normalization and maintains analytics-friendly PostgreSQL tables for heart rate, HRV, oxygen saturation, sleep, steps, exercise, and long-tail scalar metrics.

> This project uses `https://health.googleapis.com/v4`, the current Google Health API. It does not use or add functionality against the deprecated Fitbit Web API. Support for the legacy Fitbit Web API ended on September 30, 2026, and it shuts down completely on October 30, 2026; because HealtOS uses only Google Health API v4, Google OAuth, and `googlehealth.*` scopes, that shutdown does not affect it.

## Design principles

`RAW FIRST → NORMALIZE SECOND → ANALYZE LATER`

- Google Health remains the source of truth. This application only makes read requests to user data endpoints.
- Every HTTP response body Google sends is kept byte for byte in `raw_api_responses`, and every
  record parsed from it in `raw_health_data`; unknown fields are never dropped from the archive
  (see [What is preserved where](#what-is-preserved-where)).
- A Google resource name is the primary source identifier. Where a response lacks one, a deterministic identity is derived from type, time coordinates, and data source.
- Payload hashes distinguish unchanged records from source corrections. A correction never
  overwrites the old payload: every version is kept in `raw_health_data_versions` (see
  [Raw record versions](#raw-record-versions)).
- Raw and normalized UPSERTs plus their page checkpoint commit in one transaction.
- Backfill progress is stored per data type and time window, including the next page token.
- Incremental sync deliberately overlaps previous checkpoints to catch late or enriched data.
- Webhooks trigger retrieval; they are not treated as the dataset. Reconciliation repairs missed notifications.
- A webhook `DELETE` notification never deletes archive rows. The follow-up read and webhook audit record are retained.

## Architecture

### HealthOS dashboard

The application includes a Turkish dashboard at `http://localhost:8000/`.
It displays real archived data, hourly step and heart-rate charts, daily sleep,
7-day history, CSV export, synchronization status, and separate manual body
measurements. The 3D mannequin is illustrative, not a biometric reconstruction.
The dashboard includes a separate Codex conversation in an in-page panel. It uses
your locally installed Codex CLI and ChatGPT subscription through an authenticated
app-server bridge. Requested health data is sent to OpenAI for analysis. The
assistant can read daily summaries and original archived Google health records;
its data tools cannot modify the archive. Other desktop Codex chats are separate.

### Umay personality

`app/personas/umay/soul.md` defines Umay's identity, values, warm Turkish voice, and
mythological narrator voice, and example dialogues. The backend reads this file for
each new site conversation and sends it as conversation-specific developer
instructions. The health analysis and tool rules remain separate base instructions
in `app/services/codex_chat.py`. Examples are style guidance, not user health data.
This does not edit global Codex settings or affect desktop conversations.

Umay defaults to short, natural Turkish paragraphs, including when interpreting
health records. Formatting is reserved for requests or comparisons that benefit
from it. Assistant Markdown is rendered with Marked and sanitized with DOMPurify;
user messages remain literal text. The renderer retains raw streamed text so
formatting split across response chunks is not lost. Browser regression checks in
`tests/browser/umay-chat.cjs` cover streaming, formatting, unsafe HTML, and mobile
overflow (`cd frontend && npm run test:browser -- --only umay-chat`).

Edit the personality file to refine the voice. An existing conversation keeps its
initial personality; start **Yeni sohbet** to apply an edit. With the Docker image,
first run `docker compose up -d --build app` to include the changed file. Missing or
empty personality files prevent a new conversation from starting.

The personality file is not persistent memory. Closing the panel keeps the current
conversation, but a reload, reconnect, or new chat starts a fresh ephemeral thread.
Future refinements can specify preferred form of address, response length,
mythological intensity, and coaching style. Persistent memory would require a
separate, user-scoped storage design.

### Local Codex chat (Umay bridge)

The local website must remain private (the default Compose port binds to
`127.0.0.1`; the public webhook ingress exposes only the Google webhook route).

1. Install the Codex CLI so `codex` is on `PATH` and sign in with `codex login`
   using your ChatGPT account.
2. From this project directory, start the bridge with the project's dependencies:

   ```sh
   uv run --frozen --no-dev python scripts/start_codex_bridge.py
   ```

3. Apply the private `.env` values the launcher wrote and rebuild the site if needed:

   ```sh
   docker compose up -d --build app
   ```

4. Open the round Umay button on the dashboard at `http://localhost:8000/`.
   Questions and streamed responses stay on this page. Closing the panel keeps
   the current conversation; **Yeni sohbet**, reconnecting, or reloading starts
   a separate conversation. Site conversations are ephemeral, not sidebar chats.

Run the launcher again after the host restarts; no service unit is shipped. It runs the
bridge in the background and reuses a healthy instance. The launcher copies the current
ChatGPT login into an isolated `~/.codex/healthos-bridge` home (or under `CODEX_HOME`),
with desktop plugins, shell, browser, computer-use and multi-agent tools disabled.
The bridge requires a random bearer token; only the backend receives that token.
Token and credentials are never included in frontend bundles or tool responses.
The bridge log is `healthos-bridge/bridge.log`. Keep that local home private.
Windows-specific steps (an isolated npm-installed CLI run through Node) are in
[legacy/WINDOWS.md](legacy/WINDOWS.md#umay-bridge-on-windows).

The bridge listens on `CODEX_BRIDGE_BIND_HOST` (process environment or `.env`), never on
`0.0.0.0`. Left empty, it uses the `docker0` address on Linux Docker Engine — the address
`host.docker.internal` resolves to in the app container via Compose's `host-gateway` mapping — and
`127.0.0.1` elsewhere (Docker Desktop forwards `host.docker.internal` to host loopback). On a Linux
host, also apply [`deploy/firewall/codex-bridge.nft`](../deploy/firewall/codex-bridge.nft) (or
`deploy/firewall/codex-bridge.ufw.sh`) so port 8766 accepts only loopback and Docker bridge traffic.
A bridge started by an older launcher listened on all interfaces: stop the PID in
`healthos-bridge/bridge.pid` and run the launcher again (see the [runbook](RUNBOOK.md#restart-the-umay-bridge)).

Usage counts against the signed-in ChatGPT account's limits. Codex must be running
and able to reach OpenAI. If the connection closes, use **Yeniden bağlan**. For a
native backend outside Docker, set `CODEX_BRIDGE_URL=ws://127.0.0.1:8766` instead.
The site explicitly uses **GPT-6.1 Sol / medium** for new conversations and each
turn. It does not switch to a different model when access fails; the chat reports
the error. The model and effort are visible in the chat's connection status.

The assistant first discovers archive coverage. It can query up to 366 civil days
per summary and paginate original records in windows of at most 31 days, with up
to 200 records per page. Larger periods require multiple queries. Quarantined and
source-deleted raw records are excluded; undated raw records are counted in the
inventory but excluded from date-filtered record queries. Daily summaries use the
dashboard's existing day allocation and missing-data rules. AI interpretation is
not a medical diagnosis.

The frontend is built with Vite and Three.js during the Docker image build and
served by FastAPI in the existing `app` container. There is no separate Node
runtime, frontend port, or additional production service.

Run from this project directory, with the existing `.env` file intact:

```sh
docker compose up --build -d
docker compose ps
```

Open `http://localhost:8000/`. This Compose file runs in production mode, which serves no API
documentation; see [Local API](#local-api).
After frontend changes, run `docker compose up -d --build app` to update this
permanent dashboard. `npm run build` alone only updates the host files; an
existing container keeps its bundled frontend. Port `5173` is a temporary Vite
development preview, not the dashboard restored by Docker after a restart.
The header date opens the calendar; choosing a day updates the daily cards,
seven-day table, and CSV export to that date. The main menu is centered and the
personal archive badge has been removed.
Manual measurements persist in PostgreSQL's `manual_measurements` table. The
startup migration adds this table without rewriting Google source records.
The dashboard remains local-only; the public webhook proxy still exposes only
the webhook endpoint.

Daily summaries use the selected civil timezone. Sleep is assigned to the day
of waking, with awake time removed. Overlapping sessions count once; where only
stage totals exist, overlapping asleep duration is a conservative estimate.
HRV, oxygen saturation, and resting heart rate use daily source summaries, not
averages mixed with intraday samples. Missing data remains blank, not zero.
Step intervals crossing hours/days are distributed proportionally by duration;
hourly heart-rate charts show measured sample averages and preserve gaps.

For native development, build the UI before starting FastAPI:

```sh
cd frontend
npm ci
npm run build
cd ..
```

Node 24 is needed only for native frontend builds. Docker installs it in the
build stage. Browser preferences control timezone and model rotation; reduced
motion preferences disable rotation. Manual entry uses the browser's displayed
local timezone; values are stored with an explicit UTC offset.

```text
Fitbit Air → Google Health cloud → Google Health API v4
                                      ↓ OAuth 2.0 (read-only scopes)
                          FastAPI Personal Health Mirror
                         ↙ raw JSON      normalized tables ↘
                                  PostgreSQL
                         ↙                           ↘
                    pg_dump                    SQL/Grafana/Pandas
```

The core has two containers: `app` and `postgres`. APScheduler runs daily and weekly reconciliation inside the app, avoiding a mandatory queue or hosted scheduler. Run only one scheduler-enabled app replica; if scaling the API, set `RECONCILIATION_ENABLED=false` on all but one worker.

## Current Google Health API constraints

The implementation follows the official [v4 REST reference](https://developers.google.com/health/reference/rest), [data-type table](https://developers.google.com/health/data-types), [endpoint guide](https://developers.google.com/health/endpoints), and [webhook guide](https://developers.google.com/health/webhooks).

- Data can be queried as far back as it was recorded, but every page must be followed through `nextPageToken`.
- Most list endpoints allow up to 10,000 points per page; `sleep` and `exercise` are capped at 25.
- Endpoint type names use kebab case while filter message fields use snake case.
- Google Health OAuth scopes are Restricted. A personal project still needs the Google configuration and consent flow required by Google; broader distribution can require Google's verification/security review.
- Subscriber endpoints must be publicly reachable over HTTPS with TLS 1.2+.
- Subscriber creation performs an authenticated success handshake and an unauthenticated rejection challenge.
- Notification payloads are signed using a Tink-prefixed ECDSA P-256/SHA-256 signature. Google rotates keys and publishes the public keyset at a stable `gstatic.com` URL.
- Notifications can be retried for up to seven days, so periodic reconciliation remains necessary.
- The API currently does not provide a schema-enumeration endpoint. A versioned registry based on the official data-type table covers list, reconciled-stream, and rollup-only types; `EXTRA_DATA_TYPES` permits additions while the API evolves, while `ENABLED_DATA_TYPES` can deliberately limit consent and synchronization.
- The built-in registry includes every data type with a current read operation. Resource-schema types that are currently write-only (`menstrual-period`, `moods`, `ovulation-test`, and `symptoms`) are deliberately excluded so the mirror never requests write scopes or calls write endpoints.
- Read-only food and food-measurement-unit catalogs have no time filter. They are fetched once per backfill run, paginated, and retained raw like every other source resource.

## Free and private by default

There is no telemetry, tracking SDK, external logging service, hosted database, paid synchronization service, API proxy, or mandatory cloud backup. The mandatory software components are open-source and run on your hardware. Google Cloud/API account policies and any infrastructure or domain you independently choose are outside the project; no paid product is required by this code.

PostgreSQL has no published host port in the production Compose file. FastAPI is published only on `127.0.0.1`. The application container runs as an unprivileged UID with all Linux capabilities dropped, `no-new-privileges`, and a read-only root filesystem. PostgreSQL also drops all capabilities except the five its entrypoint needs to hand the data directory to the `postgres` user (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`) and runs with `no-new-privileges`; the on-demand backup container keeps no capabilities and a read-only root. Container logs use the `json-file` driver with rotation (`max-size: 20m`, `max-file: 5`) for PostgreSQL, the app, and Caddy. Put a self-hosted reverse proxy with HTTPS in front of it before exposing the webhook. Secrets are environment variables; tokens are never logged. OAuth refresh tokens obtained by the callback are Fernet-encrypted in PostgreSQL using a key that stays in the environment.

## Requirements

- Docker Engine with Docker Compose, or Python 3.12+ and PostgreSQL 15+
- `uv` 0.12.1 for deterministic native dependency installation
- A Google Cloud project with Google Health API access for real integration (not needed for development/tests)
- For webhooks: a domain/public HTTPS URL that routes to `/webhooks/google-health`

## Docker setup

Copy `.env.example` to the ignored `.env` and set a strong `POSTGRES_PASSWORD` and an
`APP_UI_TOKEN` (32+ characters); Compose refuses to start without either.

The base Compose file sets `APP_ENV=production` for the app service (overriding `.env`), so the
application refuses to start with missing or placeholder production values: `APP_UI_TOKEN`, Google
OAuth client, `GOOGLE_TOKEN_ENCRYPTION_KEY`, an HTTPS `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` and
`GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION`. The full production setup is in [DEPLOYMENT.md](DEPLOYMENT.md).

To try the stack before you have Google credentials, add the development override, which sets
`APP_ENV=development` and also publishes PostgreSQL on `127.0.0.1:5432`:

```sh
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/health/live
```

The app runs `alembic upgrade head` before startup. Compose marks the app healthy once
`/health/live` answers; `/health` additionally queries PostgreSQL and needs the UI token
(`curl -H "Authorization: Bearer $APP_UI_TOKEN" http://127.0.0.1:8000/health`).

## Native development

**Cross-platform command:**

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev alembic upgrade head
uv run --frozen --extra dev uvicorn app.main:app --reload --host 127.0.0.1 --no-access-log
```

Useful checks:

**Verification-only command:**

```sh
uv lock --check
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev alembic current
```

For a destructive-to-test-data-only end-to-end Compose smoke test, run:

**Verification-only command — destructive only to its uniquely named temporary test volume:**

```sh
python scripts/verify_compose.py
# or: make compose-smoke
```

The script uses a uniquely named Compose project prefixed `health-mirror-smoke-`, a temporary env
file derived from `.env.example` with synthetic non-placeholder values (the app runs with
`APP_ENV=production`), a temporary password, UI bearer token, backup directory and throwaway backup key, and host
port `18080`. It builds the production image, waits for PostgreSQL and FastAPI, exercises `/health`
and the installed CLI, takes an encrypted backup with `scripts/backup.sh` (age if installed,
otherwise gpg), restores it with `scripts/restore.sh` into a second database and checks the row
counts, verifies the PostgreSQL Alembic downgrade/upgrade
lifecycle, checks the hardened container and private bindings, then removes only those isolated
resources. It does not read or modify `.env`, the normal `postgres_data` volume, or Google
credentials. Keep live-account verification evidence private; it can contain personal health data.

Deployment, routine operations, image maintenance, and Cloudflare setup are split into the
[deployment](DEPLOYMENT.md), [runbook](RUNBOOK.md), [image update](IMAGE_UPDATES.md), and
[Cloudflare Tunnel](CLOUDFLARE_TUNNEL.md) guides. Notes for Windows hosts and the earlier
Windows-to-Linux move are in [legacy/](legacy/WINDOWS.md).

## Configuration

Copy `.env.example` to `.env`; never commit `.env`.

| Variable | Purpose |
|---|---|
| `APP_UI_TOKEN` | Single-user access key for the UI and API (32–512 printable characters). Required in production; empty disables the login outside production. Sent as `Authorization: Bearer …` or exchanged for a cookie at `/login` |
| `DATABASE_URL` | Async SQLAlchemy PostgreSQL URL |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth web client; leave blank until integration |
| `GOOGLE_REDIRECT_URI` | Must exactly match the authorized redirect URI |
| `GOOGLE_REFRESH_TOKEN` | Optional environment-only refresh token alternative |
| `GOOGLE_TOKEN_ENCRYPTION_KEY` | Fernet key used to encrypt callback-obtained refresh tokens |
| `GOOGLE_API_MIN_REQUEST_INTERVAL_SECONDS` | Shared request pacing interval; defaults to `0.25` (4 requests/second) |
| `GOOGLE_CLOUD_PROJECT_NUMBER` | Numeric Google Cloud project number used by subscriber APIs; not the project ID |
| `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` | Public self-hosted HTTPS receiver URL ending in `/webhooks/google-health` |
| `GOOGLE_HEALTH_WEBHOOK_SUBSCRIBER_ID` | Stable 4–36 character subscriber ID; defaults to `personal-health-mirror` |
| `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` | Exact secret header value configured for the subscriber, e.g. `Bearer …` |
| `GOOGLE_HEALTH_WEBHOOK_PUBLIC_KEY_URL` | Google's permanent public signing keyset URL |
| `WEBHOOK_HOST` | Public DNS hostname used by the optional self-hosted Caddy TLS profile |
| `BACKUP_HOST_DIR`, `BACKUP_RETENTION_DAYS`, `BACKUP_MIN_FREE_MB` | Host directory for encrypted dumps; opt-in retention in days (default: empty, keep forever; only a positive whole number deletes, anything else keeps everything; set in the environment, even empty, it overrides `.env`); free-space guard in MiB (default: twice the newest dump, at least 1 GiB, or 5 GiB before the first dump); read by `scripts/backup.sh` |
| `BACKFILL_START_DATE`, `BACKFILL_WINDOW_DAYS` | Earliest attempted history and checkpoint window size |
| `GOOGLE_API_PAGE_SIZE`, `GOOGLE_API_MAX_RETRIES` | Page and exponential retry policy |
| `EXTRA_DATA_TYPES` | Comma-separated evolving v4 data types; defaults to interval filtering |
| `ENABLED_DATA_TYPES` | Optional comma-separated allowlist; blank enables the complete built-in registry |
| `RECONCILIATION_*` | Daily/weekly schedule and lookback lengths |
| `WATCHDOG_ENABLED`, `WATCHDOG_INTERVAL_SECONDS` | Enable local pipeline checks; defaults to every 15 minutes |
| `PIPELINE_STALE_THRESHOLD_MINUTES` | Age at which frequently updated sync streams are stale; defaults to 120 minutes |
| `STARTUP_GAP_THRESHOLD_MINUTES` | Minimum persisted sync/write gap that permits startup recovery; defaults to 120 minutes |
| `AUTOMATIC_RECOVERY_ENABLED`, `AUTOMATIC_RECOVERY_MAX_DAYS` | Enable bounded reconciliation recovery and cap its lookback; defaults to 7 days |
| `RECOVERY_COOLDOWN_MINUTES` | Minimum time between automatic recovery attempts; defaults to 360 minutes |
| `CODEX_BRIDGE_BIND_HOST`, `CODEX_BRIDGE_URL`, `CODEX_BRIDGE_TOKEN` | Optional Umay bridge address and token; the launcher writes the URL and token |

Generate an encryption key locally and put it only in `.env`.

**Cross-platform command (prints a new secret; store it immediately):**

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

## OAuth and Google Health setup

Do this only after local tests pass. Exact Google Console labels can evolve; follow the current [Google Health Cloud/OAuth setup](https://developers.google.com/health/setup) and [Google OAuth web-server documentation](https://developers.google.com/identity/protocols/oauth2/web-server).

1. Confirm Google Health API access is available to your project, then create or select the project and enable the API. As of October 3, 2026, Google states that new projects are not being onboarded; see the [official setup guide](https://developers.google.com/health/setup).
2. In **Google Auth Platform > Audience**, initially leave the personal integration in Testing and add the Google account used by Fitbit as a test user.
3. In **Google Auth Platform > Data Access**, add the read-only Google Health scopes required by the enabled data types. With the default registry these are `googlehealth.activity_and_fitness.readonly`, `googlehealth.health_metrics_and_measurements.readonly`, `googlehealth.sleep.readonly`, `googlehealth.nutrition.readonly`, `googlehealth.ecg.readonly`, `googlehealth.irn.readonly`, and `googlehealth.settings.readonly`. Set `ENABLED_DATA_TYPES` first if you want a smaller consent surface.
4. Create an OAuth 2.0 **Web application** client.
5. Add the exact authorized redirect URI, initially `http://localhost:8000/auth/google/callback`. No JavaScript origin is needed for this server-side flow.
6. Copy `.env.example` to the untracked `.env`. Put only `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and a newly generated `GOOGLE_TOKEN_ENCRYPTION_KEY` there; never send these values in chat.
7. Restart the app and visit `http://localhost:8000/auth/google/start` while signed into the same Google account used by the Fitbit app. Perform the capability probe and small `steps` sync below while still in Testing.
8. Before relying on unattended synchronization, move the OAuth audience to **In production** under Google's applicable personal-use exception, or complete any verification Google requires for your configuration. Repeat the OAuth route afterward so the mirror stores a production-mode refresh token. Google documents that Testing-mode refresh tokens expire after seven days.

The app requests only the read-only scopes represented by its configured data types, with `access_type=offline`. The callback encrypts the refresh token, calls `users/me/identity`, and stores the stable Health user mapping. If Google later rotates the refresh token during an access-token refresh, the replacement is encrypted and committed before it is used on the next restart. Do not paste credentials into source files, issues, logs, or chat.

Before writing any health records, run the metadata-only capability probe. It requests at most one recent point per configured type and reports accessibility, timestamps, and top-level field names without returning or storing health values.

**Verification-only command:**

```bash
health-mirror probe --days 30
curl -H "Authorization: Bearer $APP_UI_TOKEN" 'http://127.0.0.1:8000/google-health/capabilities?days=30'
curl -H "Authorization: Bearer $APP_UI_TOKEN" http://127.0.0.1:8000/google-health/devices
```

Run `health-mirror` inside the container (`docker compose exec app health-mirror …`) or in the
native environment. Every HTTP route except `/health/live`, `/login`, the OAuth callback and the
webhook needs the UI token: `Authorization: Bearer <APP_UI_TOKEN>`, or the login cookie in a
browser. The cookie names a random session issued at login and held in memory for at most 30 days;
`POST /logout` (same-origin form, the Sistem panel's **Çıkış yap**) revokes it, and an app restart
ends all sessions. See [DEPLOYMENT.md](DEPLOYMENT.md) for opening the UI as `healthos.localhost`.

Use this to confirm the account is linked, Fitbit data is visible, and the configured scopes cover the intended types. Then verify a small persisted sync before starting the full backup.

**Writes retrieved records to the configured database:**

```bash
health-mirror sync steps
health-mirror status
```

## Historical backfill

**Starts a persisted backfill through the local API:**

```bash
curl -X POST -H "Authorization: Bearer $APP_UI_TOKEN" -H 'X-HealthOS-Request: 1' \
  'http://127.0.0.1:8000/backfill/start?data_type=steps'
curl -H "Authorization: Bearer $APP_UI_TOKEN" http://127.0.0.1:8000/backfill/status
```

**Starts a persisted backfill and waits:**

```bash
health-mirror backfill --data-type steps
health-mirror backfill
health-mirror backfill --reset-abandoned   # requeue abandoned windows, then run
```

The default attempts history from 2010 in 31-day windows. Empty windows are valid and become completed checkpoints. Recent windows run first. On interruption, rerunning resumes the saved window and `pageToken`; completed windows are skipped. A window that keeps failing is moved behind the others after five failures and marked `abandoned` after ten, so it cannot stall its data type; see the [runbook](RUNBOOK.md#abandoned-backfill-windows). Use a conservative start date to maximize completeness—the official API permits queries back to the earliest recorded data.

The `food` and `food-measurement-unit` resource catalogs remain discoverable but are not included in automatic mirror jobs. Google returns the global public food reference catalog through these unfiltered types; it is not personal health history. Personal `nutrition-log` records remain fully mirrored. Any reference rows already fetched remain preserved in the raw archive.

## Incremental sync

**Writes retrieved records to the configured database:**

```bash
health-mirror sync sleep
health-mirror sync
curl -X POST -H "Authorization: Bearer $APP_UI_TOKEN" -H 'X-HealthOS-Request: 1' http://127.0.0.1:8000/sync/sleep
curl -X POST -H "Authorization: Bearer $APP_UI_TOKEN" -H 'X-HealthOS-Request: 1' http://127.0.0.1:8000/sync/all
curl -H "Authorization: Bearer $APP_UI_TOKEN" http://127.0.0.1:8000/sync/status
```

Default overlaps are 60 minutes for heart rate, 12 hours for steps/activity streams, 72 hours for sleep/temperature derivations, seven days for daily metrics, and 24 hours otherwise. UPSERT makes every replay safe; a changed payload keeps the previous one as a [version](#raw-record-versions).

### Source deletions

Google's list responses carry no tombstones; a deleted record simply stops appearing. When a window
was fetched from its first page to its last (an incremental run, a webhook re-fetch, or a
reconciliation), archived records that the same filter must have matched but that were not returned
get `source_deleted_at`. Nothing is deleted; dashboards and Umay skip marked records, and a record
that comes back is unmarked. The marked range is derived from exactly the bounds sent to Google
(UTC, whole seconds); rollup and reconcile types (aggregates) and catalogs without a time filter are
never marked.

- Session filters use civil start times (sleep: physical end time, exclusive). A webhook re-fetch
  of one sleep or exercise session therefore widens its window (sleep: one second past the
  session's end; other sessions: 12 hours before and 14 hours after, the range of UTC offsets) so
  the deleted session is inside both the query and the marked range. Outside webhooks, a
  scheduled window only marks sessions that started at least 12 hours after its start and 14 hours
  before its end; the rest is covered by the daily 7-day reconciliation.
- A record whose time was moved upstream out of the window that is re-fetched looks deleted from
  that window and is marked until a fetch covering its new time returns it again (and unmarks it).
  For a move of more than 7 days that is the weekly 90-day reconciliation.

## Raw record versions

Google re-sends records with a different payload when it recomputes them (HRV, calories, heart-rate
zones and resting heart rate often change for a day or two). A record is identified by
`(data_type, google_data_point_id)`; when a fetch brings a payload whose `payload_hash` differs
from the stored one, the old payload is **kept as a version**, never overwritten:

- `raw_health_data` holds the current version of every record; every reader (dashboards, Umay,
  normalized tables, `analytics_*` views) keeps using it. `payload_received_at` and
  `payload_source` (`sync`, `webhook`, `reconciliation`, `backfill`) say when and how the current
  payload arrived; both are NULL for payloads stored before migration `20261020_0011`.
- `raw_health_data_versions` holds every earlier payload, numbered per record (1 = oldest), with
  `received_at` (NULL if unknown), `last_seen_at` (the last fetch that still returned it),
  `superseded_at` (the fetch that replaced it), `source` (as above, `gap-repair` for
  `scripts/audit_date_gaps.py --repair`, `legacy` for pre-0011 payloads, `archive-import` for
  payloads recovered from old dumps) and `raw_id` (the current row). A database trigger writes the
  row in the same statement that replaces the payload, whenever `payload_hash` **or** the stored
  text (`raw_json::text`) changes, so a crash, two concurrent writers or a hand-written `UPDATE`
  cannot lose it. Re-receiving the same payload writes nothing. The table is append-only: triggers
  refuse `UPDATE`, `DELETE` and `TRUNCATE`; the one exception is renumbering `version`, which
  PostgreSQL allows only inside a transaction that set `healthos.renumber = '1'` (only
  `health-mirror import-versions` does). `raw_json` is compared as text, so even `1.0` for `1`
  counts as a change.
- **No record is ever deleted.** Triggers on `raw_health_data` refuse `DELETE` (with or without
  history), `TRUNCATE` (also when reached through `TRUNCATE ... CASCADE` from a referencing table)
  and any change of a record's identity (`id`, `data_type`, `google_data_point_id`,
  `first_seen_at`, `created_at`). Every protective trigger is `ENABLE ALWAYS`, so
  `session_replication_role = replica` does not bypass it either, and `health-mirror verify`
  fails if one is missing, disabled, or its function body was replaced. What a trigger cannot
  stop is a superuser's DDL (`DROP TABLE`, `ALTER TABLE ... DISABLE TRIGGER`); `verify` detects
  the latter, and backups (see below) cover the former.
- Writers lock rows in key order (each page's rows are sorted before the UPSERT), so two
  overlapping pages written at the same time wait for each other instead of deadlocking.
- `source_start_time`, `source_end_time` and `source_timezone` are derived from `raw_json` by the
  parser; they are copied into each version but a recomputation alone (same payload) does not
  create one.
- A record that appears more than once in one page with different payloads becomes one version
  per payload, in page order.
- Normalized tables stay "current state": they are derived from the current raw payload and can be
  rebuilt from `raw_health_data`; the earlier payloads are only in the raw history.
- A record without history rows is version 1, so the migration copied nothing: the existing
  archive is not stored twice and the history grows only by payloads that really changed.

The view **`raw_health_data_history`** lists every version of every record: the history rows plus
the current row, with `version`, `is_current`, `valid_from` (when the version arrived; NULL if
unknown), `valid_to` (when it was replaced; NULL for the current version and for imported versions
whose replacement time is unknown), `last_seen_at`, `source`, `payload_hash` and `raw_json`. Filter
it by `data_type` and `google_data_point_id` (both indexed); for "what changed recently" query
`raw_health_data_versions` directly.

```sql
-- Bir kaydın tüm sürümleri (all versions of one record), oldest first
SELECT version, is_current, valid_from, valid_to, source, payload_hash, raw_json
FROM raw_health_data_history
WHERE data_type = 'heart-rate-variability'
  AND google_data_point_id = 'users/me/dataTypes/heart-rate-variability/dataPoints/…'
ORDER BY version;

-- Son 7 günde değişen kayıtlar (records replaced in the last 7 days)
SELECT v.data_type, v.google_data_point_id, v.version AS replaced_version,
       v.superseded_at, v.payload_hash AS old_hash, r.payload_hash AS current_hash
FROM raw_health_data_versions v
JOIN raw_health_data r ON r.id = v.raw_id
WHERE v.superseded_at >= now() - interval '7 days'
ORDER BY v.superseded_at DESC;

-- Veri türüne göre değişiklik sayısı (changes per data type)
SELECT data_type, count(*) AS earlier_versions, count(DISTINCT raw_id) AS changed_records,
       max(superseded_at) AS last_change
FROM raw_health_data_versions
GROUP BY data_type
ORDER BY earlier_versions DESC;
```

The same from the command line (JSON, oldest version first; no secrets, payloads are included
unless `--no-payload`):

```sh
health-mirror history --data-type heart-rate-variability                 # 20 most recently changed records
health-mirror history --data-type heart-rate-variability --since 2026-10-13 --limit 100
health-mirror history --data-type resting-heart-rate --id 'users/me/dataTypes/…' --no-payload
```

### Recovering versions from old dumps

Before migration `20261020_0011` a changed payload overwrote the stored one; those versions only
survive in older backups. `health-mirror import-versions --from-db <database>` reads a restored
dump (any revision, including `json`-typed `raw_json` from before `20261020_0010`) and merges each
record's dump rows into its timeline by `last_seen_at`:

- A dump payload is *already present* only when it lands next to the same payload in that
  timeline. A payload that left and came back (A → B → A) therefore stays three versions; it is
  never deduplicated against an older, non-adjacent version.
- New payloads become versions with `source = 'archive-import'` and `superseded_at` set (the
  dump's own value when it has one, otherwise the payload's last sighting), so
  `history --since` lists them. Existing history rows keep their order and are only renumbered.
- A record the live archive does not have at all is **imported as its own row** (`payload_source =
  'archive-import'`, `source_deleted_at` set to the dump's value or the import time, so dashboards
  skip it), its latest dump payload as current version and earlier ones as history. Nothing that
  ever existed is lost. Live current rows are never changed; an import-restored row may take a
  newer payload from a later dump (the trigger keeps the old one).
- Rows whose stored hash does not match their payload (`source_hash_mismatches`) and payloads
  jsonb cannot store unchanged (`unrepresentable`: a float written as `1e+16`, `-0.0`, a `\u0000`
  escape) are skipped and listed, without aborting the rest; `--dry-run` reports both too, and the
  command exits 1 so the dump is kept.

Running it again adds nothing. Dumps are lossless when imported **newest first** (an older
sighting then always lands at the old end of a timeline); `--from-db` may be repeated and the
command sorts the dumps that way itself. `--from-db` takes a database name on the live server or a
full SQLAlchemy URL. Procedure: [RUNBOOK](RUNBOOK.md#import-payload-versions-from-archive-dumps).

## What is preserved where

Nothing received from Google, and nothing entered by hand, is deleted or overwritten. Storage cost
is not a concern; every guard below is a database trigger, so it holds for every code path, the
CLI and hand-written SQL alike (`health-mirror verify` checks that the guards are in place).

| What | Where | Guarantee |
|---|---|---|
| Exact HTTP response bodies from the Google Health API | `raw_api_responses` | append-only (no `UPDATE`/`DELETE`/`TRUNCATE`); SHA-256 checked by `verify` |
| Current payload of every record | `raw_health_data` | no `DELETE`/`TRUNCATE`/identity change; a changed payload (hash or text) is kept as a version first |
| Every earlier payload | `raw_health_data_versions` | append-only (renumbering only during an archive import) |
| Source-deletion marks set and cleared | `raw_health_data_events` | append-only, written by trigger |
| Signed webhook bodies | `webhook_events.raw_body` | the exact bytes Google posted; `payload_hash` is their SHA-256, checked by `verify` |
| Webhook batches and elements | `webhook_events`, `webhook_notifications`, `webhook_batch_items` | no `DELETE`/`TRUNCATE`; the received payload (`payload_hash`, `raw_json`, a set `raw_body`) is frozen, processing state is not; batch items append-only |
| Manual measurements and workouts | `manual_measurements`, `manual_workouts` | append-only: the UI only adds entries; a correction is a new entry (for the same measurement time the newest entry wins) |

### Verbatim Google responses

`raw_health_data.raw_json` is the *parsed* record (jsonb normalizes spacing, key order and some
number spellings). What Google actually sent is kept separately: every HTTP response the client
receives from the Google Health API, one row per exchange, in **`raw_api_responses`**:

- `body`: the body bytes exactly as they came off the wire. The client asks for
  `Accept-Encoding: identity`; should a response arrive compressed anyway, the compressed bytes
  are stored, its `content-encoding` header is kept (and shown as `content_encoding` by
  `health-mirror responses` / `export-response`), and only the parser sees the decoded JSON.
  PostgreSQL's TOAST compresses the column losslessly on disk. `body_sha256` and `body_size` are
  over exactly the stored bytes; `complete = false` and `error` (e.g. `ReadTimeout`) when the
  connection broke mid-body: what arrived is still stored. `export-response` writes the stored
  bytes unchanged (pipe a `gzip` one through `gzip -dc` to read it).
- The request: `method`, `path`, `query` (the query string as sent, including `pageToken`, a
  pagination cursor) and `request_body` (the JSON of rollUp requests). Request headers are never
  stored, so neither is the access token; a credential-like query parameter would be dropped.
- `status_code` and the response headers `content-type`, `content-encoding`, `content-length`,
  `date`, `retry-after`; `attempt` (0 for the first exchange, retries and token refreshes count
  up), `fetched_at`.
- Provenance: `source` (`sync`, `webhook`, `reconciliation`, `backfill`, `gap-repair`, `probe`,
  `identity`, `devices`), `context_id` (`sync:<uuid>` per run, `webhook:<element hash>` (the
  `webhook_notifications` row), `reconciliation:<uuid>`, `backfill:<job id>`, `probe:<uuid>`) and
  `data_type`.

Error responses (4xx/5xx, including the 503s that are retried) and the probe's, identity and
paired-device responses are stored too. A data page is stored **in the same transaction as its
records**: a crash keeps both or neither, and each new or changed payload in `raw_health_data`
and `raw_health_data_versions` carries `response_id`, the page it was first read from
(`raw_health_data_history` shows it). If storing the records fails, the responses are still kept
in a transaction of their own; also when the task is cancelled (a restart or deploy between
fetching and storing, or during a retry's back-off): the responses already received are then
stored, shielded and for at most 10 seconds, before the cancellation goes on. Not stored: requests that never got a response (connection
refused, timeout before headers), OAuth token exchanges with Google's OAuth server (they carry
credentials), and webhook calls that fail authentication or the signature check (untrusted).

Measured cost: about 4–9 ms more per 1,000-record page (≈ 6–15 %) for inserting the page body in
the same transaction; on disk, the bodies roughly match the raw records themselves, plus every
re-fetch of an overlap window or reconciliation adds its page again (check with
`SELECT pg_size_pretty(pg_total_relation_size('raw_api_responses'))`).

```sh
health-mirror responses --data-type heart-rate --since 2026-10-20 --limit 5   # metadata, newest first
health-mirror responses --context webhook:<element hash>
health-mirror export-response 12345 --output page.json   # the exact bytes; metadata on stderr
sha256sum page.json                                      # equals body_sha256
```

`export-response` refuses (exit 1) when the stored bytes no longer match their SHA-256.

### Source deletion events

`source_deleted_at` on a record is its current state. Every change of it is also appended, by
trigger, to **`raw_health_data_events`**: `source-deleted` (marked, with the mark's time, also when
an archive import restores a record as deleted) and `source-returned` (with the fetch that saw it
again and `previous_deleted_at`, the mark being cleared). A record that disappears and comes back
therefore keeps its whole timeline; `health-mirror history` lists the events next to the
versions.

### Write paths (audit)

Every `UPDATE`, UPSERT and `DELETE` in `app/`, `scripts/` and `migrations/` that touches a table
above, and why it is allowed:

| Write | Table | Verdict |
|---|---|---|
| `store_records` UPSERT (`app/services/storage.py`) | `raw_health_data` | payload change → kept as a version by trigger first; `last_seen_at`, `source_deleted_at` → `NULL` (logged as an event), `payload_received_at`/`payload_source`/`response_id` change only with the payload; derived `source_*_time`/`source_timezone` |
| `mark_source_deletions` (`app/sync/deletions.py`) | `raw_health_data` | sets `source_deleted_at` only (logged as an event); nothing deleted |
| import-restored row takes a newer dump payload (`app/services/raw_history.py`) | `raw_health_data` | only rows the import itself created; the old payload is kept as a version by trigger |
| version renumbering (`import-versions`) | `raw_health_data_versions` | `version` only, in a transaction with `healthos.renumber = '1'` |
| quarantine of rollup records (migration `0002`, applied long ago) | `raw_health_data` | flag columns only, payload untouched |
| batch/element processing (`app/services/webhook_processing.py`) | `webhook_events`, `webhook_notifications` | operational: `status`, counters, `last_error`, `processed_at`, `failure_count`, `element_count`, filling a missing `raw_body`/`signature_verified_at` once; payload frozen by trigger |
| `signature_verified_at` backfill (migration `0003`) | `webhook_events` | operational, fills a NULL |
| sync state, backfill jobs, reliability state, appearance, OAuth token rotation | `sync_state`, `backfill_jobs`, `reliability_state`, `appearance_settings`, `oauth_tokens` | operational metadata, not received data |
| normalized UPSERTs | `heart_rate`, `steps`, … | derived, see below |
| `downgrade()` of migrations | — | refuses while versions, responses or events exist (`-x allow-version-loss=1` after a backup) |

Outside the database: `scripts/restore.sh` drops and recreates only its scratch target (never
`health`), and `scripts/backup.sh` prunes old dump *files* by its retention setting (see
[Database backup and restore](#database-backup-and-restore)).

### Derived tables

Normalized tables (`heart_rate`, `steps`, `sleep_sessions`, `scalar_metrics`, …) and the
`analytics_*` views are **derived** from the current raw payloads and can be rebuilt from
`raw_health_data` at any time, so their UPSERTs overwrite in place and that is acceptable: no
received data lives only there.

## Webhooks

Configure a Google Health subscriber with:

- `endpointUri`: `https://your-domain.example/webhooks/google-health`
- `endpointAuthorization.secret`: an unpredictable exact value also set as `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION`
- automatic subscription policies for the data types you consented to
- the **Google Cloud project number**, not its textual project ID

The endpoint rejects an unauthenticated verification challenge, accepts the authenticated
verification body, then requires both the authorization secret and Google's ECDSA signature for
notifications. Google may send either one notification object or an array containing many
notifications. The signed request is preserved as an immutable batch, future receipts retain the
exact raw request bytes, and every array element gets independent durable extraction, authorized
health-user validation, global content-hash deduplication, processing state, and retry history.
Returning HTTP 204 proves durable acceptance only; `processed_at` is set after every valid element
in the batch completes. A failed element remains retryable without re-running successful elements.
The documented deployment has one subscriber, `personal-health-mirror`, registered using
short-lived service-account impersonation; no service-account key is stored. The project number is
deployment-secret metadata and is not repeated in tracked documentation.

Run `health-mirror webhook-readiness` before subscriber registration. It reports only booleans and
never prints the endpoint authorization value. Registration is ready only when the project number
is numeric, the endpoint uses HTTPS, the authorization value includes a `Bearer` or `Basic` scheme,
and the subscriber ID matches Google's naming rules.

### Cloudflare Tunnel receiver (deployed)

The deployment uses a host-level `cloudflared` service on the VM. It makes outbound-only
connections, so the router has no inbound port forwarding. The tunnel route must match
`${CLOUDFLARE_WEBHOOK_HOSTNAME}` and path `^/webhooks/google-health$`, forward to
`http://127.0.0.1:8000`, and use a final catch-all 404 rule. Only the tunnel-scoped credential
file lives on the VM; no account-wide `cert.pem` or router fallback is used.

The app remains bound to loopback, PostgreSQL has no published port, and the path rule prevents
public access to `/health`, `/docs`, OAuth, sync, backfill, or Google Health metadata routes.
Setup and the public-surface checks are in [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md). Local
status:

**Verification-only command:**

```sh
systemctl status cloudflared
docker compose ps
curl --fail http://127.0.0.1:8000/health/live
docker compose exec -T app health-mirror status
docker compose exec -T app health-mirror webhook-readiness
docker compose exec -T app health-mirror webhook-status
```

If durable elements remain failed after a transient outage, retry only pending webhook work with:

**Operates only on already verified stored webhook batches:**

```sh
docker compose exec -T app health-mirror webhook-replay-pending
```

This command does not fabricate a webhook, change health data, or ask Google to resend anything.
It reuses already verified, stored batches and the normal read-only Google Health query path.
A live re-delivery of a signed body that was first verified more than 24 hours earlier is accepted
(HTTP 204) but not processed again (`WEBHOOK_REPLAY_IGNORED`); the operator command above is not
limited.

`cloudflared` runs with `--no-autoupdate`; update it deliberately (see the
[runbook](RUNBOOK.md#update-images-and-cloudflared)). Google retries failed webhook notifications
for up to seven days; daily and weekly reconciliation remain the recovery layer for longer outages.
A diagnosis checklist is in the [runbook](RUNBOOK.md#the-webhook-went-quiet).

### Direct Caddy receiver (unused alternative)

The backend validates `Host` for HTTP and WebSocket requests. Private routes accept only
`localhost`, `127.0.0.1`, or `[::1]`, with optional ports. The HTTPS hostname in
`GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` additionally accepts only POST requests to
`/webhooks/google-health`; webhook authorization and signature checks still apply.
Proxies must preserve the original Host header. `APP_HOST` controls listening only and does
not grant access to additional hostnames.

Webhook authorization is checked before reading the body. Payloads exceeding
`GOOGLE_HEALTH_WEBHOOK_MAX_BODY_BYTES` (default 2 MiB, configurable from 1 KiB to 16 MiB)
return 413, including chunked requests and verification payloads.

Job-start POST routes (`/sync/all`, `/sync/{data_type}`, `/backfill/start`) require
`X-HealthOS-Request: 1`. Browser requests must also have a same-origin Origin when supplied;
cross-site requests are rejected with 403. The UI includes the header automatically.
Command-line clients can omit Origin but must send the explicit header. Keep CORS disabled
for these local routes; the custom header prevents cross-site forms and forces a browser
preflight for cross-origin fetches.

The optional `webhook` Compose profile runs Caddy on the same host and forwards only
`/webhooks/google-health` to the private app container; every other public path returns 404.
Caddy obtains and renews a public TLS certificate without a paid cloud service.

1. Choose a DNS hostname (an owned domain or a free dynamic-DNS hostname) and point it directly to
   this host's public IP.
2. Confirm the connection is not behind carrier-grade NAT, then forward router TCP ports 80 and 443
   to this host. UDP 443 is optional and enables HTTP/3.
3. Set `WEBHOOK_HOST` to the hostname and set
   `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI=https://<hostname>/webhooks/google-health` in `.env`.
4. Start the profile with `docker compose --profile webhook up -d caddy` and verify both Google's
   authenticated-success and unauthenticated-rejection handshakes before registering the subscriber.

This alternative is direct self-hosting: health webhook traffic is not routed through a tunnel or hosted proxy.
If the internet connection uses carrier-grade NAT or blocks inbound TCP 80/443, obtain a public IP
or explicitly choose another privacy-compatible ingress method before registration.

## Reconciliation

Defaults:

- Daily at 02:00 UTC: re-read the last 7 days.
- Sunday at 03:00 UTC: re-read the last 90 days.

These use normal idempotent UPSERTs. Disable in `.env` when another scheduler instance owns the jobs.
Run either depth manually with `health-mirror reconcile --days 7` or
`health-mirror reconcile --days 90`.

### Outage detection and bounded automatic recovery

The app checks persisted pipeline state at startup and every `WATCHDOG_INTERVAL_SECONDS`. Freshness
is based on the latest successful sync for frequently updated streams (`heart-rate`,
`activity-level`, `steps`, `distance`, `active-energy-burned`, `total-calories`, and
`active-minutes`). Low-frequency datasets such as sleep or oxygen saturation do not make the whole
pipeline stale merely because the source has not produced a recent record. If sync state is absent
after an existing installation, startup can use the latest raw database write as its fallback
reference; an entirely empty database does not trigger recovery automatically.

At startup, the oldest successful watermark among the frequent streams is used so one freshly
webhook-synchronized stream cannot mask an outage gap in another. A gap of at least
`STARTUP_GAP_THRESHOLD_MINUTES` permits recovery. During normal
operation, automatic recovery runs only when all frequent streams are stale. It invokes the same
read-only, raw-first reconciliation and UPSERT path as scheduled/manual reconciliation, choosing
the smallest whole-day lookback that covers the detected gap and never exceeding
`AUTOMATIC_RECOVERY_MAX_DAYS`.

Recovery is guarded by an in-process lock, a PostgreSQL advisory lock, checks for active syncs, the
reconciliation service's own lock, and a persisted cooldown. Consequently, multiple app instances
or repeated health checks cannot start duplicate reconciliations. A failed recovery is recorded for
diagnosis and waits for the cooldown; it does not loop continuously, reset watermarks, delete data,
or change the Google subscriber. Set `AUTOMATIC_RECOVERY_ENABLED=false` to retain diagnostics while
disabling automatic reconciliation.

`GET /health` is the machine-readable status surface. It returns HTTP 503 only when PostgreSQL
cannot be queried, allowing Docker to restart a genuinely unusable app without creating restart
loops for upstream staleness. Otherwise it returns HTTP 200 with `status` equal to `healthy`,
`degraded`, `stale`, or `failed`, plus database/scheduler/configuration state, webhook and sync
timestamps, pending/failed counts, stale stream names, reasons, latest raw write, last successful
reconciliation, and per-stream source freshness. `stale` means all frequent streams crossed the
configured age; `degraded` covers partial staleness, backlog, incomplete configuration, or a stopped
scheduler; `failed` reports unresolved failed webhook or sync state.

After a host interruption the expected recovery sequence is:

1. The VM boots and systemd starts `docker.service` and `cloudflared.service`.
2. Docker starts the existing `postgres` and `app` containers under `unless-stopped`.
3. Compose waits for PostgreSQL's `pg_isready` health check; the app then runs migrations and its
   own database readiness query before starting the scheduler.
4. The startup watchdog assesses the persisted gap and, when eligible, starts one bounded
   reconciliation. `cloudflared` forwards webhooks again as soon as port 8000 answers; until then
   Cloudflare returns 502 and Google retries.
5. The optional Umay bridge is a host process without a service unit; start it again by hand.

A sync state or webhook element left `running` by a process that died is failed (and so becomes
retryable) at startup, but only once nothing has touched it for 15 minutes: a running fetch refreshes
its sync state on every page, so a fresher row may be a `health-mirror sync` or webhook replay still
working in another container. Rows that were fresh at startup are re-checked once the 15 minutes
have passed, and the watchdog likewise ignores `running` rows without a recent heartbeat.

If Docker itself is down, no in-container watchdog can notice. That host-level condition is visible
as a Cloudflare 502 on the webhook and must be handled by host monitoring.

## Local API

- `GET /health/live` (no database, no token; used by the container health check)
- `GET /health`
- `GET /login`, `POST /login` (exchange `APP_UI_TOKEN` for a session cookie)
- `GET /sync/status`
- `GET /sync/status/{data_type}`
- `POST /sync/{data_type}`
- `POST /sync/all`
- `POST /backfill/start`
- `GET /backfill/status`
- `POST /webhooks/google-health`
- `GET /auth/google/start` and `GET /auth/google/callback`
- `GET /google-health/capabilities` (metadata-only one-record probes)
- `GET /google-health/devices` (sanitized paired-device metadata)

Interactive OpenAPI documentation (`/docs`, `/redoc`, `/openapi.json`) is served only outside
production (`APP_ENV` other than `production`, e.g. with `docker-compose.dev.yml` or `make run`):
its page needs inline scripts and the jsDelivr CDN, an exception the login-cookie origin should not
carry in production. Every route except
`/health/live`, the login page, the OAuth callback and the webhook requires the UI token.

## Database backup and restore

Daily backups are automated by [`deploy/systemd/healthos-backup.timer`](../deploy/systemd/healthos-backup.timer)
(03:30 local time, `Persistent=true` so a missed run happens after boot), which starts
[`deploy/systemd/healthos-backup.service`](../deploy/systemd/healthos-backup.service). The service
runs [`scripts/backup.sh`](../scripts/backup.sh) on the Docker host:

1. `docker compose --profile backup run --rm -T backup` streams a compressed custom-format
   `pg_dump` to stdout (the container refuses to write a dump to a terminal);
2. the stream is encrypted before it reaches the disk and written atomically as
   `BACKUP_HOST_DIR/health-<UTC stamp>.dump.age` (or `.dump.gpg`);
3. the result is written to `BACKUP_HOST_DIR/last-backup-status.json`, on success and on failure.

**Backups are kept forever.** The script deletes nothing unless `BACKUP_RETENTION_DAYS` is
explicitly set to a positive whole number of days; then, and only after a successful new backup,
`health-*` dumps older than that are removed. Anything else deletes nothing: unset, empty, `0`,
`never`, a negative number or any other text (that last one is reported on stderr, and the backup
still runs). The environment wins over `.env` whenever the variable is set there, even to an empty
value; `.env` is read only when it is not. A large value such as `36500` also works.

**Free space.** Before dumping, the script checks the free space of `BACKUP_HOST_DIR`. If it is
below `BACKUP_MIN_FREE_MB` (default: twice the newest dump, at least 1 GiB; 5 GiB before the first
dump), it refuses to start with exit status 1, writes no file and deletes nothing; systemd marks the
unit failed. After each successful backup it prints the remaining free space. A dump interrupted by
a full disk or an error leaves no partial file. To make room, add disk or move old backups (see the
[runbook](RUNBOOK.md#disk-space-for-backups)); `BACKUP_MIN_FREE_MB=0` disables the guard.

**Status file.** `last-backup-status.json` holds `timestamp` (UTC), `result` (`success` or
`failure`), `message`, `file`, `size_bytes`, `free_bytes`, `required_free_bytes`, `retention_days`
and `pruned_files`. It is replaced atomically on every run, so a stale `timestamp` means the timer
is not running. A failed run also starts
[`healthos-backup-failure.service`](../deploy/systemd/healthos-backup-failure.service) via
`OnFailure=`, which logs at priority `crit` (`journalctl -p crit -t healthos-backup`) and
broadcasts with `wall`; nothing leaves the host.

The encryption key never lives in `.env` or next to the backups:

| Key source | Tool | Notes |
|---|---|---|
| `BACKUP_AGE_RECIPIENT=age1…` (in `/etc/healthos/backup.env`) | `age` | Preferred: the host holds only the public key; keep the identity offline. |
| `/etc/healthos/backup.key` containing `AGE-SECRET-KEY-…` | `age` | Identity on the host; mode `600`. |
| `/etc/healthos/backup.key` containing a passphrase | `gpg --symmetric` (AES256) | Fallback when `age` is not installed; mode `600`. |

`BACKUP_KEY_FILE` overrides the key path. The script refuses to run if the key file is readable by
group or others. **Store a copy of `/etc/healthos/backup.key` (or the age identity) off the host,
e.g. in a password manager.** Losing it makes every backup unreadable; nothing can recover it. Installation steps are at the top of the service file. Run a backup now with
`make backup` (or `bash scripts/backup.sh`); the path of the new file is printed.

**Restore drill (non-destructive to the live database):**

```sh
make restore FILE=backups/health-20261020T033000Z.dump.age   # or: bash scripts/restore.sh FILE [db]
```

[`scripts/restore.sh`](../scripts/restore.sh) decrypts the file (age: `BACKUP_AGE_IDENTITY` or the
key file; gpg: the key file), drops and recreates a scratch database (default `health_restore`,
never `health`), restores into it with `pg_restore --exit-on-error`, and prints per-table row
counts next to the live database's counts plus the restored Alembic revision. Drop the scratch
database afterwards.

A dump contains private health data and Fernet-encrypted OAuth refresh tokens. **A restore is only
usable with the same `GOOGLE_TOKEN_ENCRYPTION_KEY`**; without it you must re-link Google. Store that
key and the backup key together in a safe place that is not the backup directory.

**Destructive command requiring explicit confirmation:** replacing the live database deletes its
current objects. Prove a current backup with the drill above and confirm the target before use.

```sh
docker compose stop app
age --decrypt --identity /path/to/identity backups/health-<stamp>.dump.age \
  | docker compose exec -T postgres pg_restore -U health -d health --clean --if-exists --no-owner
docker compose start app
docker compose exec app health-mirror verify
```

## Integrity verification

**Verification-only command:**

```sh
health-mirror verify
```

This first checks that every protective trigger exists, is `ENABLE ALWAYS` and still calls its
original function body (a disabled or replaced guard would let deletes through unnoticed), then
recalculates every raw payload hash and every history payload hash, checks that each record has
exactly one current row, that its version numbers run 1..n without gaps, that history rows match
their record's key, and that no version repeats its neighbour's payload, and reports sync errors.
It then recomputes the SHA-256 of every stored Google response body and of every signed webhook
body, and checks that every `response_id` points to a stored response. It exits 1 on any finding. For long-term preservation also monitor PostgreSQL disk health, retain multiple generations of dumps, and periodically restore a dump to a separate local PostgreSQL instance.

## Troubleshooting

- **`ACCOUNT_NOT_LINKED`**: the selected Google account is not linked to a Fitbit account.
- **No refresh token**: revoke the app's prior grant and repeat OAuth; the flow explicitly requests offline consent.
- **403 on health endpoints**: verify API enablement, test-user access, requested restricted scopes, and consent.
- **Backfill appears slow**: sleep/exercise pages are capped at 25 by Google; progress persists after every page.
- **Webhook subscriber creation fails**: confirm public HTTPS/TLS 1.2+, exact auth secret, both handshake results, and project **number**.
- **Webhook 401**: check the exact authorization header; do not replace Google's public keyset URL with an untrusted key.
- **Repeated rows**: run `health-mirror verify`; database uniqueness is `(data_type, google_data_point_id)` for raw records.
- **Unknown new Google fields**: they are already retained in `raw_json`. Add or revise a parser later and reprocess locally without calling Google again.

## Analytics compatibility

Connect read-only analytics tools to the `analytics_*` views, such as
`analytics_heart_rate`, `analytics_sleep_sessions`, `analytics_energy_expenditure`, and
`analytics_scalar_metrics`. These views expose the normalized tables while automatically excluding
records explicitly marked `is_quarantined`. The underlying normalized tables remain available for
auditing, and the raw JSON remains available for future Pandas/Jupyter, SQL, Grafana, n8n, or
local-agent pipelines. The archive itself has no AI layer; the optional Umay chat reads it only
on request.

### Quarantined rollup records

No raw health record is deleted during quarantine. Migration `20260818_0002` preserves every raw
and normalized row, adds `is_quarantined` and `quarantine_reason`, and creates an analytics-safe
view for every normalized table. Future UPSERTs deliberately do not overwrite quarantine metadata.

The live archive contains 20,165 rollups produced before request windows were fixed to canonical
UTC-minute boundaries. They are identified by all of these conditions:

```sql
data_type IN ('calories-in-heart-rate-zone', 'total-calories')
AND (
  extract(second FROM source_start_time) <> 0
  OR extract(microseconds FROM source_start_time) <> 0
)
```

Their stable reason code is `pre_fix_rollup_non_minute_boundary_v1`. The raw rows remain in
`raw_health_data`; corresponding rows remain in `energy_expenditure` or `scalar_metrics` for audit
purposes. Use the `analytics_*` views for future analysis. If a base normalized table must be queried
directly, add `WHERE is_quarantined = false`.

## Non-goals

- No medical diagnosis or health advice; AI analysis exists only in the optional, on-request Umay chat.
- No writes, patches, or deletes against Google Health.
- No legacy Fitbit Web API compatibility layer.
- No mandatory hosted service, paid API, or public database exposure.
