# tests/test_fleet_template_store.py
from fleet_template_store import (
    FleetTemplate, Wing, Squad, Slot, RuleCondition, RuleAction,
    AssignmentRule, RebalanceSettings, template_to_dict, template_from_dict,
)


def _sample_template():
    return FleetTemplate(
        id="t1",
        name="Standard Armor Fleet",
        doctrine_id="d1",
        wings=[Wing(name="Alpha Wing", max_size=None, squads=[
            Squad(name="Logi Squad", max_size=10, slots=[
                Slot(character="Kyra Dawnfall", tag=None, role="squad_commander"),
                Slot(character=None, tag="Logistics", role="squad_member"),
                Slot(character=None, tag=None, role="squad_member"),
            ]),
        ])],
        rules=[
            AssignmentRule(priority=0,
                           condition=RuleCondition("doctrine_tag", "Links"),
                           action=RuleAction("squad_commander", "Alpha Wing", "Logi Squad")),
            AssignmentRule(priority=1,
                           condition=RuleCondition("ship_type", "Damnation"),
                           action=RuleAction("squad_commander", "Alpha Wing", None)),
        ],
        settings=RebalanceSettings(),
    )


def test_template_round_trips_through_dict():
    t = _sample_template()
    again = template_from_dict(template_to_dict(t))
    assert again == t


def test_defaults_on_rebalance_settings():
    s = RebalanceSettings()
    assert s.rebalance_interval_s == 60
    assert s.move_cooldown_s == 45
    assert s.bulk_apply_threshold == 5
    assert s.overflow_strategy == "least_populated"


# append to tests/test_fleet_template_store.py
from fleet_template_store import FleetTemplateStore


def test_add_get_rename_delete_and_persist(tmp_path):
    path = str(tmp_path / "fleet_templates.json")
    store = FleetTemplateStore(path)
    store.load()                       # missing file → empty, no error
    t = store.add_template("My Fleet")
    assert store.get_template(t.id) is t
    store.rename_template(t.id, "Renamed Fleet")
    store.save()

    reloaded = FleetTemplateStore(path)
    reloaded.load()
    assert len(reloaded.templates) == 1
    assert reloaded.templates[0].name == "Renamed Fleet"

    reloaded.delete_template(t.id)
    reloaded.save()
    assert reloaded.get_template(t.id) is None

    fresh = FleetTemplateStore(path)
    fresh.load()
    assert fresh.templates == []


def test_load_corrupt_file_is_empty_not_crash(tmp_path):
    path = tmp_path / "fleet_templates.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = FleetTemplateStore(str(path))
    store.load()
    assert store.templates == []
    assert store.cached_characters == []


# append to tests/test_fleet_template_store.py
from fleet_template_store import validate_template


def test_validate_flags_broken_wing_and_squad_refs():
    t = FleetTemplate(
        id="t", name="n", doctrine_id=None,
        wings=[Wing("Alpha Wing", None, [Squad("Logi Squad", None, [])])],
        rules=[
            AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                           RuleAction("squad_commander", "Alpha Wing", "Logi Squad")),
            AssignmentRule(1, RuleCondition("ship_type", "Guardian"),
                           RuleAction("squad_member", "Ghost Wing", None)),   # wing gone
            AssignmentRule(2, RuleCondition("ship_type", "Scimitar"),
                           RuleAction("squad_member", "Alpha Wing", "Ghost Squad")),  # squad gone
            AssignmentRule(3, RuleCondition("ship_type", "Eos"),
                           RuleAction("squad_member", None, None)),           # anywhere = OK
            AssignmentRule(4, RuleCondition("ship_type", "Sleipnir"),
                           RuleAction("squad_member", None, "Logi Squad")),   # squad w/o wing = broken
        ],
    )
    validate_template(t)
    assert [r.broken for r in t.rules] == [False, True, True, False, True]


# append to tests/test_fleet_template_store.py
def test_cache_character_dedups_case_insensitively(tmp_path):
    store = FleetTemplateStore(str(tmp_path / "f.json"))
    assert store.cache_character("Kyra Dawnfall") is True
    assert store.cache_character("  kyra dawnfall  ") is False   # dup (ci, trimmed)
    assert store.cache_character("Alt Pilot") is True
    assert store.cache_character("") is False
    assert store.cache_character("   ") is False
    assert store.cached_characters == ["Kyra Dawnfall", "Alt Pilot"]


def test_load_skips_malformed_template_entry_without_crashing(tmp_path):
    import json
    path = tmp_path / "fleet_templates.json"
    # One good template, one structurally broken (wings is a string, not a list).
    path.write_text(json.dumps({
        "version": 1,
        "templates": [
            {"id": "good", "name": "Good", "doctrine_id": None,
             "wings": [], "rules": [], "settings": {}},
            {"id": "bad", "name": "Bad", "wings": "not-a-list"},
        ],
        "cached_characters": [],
    }), encoding="utf-8")
    store = FleetTemplateStore(str(path))
    store.load()   # must not raise
    ids = [t.id for t in store.templates]
    assert "good" in ids
    assert "bad" not in ids
