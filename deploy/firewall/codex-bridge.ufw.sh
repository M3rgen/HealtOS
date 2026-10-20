#!/bin/sh
# ufw alternative to codex-bridge.nft: port 8766 only from Docker bridges.
# Review, then run once as root on the Docker host. Allow rules must precede the deny.
set -eu
ufw allow in on docker0 to any port 8766 proto tcp comment 'HealthOS Codex bridge (docker0)'
# Compose networks get br-<id> bridges with addresses from Docker's default pools.
ufw allow from 172.16.0.0/12 to any port 8766 proto tcp comment 'HealthOS Codex bridge (compose)'
ufw deny 8766/tcp comment 'HealthOS Codex bridge: no LAN access'
ufw status numbered
