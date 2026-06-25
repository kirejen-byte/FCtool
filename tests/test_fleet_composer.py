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
        [Wing("Alpha Wing", None, [Squad("Cmd Squad", None, [
            Slot(character=None, tag="Links", role="squad_commander"),
        ])])],
        rules=[
            AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                           RuleAction("squad_commander", "Alpha Wing", "Cmd Squad")),
            AssignmentRule(1, RuleCondition("ship_type", "Damnation"),
                           RuleAction("squad_member", "Alpha Wing", "Cmd Squad")),
        ],
    )
    res = compose(t, [_member(1, "Boss", "Damnation")], _EMPTY_STRUCT)
    assert res.executable[0].target_role == "squad_commander"   # priority-0 rule won


def test_six_damnations_five_sc_slots_warns_about_one_unplaced():
    squads = [Squad(f"S{i}", None, [
        Slot(character=None, tag="Links", role="squad_commander"),
    ]) for i in range(5)]
    # one generic slot for the overflow pilot to land in as a member
    squads.append(Squad("Spare", None, [Slot(character=None, tag=None, role="squad_member")]))
    t = _template([Wing("Alpha Wing", None, squads)],
                  rules=[AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                                        RuleAction("squad_commander", None, None))])
    members = [_member(i, f"Pilot{i}", "Damnation", join=f"2026-01-0{i+1}T00:00:00Z")
               for i in range(6)]
    res = compose(t, members, _EMPTY_STRUCT)
    sc = [m for m in res.executable if m.target_role == "squad_commander"]
    assert len(sc) == 5
    assert any("Damnation" in w and "unplaced" in w for w in res.warnings)


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
    t = _template([Wing("W", None, [Squad("S", None, [
        Slot(character=None, tag="Logistics", role="squad_member"),
    ])])],
        rules=[AssignmentRule(0, RuleCondition("ship_class", "Logistics Cruiser"),
                              RuleAction("squad_member", "W", "S"))])
    m = _member(1, "Logi Guy", "Guardian")
    m["ship_class"] = "Logistics Cruiser"      # pre-resolved by the window
    res = compose(t, [m], _EMPTY_STRUCT)
    assert res.executable[0].pilot_id == 1


def test_rule_slot_with_no_match_emits_unfilled_warning():
    # A tagged role slot, no rules and no doctrine → nothing can fill it (§7).
    t = _template([Wing("W", None, [Squad("S", None, [
        Slot(character=None, tag="Logistics", role="squad_member"),
    ])])])
    res = compose(t, [_member(1, "DPS Guy", "Megathron")], _EMPTY_STRUCT)
    assert res.executable == []                       # tag slot stays empty, no ESI call
    assert any("unfilled" in w for w in res.warnings)
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
