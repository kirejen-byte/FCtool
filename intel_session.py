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

# Cap on retained history per session list (A14): these lists were append-only
# for the life of the GUI process, so a long session accumulates local scans /
# d-scans / fleet pastes without bound, even though only recent entries are
# ever consulted (prior_dscan/prior_local_scan default to a 15-minute window;
# find_recent_system to 60s). 200 is generous for real ops (hours of scans
# every few seconds) while bounding memory and the linear rescans in those
# lookups. Plain list + trim-on-append (not deque) so `== []`, slicing, and
# every other list-like usage stays byte-identical to before the cap.
_SCAN_HISTORY_CAP = 200


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
        if len(self.local_scans) > _SCAN_HISTORY_CAP:
            del self.local_scans[0]

    def add_dscan(self, system: str, parsed: DScan) -> None:
        self.dscan_scans.append(ScanEntry(
            timestamp=datetime.now(timezone.utc),
            system=system,
            parsed=parsed,
        ))
        if len(self.dscan_scans) > _SCAN_HISTORY_CAP:
            del self.dscan_scans[0]

    def add_fleet_paste(self, parsed: FleetComposition | FleetSummary) -> None:
        self.fleet_pastes.append(FleetPasteEntry(
            timestamp=datetime.now(timezone.utc),
            parsed=parsed,
        ))
        if len(self.fleet_pastes) > _SCAN_HISTORY_CAP:
            del self.fleet_pastes[0]

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


def find_recent_system(
    scans: list[ScanEntry[LocalScan]],
    now: datetime,
    window_seconds: int = 60,
) -> str | None:
    """Find the most recent local-scan system within ``window_seconds``.

    Skips entries with ``system == "unknown"``. Returns ``None`` when no
    recent entry exists. Assumes scans are appended chronologically
    (oldest first).
    """
    cutoff = now - timedelta(seconds=window_seconds)
    for entry in reversed(scans):
        if entry.timestamp < cutoff:
            break
        if entry.system and entry.system != "unknown":
            return entry.system
    return None
