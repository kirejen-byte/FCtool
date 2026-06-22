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


def test_hull_table_covers_all_bonused_hulls():
    assert len(HULL_BURST_BONUS) == 28


from command_bursts import (
    PilotRow, DisciplineCell, MAX_CHARGES, build_pilot_rows,
    VERDICT_GLYPH, verdict_text,
)


def _group_of_none(_type_id):
    return None


def test_build_rows_bonused_pilot_on_roster():
    snapshot = [("Sansha Lord", {(SHIELD, "Shield Extension Charge"),
                                 (SKIRMISH, "Interdiction Maneuvers Charge")})]
    rows = build_pilot_rows(snapshot, {"sansha lord": CLAYMORE}, _group_of_none)
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "Sansha Lord"
    assert row.ship_type_id == CLAYMORE
    assert row.over_limit is False
    verdicts = {c.discipline: c.verdict for c in row.cells}
    assert verdicts == {SHIELD: Verdict.BONUSED, SKIRMISH: Verdict.BONUSED}


def test_build_rows_pilot_not_on_roster_is_unknown():
    snapshot = [("Random Guy", {(SHIELD, "Active Shielding Charge")})]
    rows = build_pilot_rows(snapshot, {}, _group_of_none)
    assert rows[0].ship_type_id is None
    assert rows[0].cells[0].verdict is Verdict.UNKNOWN


def test_build_rows_over_limit_flag():
    charges = {
        (SHIELD, "Active Shielding Charge"),
        (SHIELD, "Shield Extension Charge"),
        (SHIELD, "Shield Harmonizing Charge"),
        (ARMOR, "Rapid Repair Charge"),
    }
    rows = build_pilot_rows([("Greedy", charges)], {}, _group_of_none)
    assert rows[0].charge_count == 4
    assert rows[0].over_limit is True


def test_build_rows_uses_group_resolver_for_unknown_hull():
    calls = []

    def group_of(type_id):
        calls.append(type_id)
        return 1201  # combat BC

    snapshot = [("BC Pilot", {(SHIELD, "Active Shielding Charge")})]
    rows = build_pilot_rows(snapshot, {"bc pilot": UNKNOWN_HULL}, group_of)
    assert calls == [UNKNOWN_HULL]
    assert rows[0].cells[0].verdict is Verdict.FITS_NO_BONUS


def test_build_rows_sorted_by_name():
    snap = [("zeta", {(SHIELD, "Active Shielding Charge")}),
            ("alpha", {(ARMOR, "Rapid Repair Charge")})]
    rows = build_pilot_rows(snap, {}, _group_of_none)
    assert [r.name for r in rows] == ["alpha", "zeta"]


def test_glyph_and_text_helpers_cover_all_verdicts():
    for v in Verdict:
        assert v in VERDICT_GLYPH
    assert "bonused" in verdict_text(Verdict.BONUSED, "Shield", ["Active Shielding Charge"])
    assert "subsystem" in verdict_text(Verdict.BONUSED_CONDITIONAL, "Shield", ["x"])
    assert "cannot fit" in verdict_text(Verdict.CANT_FIT, "Shield", ["x"], ship_name="Rifter")


# ── Capital hull verdicts ─────────────────────────────────────────────────────
GRP_CARRIER = 547
GRP_FAX = 1538
GRP_TITAN = 30

ARCHON = 23757    # Amarr Carrier  — Armor + Information
APOSTLE = 37604   # Amarr FAX      — Armor + Information
CHIMERA = 23915   # Caldari Carrier — Shield + Information
AVATAR = 11567    # Amarr Titan    — no strength bonus (range only)


def test_carrier_bonused_for_correct_disciplines():
    assert evaluate_discipline(ARMOR, ARCHON, group_id=GRP_CARRIER) is Verdict.BONUSED
    assert evaluate_discipline(INFORMATION, ARCHON, group_id=GRP_CARRIER) is Verdict.BONUSED


def test_carrier_not_bonused_for_wrong_discipline():
    assert evaluate_discipline(SKIRMISH, ARCHON, group_id=GRP_CARRIER) is Verdict.FITS_NO_BONUS
    assert evaluate_discipline(SHIELD, ARCHON, group_id=GRP_CARRIER) is Verdict.FITS_NO_BONUS


def test_fax_has_two_bonused_disciplines():
    assert evaluate_discipline(ARMOR, APOSTLE, group_id=GRP_FAX) is Verdict.BONUSED
    assert evaluate_discipline(INFORMATION, APOSTLE, group_id=GRP_FAX) is Verdict.BONUSED


def test_fax_not_bonused_for_wrong_discipline():
    assert evaluate_discipline(SHIELD, APOSTLE, group_id=GRP_FAX) is Verdict.FITS_NO_BONUS
    assert evaluate_discipline(SKIRMISH, APOSTLE, group_id=GRP_FAX) is Verdict.FITS_NO_BONUS


def test_caldari_carrier_bonused_for_shield_and_information():
    assert evaluate_discipline(SHIELD, CHIMERA, group_id=GRP_CARRIER) is Verdict.BONUSED
    assert evaluate_discipline(INFORMATION, CHIMERA, group_id=GRP_CARRIER) is Verdict.BONUSED
    assert evaluate_discipline(ARMOR, CHIMERA, group_id=GRP_CARRIER) is Verdict.FITS_NO_BONUS


def test_titan_fits_no_bonus_any_discipline():
    assert evaluate_discipline(ARMOR, AVATAR, group_id=GRP_TITAN) is Verdict.FITS_NO_BONUS
    assert evaluate_discipline(INFORMATION, AVATAR, group_id=GRP_TITAN) is Verdict.FITS_NO_BONUS
