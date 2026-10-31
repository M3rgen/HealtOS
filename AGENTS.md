# Project instructions

## Required: one screen, no page scrolling

The HealthOS interface must fit the available viewport. Preventing vertical and horizontal scrolling of the main page is the highest-priority interface requirement. Every design and behavior change must preserve it.

- Arrange the top navigation, cards, charts, and history panel together according to viewport height.
- Dragging the history panel must not extend the page. It must open in proportion to the drag distance and retain its released height.
- Long content may scroll inside its own panel; the main page must not scroll.
- Using only `overflow: hidden` to clip content or make it unreachable is not a solution.
- Check interface changes on desktop and mobile. Work is incomplete while the main page overflows.

## Non-negotiable rules

These are the maintainer's decisions. Do not work around them; ask the maintainer instead.

1. **No pushing.** Never push to GitHub unless the maintainer explicitly asks. Local commits are fine.
2. **No page scrolling.** The single-screen rule above outranks every other interface change. The
   overflow checks in `tests/browser/*.cjs` must stay green.
3. **Raw data is preserved.** No record received from Google is deleted or overwritten; source
   deletions are only marked with `source_deleted_at`. No destructive migrations: no `DROP` outside
   `downgrade()`.
4. **Interface on loopback only.** The UI/API stays bound to `127.0.0.1`; it is never exposed to the
   LAN or the internet. The public tunnel passes only `POST /webhooks/google-health`.
5. **No persistent memory for Umay.** Moving the chat out of its bubble or merging it into the
   interface is out of scope until the maintainer specifies it.
6. **No new external services.** No new SaaS, telemetry, or CDN. Umay sends data to OpenAI; that
   stays as is but must be documented truthfully.
7. **The repository is public.** Commits must not contain personal paths, IP addresses, hostnames,
   e-mail addresses, secrets, or real health data. Before every commit,
   `python scripts/verify_configuration.py` must pass and
   `git diff --cached | grep -iE "192\.168|/Users/|A[I]za|y[a]29"` must print nothing (extend the pattern
   with your own user name and private network prefix).
8. **Natural Turkish interface text.** Use "Eşitleme", not "Senkronizasyon". The persona is called
   Umay; the word "Codex" is never shown to users.
9. **Version pins stay.** Keep `uv.lock`, image digests, and SHA-pinned Actions as they are;
   dependency upgrades are separate, deliberate changes.

## Verification commands

Run from the repository root after every change. They need no database or network.

```sh
make verify          # ruff check + format check, verify_configuration.py, pytest, pyright
make test-browser    # cd frontend && npm run test:browser (needs Playwright's Chromium)
cd frontend && npx vite build && cd ..   # frontend build check; writes app/static
```

`make` uses `uv run --frozen --extra dev`, like CI. The equivalent direct commands, with an existing
`.venv`:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python scripts/verify_configuration.py
.venv/bin/pyright app
```

Checks that need Docker or a running app (`scripts/verify_compose.py`, some browser tests) are run
separately; if you could not run one, say so in your report rather than skipping it silently.

## Autonomous Codex lane (server runner)

A headless Codex runner turns issues into pull requests on a private Git mirror of this
repository. It reads this file and `ROADMAP.md` on every turn. The maintainer merges; the bot
never merges and never pushes to GitHub (rule 1 holds: the runner pushes only to the private
mirror).

- **Environment:** Linux server, no Docker, no PostgreSQL, no GPU; `uv`, Node 22 and Playwright
  Chromium are installed; the Codex sandbox has no network. `make verify` and `make test-browser`
  run there. `scripts/verify_compose.py`, `docker compose config` and `tests/integration` cannot
  run: write "not run" in the PR body instead of claiming success.
- **Priority order when choosing work (no topic limit):** real bugs → data integrity of the raw
  archive → security → reliability (sync, scheduler, watchdog, webhook) → interface value under the
  one-screen rule → performance → code health. Within a priority, prefer the smallest change that
  ships a visible behavior with its tests.
- **One PR, one topic, finished.** The size of a task follows the depth of the topic: a deep item
  may take hours in a single PR, and that is preferred over many thin PRs. The goal is continuity,
  not PR count (maintainer, 2026-10-24). Production code plus tests; no PR that only adds tests,
  fixtures, docs or ROADMAP edits. Keep Turkish interface text natural (rule 8).
- **Quality gates** are measured before the PR opens and are written into the task prompt: lint
  clean, test/source line ratio, no growth of files over the size limit (split first, see
  ROADMAP 1.1), limited new CLI options and `pytest.skip`s, bounded source and docs growth.
- **Decisions reserved for the maintainer; do not open tasks for them:** deleting or rewriting any
  record, backup retention, new external services, persistent memory for Umay, narrowing what Umay
  sends to OpenAI, dependency or image upgrades, `raw_json` → `jsonb` migration.
- If a ROADMAP item cannot be finished on this server (needs Docker, a real Google webhook, the
  VM or a browser on a phone), finish the server-side code and defer the rest (`[~]`).

## Dersler (Burak)

Burak bir PR'ı reddettiğinde ya da elle düzelttiğinde nedeni buraya tek satır olarak yazılır; her
madde bağlayıcıdır. Biçim: `- YYYY-MM-DD · PR #N · <ders>`.
