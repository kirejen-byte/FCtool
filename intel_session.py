"""In-memory session state for the Intelligence tab."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Generic, TypeVar

from intel_paste import (
    DScan,
    FleetComposition,
    FleetSummary,
    LocalScan,
)

T = TypeVar("T")


@dataclass
class ScanEntry(Generic[T]):
    timestamp: datetime
    system: str
    parsed: T


@dataclass
class FleetPasteEntry:
    timestamp: datetime
    parsed: FleetComposition | FleetSummary


@dataclass
class IntelSession:
    local_scans: list[ScanEntry[LocalScan]] = field(default_factory=list)
    dscan_scans: list[ScanEntry[DScan]] = field(default_factory=list)
    fleet_pastes: list[FleetPasteEntry] = field(default_factory=list)

    def add_local_scan(self, system: str, parsed: LocalScan) -> None:
        self.local_scans.append(ScanEntry(
            timestamp=datetime.now(timezone.utc),
            system=system,
            parsed=parsed,
        ))

    def add_dscan(self, system: str, parsed: DScan) -> None:
        self.dscan_scans.append(ScanEntry(
            timestamp=datetime.now(timezone.utc),
            system=system,
            parsed=parsed,
        ))

    def add_fleet_paste(self, parsed: FleetComposition | FleetSummary) -> None:
        self.fleet_pastes.append(FleetPasteEntry(
            timestamp=datetime.now(timezone.utc),
            parsed=parsed,
        ))

    def latest_fleet_paste(self) -> FleetPasteEntry | None:
        return self.fleet_pastes[-1] if self.fleet_pastes else None

    def prior_dscan(self, system: str, window_minutes: int = 15) -> ScanEntry[DScan] | None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        candidates = [
            e for e in self.dscan_scans
            if e.system == system and e.timestamp >= cutoff
        ]
        return candidates[-1] if candidates else None

    def prior_local_scan(self, system: str, window_minutes: int = 15) -> ScanEntry[LocalScan] | None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        candidates = [
            e for e in self.local_scans
            if e.system == system and e.timestamp >= cutoff
        ]
        return candidates[-1] if candidates else None

    def clear(self) -> None:
        self.local_scans.clear()
        self.dscan_scans.clear()
        self.fleet_pastes.clear()
