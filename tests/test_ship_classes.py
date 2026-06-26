import pytest

import ship_classes
from ship_classes import (
    is_ship_type,
    cyno_loss_hull_class,
    has_cyno_module,
    CYNO_LOSS_GROUPS,
    CYNO_MODULE_IDS,
)


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


# ── CynoCheck additions: cyno_loss_hull_class / has_cyno_module ──────────────

@pytest.mark.parametrize(
    "gid,label",
    [
        (894, "HIC"),
        (833, "Force Recon"),
        (963, "Strategic Cruiser"),
        (834, "Stealth Bomber"),
        (830, "Covert Ops"),
    ],
)
def test_cyno_loss_hull_class_matches_each_group(monkeypatch, gid, label):
    """Each of the five cyno-capable groups maps to its expected label,
    resolved via get_group_id (mocked here so no ESI call happens)."""
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: gid)
    assert cyno_loss_hull_class(12345) == label


def test_cyno_loss_hull_class_non_cyno_group_returns_none(monkeypatch):
    """A hull in a non-cyno group (e.g. 26 = Cruiser) yields None."""
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: 26)
    assert cyno_loss_hull_class(621) is None


def test_cyno_loss_hull_class_unresolved_group_returns_none(monkeypatch):
    """When get_group_id can't resolve the type (None), return None, not a crash."""
    monkeypatch.setattr(ship_classes, "get_group_id", lambda tid: None)
    assert cyno_loss_hull_class(99999999) is None


def test_cyno_loss_hull_class_falsy_type_id_returns_none(monkeypatch):
    """A falsy type_id (0 / None) short-circuits to None without an ESI lookup."""
    called = False

    def _boom(tid):
        nonlocal called
        called = True
        return 894

    monkeypatch.setattr(ship_classes, "get_group_id", _boom)
    assert cyno_loss_hull_class(0) is None
    assert cyno_loss_hull_class(None) is None
    assert called is False  # never consulted ESI for a falsy id


def test_cyno_loss_groups_constant_shape():
    """Guard the exact group->label contract the GUI/backend rely on."""
    assert CYNO_LOSS_GROUPS == {
        894: "HIC",
        833: "Force Recon",
        963: "Strategic Cruiser",
        834: "Stealth Bomber",
        830: "Covert Ops",
    }


def test_has_cyno_module_normal_cyno():
    assert has_cyno_module([21096]) is True


def test_has_cyno_module_covert_cyno():
    assert has_cyno_module([28646]) is True


def test_has_cyno_module_mixed_fit_with_cyno():
    # A realistic high-slot list with a covert cyno among other modules.
    assert has_cyno_module([3520, 28646, 31716]) is True


def test_has_cyno_module_no_cyno():
    assert has_cyno_module([3520, 31716, 2456]) is False


def test_has_cyno_module_empty_and_none():
    assert has_cyno_module([]) is False
    assert has_cyno_module(None) is False


def test_has_cyno_module_ignores_non_ids():
    # Set of ints works too; spurious None entry doesn't match.
    assert has_cyno_module({None, 21096}) is True
    assert has_cyno_module({None, 12345}) is False


def test_cyno_module_ids_constant():
    assert CYNO_MODULE_IDS == {21096, 28646}


def test_new_cyno_groups_in_known_ship_groups():
    """All five cyno hull groups are recognized as ship groups (for prewarm /
    d-scan classification)."""
    for gid in (894, 833, 963, 834, 830):
        assert gid in ship_classes._SHIP_GROUP_IDS_KNOWN

def test_capsule_type_ids():
    import ship_classes
    assert ship_classes.CAPSULE_TYPE_IDS == {670, 33328}
