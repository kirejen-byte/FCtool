import pytest

import ship_classes
from ship_classes import (
    CAPITAL_GROUP_IDS,
    is_capital,
    is_subcap,
    get_group_name,
)


def test_capital_group_ids_are_the_expected_nine():
    assert CAPITAL_GROUP_IDS == {30, 485, 547, 659, 883, 902, 513, 1538, 4594}


def test_is_capital_true_for_dreadnought_group(monkeypatch):
    # 485 = Dreadnought. Avoid network: force both the ship check and the group id.
    monkeypatch.setattr(ship_classes, "is_ship_type", lambda tid: True)
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: 485)
    assert is_capital(19720) is True     # Revelation
    assert is_subcap(19720) is False


def test_is_capital_false_for_subcap_group(monkeypatch):
    # 25 = Frigate.
    monkeypatch.setattr(ship_classes, "is_ship_type", lambda tid: True)
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: 25)
    assert is_capital(587) is False      # Rifter
    assert is_subcap(587) is True


def test_is_capital_false_for_non_ship(monkeypatch):
    # A citadel: capital group id would be wrong, but is_ship_type gates it out.
    monkeypatch.setattr(ship_classes, "is_ship_type", lambda tid: False)
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: 485)
    assert is_capital(35832) is False
    assert is_subcap(35832) is False     # not a ship → neither predicate


def test_is_capital_false_when_group_unknown(monkeypatch):
    monkeypatch.setattr(ship_classes, "is_ship_type", lambda tid: True)
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: None)
    assert is_capital(999999) is False
    # A ship with an unknown group id is treated as a subcap (has a ship hull,
    # just not a capital one).
    assert is_subcap(999999) is True


def test_is_capital_falsy_type_id_is_false():
    assert is_capital(0) is False
    assert is_subcap(0) is False


def test_get_group_name_resolves_via_group_id_then_name(monkeypatch):
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: 485)
    monkeypatch.setattr(ship_classes, "_fetch_group_name", lambda gid: "Dreadnought"
                        if gid == 485 else None)
    assert get_group_name(19720) == "Dreadnought"


def test_get_group_name_none_when_group_id_missing(monkeypatch):
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: None)
    monkeypatch.setattr(ship_classes, "_fetch_group_name",
                        lambda gid: pytest.fail("should not fetch a name for None gid"))
    assert get_group_name(19720) is None


def test_get_group_name_none_when_name_fetch_fails(monkeypatch):
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: 485)
    monkeypatch.setattr(ship_classes, "_fetch_group_name", lambda gid: None)
    assert get_group_name(19720) is None


def test_get_group_name_falsy_type_id_is_none():
    assert get_group_name(0) is None
