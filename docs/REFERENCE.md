# HealtOS — Technical Reference

A fully self-hosted, subscription-free, read-only archive of health and fitness data exposed by the **Google Health API v4**. It preserves every source payload before normalization and maintains analytics-friendly PostgreSQL tables for heart rate, HRV, oxygen saturation, sleep, steps, exercise, and long-tail scalar metrics.

> This project uses `https://health.googleapis.com/v4`, the current Google Health API. It does not use or add functionality against the deprecated Fitbit Web API.

## Design principles

`RAW FIRST → NORMALIZE SECOND → ANALYZE LATER`

- Google Health remains the source of truth. This application only makes read requests to user data endpoints.
- Raw responses are retained in `raw_health_data`; unknown fields are never dropped from the archive.
- A Google resource name is the primary source identifier. Where a response lacks one, a deterministic identity is derived from type, time coordinates, and data source.
- Payload hashes distinguish unchanged records from source corrections.
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
overflow (requires Playwright on Node's module path and the local site running).

Edit the personality file to refine the voice. An existing conversation keeps its
initial personality; start **Yeni sohbet** to apply an edit. With the Docker image,
first run `docker compose up -d --build app` to include the changed file. Missing or
empty personality files prevent a new conversation from starting.

The personality file is not persistent memory. Closing the panel keeps the current
conversation, but a reload, reconnect, or new chat starts a fresh ephemeral thread.
Future refinements can specify preferred form of address, response length,
mythological intensity, and coaching style. Persistent memory would require a
separate, user-scoped storage design.

### Local Codex chat on Windows

The local website must remain private (the default Compose port binds to
`127.0.0.1`; the webhook proxy exposes only the Google webhook route).

1. Sign in to the installed CLI with `codex login` using your ChatGPT account.
2. Install the site's isolated Codex CLI (the desktop's older CLI may reject
   GPT-6.1 Sol). This leaves the globally installed CLI unchanged:

   ```powershell
   npm install --prefix "$env:USERPROFILE\.codex\healthos-bridge\cli" @openai/codex@0.159.2 --no-audit --no-fund
   ```

3. From this project directory, start the bridge:

   ```powershell
   .\.venv\Scripts\python.exe .\scripts\start_codex_bridge.py
   ```

4. Apply the private `.env` configuration and build the updated site:

   ```powershell
   docker compose up -d --build app
   ```

5. Open the Codex button below the history table at `http://localhost:8000/`.
   Questions and streamed responses stay on this page. Closing the panel keeps
   the current conversation; **Yeni sohbet**, reconnecting, or reloading starts
   a separate conversation. Site conversations are ephemeral, not sidebar chats.

Run the bridge command again after restarting Windows. It runs in the background
and reuses a running instance. The launcher copies the current ChatGPT login into
an isolated `%USERPROFILE%\.codex\healthos-bridge` home (or under `CODEX_HOME`),
with desktop plugins, shell, browser, computer-use and multi-agent tools disabled.
The bridge requires a random bearer token; only the backend receives that token.
Token and credentials are never included in frontend bundles or tool responses.
The bridge log is `healthos-bridge\bridge.log`. Keep that local home private.

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

**Current Windows command:** run from this project directory, with the existing
`.env` file intact:

```powershell
docker compose up --build -d
docker compose ps
```

Open `http://localhost:8000/`. The API documentation remains at `/docs`.
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

```powershell
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

PostgreSQL has no published host port in the production Compose file. FastAPI is published only on `127.0.0.1`. The application container runs as an unprivileged UID with all Linux capabilities dropped, `no-new-privileges`, and a read-only root filesystem. Put a self-hosted reverse proxy with HTTPS in front of it before exposing the webhook. Secrets are environment variables; tokens are never logged. OAuth refresh tokens obtained by the callback are Fernet-encrypted in PostgreSQL using a key that stays in the environment.

## Requirements

- Docker Engine with Docker Compose, or Python 3.12+ and PostgreSQL 15+
- `uv` 0.12.1 for deterministic native dependency installation
- A Google Cloud project with Google Health API access for real integration (not needed for development/tests)
- For webhooks: a domain/public HTTPS URL that routes to `/webhooks/google-health`

## Docker setup (no credentials yet)

**Current Windows command:** copy `.env.example` to the ignored `.env`, generate a strong
`POSTGRES_PASSWORD`, and set it before starting:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
curl.exe --fail http://127.0.0.1:8000/health
```

The app runs `alembic upgrade head` before startup. Compose marks the app healthy only after `/health` can query PostgreSQL successfully. Compose refuses to start without `POSTGRES_PASSWORD`; production-mode application settings also reject missing or placeholder credentials.

For local database access during development only:

**Current Windows command — development-only database binding:**

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

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

The script uses a uniquely named Compose project prefixed `health-mirror-smoke-`, `.env.example`, a
temporary password, backup directory, and host port `18080`. It builds the production image, waits
for PostgreSQL and FastAPI, exercises `/health` and the installed CLI, runs the shipped compressed
backup and restores it into a second database, verifies the PostgreSQL Alembic downgrade/upgrade
lifecycle, checks the hardened container and private bindings, then removes only those isolated
resources. It does not read or modify `.env`, the normal `postgres_data` volume, or Google
credentials. Keep live-account verification evidence private; it can contain personal health data.

Reproducible deployment, image maintenance, Cloudflare operations, and the future migration are
split into [deployment](DEPLOYMENT.md), [image update](IMAGE_UPDATES.md),
[Cloudflare Tunnel](CLOUDFLARE_TUNNEL.md), and
[Windows-to-Linux migration](WINDOWS_TO_LINUX_MIGRATION.md) guides. The current Windows command
remains `docker compose up --build -d`; the Linux guide is a template and has not been executed.

## Configuration

Copy `.env.example` to `.env`; never commit `.env`.

| Variable | Purpose |
|---|---|
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
| `BACKUP_HOST_DIR`, `BACKUP_RETENTION_DAYS` | Local/NAS dump directory and local retention period |
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
curl 'http://127.0.0.1:8000/google-health/capabilities?days=30'
curl http://127.0.0.1:8000/google-health/devices
```

Use this to confirm the account is linked, Fitbit data is visible, and the configured scopes cover the intended types. Then verify a small persisted sync before starting the full backup.

**Current Windows or cross-platform command — writes retrieved records to the configured database:**

```bash
health-mirror sync steps
health-mirror status
```

## Historical backfill

**Current Windows or cross-platform command — starts a persisted backfill through the local API:**

```bash
curl -X POST -H 'X-HealthOS-Request: 1' 'http://127.0.0.1:8000/backfill/start?data_type=steps'
curl http://127.0.0.1:8000/backfill/status
```

**Current Windows or cross-platform command — starts a persisted backfill and waits:**

```bash
health-mirror backfill --data-type steps
health-mirror backfill
```

The default attempts history from 2010 in 31-day windows. Empty windows are valid and become completed checkpoints. Recent windows run first. On interruption, rerunning resumes the saved window and `pageToken`; completed windows are skipped. Use a conservative start date to maximize completeness—the official API permits queries back to the earliest recorded data.

The `food` and `food-measurement-unit` resource catalogs remain discoverable but are not included in automatic mirror jobs. Google returns the global public food reference catalog through these unfiltered types; it is not personal health history. Personal `nutrition-log` records remain fully mirrored. Any reference rows already fetched remain preserved in the raw archive.

## Incremental sync

**Current Windows or cross-platform command — writes retrieved records to the configured database:**

```bash
health-mirror sync sleep
health-mirror sync
curl -X POST -H 'X-HealthOS-Request: 1' http://127.0.0.1:8000/sync/sleep
curl -X POST -H 'X-HealthOS-Request: 1' http://127.0.0.1:8000/sync/all
curl http://127.0.0.1:8000/sync/status
```

Default overlaps are 60 minutes for heart rate, 12 hours for steps/activity streams, 72 hours for sleep/temperature derivations, seven days for daily metrics, and 24 hours otherwise. UPSERT makes every replay safe.

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

### Free outbound Cloudflare Tunnel receiver (current deployment)

The Windows deployment uses a remotely managed Cloudflare Tunnel for the existing untracked webhook
hostname. `cloudflared` makes outbound-only connections, so the router has no inbound port
forwarding. The tunnel route must match `${CLOUDFLARE_WEBHOOK_HOSTNAME}` and path
`^/webhooks/google-health$`, forward to `http://127.0.0.1:8000`, and use a final catch-all 404 rule.
The existing proxied DNS record remains untouched. The tunnel connector is installed as an automatic Windows service with its
tunnel-scoped credential; no account-wide `cert.pem` or router fallback is used.

The app remains bound to loopback, PostgreSQL has no published port, and the path rule prevents
public access to `/health`, `/docs`, OAuth, sync, backfill, or Google Health metadata routes. Check
the local production path with the verification-only commands in
[docs/CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md). Summary:

**Verification-only command:**

```powershell
Get-Service cloudflared
docker compose ps
Invoke-WebRequest http://127.0.0.1:8000/health
docker compose exec -T app health-mirror status
docker compose exec -T app health-mirror webhook-readiness
docker compose exec -T app health-mirror webhook-status
```

If durable elements remain failed after a transient outage, retry only pending webhook work with:

**Current Windows command — operates only on already verified stored webhook batches:**

```powershell
docker compose exec -T app health-mirror webhook-replay-pending
```

This command does not fabricate a webhook, change health data, or ask Google to resend anything.
It reuses already verified, stored batches and the normal read-only Google Health query path.

Cloudflare Tunnel does not auto-update on Windows. Periodically update `cloudflared` through
Chocolatey, then verify that the service reconnects. Google retries failed webhook notifications
for up to seven days; daily and weekly reconciliation remain the recovery layer for longer outages.

For the current Windows host, `cloudflared` is an automatic Windows service. Docker Desktop must
also start after a user signs in so the existing Compose containers can honor their
`restart: unless-stopped` policies. Configure that idempotently from PowerShell with:

**Current Windows command — changes the current-user login startup entry:**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\enable-docker-desktop-autostart.ps1
```

The script adds only the current user's Docker Desktop login startup entry. It does not edit Docker
settings, recreate containers, or touch volumes. This is login recovery, not pre-login Windows boot
recovery: until that user signs in, Docker Desktop and the application are not expected to run.

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

1. A Windows user signs in and Docker Desktop starts from the current-user startup entry.
2. Docker starts the existing `postgres` and `app` containers under `unless-stopped`.
3. Compose waits for PostgreSQL's `pg_isready` health check; the app then runs migrations and its
   own database readiness query before starting the scheduler.
4. The startup watchdog assesses the persisted gap and, when eligible, starts one bounded
   reconciliation. The automatic `cloudflared` service resumes forwarding as soon as port 8000 is
   healthy.

If Docker Desktop is manually stopped or the user has not signed in, no in-container watchdog can
restart the Docker engine. That host-level condition remains visible as a Cloudflare 502 and must be
handled by Windows/Docker Desktop startup or host monitoring.

## Local API

- `GET /health`
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

Interactive OpenAPI documentation is available locally at `/docs`.

## Database backup and restore

Create the host backup directory first. A compressed custom-format dump:

**Cross-platform command — non-destructive backup:**

```sh
mkdir -p backups
docker compose exec -T postgres pg_dump -U health -d health -Fc > backups/health.dump
```

Restore into an empty/replacement database after stopping the app.

**Destructive command requiring explicit confirmation:** the following deletes objects in the
selected target database. Prove a current backup and confirm the exact target before use.

```sh
docker compose stop app
docker compose exec -T postgres pg_restore -U health -d health --clean --if-exists < backups/health.dump
docker compose start app
health-mirror verify
```

`scripts/backup.sh` creates timestamped compressed dumps and deletes dumps older than `BACKUP_RETENTION_DAYS` (30 by default). Set `BACKUP_HOST_DIR` to a local or NAS directory and schedule it with system cron; cloud storage is optional and not required. Test restores regularly. A database dump contains private health data and encrypted OAuth material, so protect it like the live database and separately safeguard the encryption key.

The optional Compose profile includes `pg_dump` and writes to `BACKUP_HOST_DIR` (`./backups` by default), so the host does not need PostgreSQL client tools:

**Cross-platform command — non-destructive backup:**

```sh
mkdir -p backups
docker compose --profile backup run --rm backup
```

Schedule that command with cron or your NAS scheduler for automated local retention. The backup container runs only on demand and is not required by the API or synchronization services.

## Integrity verification

**Verification-only command:**

```sh
health-mirror verify
```

This recalculates every raw payload hash and reports sync errors. For long-term preservation also monitor PostgreSQL disk health, retain multiple generations of dumps, and periodically restore a dump to a separate local PostgreSQL instance.

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
local-agent pipelines. No AI interpretation layer is included.

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

- No health advice or AI analysis.
- No writes, patches, or deletes against Google Health.
- No legacy Fitbit Web API compatibility layer.
- No mandatory hosted service, paid API, or public database exposure.
