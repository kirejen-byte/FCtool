from collections import Counter

from intel_paste import (
    DScan,
    DScanRow,
    FleetComposition,
    FleetMember,
    FleetSummary,
    FleetSummaryRow,
    LocalScan,
)
from intel_analyzer import (
    DScanResult,
    DScanSource,
    LocalScanResult,
    analyze_dscan,
    analyze_local_scan,
)


class _FakeAuth:
    """Stand-in for ESIAuth — overrides batch helpers only."""

    def __init__(self, name_to_id, affiliations):
        self._name_to_id = name_to_id
        self._affiliations = affiliations

    def resolve_names_to_ids(self, names):
        return {n: self._name_to_id[n] for n in names if n in self._name_to_id}

    def get_affiliations(self, ids):
        return [self._affiliations[i] for i in ids if i in self._affiliations]


def test_analyze_local_scan_classifies_pilots():
    scan = LocalScan(pilot_names=["Alice", "Bob", "Carol"])
    auth = _FakeAuth(
        name_to_id={"Alice": 1, "Bob": 2, "Carol": 3},
        affiliations={
            1: {"character_id": 1, "corporation_id": 100, "alliance_id": 200},
            2: {"character_id": 2, "corporation_id": 101, "alliance_id": 201},
            3: {"character_id": 3, "corporation_id": 102, "alliance_id": None},
        },
    )
    result = analyze_local_scan(
        scan, auth=auth,
        friendly_ids={200, 101},  # Alice's alliance, Bob's corp
        own_character_ids=set(),
    )
    assert isinstance(result, LocalScanResult)
    assert result.friendly_count == 2
    assert result.hostile_count == 1
    assert result.unresolved_names == []
    assert result.total == 3


def test_analyze_local_scan_buckets_unresolved():
    scan = LocalScan(pilot_names=["Alice", "GhostName"])
    auth = _FakeAuth(
        name_to_id={"Alice": 1},
        affiliations={1: {"character_id": 1, "corporation_id": 100, "alliance_id": 200}},
    )
    result = analyze_local_scan(scan, auth=auth, friendly_ids=set(), own_character_ids=set())
    assert result.unresolved_names == ["GhostName"]
    assert result.hostile_count == 1
    assert result.total == 2


def test_analyze_local_scan_own_chars_count_friendly():
    scan = LocalScan(pilot_names=["Me"])
    auth = _FakeAuth(
        name_to_id={"Me": 42},
        affiliations={42: {"character_id": 42, "corporation_id": 1, "alliance_id": 2}},
    )
    result = analyze_local_scan(
        scan, auth=auth,
        friendly_ids=set(),
        own_character_ids={42},
    )
    assert result.friendly_count == 1
    assert result.hostile_count == 0


def test_analyze_local_scan_top_hostile_affiliations():
    scan = LocalScan(pilot_names=["A", "B", "C", "D"])
    auth = _FakeAuth(
        name_to_id={"A": 1, "B": 2, "C": 3, "D": 4},
        affiliations={
            1: {"character_id": 1, "corporation_id": 10, "alliance_id": 100},
            2: {"character_id": 2, "corporation_id": 11, "alliance_id": 100},
            3: {"character_id": 3, "corporation_id": 12, "alliance_id": 101},
            4: {"character_id": 4, "corporation_id": 13, "alliance_id": None},
        },
    )
    result = analyze_local_scan(scan, auth=auth, friendly_ids=set(), own_character_ids=set())
    counts = dict(result.top_hostile_alliances)
    assert counts[100] == 2
    assert counts[101] == 1


def _ship_dscan(types: list[str]) -> DScan:
    return DScan(rows=[
        DScanRow(type_id=1000 + i, item_name=f"Ship {i}", type_name=t, distance_au=1.0)
        for i, t in enumerate(types)
    ])


def test_analyze_dscan_no_source_ships_only(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    scan = _ship_dscan(["Vulture", "Sabre", "Sabre"])
    result = analyze_dscan(scan, friendly_source=None, fleet_roster=None)
    assert isinstance(result, DScanResult)
    assert result.total_ships == 3
    assert result.source == DScanSource.NONE
    assert result.friendly_count is None
    assert result.hostile_count is None
    assert "No fleet roster" in result.note


def test_analyze_dscan_filters_non_ships(monkeypatch):
    def fake_is_ship(tid):
        return tid != 1002
    monkeypatch.setattr("intel_analyzer.is_ship_type", fake_is_ship)
    rows = [
        DScanRow(1000, "A", "Vulture", 1.0),
        DScanRow(1001, "B", "Sabre", 1.0),
        DScanRow(1002, "Citadel", "Astrahus", 1.0),
    ]
    result = analyze_dscan(DScan(rows=rows), friendly_source=None, fleet_roster=None)
    assert result.total_ships == 2


def test_analyze_dscan_with_pasted_summary(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    scan = _ship_dscan(["Vulture", "Sabre", "Sabre", "Sabre"])
    roster = FleetSummary(rows=[
        FleetSummaryRow("Vulture", "Command Ship", 1),
        FleetSummaryRow("Sabre", "Interdictor", 1),
    ])
    result = analyze_dscan(scan, friendly_source=DScanSource.PASTED, fleet_roster=roster)
    assert result.friendly_count == 2
    assert result.hostile_count == 2
    breakdown = dict(result.hostile_by_type)
    assert breakdown.get("Sabre") == 2
    assert breakdown.get("Vulture", 0) == 0


def test_analyze_dscan_with_pasted_composition(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    scan = _ship_dscan(["Archon", "Flycatcher"])
    roster = FleetComposition(members=[
        FleetMember("Securitas Protector", "O-BDXB", "Archon", "Carrier",
                    "Fleet Commander (Boss)", "5 - 5 - 5", ""),
        FleetMember("Tyreece Arkan", "O-BDXB", "Flycatcher", "Interdictor",
                    "Squad Member", "0 - 4 - 5", "Wing 1 / Squad 1"),
    ])
    result = analyze_dscan(scan, friendly_source=DScanSource.PASTED, fleet_roster=roster)
    assert result.friendly_count == 2
    assert result.hostile_count == 0


def test_analyze_dscan_does_not_underflow(monkeypatch):
    """If pasted fleet has more ships of a type than dscan shows (e.g., docked),
    the hostile count for that type clamps at 0."""
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    scan = _ship_dscan(["Vulture"])
    roster = FleetSummary(rows=[FleetSummaryRow("Vulture", "Command Ship", 5)])
    result = analyze_dscan(scan, friendly_source=DScanSource.PASTED, fleet_roster=roster)
    assert result.friendly_count == 1
    assert result.hostile_count == 0
