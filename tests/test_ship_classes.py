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
