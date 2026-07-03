"""Pure tests for overlay_rules: label_for precedence, rule kinds, placeholders."""
from overlay_rules import CharState, OverlayRule, label_for, seed_rules


def _state(**kw):
    base = dict(
        character_id=1, name="Alpha", online=True, ship_type_id=100,
        ship_type_name="Falcon", ship_group="Force Recon Ship",
        is_capital=False, solar_system_id=30000142, system_name="Jita",
        docked=False,
    )
    base.update(kw)
    return CharState(**base)


def test_override_beats_rules():
    st = _state()
    rules = [OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    # override keyed by lowercase name wins
    assert label_for(st, rules, {"alpha": "MANUAL"}) == "MANUAL"


def test_empty_override_hides_label():
    st = _state()
    rules = [OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    # an override present-but-empty hides the character's overlay entirely
    assert label_for(st, rules, {"alpha": ""}) == ""


def test_first_matching_rule_wins():
    st = _state()
    rules = [
        OverlayRule("ship_group", "Black Ops", "Bridger"),
        OverlayRule("ship_group", "Force Recon Ship", "Cyno"),
        OverlayRule("ship_group", "Force Recon Ship", "SECOND"),
    ]
    assert label_for(st, rules, {}) == "Cyno"


def test_no_match_returns_empty():
    st = _state(ship_group="Shuttle")
    rules = [OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    assert label_for(st, rules, {}) == ""


def test_when_ship_type():
    st = _state(ship_type_name="Falcon")
    assert label_for(st, [OverlayRule("ship_type", "falcon", "Jam")], {}) == "Jam"


def test_when_system():
    st = _state(system_name="Jita")
    assert label_for(st, [OverlayRule("system", "jita", "Trade")], {}) == "Trade"


def test_when_docked():
    st = _state(docked=True)
    assert label_for(st, [OverlayRule("docked", "", "Docked")], {}) == "Docked"
    st2 = _state(docked=False)
    assert label_for(st2, [OverlayRule("docked", "", "Docked")], {}) == ""


def test_when_offline():
    st = _state(online=False)
    assert label_for(st, [OverlayRule("offline", "", "AFK")], {}) == "AFK"
    # online None (unknown) must NOT match offline
    st_unknown = _state(online=None)
    assert label_for(st_unknown, [OverlayRule("offline", "", "AFK")], {}) == ""


def test_when_capital_and_subcap():
    cap = _state(is_capital=True)
    sub = _state(is_capital=False)
    assert label_for(cap, [OverlayRule("capital", "", "Cap")], {}) == "Cap"
    assert label_for(sub, [OverlayRule("capital", "", "Cap")], {}) == ""
    assert label_for(sub, [OverlayRule("subcap", "", "Sub")], {}) == "Sub"
    assert label_for(cap, [OverlayRule("subcap", "", "Sub")], {}) == ""


def test_placeholders_filled():
    st = _state(ship_type_name="Widow", ship_group="Black Ops", system_name="Jita")
    r = OverlayRule("ship_group", "Black Ops", "{ship} in {system} ({group})")
    assert label_for(st, [r], {}) == "Widow in Jita (Black Ops)"


def test_case_insensitive_match():
    st = _state(ship_group="Force Recon Ship")
    assert label_for(st, [OverlayRule("ship_group", "FORCE recon SHIP", "Cyno")], {}) == "Cyno"


def test_override_case_insensitive_key():
    st = _state(name="Alpha")
    assert label_for(st, [], {"ALPHA": "X"}) == "X"


def test_seed_rules_shape():
    rules = seed_rules()
    assert all(isinstance(r, OverlayRule) for r in rules)
    kinds = {(r.when, r.value.lower()) for r in rules}
    assert ("ship_group", "force recon ship") in kinds
    assert ("ship_group", "black ops") in kinds
    assert any(r.when == "capital" for r in rules)
