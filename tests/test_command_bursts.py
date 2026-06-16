from command_bursts import (
    SHIELD, ARMOR, SKIRMISH, INFORMATION,
    CHARGE_TO_DISCIPLINE, DISCIPLINE_CHARGES, parse_charges,
)


def test_charge_table_has_twelve_charges_three_per_discipline():
    assert len(CHARGE_TO_DISCIPLINE) == 12
    for disc in (SHIELD, ARMOR, SKIRMISH, INFORMATION):
        assert len(DISCIPLINE_CHARGES[disc]) == 3


def test_parse_single_charge():
    assert parse_charges("Active Shielding Charge") == {(SHIELD, "Active Shielding Charge")}


def test_parse_multiple_double_space_separated():
    msg = "Shield Harmonizing Charge  Shield Extension Charge"
    assert parse_charges(msg) == {
        (SHIELD, "Shield Harmonizing Charge"),
        (SHIELD, "Shield Extension Charge"),
    }


def test_parse_with_surrounding_free_text():
    msg = "Rapid Deployment Charge  Interdiction Maneuvers Charge ml claymore"
    assert parse_charges(msg) == {
        (SKIRMISH, "Rapid Deployment Charge"),
        (SKIRMISH, "Interdiction Maneuvers Charge"),
    }


def test_parse_is_case_insensitive_and_whitespace_tolerant():
    assert parse_charges("rapid   repair   charge") == {(ARMOR, "Rapid Repair Charge")}


def test_parse_unknown_returns_empty():
    assert parse_charges("x up for shield claymore") == set()
    assert parse_charges("") == set()


def test_parse_recognizes_all_twelve_names():
    for disc, charges in DISCIPLINE_CHARGES.items():
        for charge in charges:
            assert parse_charges(charge) == {(disc, charge)}
