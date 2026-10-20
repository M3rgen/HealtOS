# Cloudflare Tunnel for the webhook

Detailed reference for step 7 of [DEPLOYMENT.md](DEPLOYMENT.md). The tunnel exists only so Google
can deliver `POST /webhooks/google-health`. `cloudflared` opens outbound connections to Cloudflare,
so the VM needs no inbound port. The dashboard and every other API route stay private.

Three layers keep it that way:

1. **Tunnel ingress** ([`config.example.yml`](../deploy/cloudflare/config.example.yml)): one rule for
   `${CLOUDFLARE_WEBHOOK_HOSTNAME}` with path `^/webhooks/google-health$` to
   `http://127.0.0.1:${APP_PORT}`; the final rule is `http_status:404`.
2. **Host validation in the app**: the hostname of `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` is accepted
   only for `POST /webhooks/google-health`; any other method or path on it returns 400. Do not
   override the Host header in the tunnel (`httpHostHeader`), or the app rejects the request.
3. **Webhook authentication**: the exact `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` header (else 401),
   a body limit (`GOOGLE_HEALTH_WEBHOOK_MAX_BODY_BYTES`, else 413), and Google's ECDSA signature on
   every notification.

## Install cloudflared

Download the `cloudflared-linux-<arch>` binary for the VM's architecture (`uname -m`) from the
official [Cloudflare releases](https://github.com/cloudflare/cloudflared/releases), verify the
published checksum, and install it as `/usr/local/bin/cloudflared`. Record the version. The systemd
unit runs it with `--no-autoupdate`, so updates are deliberate (see the [runbook](RUNBOOK.md#update-images-and-cloudflared)).

## Create the tunnel

Create a locally managed tunnel on an admin machine, not the VM, so the account-wide `cert.pem`
never lands on the server:

```sh
cloudflared tunnel login
cloudflared tunnel create healthos-webhook          # writes <tunnel-id>.json
cloudflared tunnel route dns healthos-webhook webhook.example.com
```

Copy only `<tunnel-id>.json` to the VM, for example as `/etc/cloudflared/<tunnel-id>.json`, owned by
root with mode 0600. Never commit it; `.gitignore` already excludes `deploy/cloudflare/*.json`.

## Configure the service

Render the template by replacing its four `${...}` placeholders (cloudflared does not expand
variables inside YAML) and store the result outside the repository:

```sh
sudo install -d -m 700 /etc/cloudflared
sudo cp deploy/cloudflare/config.example.yml /etc/cloudflared/config.yml
sudoedit /etc/cloudflared/config.yml     # tunnel id, credentials path, hostname, app port (8000)
sudo chmod 600 /etc/cloudflared/config.yml
echo 'CLOUDFLARED_CONFIG_FILE=/etc/cloudflared/config.yml' | sudo tee /etc/cloudflared/tunnel.env >/dev/null
sudo cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
sudo cloudflared tunnel --config /etc/cloudflared/config.yml ingress rule \
  https://webhook.example.com/health     # must match the 404 rule
```

Install the [systemd unit](../deploy/cloudflare/cloudflared.service.example), which reads
`/etc/cloudflared/tunnel.env`, waits for the network and Docker, and restarts on failure:

```sh
sudo install -m 644 deploy/cloudflare/cloudflared.service.example /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
systemctl status cloudflared
journalctl -u cloudflared --since '1 hour ago'
```

Running `cloudflared` on the host rather than in Compose keeps the tunnel's lifecycle and logs
independent of application rebuilds, and keeps tunnel credentials out of the Compose project.

## Verify the public surface

From any machine outside the VM:

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://webhook.example.com/              # 404
curl -s -o /dev/null -w '%{http_code}\n' https://webhook.example.com/health        # 404
curl -s -o /dev/null -w '%{http_code}\n' https://webhook.example.com/docs          # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://webhook.example.com/webhooks/google-health  # 401
```

The 401 proves that Cloudflare, the tunnel and the app are connected and that authorization is
enforced. A 502 or 530 means the tunnel is up but cannot reach `127.0.0.1:8000`, or the connector
is down; a 400 `Invalid host` means `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` names a different hostname.
Do not send a test request with the real authorization header from a shell; it would end up in
shell history. Google's subscriber handshake (an authenticated verification body that must succeed
and an unauthenticated one that must fail) is the end-to-end test.

On the VM, confirm the private side:

```sh
ss -ltn | grep -E ':8000|:5432'          # only 127.0.0.1:8000, no 5432
docker compose exec -T app health-mirror webhook-readiness
docker compose exec -T app health-mirror webhook-status
```

Never paste `cloudflared` logs, the rendered config or the credentials file into an issue: they
contain the tunnel ID and hostname. Running on Windows: see [legacy/WINDOWS.md](legacy/WINDOWS.md).
