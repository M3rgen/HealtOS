from datetime import UTC, datetime, timedelta

from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.services.discovery import DiscoveryService


class Tokens:
    calls = 0

    async def get(self):
        self.calls += 1
        return "access"


class Client:
    def __init__(self):
        self.tokens = Tokens()

    async def list_data_points(self, spec, start, end, page_size=None):
        assert start < end
        assert page_size == 1
        if spec.name == "steps":
            return Page(
                [
                    {
                        "name": "users/me/dataTypes/steps/dataPoints/example",
                        "startTime": "2026-08-01T00:00:00Z",
                        "endTime": "2026-08-01T01:00:00Z",
                        "steps": 1234,
                    }
                ],
                None,
            )
        raise RuntimeError("scope unavailable")

    async def list_paired_devices(self):
        return {
            "pairedDevices": [
                {
                    "deviceVersion": "Fitbit Example",
                    "deviceType": "WATCH",
                    "lastSyncTime": "2026-08-18T10:00:00Z",
                    "features": ["HEART_RATE"],
                    "macAddress": "00:11:22:33:44:55",
                    "serialNumber": "secret",
                },
                "invalid",
            ]
        }


async def test_probe_returns_schema_metadata_without_health_values():
    client = Client()
    service = DiscoveryService(client, {"steps": REGISTRY["steps"], "sleep": REGISTRY["sleep"]})

    results = await service.probe(7)

    assert client.tokens.calls == 1
    steps = results[0]
    assert steps["accessible"] is True
    assert steps["has_recent_data"] is True
    assert steps["sample_start"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert "steps" in steps["top_level_fields"]
    assert "sample" not in steps
    assert 1234 not in steps.values()
    assert results[1]["accessible"] is False
    assert results[1]["error"] == "scope unavailable"


async def test_device_discovery_excludes_hardware_identifiers():
    devices = await DiscoveryService(Client(), {}).devices()

    assert devices == [
        {
            "name": "Fitbit Example",
            "device_type": "WATCH",
            "last_sync_time": "2026-08-18T10:00:00Z",
            "features": ["HEART_RATE"],
        }
    ]


async def test_probe_honors_rollup_window_limit():
    class RollupClient:
        tokens = Tokens()

        def __init__(self):
            self.range = None

        async def list_data_points(self, spec, start, end, page_size=None):
            self.range = (start, end)
            return Page([], None)

    client = RollupClient()
    await DiscoveryService(client, {"total-calories": REGISTRY["total-calories"]}).probe(30)

    assert client.range[1] - client.range[0] <= timedelta(days=14)
