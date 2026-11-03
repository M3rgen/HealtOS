# Contributing to HealtOS

Focused fixes, documentation improvements, and reproducible bug reports are welcome. Before starting a large change, open an issue describing the problem and proposed scope.

## Project rules

Every change keeps these rules; a pull request that breaks one is not merged.

1. **One screen, no page scrolling.** This is the highest-priority interface rule (see
   [Interface requirements](#interface-requirements)); the overflow checks in `tests/browser` must
   stay green.
2. **Raw data is preserved.** No record received from Google is deleted or overwritten; a source
   deletion is only marked with `source_deleted_at`. Migrations are not destructive: no `DROP`
   outside `downgrade()`.
3. **Interface on loopback only.** The UI and API stay bound to `127.0.0.1`; the public tunnel
   passes only `POST /webhooks/google-health`.
4. **No new external services.** No new SaaS, telemetry, or CDN. Umay, the assistant, sends the
   records it is asked to analyze to OpenAI and keeps no persistent memory; both stay documented.
5. **No secrets or personal data** in commits, issues, or screenshots (see
   [Commit hygiene](#commit-hygiene) and [Use synthetic examples](#use-synthetic-examples)).
6. **Natural Turkish interface text.** Write "Eşitleme", not "Senkronizasyon". The assistant is
   called Umay; tool and model names are not shown to users.
7. **Version pins stay.** `uv.lock`, image digests, and SHA-pinned Actions change only in a
   separate, deliberate dependency update.

## Make a change

1. Fork the repository and create a branch.
2. Follow the [development setup](docs/DEVELOPMENT.md).
3. Make the smallest change that solves the problem and add relevant verification.
4. Run the checks below. Build the frontend for UI changes.
5. Open a pull request explaining the before/after behavior and checks you ran.

## Checks

```sh
make verify          # ruff check + format check, configuration and documentation-link verifier, pytest, pyright
make test-browser    # browser checks: cd frontend && npm run test:browser
cd frontend && npx vite build && cd ..   # frontend build check for interface changes
```

`make verify` is what CI runs, using `uv run --frozen --extra dev`. It needs no database or network.
The browser checks in `tests/browser` use Playwright with synthetic fixtures; install Chromium once
with `cd frontend && npx playwright install chromium`. Run them for every interface change, at
desktop and mobile sizes. `make compose-smoke` exercises the full Docker stack in an isolated
project when you change Compose, the Dockerfile, migrations, or the backup scripts.

`scripts/verify_configuration.py` also fails on a broken relative link or heading anchor in any
Markdown file at the repository root or in `docs/`, so fix links in the same change that moves a
file or renames a heading.

## Commit hygiene

Keep commits small and focused, with a message that says what changed and why. Before every commit,
check the staged diff for private data:

```sh
git diff --cached | grep -iE "192\.168|10\.[0-9]+\.[0-9]+\.[0-9]+|/Users/|/home/|A[I]za|y[a]29|BEGIN .*PRIVATE KEY"
```

The command must print nothing. Add your own user name, hostnames, and e-mail address to the pattern.
Use placeholders such as `<vm-host>`, `example.com`, and `/opt/healthos` in documentation.

## Interface requirements

The dashboard must fit the viewport without horizontal or vertical page scrolling. Check desktop and mobile layouts. Long content may scroll inside its panel; hiding inaccessible content is not a fix. The history panel must follow a drag and retain its released height.

## Use synthetic examples

Never include real health records, OAuth credentials, account identifiers, private hostnames, `.env` files, database dumps, or real-account screenshots in an issue or pull request. Replace data with a synthetic example that still reproduces the problem. Review staged files and commit history before pushing.

Preserve original source payloads. Google health-data requests must remain read-only. Keep manual entries distinguishable from imported records. AI access must stay explicit and limited to requested analysis.

## Sensitive reports

Do not post credentials or sensitive records publicly. Use GitHub private vulnerability reporting when available for security issues. Ordinary bugs can be reported publicly with synthetic reproduction steps.
