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
    assert s.sync_active_s == 10
    assert s.sync_idle_s == 30
    assert s.move_spacing_ms == 400
    assert s.burst_cap == 25
    assert s.settle_s == 3
    assert s.bulk_apply_threshold == 5
    # dropped in v2:
    assert not hasattr(s, "overflow_strategy")
    assert not hasattr(s, "rebalance_interval_s")
    assert not hasattr(s, "move_cooldown_s")


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
    assert store.cached_characters == [
        {"name": "Kyra Dawnfall", "character_id": None},
        {"name": "Alt Pilot", "character_id": None},
    ]


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


# ── Phase A: schema v2 + migration ───────────────────────────────────────────
import json as _json
from fleet_template_store import (
    SCHEMA_VERSION, validate_template, _migrate_template_v1,
)


def test_schema_version_is_2():
    assert SCHEMA_VERSION == 2


def test_slot_carries_character_id_round_trip():
    s = Slot(character="Kyra", tag=None, role="squad_member")
    s.character_id = 42
    again = template_from_dict(template_to_dict(
        FleetTemplate(id="t", name="n", doctrine_id=None,
                      wings=[Wing("W", None, [Squad("S", None, [s])])])))
    assert again.wings[0].squads[0].slots[0].character_id == 42


def test_rebalance_settings_v2_fields():
    s = RebalanceSettings()
    assert s.sync_active_s == 10
    assert s.sync_idle_s == 30
    assert s.move_spacing_ms == 400
    assert s.burst_cap == 25
    assert s.settle_s == 3
    assert s.bulk_apply_threshold == 5
    # dropped in v2:
    assert not hasattr(s, "overflow_strategy")


def test_settings_v2_round_trip():
    s = RebalanceSettings(sync_active_s=8, burst_cap=15, bulk_apply_threshold=7)
    again = template_from_dict(template_to_dict(
        FleetTemplate(id="t", name="n", doctrine_id=None, settings=s))).settings
    assert (again.sync_active_s, again.burst_cap, again.bulk_apply_threshold) == (8, 15, 7)


def test_wing_max_size_dropped_from_serialization():
    d = template_to_dict(FleetTemplate(id="t", name="n", doctrine_id=None,
                                       wings=[Wing("W", 99, [])]))
    assert "max_size" not in d["wings"][0]


def test_migrate_v1_tagged_slots_become_rules_and_clear_tags():
    t = FleetTemplate(
        id="t", name="n", doctrine_id=None,
        wings=[Wing("Alpha", None, [Squad("Logi", None, [
            Slot(character=None, tag="Logistics", role="squad_commander"),
            Slot(character=None, tag="Logistics", role="squad_member"),  # dup tag
            Slot(character=None, tag="Links", role="squad_member"),
            Slot(character="Kyra", tag=None, role="squad_member"),        # named untouched
            Slot(character=None, tag=None, role="squad_member"),          # generic untouched
        ])])],
        rules=[AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                              RuleAction("wing_commander", "Alpha", None))],
    )
    _migrate_template_v1(t)
    # Tags cleared on every slot.
    assert all(s.tag is None for s in t.wings[0].squads[0].slots)
    # Named slot preserved.
    assert t.wings[0].squads[0].slots[3].character == "Kyra"
    # Two new doctrine_tag rules (Logistics, Links) appended after the existing rule,
    # priorities continuing sequentially, routing to Alpha/Logi.
    new = [r for r in t.rules if r.condition.type == "doctrine_tag"]
    assert {r.condition.value for r in new} == {"Logistics", "Links"}
    for r in new:
        assert (r.action.wing_name, r.action.squad_name) == ("Alpha", "Logi")
    assert [r.priority for r in t.rules] == [0, 1, 2]
    # Logistics rule took the first Logistics slot's role (squad_commander).
    logi = next(r for r in new if r.condition.value == "Logistics")
    assert logi.action.role == "squad_commander"


def test_migrate_v1_no_tags_is_noop_on_rules():
    t = FleetTemplate(id="t", name="n", doctrine_id=None,
                      wings=[Wing("A", None, [Squad("S", None, [
                          Slot(character="X", tag=None, role="squad_member")])])],
                      rules=[])
    _migrate_template_v1(t)
    assert t.rules == []


def test_load_migrates_v1_file(tmp_path):
    path = tmp_path / "fleet_templates.json"
    v1 = {
        "version": 1,
        "templates": [{
            "id": "t1", "name": "Old", "doctrine_id": None,
            "wings": [{"name": "A", "max_size": 5, "squads": [
                {"name": "S", "max_size": 3, "slots": [
                    {"character": None, "tag": "DPS", "role": "squad_member"},
                ]}]}],
            "rules": [],
            "settings": {"rebalance_interval_s": 60, "move_cooldown_s": 45,
                         "bulk_apply_threshold": 5, "overflow_strategy": "least_populated"},
        }],
        "cached_characters": ["Kyra Dawnfall", "Bob"],
    }
    path.write_text(_json.dumps(v1), encoding="utf-8")
    store = FleetTemplateStore(str(path))
    store.load()
    t = store.templates[0]
    assert all(s.tag is None for s in t.wings[0].squads[0].slots)
    assert any(r.condition.type == "doctrine_tag" and r.condition.value == "DPS"
               for r in t.rules)
    # cached_characters upgraded to dicts.
    assert store.cached_characters == [
        {"name": "Kyra Dawnfall", "character_id": None},
        {"name": "Bob", "character_id": None},
    ]
    assert store.cached_id("kyra dawnfall") is None
    assert store.cached_character_names() == ["Kyra Dawnfall", "Bob"]


def test_load_refuses_future_version(tmp_path, caplog):
    path = tmp_path / "fleet_templates.json"
    original = _json.dumps({"version": 99, "templates": [
        {"id": "x", "name": "future", "doctrine_id": None, "wings": [], "rules": [],
         "settings": {}}]})
    path.write_text(original, encoding="utf-8")
    store = FleetTemplateStore(str(path))
    store.load()
    assert store.templates == []           # refused, empty
    # File left untouched (no forced save).
    assert path.read_text(encoding="utf-8") == original


def test_cache_character_upsert_fills_id():
    store = FleetTemplateStore("unused")
    assert store.cache_character("Kyra") is True          # new
    assert store.cache_character("kyra", 123) is False    # existing name → no new row
    assert store.cached_id("KYRA") == 123                 # but id was filled in
    assert store.cached_id("nobody") is None


def test_cached_character_names_returns_flat_deduped_names():
    store = FleetTemplateStore("unused")
    store.cache_character("Kyra Dawnfall", 7)
    store.cache_character("  kyra dawnfall  ", 7)          # dup (ci, trimmed) → no new row
    store.cache_character("Alt Pilot")                     # id-less entry still listed
    names = store.cached_character_names()
    assert names == ["Kyra Dawnfall", "Alt Pilot"]        # flat strings, first-seen casing, deduped
    assert all(isinstance(n, str) for n in names)         # never dicts (fc_gui does set(names))


def test_cached_characters_v2_round_trip(tmp_path):
    path = tmp_path / "fleet_templates.json"
    store = FleetTemplateStore(str(path))
    store.cache_character("Kyra", 7)
    store.save()
    store2 = FleetTemplateStore(str(path))
    store2.load()
    assert store2.cached_id("kyra") == 7


def test_validate_marks_extra_default_rules_broken():
    t = FleetTemplate(id="t", name="n", doctrine_id=None,
                      wings=[Wing("A", None, [Squad("S", None, [])])],
                      rules=[
                          AssignmentRule(0, RuleCondition("default", ""),
                                         RuleAction("squad_member", "A", "S")),
                          AssignmentRule(1, RuleCondition("default", ""),
                                         RuleAction("squad_member", "A", "S")),
                      ])
    validate_template(t)
    assert t.rules[0].broken is False
    assert t.rules[1].broken is True      # second default → broken


def test_validate_default_rule_still_checks_dangling_ref():
    t = FleetTemplate(id="t", name="n", doctrine_id=None,
                      wings=[Wing("A", None, [Squad("S", None, [])])],
                      rules=[AssignmentRule(0, RuleCondition("default", ""),
                                            RuleAction("squad_member", "Ghost", "Nope"))])
    validate_template(t)
    assert t.rules[0].broken is True      # dangling wing/squad


# append — Phase B: dropped rebalance settings fields
def test_settings_dict_has_no_dropped_fields():
    from fleet_template_store import RebalanceSettings, _settings_to_dict
    d = _settings_to_dict(RebalanceSettings())
    assert "rebalance_interval_s" not in d
    assert "move_cooldown_s" not in d
    assert not hasattr(RebalanceSettings(), "rebalance_interval_s")


def test_old_v2_file_with_dropped_keys_still_loads(tmp_path):
    import json
    from fleet_template_store import FleetTemplateStore
    p = tmp_path / "fleet_templates.json"
    p.write_text(json.dumps({
        "version": 2,
        "templates": [{"id": "x", "name": "T", "doctrine_id": None, "wings": [],
                       "rules": [], "settings": {"rebalance_interval_s": 60,
                                                 "move_cooldown_s": 45,
                                                 "sync_active_s": 12}}],
        "cached_characters": [],
    }), encoding="utf-8")
    s = FleetTemplateStore(str(p))
    s.load()   # unknown keys ignored, known keys honored
    assert s.templates[0].settings.sync_active_s == 12


def test_build_template_from_live_full():
    from datetime import datetime
    from fleet_template_store import build_template_from_live

    live_structure = {"wings": [
        {"id": 1, "name": "Assault Wing", "squads": [
            {"id": 10, "name": "DPS Squad"},
            {"id": 11, "name": "Logi Squad"},
        ]},
        {"id": 2, "name": "Tackle Wing", "squads": [
            {"id": 20, "name": "Fast Tackle"},
        ]},
    ]}
    live_members = [
        # FC (no wing/squad in EVE's boss slot)
        {"character_id": 100, "name": "Boss", "role": "fleet_commander",
         "wing_id": -1, "squad_id": -1},
        # WC of wing 1
        {"character_id": 101, "name": "WingLead", "role": "wing_commander",
         "wing_id": 1, "squad_id": -1},
        # SC of squad 10
        {"character_id": 102, "name": "SquadLead", "role": "squad_commander",
         "wing_id": 1, "squad_id": 10},
        # plain member of squad 10
        {"character_id": 103, "name": "Grunt", "role": "squad_member",
         "wing_id": 1, "squad_id": 10},
        # member of squad 20
        {"character_id": 104, "name": "Scout", "role": "squad_member",
         "wing_id": 2, "squad_id": 20},
        # an unplaced pilot (no known wing/squad) — must NOT create a slot
        {"character_id": 105, "name": "Floater", "role": "squad_member",
         "wing_id": -1, "squad_id": -1},
    ]
    now = datetime(2026, 7, 2, 20, 30)
    t = build_template_from_live(live_members, live_structure,
                                 now=now, new_id="abc123")

    assert t.id == "abc123"
    assert t.name == "Import 2026-07-02 20:30"
    # Structure preserved by display name, in order.
    assert [w.name for w in t.wings] == ["Assault Wing", "Tackle Wing"]
    assert [s.name for s in t.wings[0].squads] == ["DPS Squad", "Logi Squad"]
    # Squad 10 has SC + grunt as named slots; SC keeps its role.
    dps = t.wings[0].squads[0]
    by_char = {s.character: s for s in dps.slots}
    assert by_char["SquadLead"].role == "squad_commander"
    assert by_char["SquadLead"].character_id == 102
    assert by_char["Grunt"].role == "squad_member"
    assert by_char["Grunt"].character_id == 103
    # Logi squad is empty (no members) but still exists.
    assert t.wings[0].squads[1].slots == []
    # Floater (unplaced) produced no slot anywhere.
    all_chars = {s.character for w in t.wings for sq in w.squads for s in sq.slots}
    assert "Floater" not in all_chars
    # FC/WC are commanders but not squad-members: they should NOT appear as
    # squad slots (they have no squad). No slot carries character_id 100 or 101.
    all_ids = {s.character_id for w in t.wings for sq in w.squads for s in sq.slots}
    assert 100 not in all_ids and 101 not in all_ids


def test_build_template_from_live_empty_fleet():
    from datetime import datetime
    from fleet_template_store import build_template_from_live
    t = build_template_from_live([], {"wings": []},
                                 now=datetime(2026, 1, 1, 0, 0), new_id="x")
    assert t.wings == []
    assert t.name == "Import 2026-01-01 00:00"


def test_add_template_seeds_one_wing_one_squad():
    from fleet_template_store import FleetTemplateStore
    store = FleetTemplateStore("unused.json")
    t = store.add_template("Fresh")
    assert [w.name for w in t.wings] == ["Wing 1"]
    assert [s.name for s in t.wings[0].squads] == ["Squad 1"]
    assert t.wings[0].squads[0].slots == []


def test_duplicate_template_deep_copies_with_copy_suffix(tmp_path):
    from fleet_template_store import (FleetTemplateStore, Wing, Squad, Slot,
                                      AssignmentRule, RuleCondition, RuleAction)
    store = FleetTemplateStore(str(tmp_path / "f.json"))
    src = store.add_template("Doctrine A")
    src.wings = [Wing("W", None, [Squad("S", 5, [Slot("Kyra", None,
                                                      "squad_member", 95)])])]
    src.rules = [AssignmentRule(0, RuleCondition("capital", ""),
                               RuleAction("squad_member", "W", "S"))]
    dup = store.duplicate_template(src.id)
    assert dup is not None
    assert dup.id != src.id
    assert dup.name == "Doctrine A (copy)"
    assert store.templates[-1] is dup
    # Deep copy: mutating the copy must not touch the source.
    dup.wings[0].squads[0].slots[0].character = "Someone Else"
    dup.rules[0].condition.type = "subcap"
    assert src.wings[0].squads[0].slots[0].character == "Kyra"
    assert src.rules[0].condition.type == "capital"


def test_duplicate_missing_template_returns_none(tmp_path):
    from fleet_template_store import FleetTemplateStore
    store = FleetTemplateStore(str(tmp_path / "f.json"))
    assert store.duplicate_template("nope") is None


def test_ui_dict_round_trips(tmp_path):
    from fleet_template_store import FleetTemplateStore
    path = str(tmp_path / "f.json")
    store = FleetTemplateStore(path)
    store.load()
    store.ui["geometry"] = "1024x700+40+40"
    store.save()
    reloaded = FleetTemplateStore(path)
    reloaded.load()
    assert reloaded.ui.get("geometry") == "1024x700+40+40"


def test_ui_defaults_empty_when_absent(tmp_path):
    from fleet_template_store import FleetTemplateStore
    store = FleetTemplateStore(str(tmp_path / "missing.json"))
    store.load()
    assert store.ui == {}
