import os

import pytest

from intel_paste import (
    DScan,
    FleetComposition,
    FleetSummary,
    LocalScan,
    detect_and_parse,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "intel")


def _read(name: str) -> str:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_dataclasses_exist():
    assert LocalScan is not None
    assert DScan is not None
    assert FleetComposition is not None
    assert FleetSummary is not None


def test_parse_local_scan_basic():
    from intel_paste import parse_local_scan
    text = "Securitas Protector\nTyreece Arkan\nNessa Volkov\n"
    result = parse_local_scan(text)
    assert isinstance(result, LocalScan)
    assert result.pilot_names == ["Securitas Protector", "Tyreece Arkan", "Nessa Volkov"]


def test_parse_local_scan_skips_blank_lines():
    from intel_paste import parse_local_scan
    text = "Alice\n\n\nBob\n"
    result = parse_local_scan(text)
    assert result.pilot_names == ["Alice", "Bob"]


def test_parse_local_scan_rejects_lines_with_digits():
    from intel_paste import parse_local_scan
    text = "Alice\nRandomEnemy123\nBob\n"
    result = parse_local_scan(text)
    assert result.pilot_names == ["Alice", "Bob"]


def test_parse_local_scan_keeps_apostrophes_and_hyphens():
    from intel_paste import parse_local_scan
    text = "O'Reilly\nJean-Luc\n"
    result = parse_local_scan(text)
    assert result.pilot_names == ["O'Reilly", "Jean-Luc"]


def test_parse_local_scan_from_fixture():
    from intel_paste import parse_local_scan
    result = parse_local_scan(_read("local_scan.txt"))
    assert "Securitas Protector" in result.pilot_names
    assert "RandomEnemy123" not in result.pilot_names  # has digits


def test_parse_dscan_basic():
    from intel_paste import parse_dscan
    text = "12345\tRagnar's Vulture\tVulture\t5.2 AU\n"
    result = parse_dscan(text)
    assert isinstance(result, DScan)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.type_id == 12345
    assert row.item_name == "Ragnar's Vulture"
    assert row.type_name == "Vulture"
    assert row.distance_au == pytest.approx(5.2)


def test_parse_dscan_handles_km():
    from intel_paste import parse_dscan
    text = "999\tThing\tHurricane\t48,231 km\n"
    result = parse_dscan(text)
    assert result.rows[0].distance_au == pytest.approx(48231 / 149_597_870.7, rel=1e-3)


def test_parse_dscan_handles_dash_distance():
    from intel_paste import parse_dscan
    text = "1\tA\tB\t-\n"
    result = parse_dscan(text)
    assert result.rows[0].distance_au is None


def test_parse_dscan_skips_malformed_lines():
    from intel_paste import parse_dscan
    text = "12345\tA\tHurricane\t1.0 AU\nbroken line\n67890\tB\tSabre\t2.0 AU\n"
    result = parse_dscan(text)
    assert len(result.rows) == 2
    assert result.rows[1].type_id == 67890


def test_parse_dscan_from_fixture():
    from intel_paste import parse_dscan
    result = parse_dscan(_read("dscan.txt"))
    assert len(result.rows) == 4
    assert result.rows[0].type_name == "Vulture"


def test_parse_fleet_composition_basic():
    from intel_paste import parse_fleet_composition
    text = (
        "Securitas Protector\tO-BDXB (Docked)\tArchon\tCarrier\t"
        "Fleet Commander (Boss)\t5 - 5 - 5\t\n"
        "Tyreece Arkan\tC-N4OD (Docked)\tFlycatcher\tInterdictor\t"
        "Squad Member\t0 - 4 - 5\tWing 1 / Squad 1\n"
    )
    result = parse_fleet_composition(text)
    assert isinstance(result, FleetComposition)
    assert len(result.members) == 2
    boss = result.members[0]
    assert boss.pilot == "Securitas Protector"
    assert boss.system == "O-BDXB (Docked)"
    assert boss.ship_name == "Archon"
    assert boss.ship_class == "Carrier"
    assert boss.role == "Fleet Commander (Boss)"
    assert boss.links == "5 - 5 - 5"
    assert boss.wing_squad == ""
    member = result.members[1]
    assert member.wing_squad == "Wing 1 / Squad 1"


def test_parse_fleet_composition_handles_trailing_tab():
    from intel_paste import parse_fleet_composition
    text = "Alice\tJita\tRifter\tFrigate\tFleet Commander (Boss)\t5 - 5 - 5\t\t\n"
    result = parse_fleet_composition(text)
    assert len(result.members) == 1
    assert result.members[0].wing_squad == ""


def test_parse_fleet_composition_from_fixture():
    from intel_paste import parse_fleet_composition
    result = parse_fleet_composition(_read("fleet_composition.txt"))
    assert len(result.members) == 2
    assert result.members[0].ship_name == "Archon"


def test_parse_fleet_summary_basic():
    from intel_paste import parse_fleet_summary
    text = "Flycatcher\tInterdictor\t1\nArchon\tCarrier\t1\n"
    result = parse_fleet_summary(text)
    assert isinstance(result, FleetSummary)
    assert len(result.rows) == 2
    assert result.rows[0].ship_name == "Flycatcher"
    assert result.rows[0].ship_class == "Interdictor"
    assert result.rows[0].count == 1


def test_parse_fleet_summary_skips_non_integer_count():
    from intel_paste import parse_fleet_summary
    text = "Flycatcher\tInterdictor\t1\nGarbage\tRow\tnot-a-number\n"
    result = parse_fleet_summary(text)
    assert len(result.rows) == 1


def test_parse_fleet_summary_from_fixture():
    from intel_paste import parse_fleet_summary
    result = parse_fleet_summary(_read("fleet_summary.txt"))
    assert len(result.rows) == 2
    assert result.rows[1].ship_name == "Archon"


def test_detect_local_scan():
    result = detect_and_parse(_read("local_scan.txt"))
    assert isinstance(result, LocalScan)


def test_detect_dscan():
    result = detect_and_parse(_read("dscan.txt"))
    assert isinstance(result, DScan)
    assert len(result.rows) == 4


def test_detect_fleet_composition():
    result = detect_and_parse(_read("fleet_composition.txt"))
    assert isinstance(result, FleetComposition)
    assert len(result.members) == 2


def test_detect_fleet_summary():
    result = detect_and_parse(_read("fleet_summary.txt"))
    assert isinstance(result, FleetSummary)
    assert len(result.rows) == 2


def test_detect_unrecognized_returns_none():
    assert detect_and_parse("") is None
    assert detect_and_parse("   \n\n  ") is None


def test_detect_priority_fleet_summary_over_dscan_when_three_cols():
    text = "Flycatcher\tInterdictor\t1\nArchon\tCarrier\t2\n"
    assert isinstance(detect_and_parse(text), FleetSummary)


def test_detect_priority_fleet_composition_over_others():
    text = (
        "Alice\tJita\tRifter\tFrigate\tFleet Commander (Boss)\t5 - 5 - 5\t\n"
        "Bob\tAmarr\tRifter\tFrigate\tSquad Member\t0 - 4 - 5\tWing 1 / Squad 1\n"
    )
    assert isinstance(detect_and_parse(text), FleetComposition)
