"""Non-destructive repository and environment configuration checks."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDERS = {"change_me", "changeme", "todo", "replace_me", "example-secret"}
REQUIRED_PRODUCTION = {
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_TOKEN_ENCRYPTION_KEY",
    "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI",
    "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION",
}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def check_environment(path: Path) -> list[str]:
    values = parse_env(path)
    errors: list[str] = []
    for name in sorted(REQUIRED_PRODUCTION):
        value = values.get(name, "").strip()
        if not value:
            errors.append(f"{name}: missing or empty")
        elif value.casefold() in PLACEHOLDERS or "change_me" in value.casefold():
            errors.append(f"{name}: placeholder value")
    if values.get("POSTGRES_PASSWORD") in {"health", "postgres", "password"}:
        errors.append("POSTGRES_PASSWORD: known insecure default")
    endpoint = values.get("GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI", "")
    if endpoint and not endpoint.startswith("https://"):
        errors.append("GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI: must use HTTPS")
    return errors


def source_head() -> str:
    revisions: dict[str, str | None] = {}
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision = "([^"]+)"', text, re.MULTILINE)
        parent = re.search(r'^down_revision = (?:"([^"]+)"|None)', text, re.MULTILINE)
        if revision and parent:
            revisions[revision.group(1)] = parent.group(1)
    parents = {parent for parent in revisions.values() if parent}
    heads = sorted(set(revisions) - parents)
    if len(heads) != 1:
        raise RuntimeError(f"expected one Alembic head; found {heads}")
    return heads[0]


def check_repository() -> list[str]:
    errors: list[str] = []
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    forbidden = ("POSTGRES_PASSWORD:-health", "postgresql://health:health@postgres")
    for value in forbidden:
        if value in compose or value in dockerfile:
            errors.append(f"insecure fallback remains: {value}")
    image_lines = re.findall(r"^\s*image:\s*(\S+)", compose, re.MULTILINE)
    if not image_lines or any("@sha256:" not in image for image in image_lines):
        errors.append("every Compose image must have a digest")
    from_lines = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    if any("@sha256:" not in image for image in from_lines):
        errors.append("every Dockerfile base image must have a digest")
    if "postgres_data:/var/lib/postgresql/data" not in compose:
        errors.append("the stable PostgreSQL named-volume target changed")
    if '"127.0.0.1:${APP_PORT:-8000}:8000"' not in compose:
        errors.append("the administrative API is not explicitly loopback-only")
    postgres_block = compose.split("  app:", 1)[0]
    if "ports:" in postgres_block:
        errors.append("PostgreSQL unexpectedly has a base Compose port mapping")
    for override in ("docker-compose.windows.yml", "docker-compose.linux.example.yml"):
        override_text = (ROOT / override).read_text(encoding="utf-8")
        if "postgres_data" in override_text or "postgres:" in override_text:
            errors.append(f"{override} must not redefine PostgreSQL persistence")
    expected = source_head()
    verifier = (ROOT / "scripts" / "verify_compose.py").read_text(encoding="utf-8")
    if f'EXPECTED_REVISION = "{expected}"' not in verifier:
        errors.append("Compose verifier Alembic head is stale")
    if '"WATCHDOG_ENABLED": "false"' not in verifier:
        errors.append("Compose verifier must explicitly disable all schedulers")
    cloudflare = (ROOT / "deploy" / "cloudflare" / "config.example.yml").read_text(encoding="utf-8")
    if "http_status:404" not in cloudflare:
        errors.append("Cloudflare example lacks a deny-by-default final rule")
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    if "@google_health_webhook" not in caddy or "respond 404" not in caddy:
        errors.append("Caddy must expose only the webhook path and deny unmatched routes")
    image_docs = (ROOT / "docs" / "IMAGE_UPDATES.md").read_text(encoding="utf-8")
    for image in (*image_lines, *from_lines):
        if image not in image_docs:
            errors.append(f"pinned image is missing from update documentation: {image}")
    template = parse_env(ROOT / ".env.example")
    for name in REQUIRED_PRODUCTION:
        if name not in template:
            errors.append(f".env.example omits {name}")
    if any(value.casefold() in PLACEHOLDERS for value in template.values()):
        errors.append(".env.example contains an accepted placeholder secret")
    markdown_files = [
        *ROOT.glob("*.md"),
        *(ROOT / "docs").glob("*.md"),
    ]
    for markdown in markdown_files:
        content = markdown.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", content):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            relative = target.split("#", 1)[0]
            if relative and not (markdown.parent / relative).resolve().exists():
                errors.append(f"broken documentation link in {markdown.name}: {target}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        type=Path,
        help="validate a production env file without printing values",
    )
    args = parser.parse_args()
    errors = check_environment(args.env_file) if args.env_file else check_repository()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Configuration validation passed; no secret values were printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
