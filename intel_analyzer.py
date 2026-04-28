"""
Pure-function analyzers for parsed intel.

Each analyzer takes a ParsedScan plus context (ESI auth, standings sets,
session state) and returns a structured result that the GUI renders.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from intel_paste import (
    DScan,
    FleetComposition,
    FleetSummary,
    LocalScan,
)
from ship_classes import is_ship_type
from standings_cache import is_friendly


@dataclass
class LocalScanResult:
    total: int
    friendly_count: int
    hostile_count: int
    unresolved_names: list[str]
    hostile_pilots: list[tuple[str, int | None, int | None]]  # (name, corp_id, alliance_id)
    top_hostile_alliances: list[tuple[int, int]]  # [(alliance_id, count), ...]
    top_hostile_corps: list[tuple[int, int]]


def analyze_local_scan(
    scan: LocalScan,
    auth,
    friendly_ids: set[int],
    own_character_ids: set[int],
) -> LocalScanResult:
    name_to_id = auth.resolve_names_to_ids(scan.pilot_names)
    unresolved = [n for n in scan.pilot_names if n not in name_to_id]

    affiliations = auth.get_affiliations(list(name_to_id.values()))
    aff_by_char = {a["character_id"]: a for a in affiliations if a.get("character_id")}

    friendly = 0
    hostile = 0
    hostile_pilots: list[tuple[str, int | None, int | None]] = []
    hostile_corp_counter: Counter[int] = Counter()
    hostile_alliance_counter: Counter[int] = Counter()

    for name, cid in name_to_id.items():
        aff = aff_by_char.get(cid, {})
        corp = aff.get("corporation_id")
        alliance = aff.get("alliance_id")
        if is_friendly(cid, corp, alliance, friendly_ids, own_character_ids):
            friendly += 1
        else:
            hostile += 1
            hostile_pilots.append((name, corp, alliance))
            if corp is not None:
                hostile_corp_counter[corp] += 1
            if alliance is not None:
                hostile_alliance_counter[alliance] += 1

    # NOTE: unresolved names are reported separately (see formatter). They are
    # NOT folded into hostile_count, so total = friendly + hostile + unresolved.

    return LocalScanResult(
        total=len(scan.pilot_names),
        friendly_count=friendly,
        hostile_count=hostile,
        unresolved_names=unresolved,
        hostile_pilots=hostile_pilots,
        top_hostile_alliances=hostile_alliance_counter.most_common(5),
        top_hostile_corps=hostile_corp_counter.most_common(5),
    )


class DScanSource(Enum):
    PASTED = "pasted"
    ESI = "esi"
    NONE = "none"


@dataclass
class DScanResult:
    total_ships: int
    source: DScanSource
    friendly_count: int | None
    hostile_count: int | None
    hostile_by_type: list[tuple[str, int]]
    friendly_by_type: list[tuple[str, int]]
    dscan_by_type: list[tuple[str, int]]
    note: str = ""


def _roster_type_counts(roster) -> Counter[str]:
    counts: Counter[str] = Counter()
    if isinstance(roster, FleetSummary):
        for row in roster.rows:
            counts[row.ship_name] += row.count
    elif isinstance(roster, FleetComposition):
        for member in roster.members:
            counts[member.ship_name] += 1
    return counts


def analyze_dscan(
    scan: DScan,
    friendly_source: DScanSource | None,
    fleet_roster,
) -> DScanResult:
    ship_rows = [r for r in scan.rows if is_ship_type(r.type_id)]
    dscan_counts: Counter[str] = Counter(r.type_name for r in ship_rows)
    total = sum(dscan_counts.values())

    if friendly_source is None or friendly_source == DScanSource.NONE or fleet_roster is None:
        return DScanResult(
            total_ships=total,
            source=DScanSource.NONE,
            friendly_count=None,
            hostile_count=None,
            hostile_by_type=[],
            friendly_by_type=[],
            dscan_by_type=dscan_counts.most_common(),
            note="No fleet roster: paste a fleet composition, or be fleet boss to use ESI.",
        )

    roster_counts = _roster_type_counts(fleet_roster)
    friendly_counts: Counter[str] = Counter()
    hostile_counts: Counter[str] = Counter()
    for type_name, count in dscan_counts.items():
        f = min(count, roster_counts.get(type_name, 0))
        friendly_counts[type_name] = f
        hostile_counts[type_name] = count - f

    return DScanResult(
        total_ships=total,
        source=friendly_source,
        friendly_count=sum(friendly_counts.values()),
        hostile_count=sum(hostile_counts.values()),
        hostile_by_type=[(t, c) for t, c in hostile_counts.most_common() if c > 0],
        friendly_by_type=[(t, c) for t, c in friendly_counts.most_common() if c > 0],
        dscan_by_type=dscan_counts.most_common(),
    )
