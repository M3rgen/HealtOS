"""Shared archive-import values and counters; no database operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

IMPORT_SOURCE = "archive-import"


@dataclass
class ImportStats:
    source_rows: int = 0
    already_present: int = 0
    imported: int = 0
    renumbered: int = 0
    # Records with at least one imported version (raw ids, counted once across chunks).
    records: set[Any] = field(default_factory=set)
    # Records in the dump that the live archive did not have at all: imported as rows of
    # their own, marked source-deleted (per data type).
    restored_records: dict[str, int] = field(default_factory=dict)
    source_hash_mismatches: list[str] = field(default_factory=list)
    # Payloads jsonb cannot store unchanged (e.g. 1e+16, \u0000): skipped, keep the dump.
    unrepresentable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_rows": self.source_rows,
            "already_present": self.already_present,
            "imported": self.imported,
            "renumbered": self.renumbered,
            "records_with_new_versions": len(self.records),
            "restored_records": dict(sorted(self.restored_records.items())),
            "source_hash_mismatches": self.source_hash_mismatches,
            "unrepresentable": self.unrepresentable,
        }


@dataclass
class _Candidate:
    data_type: str
    google_id: str
    raw_text: str
    payload_hash: str
    source_start_time: datetime | None
    source_end_time: datetime | None
    source_timezone: str | None
    received_at: datetime | None
    last_seen_at: datetime
    superseded_at: datetime | None
    first_seen_at: datetime | None = None
    source_deleted_at: datetime | None = None
    is_quarantined: bool | None = None
    quarantine_reason: str | None = None
    value: Any = None

    @property
    def label(self) -> str:
        return f"{self.data_type}/{self.google_id}"


@dataclass
class _Old:
    version: int
    row_id: Any
    payload_hash: str
    last_seen_at: datetime


@dataclass
class _Current:
    raw_id: Any
    payload_hash: str
    last_seen_at: datetime
    payload_source: str | None
