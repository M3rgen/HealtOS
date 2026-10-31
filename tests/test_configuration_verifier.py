from pathlib import Path

import pytest
import yaml

from scripts.verify_configuration import (
    check_compose_runtime,
    check_documentation_links,
    check_environment,
    check_repository,
    check_service_hardening,
    heading_anchors,
)


def test_repository_configuration_is_consistent():
    assert check_repository() == []


def test_environment_check_reports_names_not_values(tmp_path: Path):
    path = tmp_path / "deployment.env"
    path.write_text("POSTGRES_PASSWORD=health\n", encoding="utf-8")
    errors = check_environment(path)
    assert "POSTGRES_PASSWORD: known insecure default" in errors
    assert all("health" not in error for error in errors if "POSTGRES_PASSWORD" not in error)


def test_repository_documentation_has_no_broken_links():
    assert check_documentation_links() == []


def test_documentation_link_check_reports_missing_files_and_anchors(tmp_path: Path):
    docs = tmp_path / "docs"
    (docs / "media").mkdir(parents=True)
    (docs / "media" / "shot.png").write_bytes(b"")
    (docs / "GUIDE.md").write_text("# Guide\n\n## Back up `data` now\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "\n".join(
            [
                "[ok](docs/GUIDE.md) [anchor](docs/GUIDE.md#back-up-data-now)",
                "![img](docs/media/shot.png) [![badge](https://x.test/b.svg)](LICENSE)",
                '<img src="docs/media/missing.png" alt="x" />',
                "[gone](docs/MISSING.md) [bad anchor](docs/GUIDE.md#nope)",
                "[web](https://example.com/x.md) [local](#guide) [mail](mailto:a@example.com)",
                "`[code](docs/IGNORED.md)`",
                "```md",
                "[fenced](docs/IGNORED.md)",
                "```",
            ]
        ),
        encoding="utf-8",
    )
    errors = check_documentation_links(tmp_path)
    assert errors == [
        "broken documentation link in README.md: LICENSE",
        "broken documentation link in README.md: docs/MISSING.md",
        "broken documentation anchor in README.md: docs/GUIDE.md#nope",
        "broken documentation link in README.md: docs/media/missing.png",
    ]


def test_heading_anchors_follow_github_slugs():
    markdown = "# HealtOS — Reference\n## Setup\n## Setup\n### [Link](x) and `code`\n"
    anchors = heading_anchors(markdown)
    assert anchors == {"healtos--reference", "setup", "setup-1", "link-and-code"}


@pytest.fixture
def compose_models():
    root = Path(__file__).resolve().parents[1]
    return tuple(
        yaml.safe_load((root / filename).read_text())
        for filename in ("docker-compose.yml", "docker-compose.dev.yml")
    )


def test_hardened_compose_and_development_override_pass(compose_models):
    base, dev = compose_models
    assert check_compose_runtime(base, dev) == []
    # The development override changes APP_ENV and the loopback port only.
    for name in ("app", "postgres"):
        merged = {**base["services"][name], **dev["services"][name]}
        assert check_service_hardening(name, merged) == []


@pytest.mark.parametrize("name", ["app", "postgres"])
@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("read_only", None, "read_only"),
        ("read_only", False, "read_only"),
        ("read_only", "false", "read_only"),
        ("security_opt", None, "no-new-privileges"),
        ("security_opt", ["no-new-privileges:false"], "no-new-privileges"),
        ("security_opt", ["no-new-privileges=false"], "no-new-privileges"),
        ("security_opt", ["label:disable"], "no-new-privileges"),
        (
            "security_opt",
            ["no-new-privileges:true", "no-new-privileges=false"],
            "no-new-privileges",
        ),
        ("cap_drop", None, "cap_drop"),
        ("cap_drop", ["NET_RAW"], "cap_drop"),
        ("tmpfs", None, "/tmp"),
    ],
)
def test_removed_or_relaxed_runtime_protections_fail(compose_models, name, field, value, reason):
    base, dev = compose_models
    if value is None:
        del base["services"][name][field]
    else:
        base["services"][name][field] = value
    errors = check_compose_runtime(base, dev)
    assert any(name in error and reason in error for error in errors), errors


@pytest.mark.parametrize(
    ("name", "target"), [("app", "/tmp"), ("postgres", "/tmp"), ("postgres", "/var/run/postgresql")]
)
@pytest.mark.parametrize("failure", ["missing", "ro", "permissions", "bind", "readonly_long"])
def test_required_tmpfs_cannot_be_removed_or_made_unusable(compose_models, name, target, failure):
    base, dev = compose_models
    service = base["services"][name]
    service["tmpfs"] = [entry for entry in service["tmpfs"] if entry.split(":")[0] != target]
    if failure in {"ro", "permissions"}:
        service["tmpfs"].append(target + (":ro" if failure == "ro" else ":mode=0555"))
    elif failure == "bind":
        service.setdefault("volumes", []).append(f"./scratch:{target}")
    elif failure == "readonly_long":
        service.setdefault("volumes", []).append(
            {"type": "tmpfs", "target": target, "read_only": "true"}
        )
    errors = check_compose_runtime(base, dev)
    assert any(name in error and target in error for error in errors), errors


@pytest.mark.parametrize(
    "volume",
    [
        None,
        "postgres_data:/var/lib/postgresql/data:ro",
        "./scratch:/var/lib/postgresql/data",
        "other_data:/var/lib/postgresql/data",
        "postgres_data:/other",
        {
            "type": "volume",
            "source": "postgres_data",
            "target": "/var/lib/postgresql/data",
            "read_only": "true",
        },
        {
            "type": "volume",
            "source": "postgres_data",
            "target": "/var/lib/postgresql/data",
            "volume": {"subpath": "other"},
        },
    ],
)
def test_postgres_requires_the_original_writable_data_volume(compose_models, volume):
    base, dev = compose_models
    base["services"]["postgres"]["volumes"] = [volume] if volume else []
    errors = check_compose_runtime(base, dev)
    assert any("postgres" in error and "writable postgres_data" in error for error in errors)


@pytest.mark.parametrize("name", ["app", "postgres"])
@pytest.mark.parametrize("volume", ["./scratch:/other", "./scratch:/tmp/child:ro"])
def test_additional_mounts_cannot_bypass_the_writable_path_policy(compose_models, name, volume):
    base, dev = compose_models
    base["services"][name].setdefault("volumes", []).append(volume)
    assert any(
        name in error and "unexpected mount" in error for error in check_compose_runtime(base, dev)
    )


@pytest.mark.parametrize(
    "security", ["no-new-privileges:true", "no-new-privileges=true", "no-new-privileges"]
)
@pytest.mark.parametrize("long_mounts", [False, True])
def test_equivalent_yaml_representations_pass(compose_models, security, long_mounts):
    base, dev = compose_models
    for name in ("app", "postgres"):
        service = base["services"][name]
        service["read_only"] = "true"
        service["security_opt"] = [security]
        if long_mounts:
            service["tmpfs"] = []
            service["volumes"] = yaml.safe_load("""
- type: tmpfs
  target: /tmp
  tmpfs:
    mode: 01777
""")
            if name == "postgres":
                service["volumes"] += yaml.safe_load("""
- type: tmpfs
  target: /var/run/postgresql
  tmpfs:
    mode: "03775"
- type: volume
  source: postgres_data
  target: /var/lib/postgresql/data
  read_only: "false"
""")
        elif name == "app":
            service["tmpfs"] = "/tmp:rw,mode=1777"
    assert check_compose_runtime(base, dev) == []


@pytest.mark.parametrize("tmpfs", [True, {"/tmp": "rw"}])
def test_invalid_tmpfs_shape_reports_missing_mount_instead_of_crashing(compose_models, tmpfs):
    base, dev = compose_models
    base["services"]["app"]["tmpfs"] = tmpfs
    assert "app service requires a writable tmpfs at /tmp" in check_compose_runtime(base, dev)
