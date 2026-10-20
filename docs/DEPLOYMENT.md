# Deployment and configuration

## Reproducible Python setup

Install pinned `uv` 0.12.1 through an approved package source or official release and verify
`uv --version`.

**Current Windows command:**

```powershell
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest -q
```

**Future Linux command:**

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest -q
```

Runtime-only environments use `uv sync --frozen --no-dev`; the Dockerfile additionally installs the
project non-editably. `--frozen` fails instead of changing a stale lock. Intentionally update with
`uv lock --upgrade-package <name>`, review the diff, and rerun every gate.

## Compose layouts

`docker-compose.yml` is platform-independent and remains the default. It retains the named
`postgres_data` volume, loopback-only application port, database health check, hardened read-only app,
internal database network, and optional backup/Caddy profiles.

**Current Windows command:**

```powershell
docker compose up --build -d
docker compose ps
```

The optional `docker-compose.windows.yml` changes only the backup bind source and requires
`WINDOWS_BACKUP_HOST_DIR`. The existing base command remains valid and is the compatibility path.

**Future Linux command:** use `docker-compose.linux.example.yml` only after setting a deliberately
provisioned `LINUX_BACKUP_HOST_DIR`; it is a template, not a guessed production layout.

**Verification-only command:**

```powershell
$env:POSTGRES_PASSWORD = 'validation-only-not-production'
docker compose --env-file .env.example config --quiet
```

This validates YAML without starting or recreating containers.

## Secrets

Generate passwords/tokens with a cryptographically secure tool, such as Python's standard library:

```powershell
# Cross-platform command (prints a newly generated value; store it immediately in a secret manager)
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Production mode rejects empty/placeholder database, Google OAuth, encryption, endpoint, and webhook
authorization values. Compose requires `POSTGRES_PASSWORD` and has no known default fallback.
Validate a future production file without printing values using `python
scripts/verify_configuration.py --env-file <path>`.

Transfer during migration: the protected environment file, Google OAuth client configuration, token
encryption key, existing Cloudflare tunnel credentials/config, and the verified encrypted database
dump. The database contains encrypted refresh-token material, but it is unusable without the separate
encryption key. Never commit any of these, any rendered tunnel config, or dump files.

Variable classes are documented directly in `.env.example`: shared app, PostgreSQL, OAuth, webhook,
Cloudflare deployment, Windows path, and future Linux path. No current variable was renamed.

## Backup and restore safety

**Cross-platform command (needs bash and age or gpg on the host) — non-destructive backup:**

```sh
bash scripts/backup.sh        # or: make backup; scheduled daily by deploy/systemd/healthos-backup.timer
```

The backup profile streams a custom-format dump that `scripts/backup.sh` encrypts before writing it
to the configured host directory. Never inspect dump contents. Verify integrity by restoring into an
isolated database with `scripts/restore.sh`, as `scripts/verify_compose.py` does. See the
[backup and restore reference](REFERENCE.md#database-backup-and-restore).

**Destructive command requiring explicit confirmation:** `pg_restore --clean --if-exists` deletes
objects in its target database. Stop the app/scheduler, prove a current backup, confirm the exact
target is not the active database, and establish rollback before using `scripts/restore.sh`.
