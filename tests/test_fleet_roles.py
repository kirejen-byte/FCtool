"""Tests for find_character_role — resolve a preview character's fleet role.

Task B4: given a character name (case-insensitive) or resolved character_id,
walk the store's templates → wings → squads → slots and return
(role, wing_name, squad_name) for the first named slot that matches, else None.
"""
from fleet_template_store import (
    FleetTemplate,
    Slot,
    Squad,
    Wing,
    find_character_role,
    FleetTemplateStore,
)


def _store_with(*templates) -> FleetTemplateStore:
    store = FleetTemplateStore(path="unused.json")
    store.templates = list(templates)
    return store


def _template_with_slot(slot: Slot, *, wing: str, squad: str) -> FleetTemplate:
    return FleetTemplate(
        id="t1", name="T1", doctrine_id=None,
        wings=[Wing(name=wing, max_size=None,
                    squads=[Squad(name=squad, max_size=None, slots=[slot])])],
    )


def test_find_character_role_matches_name_case_insensitive_and_id():
    slot = Slot(character="Kirejen", tag=None, role="fleet_commander",
                character_id=91)
    store = _store_with(_template_with_slot(slot, wing="W1", squad="S1"))
    assert find_character_role(store, "kirejen") == ("fleet_commander", "W1", "S1")
    assert find_character_role(store, "KIREJEN") == ("fleet_commander", "W1", "S1")
    assert find_character_role(store, 91) == ("fleet_commander", "W1", "S1")
    assert find_character_role(store, "nobody") is None
    assert find_character_role(store, 999) is None


def test_find_character_role_ignores_generic_slots():
    generic = Slot(character=None, tag="Logistics", role="squad_member")
    store = _store_with(_template_with_slot(generic, wing="W1", squad="S1"))
    assert find_character_role(store, "anyone") is None


def test_find_character_role_returns_first_match_across_templates():
    slot_a = Slot(character="Kirejen", tag=None, role="wing_commander",
                  character_id=91)
    slot_b = Slot(character="Kirejen", tag=None, role="squad_member",
                  character_id=91)
    store = _store_with(
        _template_with_slot(slot_a, wing="Wa", squad="Sa"),
        _template_with_slot(slot_b, wing="Wb", squad="Sb"),
    )
    assert find_character_role(store, "kirejen") == ("wing_commander", "Wa", "Sa")


def test_find_character_role_none_query_is_safe():
    slot = Slot(character="Kirejen", tag=None, role="fleet_commander",
                character_id=91)
    store = _store_with(_template_with_slot(slot, wing="W1", squad="S1"))
    assert find_character_role(store, None) is None
    assert find_character_role(store, "") is None


CHIP = {"fleet_commander": "FC", "wing_commander": "WC",
        "squad_commander": "SC", "squad_member": ""}
