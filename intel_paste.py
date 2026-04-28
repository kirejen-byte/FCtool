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
from dataclasses import dataclass


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


_KM_PER_AU = 149_597_870.7


def _parse_distance(token: str) -> float | None:
    token = token.strip()
    if not token or token == "-":
        return None
    cleaned = token.replace(",", "").replace(" ", "")
    if cleaned.endswith("AU"):
        try:
            return float(cleaned[:-2])
        except ValueError:
            return None
    if cleaned.endswith("km"):
        try:
            return float(cleaned[:-2]) / _KM_PER_AU
        except ValueError:
            return None
    if cleaned.endswith("m"):
        try:
            return float(cleaned[:-1]) / (_KM_PER_AU * 1000)
        except ValueError:
            return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_dscan(text: str) -> DScan:
    rows: list[DScanRow] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n").rstrip("\r")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        try:
            type_id = int(parts[0].strip())
        except ValueError:
            continue
        rows.append(DScanRow(
            type_id=type_id,
            item_name=parts[1].strip(),
            type_name=parts[2].strip(),
            distance_au=_parse_distance(parts[3]),
        ))
    return DScan(rows=rows)


def parse_fleet_composition(text: str) -> FleetComposition:
    members: list[FleetMember] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.rstrip("\n").rstrip("\r").split("\t")
        if len(parts) < 6:
            continue
        # Pad to at least 7 fields so wing_squad is always defined
        while len(parts) < 7:
            parts.append("")
        members.append(FleetMember(
            pilot=parts[0].strip(),
            system=parts[1].strip(),
            ship_name=parts[2].strip(),
            ship_class=parts[3].strip(),
            role=parts[4].strip(),
            links=parts[5].strip(),
            wing_squad=parts[6].strip(),
        ))
    return FleetComposition(members=members)


def parse_fleet_summary(text: str) -> FleetSummary:
    rows: list[FleetSummaryRow] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.rstrip("\n").rstrip("\r").split("\t")
        if len(parts) != 3:
            continue
        try:
            count = int(parts[2].strip())
        except ValueError:
            continue
        rows.append(FleetSummaryRow(
            ship_name=parts[0].strip(),
            ship_class=parts[1].strip(),
            count=count,
        ))
    return FleetSummary(rows=rows)


_LEADERSHIP_RE = re.compile(r"\d+\s*-\s*\d+\s*-\s*\d+")
_FLEET_KEYWORDS = ("Boss", "Wing ", "Squad ")


def _looks_like_fleet_composition(non_blank: list[str]) -> bool:
    if not non_blank:
        return False
    if not all(len(line.split("\t")) >= 5 for line in non_blank):
        return False
    return any(
        any(kw in line for kw in _FLEET_KEYWORDS) or _LEADERSHIP_RE.search(line)
        for line in non_blank
    )


def _looks_like_fleet_summary(non_blank: list[str]) -> bool:
    if not non_blank:
        return False
    for line in non_blank:
        parts = line.split("\t")
        if len(parts) != 3:
            return False
        try:
            int(parts[2].strip())
        except ValueError:
            return False
    return True


def _looks_like_dscan(non_blank: list[str]) -> bool:
    for line in non_blank:
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        last = parts[3].strip()
        if last.endswith("AU") or last.endswith("km") or last == "-":
            return True
    return False


def detect_and_parse(text: str) -> ParsedScan | None:
    non_blank = [
        ln.rstrip("\r") for ln in text.splitlines()
        if ln.strip()
    ]
    if not non_blank:
        return None
    if _looks_like_fleet_composition(non_blank):
        return parse_fleet_composition(text)
    if _looks_like_fleet_summary(non_blank):
        return parse_fleet_summary(text)
    if _looks_like_dscan(non_blank):
        return parse_dscan(text)
    parsed = parse_local_scan(text)
    if parsed.pilot_names:
        return parsed
    return None
