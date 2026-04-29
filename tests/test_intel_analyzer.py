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
    DScanTrend,
    LocalScanResult,
    LocalScanTrend,
    analyze_dscan,
    analyze_local_scan,
    compute_dscan_trend,
    compute_local_scan_trend,
    format_dscan_result,
    format_local_scan_result,
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


def test_trend_no_prior_returns_none(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    current = analyze_dscan(_ship_dscan(["Sabre"]),
                            friendly_source=None, fleet_roster=None)
    assert compute_dscan_trend(current_result=current, prior_result=None,
                               minutes_ago=0) is None


def test_trend_basic_delta(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    prior = analyze_dscan(
        _ship_dscan(["Hurricane", "Hurricane", "Stiletto"]),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[]),  # everyone hostile
    )
    current = analyze_dscan(
        _ship_dscan(["Hurricane", "Hurricane", "Hurricane",
                     "Hurricane", "Hurricane",
                     "Sabre", "Sabre", "Damnation"]),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[]),
    )
    trend = compute_dscan_trend(current_result=current, prior_result=prior, minutes_ago=2)
    assert isinstance(trend, DScanTrend)
    assert trend.hostile_prior == 3
    assert trend.hostile_current == 8
    assert trend.hostile_delta == 5
    diff = dict(trend.type_delta)
    assert diff["Hurricane"] == 3
    assert diff["Sabre"] == 2
    assert diff["Damnation"] == 1
    assert diff["Stiletto"] == -1
    assert "Damnation" in trend.new_types


def test_format_local_scan_basic():
    r = LocalScanResult(
        total=10, friendly_count=3, hostile_count=7,
        unresolved_names=[], hostile_pilots=[],
        top_hostile_alliances=[], top_hostile_corps=[],
    )
    text = format_local_scan_result(r)
    assert "Local — 10 pilots" in text
    assert "Friendly: 3" in text
    assert "Hostile:  7" in text


def test_format_local_scan_with_resolver():
    fakes = {
        (99005338, "alliance"): "Pandemic Horde",
        (1354830081, "alliance"): "Goonswarm Federation",
        (1, "corporation"): "Test Corp",
    }
    def fake_resolve(eid, category):
        return fakes.get((eid, category), f"unknown-{eid}")

    r = LocalScanResult(
        total=10, friendly_count=2, hostile_count=8,
        unresolved_names=[],
        hostile_pilots=[
            ("Alice", 1, 99005338),
            ("Bob", 1, 99005338),
            ("Carol", 1, 1354830081),
        ],
        top_hostile_alliances=[(99005338, 6), (1354830081, 2)],
        top_hostile_corps=[],
    )
    text = format_local_scan_result(r, resolve_name=fake_resolve)
    assert "Pandemic Horde × 6" in text
    assert "Goonswarm Federation × 2" in text
    assert "Alice [Pandemic Horde]" in text
    assert "Bob [Pandemic Horde]" in text
    assert "Carol [Goonswarm Federation]" in text
    # No raw IDs leak into output
    assert "99005338" not in text
    assert "1354830081" not in text


def test_format_local_scan_truncates_pilot_list():
    r = LocalScanResult(
        total=10, friendly_count=0, hostile_count=10,
        unresolved_names=[],
        hostile_pilots=[(f"P{i}", None, None) for i in range(10)],
        top_hostile_alliances=[], top_hostile_corps=[],
    )
    text = format_local_scan_result(r)
    assert "P0 [Independent]" in text
    assert "P4 [Independent]" in text
    assert "P5" not in text  # 5th index onward truncated
    assert "… and 5 more" in text


def test_format_local_scan_without_resolver_falls_back():
    r = LocalScanResult(
        total=2, friendly_count=0, hostile_count=2,
        unresolved_names=[],
        hostile_pilots=[("Alice", 100, 200)],
        top_hostile_alliances=[(200, 1)],
        top_hostile_corps=[],
    )
    text = format_local_scan_result(r)
    # Without a resolver, falls back to "Alliance <id>"
    assert "Alliance 200 × 1" in text
    assert "Alice [Alliance 200]" in text


def test_format_local_scan_folds_unresolved_into_hostile():
    """Unresolved names (typos / deleted characters) are surfaced as part of
    the Hostile count rather than as a separate bucket. This avoids confusing
    the FC with three numbers that don't add up to a useful total."""
    r = LocalScanResult(
        total=10, friendly_count=2, hostile_count=5,
        unresolved_names=["Ghost1", "Ghost2", "Ghost3"],
        hostile_pilots=[], top_hostile_alliances=[], top_hostile_corps=[],
    )
    text = format_local_scan_result(r)
    assert "Hostile:  8" in text  # 5 resolved hostile + 3 unresolved
    assert "(of which 3 unresolved" in text
    # No separate "Unresolved:" line
    lines = text.splitlines()
    assert not any(line.strip().startswith("Unresolved:") for line in lines)


def test_format_dscan_no_source(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    r = analyze_dscan(_ship_dscan(["Vulture"]), friendly_source=None, fleet_roster=None)
    text = format_dscan_result(r, trend=None, roster_age_minutes=None)
    assert "D-Scan — 1 ships in range" in text
    assert "No fleet roster" in text


def test_format_dscan_with_breakdown(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    r = analyze_dscan(
        _ship_dscan(["Vulture", "Sabre", "Sabre"]),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[FleetSummaryRow("Vulture", "Command Ship", 1)]),
    )
    text = format_dscan_result(r, trend=None, roster_age_minutes=2)
    assert "Friendly" in text and "1" in text
    assert "Hostile" in text and "2" in text
    assert "Sabre × 2" in text


def test_format_dscan_with_stale_roster_warning(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    r = analyze_dscan(
        _ship_dscan(["Vulture"]),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[FleetSummaryRow("Vulture", "Command Ship", 1)]),
    )
    text = format_dscan_result(r, trend=None, roster_age_minutes=8)
    assert "8m old" in text


def test_format_dscan_with_trend(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    r = analyze_dscan(
        _ship_dscan(["Hurricane"] * 5),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[]),
    )
    trend = DScanTrend(
        minutes_ago=2,
        hostile_prior=3,
        hostile_current=5,
        hostile_delta=2,
        type_delta=[("Hurricane", 2)],
        new_types=[],
    )
    text = format_dscan_result(r, trend=trend, roster_age_minutes=1)
    assert "Trend" in text
    assert "3 → 5" in text
    assert "+2 Hurricane" in text


def test_compute_local_scan_trend_no_prior():
    assert compute_local_scan_trend(current_count=10, prior_count=None, minutes_ago=0) is None


def test_compute_local_scan_trend_basic():
    trend = compute_local_scan_trend(current_count=15, prior_count=10, minutes_ago=3)
    assert isinstance(trend, LocalScanTrend)
    assert trend.minutes_ago == 3
    assert trend.prior == 10
    assert trend.current == 15
    assert trend.delta == 5


def test_compute_local_scan_trend_negative():
    trend = compute_local_scan_trend(current_count=8, prior_count=12, minutes_ago=5)
    assert trend.delta == -4


def test_format_local_scan_with_trend():
    r = LocalScanResult(
        total=15, friendly_count=2, hostile_count=13,
        unresolved_names=[], hostile_pilots=[],
        top_hostile_alliances=[], top_hostile_corps=[],
    )
    trend = LocalScanTrend(minutes_ago=3, prior=10, current=15, delta=5)
    text = format_local_scan_result(r, trend=trend)
    assert "Trend (vs scan 3m ago):" in text
    assert "Pilots: 10 → 15 (+5)" in text


def test_format_local_scan_with_negative_trend():
    r = LocalScanResult(
        total=8, friendly_count=2, hostile_count=6,
        unresolved_names=[], hostile_pilots=[],
        top_hostile_alliances=[], top_hostile_corps=[],
    )
    trend = LocalScanTrend(minutes_ago=2, prior=12, current=8, delta=-4)
    text = format_local_scan_result(r, trend=trend)
    assert "Pilots: 12 → 8 (-4)" in text
