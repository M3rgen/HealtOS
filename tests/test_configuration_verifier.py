from pathlib import Path

from scripts.verify_configuration import check_environment, check_repository


def test_repository_configuration_is_consistent():
    assert check_repository() == []


def test_environment_check_reports_names_not_values(tmp_path: Path):
    path = tmp_path / "deployment.env"
    path.write_text("POSTGRES_PASSWORD=health\n", encoding="utf-8")
    errors = check_environment(path)
    assert "POSTGRES_PASSWORD: known insecure default" in errors
    assert all("health" not in error for error in errors if "POSTGRES_PASSWORD" not in error)
