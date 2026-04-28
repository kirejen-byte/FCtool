"""
Paste-format detection and parsing for the Intelligence tab.

Supports four EVE Online paste formats:
- Local scan (one character name per line)
- Directional scan (tab-separated: type_id, item_name, type_name, distance)
- Fleet composition (tab-separated, includes leadership-skill column)
- Fleet summary (tab-separated: ship_name, ship_class, count)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class LocalScan:
    pilot_names: list[str]


@dataclass
class DScanRow:
    type_id: int
    item_name: str
    type_name: str
    distance_au: float | None  # None when distance is "-" or absent


@dataclass
class DScan:
    rows: list[DScanRow]


@dataclass
class FleetMember:
    pilot: str
    system: str
    ship_name: str
    ship_class: str
    role: str
    links: str
    wing_squad: str


@dataclass
class FleetComposition:
    members: list[FleetMember]


@dataclass
class FleetSummaryRow:
    ship_name: str
    ship_class: str
    count: int


@dataclass
class FleetSummary:
    rows: list[FleetSummaryRow]


ParsedScan = LocalScan | DScan | FleetComposition | FleetSummary


def detect_and_parse(text: str) -> ParsedScan | None:
    """Auto-detect format and parse. Returns None for unrecognized input."""
    raise NotImplementedError


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z' \-]{0,36}[A-Za-z]$")


def parse_local_scan(text: str) -> LocalScan:
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _NAME_RE.match(line):
            names.append(line)
    return LocalScan(pilot_names=names)
