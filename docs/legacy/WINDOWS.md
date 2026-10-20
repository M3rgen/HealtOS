# Windows host notes (legacy)

HealtOS first ran on a Windows desktop with Docker Desktop. The supported deployment is now a Linux
VM described in [DEPLOYMENT.md](../DEPLOYMENT.md). These notes are kept for anyone still running the
stack on Windows, and for the staged move documented in
[WINDOWS_TO_LINUX_MIGRATION.md](WINDOWS_TO_LINUX_MIGRATION.md). Nothing here is tested in CI.

The application, Compose file and backups behave the same on Docker Desktop. The differences are in
how the host keeps things running and in a few command spellings.

## Configuration file

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"          # POSTGRES_PASSWORD
python -c "import secrets; print(secrets.token_urlsafe(32))"      # APP_UI_TOKEN
docker compose up --build -d
curl.exe --fail http://127.0.0.1:8000/health/live
```

`scripts/backup.sh` and `scripts/restore.sh` are bash scripts. On Windows run them from Git Bash or
WSL with `age` or `gpg` installed; there is no PowerShell port and no Windows scheduled task in this
repository. `docker-compose.windows.yml` predates the encrypted backup script: it only bind-mounts
`WINDOWS_BACKUP_HOST_DIR` into the `backup` container, which now streams the dump to `backup.sh`
instead of writing a file, so the override is no longer needed for backups.

## Start Docker Desktop after sign-in

Docker Desktop only starts after a user signs in. To start it at login so the containers' `restart:
unless-stopped` policy brings the stack back, run once:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\enable-docker-desktop-autostart.ps1
```

The script adds only the current user's Docker Desktop entry under the `Run` registry key. It does
not edit Docker settings, recreate containers or touch volumes. Until that user signs in, Docker
Desktop and HealtOS are not running; Cloudflare then reports 502 for the webhook and Google retries
notifications for up to seven days.

## Umay bridge on Windows

The launcher has a Windows branch: it runs the npm-installed Codex CLI through Node, and prefers an
isolated copy so the desktop's own CLI stays untouched.

```powershell
codex login
npm install --prefix "$env:USERPROFILE\.codex\healthos-bridge\cli" @openai/codex@0.159.2 --no-audit --no-fund
.\.venv\Scripts\python.exe .\scripts\start_codex_bridge.py
docker compose up -d --build app
```

Run the launcher again after every Windows restart; it reuses a healthy bridge. On Docker Desktop
the bridge listens on `127.0.0.1` (Desktop forwards `host.docker.internal` to host loopback). The
bridge home is `%USERPROFILE%\.codex\healthos-bridge` (or under `CODEX_HOME`); its log is
`bridge.log` and its process ID `bridge.pid` there.

## cloudflared as a Windows service

Install a pinned `cloudflared-windows-amd64.exe` from the official Cloudflare release, verify its
checksum, rename it to `cloudflared.exe` and install the tunnel as a service. It does not
auto-update; update it deliberately and check that it reconnects.

```powershell
cloudflared --version
Get-Service -Name cloudflared | Select-Object Name, Status, StartType
Restart-Service cloudflared        # interrupts the webhook; maintenance window only
Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddHours(-1)} |
  Where-Object ProviderName -Match 'cloudflared' |
  Select-Object TimeCreated, LevelDisplayName, Message
Get-NetTCPConnection -State Listen -LocalPort 8000 | Select-Object LocalAddress, LocalPort
```

Do not print the service command line: a token-based installation keeps its token there. The
ingress rules and public-route checks are the same as on Linux; see
[CLOUDFLARE_TUNNEL.md](../CLOUDFLARE_TUNNEL.md).
