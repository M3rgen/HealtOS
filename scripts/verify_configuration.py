"""Non-destructive repository and environment configuration checks."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import yaml

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


def compose_service(document: object, name: str) -> dict:
    services = document.get("services") if isinstance(document, dict) else None
    service = services.get(name) if isinstance(services, dict) else None
    return service if isinstance(service, dict) else {}


def service_environment(service: dict) -> dict[str, str]:
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        pairs = (item.split("=", 1) for item in environment if "=" in item)
        return {name: value for name, value in pairs}
    return {str(name): str(value) for name, value in environment.items()}


def check_compose_runtime(compose_doc: object, dev_doc: object) -> list[str]:
    errors: list[str] = []
    app = compose_service(compose_doc, "app")
    if service_environment(app).get("APP_ENV") != "production":
        errors.append("docker-compose.yml app service must set APP_ENV: production")
    for name in ("postgres", "app"):
        logging = compose_service(compose_doc, name).get("logging") or {}
        options = logging.get("options") or {}
        if (
            logging.get("driver") != "json-file"
            or not options.get("max-size")
            or not options.get("max-file")
        ):
            errors.append(f"{name} service lacks json-file log rotation (max-size, max-file)")
    postgres = compose_service(compose_doc, "postgres")
    if "ALL" not in (postgres.get("cap_drop") or []):
        errors.append("postgres service must drop all Linux capabilities (cap_drop: [ALL])")
    dev_app = compose_service(dev_doc, "app")
    if service_environment(dev_app).get("APP_ENV") != "development":
        errors.append("docker-compose.dev.yml must override APP_ENV with development")
    return errors


def check_small_hardening() -> list[str]:
    errors: list[str] = []
    keyset_default = re.search(
        r'google_health_webhook_public_key_url: str = \(\s*"([^"]+)"',
        (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8"),
    )
    if not keyset_default or not keyset_default.group(1).startswith("https://www.gstatic.com/"):
        errors.append("the default webhook keyset URL must be on https://www.gstatic.com/")
    template_keyset = parse_env(ROOT / ".env.example").get("GOOGLE_HEALTH_WEBHOOK_PUBLIC_KEY_URL")
    if template_keyset and not template_keyset.startswith("https://www.gstatic.com/"):
        errors.append(".env.example webhook keyset URL must be on https://www.gstatic.com/")
    alembic_url = re.search(
        r"^sqlalchemy\.url\s*=(.*)$",
        (ROOT / "alembic.ini").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if alembic_url and alembic_url.group(1).strip():
        errors.append("alembic.ini sqlalchemy.url must be empty; env.py sets it from DATABASE_URL")
    return errors


def check_backup_automation(compose_doc: object, verifier: str) -> list[str]:
    errors: list[str] = []
    systemd = ROOT / "deploy" / "systemd"
    timer = systemd / "healthos-backup.timer"
    service = systemd / "healthos-backup.service"
    if not timer.is_file() or "Persistent=true" not in timer.read_text(encoding="utf-8"):
        errors.append("deploy/systemd/healthos-backup.timer is missing or not Persistent=true")
    if not service.is_file() or "scripts/backup.sh" not in service.read_text(encoding="utf-8"):
        errors.append("deploy/systemd/healthos-backup.service must run scripts/backup.sh")
    for script in ("backup.sh", "restore.sh"):
        if not (ROOT / "scripts" / script).is_file():
            errors.append(f"scripts/{script} is missing")
    backup_script = (ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    if "age --encrypt" not in backup_script or "gpg " not in backup_script:
        errors.append("scripts/backup.sh must encrypt dumps with age or gpg")
    if compose_service(compose_doc, "backup").get("volumes"):
        errors.append("the backup service must stream to scripts/backup.sh, not write to a volume")
    if "restore.sh" not in verifier or "backup.sh" not in verifier:
        errors.append("scripts/verify_compose.py must exercise backup.sh and restore.sh")
    return errors


# Built from parts so this file does not match its own rule.
WILDCARD_BRIDGE = "0.0.0.0" + ":8766"
BRIDGE_SCAN_GLOBS = (
    "*.yml",
    "*.md",
    ".env.example",
    "Dockerfile",
    "Makefile",
    "app/**/*.py",
    "scripts/*",
    "deploy/**/*",
    "docs/**/*.md",
)


def repository_files() -> list[Path]:
    """Tracked files (so local untracked notes are ignored); globbing outside Git."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        return sorted({path for pattern in BRIDGE_SCAN_GLOBS for path in ROOT.glob(pattern)})
    tracked = {ROOT / name for name in listed.split("\0") if name}
    return sorted({path for pattern in BRIDGE_SCAN_GLOBS for path in ROOT.glob(pattern)} & tracked)


def check_codex_bridge_binding() -> list[str]:
    errors: list[str] = []
    for path in repository_files():
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if WILDCARD_BRIDGE in text:
                relative = path.relative_to(ROOT).as_posix()
                errors.append(f"{relative}: the Codex bridge must not listen on all interfaces")
    bridge = (ROOT / "scripts" / "start_codex_bridge.py").read_text(encoding="utf-8")
    if "CODEX_BRIDGE_BIND_HOST" not in bridge:
        errors.append("scripts/start_codex_bridge.py must take its bind address from the env")
    if not (ROOT / "deploy" / "firewall" / "codex-bridge.nft").is_file():
        errors.append("deploy/firewall/codex-bridge.nft example is missing")
    return errors


def check_repository() -> list[str]:
    errors: list[str] = []
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    compose_doc = yaml.safe_load(compose)
    dev_doc = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))
    errors.extend(check_compose_runtime(compose_doc, dev_doc))
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
    errors.extend(check_backup_automation(compose_doc, verifier))
    errors.extend(check_codex_bridge_binding())
    errors.extend(check_small_hardening())
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
