# tests/test_fleet_composer.py
from fleet_composer import Move, ComposeResult, build_tag_index


class _FakeFit:
    def __init__(self, fit_id, hull_type_id):
        self.id = fit_id
        self.hull_type_id = hull_type_id


class _FakeMember:
    def __init__(self, fit_id, tags):
        self.fit_id = fit_id
        self.tags = tags


class _FakeDoctrine:
    def __init__(self, members):
        self.members = members


class _FakeFittings:
    def __init__(self, fits):
        self._fits = {f.id: f for f in fits}

    def get_fit(self, fit_id):
        return self._fits.get(fit_id)


def test_build_tag_index_unions_tags_per_hull():
    fits = [_FakeFit("f-dam", 22474), _FakeFit("f-guard", 11987)]
    doctrine = _FakeDoctrine([
        _FakeMember("f-dam", ["Links"]),
        _FakeMember("f-dam", ["DPS"]),       # same hull, second member → union
        _FakeMember("f-guard", ["Logistics"]),
    ])
    idx = build_tag_index(doctrine, _FakeFittings(fits))
    assert idx[22474] == {"Links", "DPS"}
    assert idx[11987] == {"Logistics"}


def test_build_tag_index_empty_without_doctrine():
    assert build_tag_index(None, _FakeFittings([])) == {}


def test_move_defaults_to_executable():
    m = Move(pilot_id=1, pilot_name="X", target_wing_name="W",
             target_squad_name="S", target_role="squad_member")
    assert m.skip_reason is None


# append to tests/test_fleet_composer.py
from fleet_composer import compose
from fleet_template_store import (
    FleetTemplate, Wing, Squad, Slot, RuleCondition, RuleAction,
    AssignmentRule, RebalanceSettings,
)


def _member(cid, name, tname, role="squad_member", wing_id=None, squad_id=None,
            join="2026-01-01T00:00:00Z", ship_type_id=0):
    return {"character_id": cid, "name": name, "ship_type_id": ship_type_id,
            "ship_type_name": tname, "role": role, "wing_id": wing_id,
            "squad_id": squad_id, "join_time": join}


# Empty live structure (no wings exist yet) for tests that only check targeting.
_EMPTY_STRUCT = {"wings": []}


def _template(wings, rules=None):
    return FleetTemplate(id="t", name="n", doctrine_id=None, wings=wings,
                         rules=rules or [], settings=RebalanceSettings())


def test_named_slot_exact_match_case_insensitive():
    t = _template([Wing("Alpha Wing", None, [Squad("Logi Squad", None, [
        Slot(character="Kyra Dawnfall", tag=None, role="squad_commander"),
    ])])])
    members = [_member(1, "kyra dawnfall", "Archon")]
    res = compose(t, members, _EMPTY_STRUCT)
    assert len(res.executable) == 1
    mv = res.executable[0]
    assert (mv.pilot_id, mv.target_wing_name, mv.target_squad_name, mv.target_role) \
        == (1, "Alpha Wing", "Logi Squad", "squad_commander")
    assert res.unassigned == []


def test_named_slot_missing_pilot_leaves_slot_empty_no_move():
    t = _template([Wing("Alpha Wing", None, [Squad("Logi Squad", None, [
        Slot(character="Absent Pilot", tag=None, role="squad_commander"),
    ])])])
    res = compose(t, [_member(1, "Someone Else", "Rifter")], _EMPTY_STRUCT)
    # The named slot generates no move; the unmatched pilot is Unassigned.
    assert res.executable == []
    assert [m["character_id"] for m in res.unassigned] == [1]


def test_rule_priority_first_matching_rule_wins():
    t = _template(
        [Wing("Alpha Wing", None, [Squad("Cmd Squad", None, [])])],
        rules=[
            AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                           RuleAction("squad_commander", "Alpha Wing", "Cmd Squad")),
            AssignmentRule(1, RuleCondition("ship_type", "Damnation"),
                           RuleAction("squad_member", "Alpha Wing", "Cmd Squad")),
        ],
    )
    res = compose(t, [_member(1, "Boss", "Damnation")], _EMPTY_STRUCT)
    assert res.executable[0].target_role == "squad_commander"   # priority-0 rule won


def test_six_damnations_one_rule_caps_at_five_sc_slots():
    # v2: one routing rule into a max_size-5 squad. 6 Damnations → 5 placed,
    # the last-joined 1 overflows with a "target full" reason (no tagged slots).
    t = _template(
        [Wing("Alpha Wing", None, [Squad("Cmd", 5, [])])],
        rules=[AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                              RuleAction("squad_commander", "Alpha Wing", "Cmd"))])
    members = [_member(i, f"Pilot{i}", "Damnation",
                       join=f"2026-01-0{i + 1}T00:00:00Z") for i in range(6)]
    res = compose(t, members, _EMPTY_STRUCT)
    placed = [m for m in res.executable if m.target_squad_name == "Cmd"]
    assert len(placed) == 5
    assert all(m.target_role == "squad_commander" for m in placed)
    # exactly one pilot overflowed the cap:
    overflow = [cid for cid, why in res.unassigned_reasons.items()
                if why == "target full"]
    assert len(overflow) == 1
    assert any(m["character_id"] == overflow[0] for m in res.unassigned)


def test_already_correct_position_is_skipped():
    struct = {"wings": [{"id": 100, "name": "Alpha Wing",
                         "squads": [{"id": 200, "name": "Logi Squad"}]}]}
    t = _template([Wing("Alpha Wing", None, [Squad("Logi Squad", None, [
        Slot(character="Kyra", tag=None, role="squad_member"),
    ])])])
    members = [_member(1, "Kyra", "Guardian", role="squad_member",
                       wing_id=100, squad_id=200)]
    res = compose(t, members, struct)
    assert res.executable == []                       # no ESI write needed
    assert any(m.skip_reason == "already_correct" for m in res.moves)


def test_doctrine_tag_rule_inactive_without_doctrine():
    t = _template([Wing("Alpha Wing", None, [Squad("Cmd", None, [
        Slot(character=None, tag=None, role="squad_commander"),
    ])])],
        rules=[AssignmentRule(0, RuleCondition("doctrine_tag", "Links"),
                              RuleAction("squad_commander", "Alpha Wing", "Cmd"))])
    res = compose(t, [_member(1, "P", "Damnation")], _EMPTY_STRUCT)  # no doctrine
    # The doctrine_tag rule cannot fire → the SC slot is generic-filled instead.
    assert res.executable[0].target_role == "squad_commander"
    assert res.executable[0].pilot_id == 1


def test_broken_rule_is_skipped_and_does_not_crash():
    t = _template([Wing("Alpha Wing", None, [Squad("Cmd", None, [
        Slot(character=None, tag=None, role="squad_member"),
    ])])],
        rules=[AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                              RuleAction("squad_commander", "Ghost", None), broken=True)])
    res = compose(t, [_member(1, "P", "Damnation")], _EMPTY_STRUCT)
    # Broken rule ignored; pilot still generic-fills the member slot.
    assert res.executable[0].pilot_id == 1


def test_generic_slots_fill_in_tree_order_by_join_time():
    t = _template([Wing("W", None, [Squad("S", None, [
        Slot(character=None, tag=None, role="squad_member"),
        Slot(character=None, tag=None, role="squad_member"),
    ])])])
    members = [_member(2, "Late", "Rifter", join="2026-02-01T00:00:00Z"),
               _member(1, "Early", "Rifter", join="2026-01-01T00:00:00Z")]
    res = compose(t, members, _EMPTY_STRUCT)
    # Longest-serving (Early, join Jan) fills the first slot.
    assert [m.pilot_id for m in res.executable] == [1, 2]


def test_pilot_with_no_slot_or_rule_match_is_unassigned():
    t = _template([Wing("W", None, [Squad("S", None, [
        Slot(character="OnlyThisGuy", tag=None, role="squad_member"),
    ])])])
    res = compose(t, [_member(9, "Nobody", "Rifter")], _EMPTY_STRUCT)
    assert res.executable == []
    assert [m["character_id"] for m in res.unassigned] == [9]


def test_ship_class_condition_uses_pre_resolved_ship_class():
    t = _template([Wing("W", None, [Squad("S", None, [])])],
        rules=[AssignmentRule(0, RuleCondition("ship_class", "Logistics Cruiser"),
                              RuleAction("squad_member", "W", "S"))])
    m = _member(1, "Logi Guy", "Guardian")
    m["ship_class"] = "Logistics Cruiser"      # pre-resolved by the window
    res = compose(t, [m], _EMPTY_STRUCT)
    assert res.executable[0].pilot_id == 1


def test_rule_with_no_matching_pilot_emits_warning():
    # v2: a routing rule whose condition matches nobody in the pool emits a
    # warning that names the condition value (the Pass-2 tail; §7 replacement).
    t = _template(
        [Wing("W", None, [Squad("S", None, [])])],
        rules=[AssignmentRule(0, RuleCondition("ship_type", "Nightmare"),
                              RuleAction("squad_member", "W", "S"))])
    res = compose(t, [_member(1, "DPS Guy", "Megathron")], _EMPTY_STRUCT)
    assert res.executable == []                       # nothing routed
    assert any("Nightmare" in w for w in res.warnings) # warning names the value
    assert [m["character_id"] for m in res.unassigned] == [1]


# append to tests/test_fleet_composer.py
from fleet_composer import summarize_moves


def test_summarize_counts_repositions_role_changes_and_unfilled():
    res = ComposeResult(
        moves=[
            Move(1, "A", "W", "S1", "squad_member"),                 # reposition
            Move(2, "B", "W", "S1", "squad_commander"),              # reposition
            Move(3, "C", "W", "S1", "squad_member", skip_reason="already_correct"),
        ],
        unassigned=[{"character_id": 9}],
        warnings=["1 slot unfilled (no match): W/S2 [Logistics]"],
    )
    s = summarize_moves(res)
    assert s["executable"] == 2
    assert s["unfilled"] == 1
    assert s["unassigned"] == 1
    assert s["esi_calls"] == 2


# append to tests/test_fleet_composer.py
from fleet_composer import plan_rebalance, RebalanceAction


def _struct(wings):
    return {"wings": wings}


def test_rebalance_moves_last_joined_overflow_to_undercap_squad_same_wing():
    # Wing W: S1 cap 2 has 3 members; S2 cap 5 has 0 → move newest from S1 to S2.
    struct = _struct([{"id": 1, "name": "W", "squads": [
        {"id": 10, "name": "S1"}, {"id": 11, "name": "S2"}]}])
    members = [
        {"character_id": 1, "name": "A", "wing_id": 1, "squad_id": 10,
         "join_time": "2026-01-01T00:00:00Z"},
        {"character_id": 2, "name": "B", "wing_id": 1, "squad_id": 10,
         "join_time": "2026-01-02T00:00:00Z"},
        {"character_id": 3, "name": "C", "wing_id": 1, "squad_id": 10,
         "join_time": "2026-01-03T00:00:00Z"},   # newest → overflow
    ]
    max_sizes = {("W", "S1"): 2, ("W", "S2"): 5}
    act = plan_rebalance(members, struct, max_sizes=max_sizes)
    assert isinstance(act, RebalanceAction)
    assert act.pilot_id == 3
    assert act.target_wing_name == "W"
    assert act.target_squad_name == "S2"
    assert act.create_squad is False


def test_rebalance_returns_none_when_all_within_cap():
    struct = _struct([{"id": 1, "name": "W", "squads": [{"id": 10, "name": "S1"}]}])
    members = [{"character_id": 1, "name": "A", "wing_id": 1, "squad_id": 10,
                "join_time": "2026-01-01T00:00:00Z"}]
    assert plan_rebalance(members, struct, max_sizes={("W", "S1"): 5}) is None


def test_rebalance_signals_create_when_no_undercap_target_exists():
    struct = _struct([{"id": 1, "name": "W", "squads": [{"id": 10, "name": "S1"}]}])
    members = [{"character_id": i, "name": str(i), "wing_id": 1, "squad_id": 10,
                "join_time": f"2026-01-0{i}T00:00:00Z"} for i in (1, 2, 3)]
    act = plan_rebalance(members, struct, max_sizes={("W", "S1"): 2})
    assert act.pilot_id == 3
    assert act.target_wing_name == "W"
    assert act.create_squad is True
    assert act.target_squad_name is None


def test_already_correct_with_clamped_live_name_is_skipped():
    # Live structure names are clamped to 10 chars; template uses full names.
    struct = {"wings": [{"id": 100, "name": "Logistics ",
                         "squads": [{"id": 200, "name": "Guardians "}]}]}
    t = _template([Wing("Logistics Wing", None, [Squad("Guardians Squad", None, [
        Slot(character="Kyra", tag=None, role="squad_member"),
    ])])])
    members = [_member(1, "Kyra", "Guardian", role="squad_member",
                       wing_id=100, squad_id=200)]
    res = compose(t, members, struct)
    assert res.executable == []   # clamped names match → no redundant move
    assert any(m.skip_reason == "already_correct" for m in res.moves)


def test_plan_rebalance_caps_match_clamped_live_names():
    # Live squad name is clamped to 10 chars; max_sizes keyed by that clamped name.
    struct = {"wings": [{"id": 1, "name": "Logistics ",
                         "squads": [{"id": 10, "name": "Guardians "}]}]}
    members = [{"character_id": i, "name": str(i), "wing_id": 1, "squad_id": 10,
                "join_time": f"2026-01-0{i}T00:00:00Z"} for i in (1, 2, 3)]
    max_sizes = {("Logistics ", "Guardians "): 2}   # clamped key, cap 2, 3 members
    act = plan_rebalance(members, struct, max_sizes=max_sizes)
    assert act is not None
    assert act.pilot_id == 3        # last-joined overflow pilot


def test_live_layout_groups_members_commanders_and_unplaced():
    from fleet_composer import live_layout
    structure = {"wings": [{"id": 1, "name": "Alpha", "squads": [
        {"id": 10, "name": "Squad 1"}, {"id": 11, "name": "Logi"}]}]}
    members = [
        {"character_id": 100, "name": "Boss", "ship_type_name": "Loki",
         "role": "fleet_commander", "wing_id": -1, "squad_id": -1},
        {"character_id": 101, "name": "WCdr", "ship_type_name": "Dami",
         "role": "wing_commander", "wing_id": 1, "squad_id": -1},
        {"character_id": 102, "name": "SCdr", "ship_type_name": "Guard",
         "role": "squad_commander", "wing_id": 1, "squad_id": 11},
        {"character_id": 103, "name": "Grunt", "ship_type_name": "Megathron",
         "role": "squad_member", "wing_id": 1, "squad_id": 10},
        {"character_id": 104, "name": "Floater", "ship_type_name": "Rifter",
         "role": "squad_member", "wing_id": -1, "squad_id": -1},
    ]
    layout = live_layout(members, structure)
    assert layout["fc"]["character_id"] == 100
    w = layout["wings"][0]
    assert w["wc"]["character_id"] == 101
    assert w["squads"][0]["name"] == "Squad 1"
    assert [m["character_id"] for m in w["squads"][0]["members"]] == [103]
    assert w["squads"][1]["sc"]["character_id"] == 102
    assert [m["character_id"] for m in w["squads"][1]["members"]] == [102]
    assert [m["character_id"] for m in layout["unplaced"]] == [104]


def test_live_layout_empty_structure_all_unplaced():
    from fleet_composer import live_layout
    members = [{"character_id": 5, "name": "X", "ship_type_name": "Rifter",
                "role": "squad_member", "wing_id": -1, "squad_id": -1}]
    layout = live_layout(members, {"wings": []})
    assert layout["wings"] == []
    assert [m["character_id"] for m in layout["unplaced"]] == [5]


# ── Phase A: rules-direct composer ───────────────────────────────────────────
from fleet_composer import _pilot_matches


def _cap_member(cid, name, tname, is_capital, ship_class="Dreadnought",
                join="2026-01-01T00:00:00Z", wing_id=None, squad_id=None):
    return {"character_id": cid, "name": name, "ship_type_id": 0,
            "ship_type_name": tname, "ship_class": ship_class,
            "is_capital": is_capital, "role": "squad_member",
            "wing_id": wing_id, "squad_id": squad_id, "join_time": join}


def test_pilot_matches_capital_subcap_default():
    cap = _cap_member(1, "Cap", "Revelation", True)
    sub = _cap_member(2, "Sub", "Rifter", False, ship_class="Frigate")
    assert _pilot_matches(RuleCondition("capital", ""), cap, {}) is True
    assert _pilot_matches(RuleCondition("capital", ""), sub, {}) is False
    assert _pilot_matches(RuleCondition("subcap", ""), sub, {}) is True
    assert _pilot_matches(RuleCondition("subcap", ""), cap, {}) is False
    assert _pilot_matches(RuleCondition("default", ""), cap, {}) is True
    assert _pilot_matches(RuleCondition("default", ""), sub, {}) is True


def test_subcap_requires_a_ship_class():
    # is_capital False but no class resolved yet → not a confirmed subcap.
    m = {"character_id": 9, "name": "Unknown", "is_capital": False, "ship_class": None}
    assert _pilot_matches(RuleCondition("subcap", ""), m, {}) is False


def test_rule_routes_capitals_directly_to_squad():
    t = _template(
        [Wing("Cap Wing", None, [Squad("Dreads", None, [])])],
        rules=[AssignmentRule(0, RuleCondition("capital", ""),
                              RuleAction("squad_member", "Cap Wing", "Dreads"))])
    members = [_cap_member(1, "A", "Revelation", True),
               _cap_member(2, "B", "Rifter", False, ship_class="Frigate")]
    res = compose(t, members, _EMPTY_STRUCT)
    moved = {m.pilot_id: (m.target_wing_name, m.target_squad_name) for m in res.executable}
    assert moved == {1: ("Cap Wing", "Dreads")}      # only the capital routed
    assert [m["character_id"] for m in res.unassigned] == [2]


def test_rules_apply_in_priority_order():
    # Two rules both match the same pilot; lower priority wins.
    t = _template(
        [Wing("W", None, [Squad("First", None, []), Squad("Second", None, [])])],
        rules=[
            AssignmentRule(0, RuleCondition("ship_type", "Revelation"),
                           RuleAction("squad_member", "W", "First")),
            AssignmentRule(1, RuleCondition("capital", ""),
                           RuleAction("squad_member", "W", "Second")),
        ])
    members = [_cap_member(1, "A", "Revelation", True)]
    res = compose(t, members, _EMPTY_STRUCT)
    assert res.executable[0].target_squad_name == "First"


def test_default_rule_evaluates_last_regardless_of_priority():
    # default has priority 0 but must run AFTER the capital rule (priority 5).
    t = _template(
        [Wing("W", None, [Squad("Caps", None, []), Squad("Misc", None, [])])],
        rules=[
            AssignmentRule(0, RuleCondition("default", ""),
                           RuleAction("squad_member", "W", "Misc")),
            AssignmentRule(5, RuleCondition("capital", ""),
                           RuleAction("squad_member", "W", "Caps")),
        ])
    members = [_cap_member(1, "Cap", "Revelation", True),
               _cap_member(2, "Sub", "Rifter", False, ship_class="Frigate")]
    res = compose(t, members, _EMPTY_STRUCT)
    dest = {m.pilot_id: m.target_squad_name for m in res.executable}
    assert dest == {1: "Caps", 2: "Misc"}     # cap → Caps, everyone else → Misc


def test_cap_respect_overflow_reason_target_full():
    t = _template(
        [Wing("W", None, [Squad("Small", 1, [])])],   # max_size 1
        rules=[AssignmentRule(0, RuleCondition("capital", ""),
                              RuleAction("squad_member", "W", "Small"))])
    members = [_cap_member(1, "A", "Revelation", True, join="2026-01-01T00:00:00Z"),
               _cap_member(2, "B", "Moros", True, join="2026-01-02T00:00:00Z")]
    res = compose(t, members, _EMPTY_STRUCT)
    assert len(res.executable) == 1
    assert res.executable[0].pilot_id == 1           # earliest join fills the seat
    assert res.unassigned_reasons.get(2) == "target full"
    assert any(m["character_id"] == 2 for m in res.unassigned)


def test_named_pin_counts_against_cap_for_a_rule():
    # max_size 2: a named slot pins A into the squad (Pass 1), a rule matches
    # B and C to the SAME squad. A + B = 2 fills the cap → C is "target full".
    slot = Slot(character="A", tag=None, role="squad_member")
    t = _template(
        [Wing("W", None, [Squad("Cap2", 2, [slot])])],
        rules=[AssignmentRule(0, RuleCondition("capital", ""),
                              RuleAction("squad_member", "W", "Cap2"))])
    members = [
        _member(10, "A", "Archon"),   # Pass-1 named pin (not a capital match target)
        _cap_member(11, "B", "Revelation", True, join="2026-01-01T00:00:00Z"),
        _cap_member(12, "C", "Moros", True, join="2026-01-02T00:00:00Z"),
    ]
    res = compose(t, members, _EMPTY_STRUCT)
    dest = {m.pilot_id: m.target_squad_name for m in res.executable}
    assert dest.get(10) == "Cap2"                     # named pin placed
    assert dest.get(11) == "Cap2"                     # first capital fills the 2nd seat
    assert 12 not in dest                             # C blocked by the cap
    assert res.unassigned_reasons.get(12) == "target full"
    assert any(m["character_id"] == 12 for m in res.unassigned)


def test_passes_1_2_skips_generic_pass3():
    # A generic slot would grab the leftover in a full compose; passes=(1,2) must not.
    t = _template([Wing("W", None, [Squad("Gen", None, [
        Slot(character=None, tag=None, role="squad_member")])])],
        rules=[])
    members = [_cap_member(1, "A", "Rifter", False, ship_class="Frigate")]
    full = compose(t, members, _EMPTY_STRUCT)
    assert len(full.executable) == 1                 # Pass 3 placed it
    limited = compose(t, members, _EMPTY_STRUCT, passes=(1, 2))
    assert limited.executable == []                  # Pass 3 skipped
    assert [m["character_id"] for m in limited.unassigned] == [1]


def test_named_slot_prefers_character_id_over_name():
    slot = Slot(character="Old Name", tag=None, role="squad_commander")
    slot.character_id = 42
    t = _template([Wing("W", None, [Squad("S", None, [slot])])])
    # Member whose NAME differs but whose id matches → still placed by id.
    members = [_member(42, "Renamed Pilot", "Archon")]
    res = compose(t, members, _EMPTY_STRUCT)
    assert res.executable[0].pilot_id == 42
    assert res.executable[0].target_role == "squad_commander"
