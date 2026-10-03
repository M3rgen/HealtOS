# Cloudflare Tunnel deployment

The current deployment is a host-level Windows service. This document does not authorize replacing
the tunnel, changing its public hostname, or printing credentials. The tracked example allows only
`/webhooks/google-health`; the final ingress rule returns 404 for everything else.

## Current Windows deployment

**Current Windows command — install a pinned binary on a new host only:** download the selected
`cloudflared-windows-amd64.exe` from the official Cloudflare release, verify its published checksum,
rename it to `cloudflared.exe`, and record the version. Do not reinstall over the active service as
part of routine verification. The version observed on 2026-08-25 was `2026.8.1`.

**Verification-only command:**

```powershell
cloudflared --version
Get-Service -Name cloudflared | Select-Object Name, Status, StartType
```

**Current Windows command — service control (causes a webhook interruption):**

```powershell
Start-Service cloudflared
Stop-Service cloudflared
Restart-Service cloudflared
```

Use those commands only in an approved maintenance window. `Restart-Service` is not a health check.

**Verification-only command — Windows service events:**

```powershell
Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddHours(-1)} |
  Where-Object ProviderName -Match 'cloudflared' |
  Select-Object TimeCreated, LevelDisplayName, Message
```

Review locally. Do not paste log lines containing hostnames, tunnel IDs, tokens, or account data.
Do not print the service command line: token-based installations may contain a token there.

To confirm the origin target, locate the service's existing config path locally, then run
`cloudflared tunnel ingress validate` against that config and compare its webhook rule with
`http://127.0.0.1:<APP_PORT>`. Never print the config or credentials. Also confirm the app listener:

```powershell
# Verification-only command
Get-NetTCPConnection -State Listen -LocalPort 8000 |
  Select-Object LocalAddress, LocalPort, OwningProcess
curl.exe --fail http://127.0.0.1:8000/health
```

The listener must be loopback-only on the host. PostgreSQL must have no published host port.

For public route checks, enter the existing hostname interactively so it is not stored in shell
history or tracked files. A request to `/health`, `/docs`, or any other non-webhook route must return
Cloudflare/origin 404. The webhook verification POST should be performed only with the existing
authorization header in a controlled end-to-end test; do not print that header and do not send a
data notification. These checks do not modify Cloudflare configuration or the webhook URL.

## Future Linux host template

Use [config.example.yml](../deploy/cloudflare/config.example.yml) as a rendering source and keep the
rendered file plus tunnel credentials outside the repository with owner-only permissions. Install
the same explicitly selected cloudflared release for the server's confirmed CPU architecture,
verify the vendor checksum, and disable in-process auto-update. Record the version and checksum.

The [systemd example](../deploy/cloudflare/cloudflared.service.example) expects a protected
`EnvironmentFile` containing only `CLOUDFLARED_CONFIG_FILE`. Render placeholder values before
installation; cloudflared does not promise shell-style expansion inside YAML. Protect both config
and credential JSON with root ownership and mode 0600.

**Future Linux command:**

```sh
sudo systemctl daemon-reload
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
sudo systemctl status cloudflared
sudo journalctl -u cloudflared --since '1 hour ago'
```

Do not start the Linux service until the Windows scheduler is disabled and the cutover checkpoint in
the migration runbook is approved. Transfer the existing tunnel credential securely; do not create
a second active tunnel or change DNS. Validate the rendered ingress with `cloudflared tunnel ingress
validate` before starting it.

Host-level cloudflared is recommended for a future single-server Linux deployment: systemd can order
it after networking, logs remain available even if Compose is unhealthy, and the tunnel lifecycle is
separate from application rebuilds. A Compose-managed tunnel is more portable and has one control
plane, but it couples ingress restarts to stack operations and adds credential mounts to Compose.
Either model can be secure; retaining host-level operation minimizes cutover change.
