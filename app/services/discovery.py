from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import DataTypeSpec
from app.parsers.common import temporal


class DiscoveryService:
    def __init__(
        self,
        client: GoogleHealthClient,
        specs: dict[str, DataTypeSpec],
        concurrency: int = 2,
    ):
        self.client = client
        self.specs = specs
        self.concurrency = concurrency

    async def probe(self, days: int = 30) -> list[dict]:
        # Fail once and clearly before launching one failing task per data type.
        await self.client.tokens.get()
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(spec: DataTypeSpec) -> dict:
            async with semaphore:
                try:
                    probe_start = (
                        max(start, end - timedelta(days=spec.max_window_days))
                        if spec.max_window_days
                        else start
                    )
                    page = await self.client.list_data_points(spec, probe_start, end, page_size=1)
                    sample = page.data_points[0] if page.data_points else None
                    sample_start = sample_end = None
                    keys: list[str] = []
                    if isinstance(sample, dict):
                        sample_start, sample_end, _ = temporal(spec, sample)
                        keys = sorted(sample)
                    return {
                        "data_type": spec.name,
                        "access_method": spec.access_method,
                        "accessible": True,
                        "has_recent_data": sample is not None,
                        "sample_start": sample_start,
                        "sample_end": sample_end,
                        "top_level_fields": keys,
                    }
                except Exception as exc:
                    return {
                        "data_type": spec.name,
                        "access_method": spec.access_method,
                        "accessible": False,
                        "has_recent_data": False,
                        "error": str(exc)[:300],
                    }

        return await asyncio.gather(*(one(spec) for spec in self.specs.values()))

    async def devices(self) -> list[dict]:
        payload = await self.client.list_paired_devices()
        devices = payload.get("pairedDevices", [])
        if not isinstance(devices, list):
            return []
        return [
            {
                "name": item.get("deviceVersion") or item.get("displayName"),
                "device_type": item.get("deviceType"),
                "last_sync_time": item.get("lastSyncTime"),
                "features": item.get("features", []),
            }
            for item in devices
            if isinstance(item, dict)
        ]
