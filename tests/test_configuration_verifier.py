from pathlib import Path

from scripts.verify_configuration import (
    check_documentation_links,
    check_environment,
    check_repository,
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
