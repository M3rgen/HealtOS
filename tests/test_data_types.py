from app.core.config import Settings
from app.google_health.data_types import (
    REGISTRY,
    SETTINGS_SCOPE,
    AccessMethod,
    mirror_data_types,
    readonly_scopes,
    supported_data_types,
)


def test_filters_match_official_record_kind_contracts():
    start = "2026-08-01T00:00:00Z"
    end = "2026-09-01T00:00:00Z"
    assert "heart_rate.sample_time.physical_time" in REGISTRY["heart-rate"].filter_for(start, end)
    assert "steps.interval.start_time" in REGISTRY["steps"].filter_for(start, end)
    assert "daily_resting_heart_rate.date" in REGISTRY["daily-resting-heart-rate"].filter_for(
        start, end
    )
    assert "exercise.interval.civil_start_time" in REGISTRY["exercise"].filter_for(start, end)
    assert "sleep.interval.end_time" in REGISTRY["sleep"].filter_for(start, end)


def test_daily_filter_ceil_makes_same_day_window_valid():
    result = REGISTRY["daily-resting-heart-rate"].filter_for(
        "2026-08-18T11:40:01Z", "2026-08-18T11:41:02Z"
    )
    assert 'date >= "2026-08-18"' in result
    assert 'date < "2026-08-19"' in result


def test_extra_data_types_accept_comma_separated_environment(monkeypatch):
    monkeypatch.setenv("EXTRA_DATA_TYPES", "new-type,another-type")
    monkeypatch.setenv("ENABLED_DATA_TYPES", "steps,sleep")
    settings = Settings(_env_file=None)
    assert settings.extra_data_types == ["new-type", "another-type"]
    assert settings.enabled_data_types == ["steps", "sleep"]


def test_registry_covers_list_reconcile_and_rollup_only_health_streams():
    assert REGISTRY["blood-glucose"].access_method == AccessMethod.list
    assert REGISTRY["floors"].access_method == AccessMethod.reconcile
    assert REGISTRY["total-calories"].access_method == AccessMethod.rollup
    assert REGISTRY["total-calories"].max_window_days == 14
    assert all("writeonly" not in scope for scope in readonly_scopes())


def test_registry_matches_current_readable_v4_catalog_additions():
    # Official data-type table audited 2026-08-18. The four omitted resource-schema
    # unions (menstrual-period, moods, ovulation-test, symptoms) are write-only.
    readable_v4_types = {
        "active-energy-burned",
        "active-minutes",
        "active-zone-minutes",
        "activity-level",
        "altitude",
        "blood-glucose",
        "body-fat",
        "calories-in-heart-rate-zone",
        "core-body-temperature",
        "daily-heart-rate-variability",
        "daily-heart-rate-zones",
        "daily-oxygen-saturation",
        "daily-respiratory-rate",
        "daily-resting-heart-rate",
        "daily-sleep-temperature-derivations",
        "daily-vo2-max",
        "distance",
        "electrocardiogram",
        "exercise",
        "floors",
        "food",
        "food-measurement-unit",
        "heart-rate",
        "heart-rate-variability",
        "height",
        "hydration-log",
        "irregular-rhythm-notification",
        "nutrition-log",
        "oxygen-saturation",
        "respiratory-rate-sleep-summary",
        "run-vo2-max",
        "sedentary-period",
        "sleep",
        "steps",
        "swim-lengths-data",
        "time-in-heart-rate-zone",
        "total-calories",
        "vo2-max",
        "weight",
    }
    assert set(REGISTRY) == readable_v4_types
    assert "basal-energy-burned" not in REGISTRY
    assert REGISTRY["food"].time_filtered is False
    assert REGISTRY["food-measurement-unit"].time_filtered is False
    assert set(REGISTRY) - set(mirror_data_types()) == {"food", "food-measurement-unit"}


def test_enabled_data_types_limit_scope_and_sync_surface():
    specs = supported_data_types(enabled=["steps", "sleep"])
    assert list(specs) == ["steps", "sleep"]
    scopes = readonly_scopes(enabled=["steps", "sleep"])
    assert len(scopes) == 3
    assert SETTINGS_SCOPE in scopes
