"""Thread-safe per-pilot command-burst charge tracker (in-memory only).

A pilot's whole charge set is replaced whenever they post a new one. Coverage
is the fleet-wide union across all tracked pilots.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from command_bursts import DISCIPLINES, DISCIPLINE_CHARGES, parse_charges


@dataclass
class CoverageStatus:
    discipline: str
    present: list[str]
    missing: list[str]
    full: bool
    redundancy: int


class ChargeTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_pilot: dict[str, set[tuple[str, str]]] = {}

    def record(self, sender: str, message: str) -> bool:
        """Parse a fleet-chat message; if it has >=1 recognized charge, replace
        the sender's whole set. Returns True only if stored state changed."""
        if not sender:
            return False
        charges = parse_charges(message)
        if not charges:
            return False
        with self._lock:
            if self._by_pilot.get(sender) == charges:
                return False
            self._by_pilot[sender] = charges
            return True

    def remove_pilot(self, sender: str) -> bool:
        """Drop a single pilot's charge record. Returns True if one was removed."""
        with self._lock:
            return self._by_pilot.pop(sender, None) is not None

    def snapshot(self) -> list[tuple[str, set[tuple[str, str]]]]:
        with self._lock:
            return [(name, set(ch)) for name, ch in self._by_pilot.items()]

    def coverage(self) -> dict[str, CoverageStatus]:
        with self._lock:
            # Distinct-pilot count per (discipline, charge). Each pilot's set
            # holds a given (disc, charge) at most once, so one increment per
            # pilot == number of distinct pilots that linked it.
            counts: dict[tuple[str, str], int] = {}
            for charges in self._by_pilot.values():
                for key in charges:
                    counts[key] = counts.get(key, 0) + 1
        result: dict[str, CoverageStatus] = {}
        for disc in DISCIPLINES:
            disc_charges = DISCIPLINE_CHARGES[disc]
            present = [c for c in disc_charges if counts.get((disc, c), 0) > 0]
            missing = [c for c in disc_charges if counts.get((disc, c), 0) == 0]
            redundancy = min(counts.get((disc, c), 0) for c in disc_charges)
            result[disc] = CoverageStatus(
                discipline=disc,
                present=sorted(present),
                missing=sorted(missing),
                full=not missing,
                redundancy=redundancy,
            )
        return result

    def clear(self) -> None:
        with self._lock:
            self._by_pilot.clear()
