"""
Pure-function analyzers for parsed intel.

Each analyzer takes a ParsedScan plus context (ESI auth, standings sets,
session state) and returns a structured result that the GUI renders.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from intel_paste import (
    DScan,
    FleetComposition,
    FleetSummary,
    LocalScan,
)
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
