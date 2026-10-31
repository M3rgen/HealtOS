"""Operational hardening that lives in Compose and deployment files."""

from pathlib import Path

import pytest
import yaml

from scripts.verify_configuration import check_compose_runtime

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
DEV = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))


def test_app_and_postgres_logs_rotate():
    for name in ("app", "postgres"):
        logging = COMPOSE["services"][name]["logging"]
        assert logging["driver"] == "json-file"
        assert logging["options"] == {"max-size": "20m", "max-file": "5"}


def test_verifier_reports_missing_log_rotation():
    broken = yaml.safe_load(yaml.safe_dump(COMPOSE))
    del broken["services"]["postgres"]["logging"]
    errors = check_compose_runtime(broken, DEV)
    assert "postgres service lacks json-file log rotation (max-size, max-file)" in errors


def test_codex_bridge_never_binds_all_interfaces(monkeypatch, tmp_path):
    from scripts import start_codex_bridge as bridge

    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setattr(bridge, "docker0_address", lambda: None)
    monkeypatch.delenv("CODEX_BRIDGE_BIND_HOST", raising=False)
    assert bridge.bind_host() == "127.0.0.1"
    monkeypatch.setattr(bridge, "docker0_address", lambda: "172.17.0.1")
    assert bridge.bind_host() == "172.17.0.1"
    (tmp_path / ".env").write_text("CODEX_BRIDGE_BIND_HOST=127.0.0.1\n", encoding="utf-8")
    assert bridge.bind_host() == "127.0.0.1"
    for unsafe in ("0.0.0.0", "::", "[::]", "localhost-ish"):
        monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", unsafe)
        with pytest.raises(SystemExit):
            bridge.bind_host()
    monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", "::1")
    assert bridge.bind_host() == "[::1]"


def test_app_resolves_host_gateway_and_repo_has_no_wildcard_bridge():
    from scripts.verify_configuration import check_codex_bridge_binding

    assert "host.docker.internal:host-gateway" in COMPOSE["services"]["app"]["extra_hosts"]
    assert check_codex_bridge_binding() == []


def test_postgres_and_backup_drop_capabilities():
    postgres = COMPOSE["services"]["postgres"]
    assert postgres["cap_drop"] == ["ALL"]
    assert set(postgres["cap_add"]) <= {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}
    assert "no-new-privileges:true" in postgres["security_opt"]
    backup = COMPOSE["services"]["backup"]
    assert backup["cap_drop"] == ["ALL"] and backup["read_only"] is True
    assert "volumes" not in backup


def test_alembic_ini_holds_no_database_url():
    import configparser

    parser = configparser.ConfigParser()
    parser.read(ROOT / "alembic.ini", encoding="utf-8")
    assert parser["alembic"]["sqlalchemy.url"] == ""


def test_ui_smoke_script_compiles_and_authenticates():
    from scripts.verify_compose import UI_SMOKE_SCRIPT

    compile(UI_SMOKE_SCRIPT, "ui_smoke", "exec")
    assert "Bearer " in UI_SMOKE_SCRIPT and "'X-HealthOS-Request': '1'" in UI_SMOKE_SCRIPT
    assert COMPOSE["services"]["app"]["environment"]["APP_UI_TOKEN"].startswith("${APP_UI_TOKEN:?")


def test_ui_smoke_title_check_matches_the_served_page():
    # The repo is HealtOS, the product HealthOS; the smoke test must look for the latter.
    import re

    from scripts.verify_compose import UI_SMOKE_SCRIPT

    expected = re.search(r"assert '(<title>[^']+)' in html", UI_SMOKE_SCRIPT)
    page = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert expected and expected.group(1) in page


def _run_smoke_scripts_against_fake_app(monkeypatch, token: str):
    """Execute the in-container smoke scripts with urllib answered like UITokenMiddleware."""
    import io
    import json
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit

    from app.core.auth import EXEMPT
    from scripts.verify_compose import HEALTH_SMOKE_SCRIPT, UI_SMOKE_SCRIPT

    saved: list[dict] = []
    calls: list[tuple[str, str, bool]] = []

    class Response(io.BytesIO):
        def __init__(self, body: bytes, status: int = 200) -> None:
            super().__init__(body)
            self.status = status

    def payload(method: str, path: str, data: bytes | None) -> Response:
        if path == "/":
            return Response(
                b'<title>HealthOS</title><script src="/assets/a.js"></script>'
                b'<link href="/assets/a.css">'
            )
        if path.startswith("/assets/") or path == "/health/live":
            return Response(b"{}")
        if path == "/health":
            body = {
                "database": "healthy",
                "service": "personal-health-mirror",
                "scheduler": "disabled",
            }
            return Response(json.dumps(body).encode())
        if path == "/api/dashboard":
            profile = {"muscle": {"value": saved[-1]["value"]}} if saved else {}
            days = [{"steps": None}] * 7
            return Response(json.dumps({"days": days, "profile": profile}).encode())
        if path == "/api/dashboard/measurements" and method == "POST":
            saved.append(json.loads(data or b"{}"))
            return Response(b"{}", status=201)
        if path == "/api/dashboard/measurements":
            return Response(json.dumps([{**m, "source": "Manuel"} for m in saved]).encode())
        if path == "/api/dashboard/export.csv":
            return Response(b"date,sleep_seconds,timezone\n2026-09-29,,UTC\n")
        raise AssertionError(f"unexpected smoke request {method} {path}")

    def urlopen(request, *args, **kwargs):
        if isinstance(request, str):
            request = urllib.request.Request(request)
        method, path = request.get_method(), urlsplit(request.full_url).path
        authorized = request.get_header("Authorization") == f"Bearer {token}"
        calls.append((method, path, authorized))
        if not authorized and (method, path) not in EXEMPT:
            raise urllib.error.HTTPError(request.full_url, 401, "auth_required", None, None)
        return payload(method, path, request.data)

    monkeypatch.setenv("APP_UI_TOKEN", token)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    for script in (HEALTH_SMOKE_SCRIPT, UI_SMOKE_SCRIPT):
        exec(compile(script, "smoke", "exec"), {})
    return calls


def test_smoke_scripts_send_the_ui_token_to_every_protected_route(monkeypatch):
    from app.core.auth import EXEMPT

    calls = _run_smoke_scripts_against_fake_app(monkeypatch, "t" * 40)
    anonymous = {
        (method, path) for method, path, ok in calls if not ok and (method, path) not in EXEMPT
    }
    # Only the explicit "rejected without a token" probes go out unauthenticated.
    assert anonymous == {("GET", "/health"), ("GET", "/api/dashboard")}
    assert ("GET", "/health", True) in calls and ("GET", "/health/live", False) in calls
    assert ("POST", "/api/dashboard/measurements", True) in calls


def test_smoke_scripts_fail_when_the_bearer_header_is_missing(monkeypatch):
    import urllib.error
    import urllib.request

    real = urllib.request.Request

    class Unauthenticated(real):  # strips the bearer header, as a forgotten header would
        def __init__(self, *args, headers=None, **kwargs):
            headers = {k: v for k, v in (headers or {}).items() if k != "Authorization"}
            super().__init__(*args, headers=headers, **kwargs)

    monkeypatch.setattr(urllib.request, "Request", Unauthenticated)
    with pytest.raises(urllib.error.HTTPError):
        _run_smoke_scripts_against_fake_app(monkeypatch, "t" * 40)


def test_systemd_bridge_owns_its_process_group_and_private_state():
    import configparser

    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.optionxform = str
    path = ROOT / "deploy/systemd/healthos-bridge.service"
    unit.read(path)
    service, limits = unit["Service"], unit["Unit"]
    assert service["User"] == service["Group"] == "healthos-bridge"
    assert service["WorkingDirectory"] == "/opt/healthos"
    assert service["EnvironmentFile"] == "/etc/healthos/bridge.env"
    assert service["ExecStart"].endswith("scripts/start_codex_bridge.py --foreground")
    assert service["Type"] == "simple"
    assert service["Restart"] == "on-failure" and int(service["RestartSec"]) >= 10
    assert int(limits["StartLimitBurst"]) <= 5
    assert int(limits["StartLimitIntervalSec"]) >= 300
    assert service["KillMode"] == "control-group"
    assert int(service["TimeoutStopSec"]) > 5
    assert service["UMask"] == "0077"
    assert service["StateDirectoryMode"] == "0700"
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == service["NoNewPrivileges"] == "true"
    assert service["ReadWritePaths"] == "/var/lib/healthos-bridge"
    assert "Environment=CODEX_HOME=/var/lib/healthos-bridge" in path.read_text()
    assert "Environment=HOME=/var/lib/healthos-bridge" in path.read_text()
    # UI/API exposure and the already present Docker mapping remain unchanged.
    assert COMPOSE["services"]["app"]["ports"] == ["127.0.0.1:${APP_PORT:-8000}:8000"]
    assert COMPOSE["services"]["app"]["extra_hosts"].count("host.docker.internal:host-gateway") == 1


def test_postgres_root_is_readonly_without_replacing_entrypoint_or_data_volume():
    postgres = COMPOSE["services"]["postgres"]
    assert postgres["read_only"] is True
    assert postgres["volumes"] == ["postgres_data:/var/lib/postgresql/data"]
    assert "user" not in postgres and "entrypoint" not in postgres and "command" not in postgres
    assert set(postgres["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}
    assert postgres["tmpfs"] == [
        "/var/run/postgresql:rw,nosuid,nodev,noexec,mode=3775",
        "/tmp:rw,nosuid,nodev,noexec,mode=1777",
    ]
    app = COMPOSE["services"]["app"]
    assert app["read_only"] is True and app["tmpfs"] == ["/tmp"]
    assert app["cap_drop"] == ["ALL"]
    assert app["security_opt"] == ["no-new-privileges:true"]


@pytest.fixture
def runtime_inspections():
    from scripts.verify_compose import PROJECT_NAME

    return {
        name: {
            "HostConfig": {
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Tmpfs": {
                    entry.split(":")[0]: entry.partition(":")[2]
                    for entry in COMPOSE["services"][name]["tmpfs"]
                },
            },
            "Mounts": (
                [
                    {
                        "Type": "volume",
                        "Name": f"{PROJECT_NAME}_postgres_data",
                        "Destination": "/var/lib/postgresql/data",
                        "RW": True,
                    }
                ]
                if name == "postgres"
                else []
            ),
        }
        for name in ("app", "postgres")
    }


@pytest.mark.parametrize("name", ["app", "postgres"])
@pytest.mark.parametrize("long_mounts", [False, True])
def test_engine_runtime_accepts_hardened_short_and_long_mounts(
    runtime_inspections, name, long_mounts
):
    from scripts.verify_compose import verify_runtime

    inspection = runtime_inspections[name]
    if long_mounts:
        config = inspection["HostConfig"]
        config["Mounts"] = [
            {"Type": "tmpfs", "Target": target, "ReadOnly": False} for target in config.pop("Tmpfs")
        ]
        config["SecurityOpt"] = ["no-new-privileges=true"]
    verify_runtime(name, inspection)


@pytest.mark.parametrize("name", ["app", "postgres"])
@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("ReadonlyRootfs", False, "root filesystem"),
        ("CapDrop", [], "capabilities"),
        ("SecurityOpt", [], "no-new-privileges"),
        ("SecurityOpt", ["no-new-privileges=false"], "no-new-privileges"),
        ("Tmpfs", {}, "/tmp"),
        ("Tmpfs", {"/tmp": "ro"}, "/tmp"),
    ],
)
def test_engine_runtime_rejects_removed_protections(
    runtime_inspections, name, field, value, reason
):
    from scripts.verify_compose import verify_runtime

    inspection = runtime_inspections[name]
    inspection["HostConfig"][field] = value
    with pytest.raises(RuntimeError, match=f"{name}:.*{reason}"):
        verify_runtime(name, inspection)


@pytest.mark.parametrize(
    "failure",
    [
        "missing_socket",
        "readonly_socket",
        "readonly_data",
        "wrong_volume",
        "missing_data",
        "long_readonly",
    ],
)
def test_engine_runtime_rejects_unusable_postgres_mounts(runtime_inspections, failure):
    from scripts.verify_compose import verify_runtime

    inspection = runtime_inspections["postgres"]
    config = inspection["HostConfig"]
    if failure == "missing_socket":
        del config["Tmpfs"]["/var/run/postgresql"]
    elif failure == "readonly_socket":
        config["Tmpfs"]["/var/run/postgresql"] = "ro"
    elif failure == "readonly_data":
        inspection["Mounts"][0]["RW"] = False
    elif failure == "wrong_volume":
        inspection["Mounts"][0]["Name"] = "another_project_postgres_data"
    elif failure == "missing_data":
        inspection["Mounts"] = []
    else:
        del config["Tmpfs"]["/var/run/postgresql"]
        config["Mounts"] = [{"Type": "tmpfs", "Target": "/var/run/postgresql", "ReadOnly": True}]
    with pytest.raises(RuntimeError, match="postgres:"):
        verify_runtime("postgres", inspection)


@pytest.mark.parametrize("lose_after", [None, "restart", "recreate"])
def test_docker_drill_checks_data_after_restart_and_existing_volume_start(monkeypatch, lose_after):
    from subprocess import CompletedProcess

    from scripts import verify_compose as verifier

    stage = "initial"
    checked = []
    commands = []

    def compose(*arguments, **kwargs):
        nonlocal stage
        commands.append(arguments)
        if arguments[0] == "restart":
            stage = "restart"
        elif "--force-recreate" in arguments:
            stage = "recreate"
        if "psql" in arguments:
            checked.append(stage)
            return CompletedProcess(arguments, 0, stdout="0" if stage == lose_after else "1")
        return CompletedProcess(arguments, 0, stdout="")

    monkeypatch.setattr(verifier, "compose", compose)
    hardening_stages = []
    monkeypatch.setattr(
        verifier, "check_running_hardening", lambda **kw: hardening_stages.append(stage)
    )
    if lose_after:
        with pytest.raises(RuntimeError, match="did not persist"):
            verifier.verify_postgres_lifecycle(env={})
        assert checked[-1] == lose_after
    else:
        verifier.verify_postgres_lifecycle(env={})
        assert checked == hardening_stages == ["restart", "recreate"]
        assert any(verifier.HEALTH_SMOKE_SCRIPT in command for command in commands)
    assert not any("down" in command or "--volumes" in command for command in commands)


def test_smoke_pins_compose_file_and_disables_inherited_profiles(monkeypatch, tmp_path):
    from scripts import verify_compose as verifier

    monkeypatch.setattr(verifier, "DOCKER_COMMAND", "/usr/bin/docker")
    monkeypatch.setattr(verifier, "BASH", "/usr/bin/bash")
    monkeypatch.setattr(verifier, "SMOKE_ENV_FILE", tmp_path / "smoke-config")
    monkeypatch.setattr(verifier, "SMOKE_KEY_FILE", tmp_path / "unused-key")
    monkeypatch.setattr(verifier, "BACKUP_DIR", tmp_path / "unused-backups")
    monkeypatch.setattr(verifier, "STATUS_DIR", tmp_path / "unused-status")
    monkeypatch.setattr(verifier, "write_smoke_env_file", lambda: None)
    monkeypatch.setenv("COMPOSE_FILE", "unrelated-compose.yml")
    monkeypatch.setenv("COMPOSE_PROFILES", "webhook")
    received = []

    def stop_before_start(*args, env, **kwargs):
        received.append(env)
        raise RuntimeError("Stop before starting any containers")

    monkeypatch.setattr(verifier, "compose", stop_before_start)
    assert verifier.main() == 1
    assert len(received) == 1
    assert received[0]["COMPOSE_FILE"] == str(ROOT / "docker-compose.yml")
    assert received[0]["COMPOSE_PROFILES"] == ""
    assert not list(tmp_path.iterdir())


def test_backup_mount_is_a_readonly_directory_and_never_an_archive_or_key():
    from scripts.verify_configuration import check_backup_automation

    app = COMPOSE["services"]["app"]
    assert app["volumes"] == [
        {
            "type": "bind",
            "source": "${BACKUP_STATUS_HOST_DIR:-./backup-status}",
            "target": "/run/healthos-backup",
            "read_only": True,
            "bind": {"create_host_path": True},
        }
    ]
    assert app["environment"]["BACKUP_STATUS_FILE"] == "/run/healthos-backup/status.json"
    assert check_backup_automation(COMPOSE, "backup.sh restore.sh") == []
    for name in (".gitignore", ".dockerignore"):
        assert "backup-status" in (ROOT / name).read_text()


@pytest.mark.parametrize(
    "damage",
    ["writable", "file_mount", "archive", "key", "missing", "wrong_file", "no_create"],
)
def test_configuration_rejects_unsafe_backup_metadata_mounts(damage):
    from copy import deepcopy

    from scripts.verify_configuration import check_backup_automation

    model = deepcopy(COMPOSE)
    app = model["services"]["app"]
    mount = app["volumes"][0]
    if damage == "writable":
        mount["read_only"] = False
    elif damage == "file_mount":
        mount["source"] += "/status.json"
        mount["target"] += "/status.json"
    elif damage == "archive":
        mount["source"] = "./backups"
    elif damage == "key":
        app["volumes"].append("./backup.key:/private/key:ro")
    elif damage == "missing":
        app["volumes"] = []
    elif damage == "no_create":
        del mount["bind"]
    else:
        app["environment"]["BACKUP_STATUS_FILE"] = "/private/path"
    assert check_backup_automation(model, "backup.sh restore.sh")


def test_controlled_drill_requires_every_table_to_match():
    from scripts.verify_compose import verify_restore_counts

    report = (
        "Restored synthetic backup.\n"
        "table\trestored\tlive\tcomparison\n"
        "manual_measurements\t1\t1\tequal\nraw_health_data\t0\t0\tequal\n"
        "extra_table\t12\t12\tequal\n"
        "Counts compare a backup snapshot with the current live archive.\n"
        "Alembic revision: synthetic\n"
    )
    tables = {"manual_measurements", "raw_health_data", "extra_table"}
    verify_restore_counts(report, tables)
    broken_reports = [
        report.replace("12\t12\tequal", "12\t13\tdifferent"),
        report.replace("12\t12\tequal", "12\t-\trestored_only"),
        report.replace("12\t12\tequal", "-\t12\tlive_only"),
        report.replace("extra_table\t12\t12\tequal\n", ""),
        report.replace("extra_table\t12\t12\tequal", "invalid row"),
        report.replace("extra_table\t12\t12\tequal", "extra_table\toops\toops\tequal"),
        report.replace(
            "extra_table\t12\t12\tequal", "extra_table\t12\t12\tequal\nextra_table\t12\t12\tequal"
        ),
        report.replace("manual_measurements\t1\t1", "manual_measurements\t0\t0"),
        "query failed",
        "table\trestored\tlive\tcomparison\n",
    ]
    for broken in broken_reports:
        with pytest.raises(RuntimeError):
            verify_restore_counts(broken, tables)


@pytest.mark.parametrize("response_kind", ["correct", "stale", "leaking", "anonymous_allowed"])
def test_docker_backup_health_probe_checks_auth_and_exact_safe_evidence(monkeypatch, response_kind):
    import io
    import json
    import urllib.error
    import urllib.request

    from scripts import verify_compose as verifier

    expected = {
        "status": "available",
        "last_attempt": {"result": "failure", "timestamp": "2026-10-25T00:00:00Z"},
        "last_success_at": "2026-10-24T00:00:00Z",
    }
    calls = []

    def urlopen(request, **kwargs):
        authorized = (
            not isinstance(request, str)
            and request.get_header("Authorization") == "Bearer synthetic"
        )
        calls.append(authorized)
        if not authorized and response_kind != "anonymous_allowed":
            raise urllib.error.HTTPError("http://localhost/health/backup", 401, "auth", None, None)
        data = dict(expected)
        if response_kind == "stale":
            data["last_attempt"] = {"result": "success", "timestamp": expected["last_success_at"]}
        if response_kind == "leaking":
            data["file"] = "/private/path"
        return io.BytesIO(json.dumps(data).encode())

    def compose(*arguments, **kwargs):
        assert arguments[:5] == ("exec", "-T", "app", "python", "-c")
        exec(compile(arguments[5], "backup_probe", "exec"), {})

    monkeypatch.setenv("APP_UI_TOKEN", "synthetic")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(verifier, "compose", compose)
    if response_kind == "correct":
        verifier.check_backup_health(expected, env={})
        assert calls == [False, True]
    else:
        with pytest.raises((AssertionError, SystemExit)):
            verifier.check_backup_health(expected, env={})


@pytest.mark.parametrize("mismatch", [False, True])
def test_existing_compose_drill_freezes_data_and_checks_failure_after_recreation(
    tmp_path,
    monkeypatch,
    mismatch,
):
    import json
    import stat
    from subprocess import CompletedProcess

    from scripts import verify_compose as verifier
    from scripts.write_backup_status import record

    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(verifier, "DOCKER_COMMAND", "/usr/bin/docker")
    monkeypatch.setattr(verifier, "BASH", "/usr/bin/bash")
    monkeypatch.setattr(verifier, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(verifier, "STATUS_DIR", tmp_path / "metadata")
    monkeypatch.setattr(verifier, "SMOKE_ENV_FILE", tmp_path / "unused-config")
    monkeypatch.setattr(verifier, "SMOKE_KEY_FILE", tmp_path / "unused-key")
    monkeypatch.setattr(verifier, "write_smoke_env_file", lambda: None)
    monkeypatch.setattr(verifier, "backup_key_env", lambda: {})
    monkeypatch.setattr(verifier, "check_running_hardening", lambda **kwargs: None)
    monkeypatch.setattr(verifier, "verify_postgres_lifecycle", lambda **kwargs: None)
    monkeypatch.setattr(
        verifier,
        "published_ports",
        lambda service, **kwargs: (
            {"5432/tcp": None}
            if service == "postgres"
            else {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": verifier.HOST_PORT}]}
        ),
    )
    commands = []
    stopped = False
    recreated = False
    probes = []

    def compose(*arguments, env, **kwargs):
        nonlocal stopped, recreated
        commands.append(arguments)
        assert env["BACKUP_STATUS_HOST_DIR"] == str(tmp_path / "metadata")
        if arguments[0] == "up":
            assert stat.S_IMODE((tmp_path / "metadata").stat().st_mode) == 0o755
            stopped = False
        if "--force-recreate" in arguments:
            recreated = True
        if arguments[:2] == ("stop", "app"):
            stopped = True
        output = ""
        if "current" in arguments:
            output = verifier.EXPECTED_REVISION
        elif arguments[-2:] == ("id", "-u"):
            output = "10001"
        elif "psql" in arguments and "information_schema" in arguments[-1]:
            assert stopped
            output = "manual_measurements\nraw_health_data\nthird_table\n"
        elif "psql" in arguments:
            output = "1"
        return CompletedProcess(arguments, 0, stdout=output)

    def run(arguments, **kwargs):
        assert kwargs["env"]["COMPOSE_FILE"] == str(tmp_path / "docker-compose.yml")
        if arguments[1].endswith("backup.sh"):
            failed = kwargs["env"]["BACKUP_MIN_FREE_MB"] == "invalid"
            legacy = tmp_path / "backups" / "last-backup-status.json"
            record(
                tmp_path / "metadata",
                legacy,
                "failure" if failed else "success",
                "2026-10-25T01:00:00Z" if failed else "2026-10-24T01:00:00Z",
            )
            if failed:
                return CompletedProcess(arguments, 1, stdout="")
            assert stopped
            legacy.parent.mkdir()
            legacy.write_text(json.dumps({"result": "success"}))
            output = legacy.parent / "health-synthetic.dump.age"
            output.write_bytes(b"synthetic-encrypted-artifact")
            return CompletedProcess(arguments, 0, stdout=str(output))
        assert arguments[1].endswith("restore.sh") and stopped
        report = (
            "table\trestored\tlive\tcomparison\n"
            "manual_measurements\t1\t1\tequal\nraw_health_data\t0\t0\tequal\n"
            + ("third_table\t1\t2\tdifferent\n" if mismatch else "third_table\t2\t2\tequal\n")
            + "Counts compare a backup snapshot with the current live archive.\n"
        )
        return CompletedProcess(arguments, 0, stdout=report)

    def probe(expected, **kwargs):
        probes.append((json.loads(json.dumps(expected)), recreated))

    monkeypatch.setattr(verifier, "compose", compose)
    monkeypatch.setattr(verifier.subprocess, "run", run)
    monkeypatch.setattr(verifier, "check_backup_health", probe)
    assert verifier.main() == (1 if mismatch else 0)
    if mismatch:
        assert len(probes) == 1  # stop at the mismatched third table
        assert not recreated
    else:
        assert len(probes) == 4
        assert probes[0][0]["status"] == "missing"
        assert probes[1][0]["last_attempt"]["result"] == "success"
        assert probes[2][0]["last_attempt"]["result"] == "failure"
        assert probes[2][1] is False
        assert probes[-1][0]["last_success_at"] == "2026-10-24T01:00:00Z"
        assert probes[-1][0]["last_attempt"]["result"] == "failure"
        assert probes[-1][1] is True
    assert commands[-1] == ("down", "--volumes", "--remove-orphans")
    assert not list(tmp_path.iterdir())
