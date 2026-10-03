# Pinned image update procedure

HealtOS frontend build image, resolved 2026-09-30:
`node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6`.
Node is used only during compilation; the production runtime remains Python.

Resolved multi-architecture manifest-list digests recorded on 2026-08-25:

| Component | Pinned reference |
| --- | --- |
| Python | `python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2` |
| uv build tool | `ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded` |
| PostgreSQL/backup | `postgres:17.10-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193` |
| Caddy | `caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` |

Digests pin bytes while patch tags explain update intent and make reviews understandable. The listed
digests are manifest lists, preserving architecture selection on Windows Docker Desktop and a future
Linux Engine.

1. **Verification-only command:** inspect the candidate with `docker buildx imagetools inspect
   <patch-tag>`; confirm supported architectures and record the new digest. This does not pull it.
2. Change one component at a time. For Python or uv, regenerate/check `uv.lock` only if dependency
   metadata requires it.
3. **Verification-only command:** run the lock, lint, unit, PostgreSQL migration, image build, and
   isolated Compose smoke gates.
4. Create a fresh verified backup before approving deployment.
5. **Current Windows command:** deploy in a maintenance window with `docker compose up --build -d`.
   This may recreate containers; it is not a verification-only command.
6. Verify health, Alembic head, scheduler ownership, webhook path, and data freshness.
7. Roll back by restoring the previous tag+digest references and rebuilding. A database restore is
   not normally needed for patch-image rollback; if schema/data changed, use the separately verified
   backup and explicit restore procedure.

Never change PostgreSQL 17 to another major by editing an image tag. PostgreSQL major versions have
different on-disk formats and require a dedicated logical dump/restore or `pg_upgrade` migration,
extension compatibility checks, downtime planning, and rollback proof. The existing named volume
must never be attached to a different PostgreSQL major as an experiment.
