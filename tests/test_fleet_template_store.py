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
