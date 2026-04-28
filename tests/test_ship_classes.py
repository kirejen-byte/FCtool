import pytest

import ship_classes
from ship_classes import is_ship_type


def test_known_capital_is_ship():
    # Apostle (FAX) — present in FAX hardcoded set
    assert is_ship_type(37604) is True


def test_known_command_ship_is_ship():
    # Vulture
    assert is_ship_type(22448) is True


def test_known_logistics_is_ship():
    # Guardian
    assert is_ship_type(11987) is True


def test_known_destroyer_via_classify(monkeypatch):
    # classify_ship returns "t3_destroyer" for type_id in TACTICAL_DESTROYERS — those are ships
    monkeypatch.setattr(ship_classes, "classify_ship", lambda tid: "t3_destroyer" if tid == 16236 else None)
    assert is_ship_type(16236) is True


def test_non_ship_returns_false(monkeypatch):
    # Astrahus (citadel) — group 1657 is structure, not a ship
    monkeypatch.setattr(ship_classes, "_fetch_group_id_for_type", lambda tid: 1657)
    assert is_ship_type(35832) is False


def test_unknown_falls_back_to_group_lookup(monkeypatch):
    monkeypatch.setattr(ship_classes, "_fetch_group_id_for_type", lambda tid: 25)  # Frigate
    assert is_ship_type(587) is True  # Rifter, not in any hardcoded set


def test_unknown_with_no_group_returns_false(monkeypatch):
    monkeypatch.setattr(ship_classes, "_fetch_group_id_for_type", lambda tid: None)
    assert is_ship_type(99999999) is False


def test_known_ship_groups_include_common_combat(monkeypatch):
    """A pasted d-scan with Hurricanes, Drakes, Ravens, etc. must classify as ships."""
    # Group 26 = Cruiser, 419 = Battlecruiser, 27 = Battleship
    # These are the bread-and-butter PvP hulls; d-scan filtering must keep them.
    monkeypatch.setattr(ship_classes, "_fetch_group_id_for_type", lambda tid: {
        24700: 419,  # Drake (battlecruiser)
        638: 27,     # Raven (battleship)
        621: 26,     # Caracal (cruiser)
        17738: 358,  # Vagabond (HAC)
        29984: 963,  # Tengu (T3C)
    }.get(tid))
    # Ensure none of these are in the hardcoded type-id set first (else the test is moot)
    assert 24700 not in ship_classes._KNOWN_SHIP_TYPE_IDS
    assert 638 not in ship_classes._KNOWN_SHIP_TYPE_IDS
    assert is_ship_type(24700) is True   # Drake
    assert is_ship_type(638) is True     # Raven
    assert is_ship_type(621) is True     # Caracal
    assert is_ship_type(17738) is True   # Vagabond
    assert is_ship_type(29984) is True   # Tengu
