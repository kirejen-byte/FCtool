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


from command_bursts import Verdict, evaluate_discipline, HULL_BURST_BONUS

CLAYMORE = 22468   # Shield + Skirmish
TENGU = 29984      # T3C: Shield + Information + Skirmish
LOKI = 29990       # T3C: Armor + Shield + Skirmish
UNKNOWN_HULL = 999999  # not in HULL_BURST_BONUS
GRP_COMBAT_BC = 1201
GRP_ATTACK_BC = 1202
GRP_FRIGATE = 25


def test_evaluate_bonused_primary_and_secondary():
    assert evaluate_discipline(SHIELD, CLAYMORE, group_id=540) is Verdict.BONUSED
    assert evaluate_discipline(SKIRMISH, CLAYMORE, group_id=540) is Verdict.BONUSED


def test_evaluate_known_hull_wrong_discipline_fits_no_bonus():
    # Claymore can fit an armor burst but is not bonused for armor
    assert evaluate_discipline(ARMOR, CLAYMORE, group_id=540) is Verdict.FITS_NO_BONUS


def test_evaluate_t3c_is_conditional():
    assert evaluate_discipline(SHIELD, TENGU, group_id=963) is Verdict.BONUSED_CONDITIONAL
    # Tengu has no armor bonus -> fits but not bonused
    assert evaluate_discipline(ARMOR, TENGU, group_id=963) is Verdict.FITS_NO_BONUS
    assert evaluate_discipline(ARMOR, LOKI, group_id=963) is Verdict.BONUSED_CONDITIONAL


def test_evaluate_unknown_hull_in_capable_group_fits_no_bonus():
    assert evaluate_discipline(SHIELD, UNKNOWN_HULL, group_id=GRP_COMBAT_BC) is Verdict.FITS_NO_BONUS


def test_evaluate_unknown_hull_in_incapable_group_cant_fit():
    assert evaluate_discipline(SHIELD, UNKNOWN_HULL, group_id=GRP_ATTACK_BC) is Verdict.CANT_FIT
    assert evaluate_discipline(SHIELD, UNKNOWN_HULL, group_id=GRP_FRIGATE) is Verdict.CANT_FIT


def test_evaluate_missing_data_is_unknown():
    assert evaluate_discipline(SHIELD, None, group_id=None) is Verdict.UNKNOWN
    assert evaluate_discipline(SHIELD, UNKNOWN_HULL, group_id=None) is Verdict.UNKNOWN


def test_hull_table_covers_sixteen_hulls():
    assert len(HULL_BURST_BONUS) == 16
