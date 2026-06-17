from fit_dna import to_dna, to_esi_items
from fit_models import ParsedModule, DroneStack, CargoStack, ParsedFit


def _fit():
    return ParsedFit(
        ship_type_id=12015, ship_name="Muninn",
        modules=[
            ParsedModule(2048, "Damage Control II", "low"),
            ParsedModule(2185, "720mm Howitzer Artillery II", "high",
                         charge_type_id=215, charge_name="EMP"),
            ParsedModule(2185, "720mm Howitzer Artillery II", "high",
                         charge_type_id=215, charge_name="EMP"),
        ],
        drones=[DroneStack(12058, "Hobgoblin II", 5)],
        cargo=[CargoStack(216, "Plasma", 1000)],
        subsystems=[],
    )


def test_to_dna_round_trips_through_parser():
    from fit_parser import parse_dna

    class Cat:
        def category_of(self, tid): return {12058: "drone", 215: "charge", 216: "charge"}.get(tid, "module")
        def slot_of(self, tid): return {2048: "low", 2185: "high"}.get(tid)
        def resolve_name(self, tid): return str(tid)
    dna = to_dna(_fit())
    assert dna.endswith("::")
    res = parse_dna(dna, Cat())
    assert res.fit.ship_type_id == 12015
    assert len([m for m in res.fit.modules if m.type_id == 2185]) == 2


def test_to_esi_items_uses_string_flags_sequentially():
    items = to_esi_items(_fit())
    flags = {i["type_id"]: i["flag"] for i in items}
    assert flags[2048] == "LoSlot0"
    highs = sorted(i["flag"] for i in items if i["type_id"] == 2185)
    assert highs == ["HiSlot0", "HiSlot1"]               # two guns -> sequential high flags
    assert any(i["flag"] == "DroneBay" and i["quantity"] == 5 for i in items)
    assert any(i["flag"] == "Cargo" and i["quantity"] == 1000 for i in items)
    assert all(isinstance(i["flag"], str) for i in items)   # never integer flags


from fit_dna import to_esi_items, esi_items_to_parsed


class Cat:
    def resolve_name(self, tid): return {12015:"Muninn",2048:"DCII",2185:"Gun",215:"EMP",
                                          12058:"Hob",216:"Plasma"}.get(tid)
    def category_of(self, tid): return {12058:"drone",215:"charge",216:"charge"}.get(tid,"module")
    def slot_of(self, tid): return {2048:"low",2185:"high"}.get(tid)


def test_esi_items_round_trip_back_to_parsed():
    parsed = _fit()                                  # the fixture from earlier tests
    items = to_esi_items(parsed)
    back = esi_items_to_parsed(items, Cat())
    back.ship_type_id = parsed.ship_type_id          # hull supplied by caller, not by items[]
    assert sorted(m.type_id for m in back.modules) == [2048, 2185, 2185]
    assert any(m.slot == "high" for m in back.modules if m.type_id == 2185)
    assert any(d.type_id == 12058 and d.quantity == 5 for d in back.drones)
    assert any(c.type_id == 216 and c.quantity == 1000 for c in back.cargo)


def test_esi_items_flag_prefix_routes_to_slot():
    items = [{"type_id": 2048, "flag": "LoSlot0", "quantity": 1},
             {"type_id": 9999, "flag": "RigSlot2", "quantity": 1},
             {"type_id": 12058, "flag": "DroneBay", "quantity": 3},
             {"type_id": 216, "flag": "Cargo", "quantity": 50}]
    p = esi_items_to_parsed(items, Cat())
    slots = {m.type_id: m.slot for m in p.modules}
    assert slots[2048] == "low" and slots[9999] == "rig"
    assert any(d.type_id == 12058 and d.quantity == 3 for d in p.drones)
    assert any(c.type_id == 216 and c.quantity == 50 for c in p.cargo)


# ── Tech III strategic cruiser DNA (subsystems must be `id;1`, slot-sorted) ────
#
# Real Loki (hull 29990) subsystems from fit_types.json, one per subsystem slot:
#   45631 Loki Core - Dissolution Sequencer       (group 958, subSystemSlot 125)
#   45595 Loki Defensive - Covert Reconfiguration (group 954, subSystemSlot 126)
#   45607 Loki Offensive - Projectile Scoping Array (group 956, subSystemSlot 127)
#   45619 Loki Propulsion - Interdiction Nullifier (group 957, subSystemSlot 128)
#
# pyfa's service/port/dna.py (the authoritative generator the live client
# consumes) emits subsystems as ``:typeID;1`` quantity items, sorted by
# subSystemSlot (Core, Defensive, Offensive, Propulsion) — never as bare ids in
# the ship token. Bare ids make the modern client render a subsystem as the hull.

_LOKI = 29990
_SUB_CORE = 45631
_SUB_DEF = 45595
_SUB_OFF = 45607
_SUB_PROP = 45619


class T3Cat:
    """Catalog stub reporting category/slot/group for the Loki fixture."""

    _GROUP = {_SUB_CORE: 958, _SUB_DEF: 954, _SUB_OFF: 956, _SUB_PROP: 957}

    def category_of(self, tid):
        if tid in self._GROUP:
            return "subsystem"
        return {12058: "drone", 215: "charge", 216: "charge"}.get(tid, "module")

    def slot_of(self, tid):
        if tid in self._GROUP:
            return "subsystem"
        return {2048: "low", 2185: "high"}.get(tid)

    def resolve_name(self, tid):
        return {
            _LOKI: "Loki", _SUB_CORE: "Loki Core", _SUB_DEF: "Loki Defensive",
            _SUB_OFF: "Loki Offensive", _SUB_PROP: "Loki Propulsion",
            2048: "Damage Control II",
        }.get(tid, str(tid))

    def group_of(self, tid):
        return self._GROUP.get(tid)


def _loki_fit(sub_order):
    return ParsedFit(
        ship_type_id=_LOKI, ship_name="Loki",
        modules=[ParsedModule(2048, "Damage Control II", "low")],
        drones=[], cargo=[],
        subsystems=list(sub_order),
    )


def test_t3_subsystems_emitted_as_quantity_items_not_bare():
    # Subsystems must appear as `id;1`, never as a bare id glued to the hull.
    fit = _loki_fit([_SUB_CORE, _SUB_DEF, _SUB_OFF, _SUB_PROP])
    dna = to_dna(fit, T3Cat())
    groups = dna.rstrip(":").split(":")
    assert groups[0] == str(_LOKI)                      # hull is always first group
    for sub in (_SUB_CORE, _SUB_DEF, _SUB_OFF, _SUB_PROP):
        assert f"{sub};1" in groups                     # each subsystem is `id;1`
        assert str(sub) not in groups                   # never a bare id token


def test_t3_dna_exact_shape_slot_sorted():
    # Even when handed in a scrambled order, subsystems sort Core→Def→Off→Prop.
    fit = _loki_fit([_SUB_PROP, _SUB_OFF, _SUB_DEF, _SUB_CORE])
    dna = to_dna(fit, T3Cat())
    assert dna == (
        f"{_LOKI}:{_SUB_CORE};1:{_SUB_DEF};1:{_SUB_OFF};1:{_SUB_PROP};1:2048;1::"
    )


def test_t3_dna_round_trips_ship_and_all_subsystems():
    from fit_parser import parse_dna

    fit = _loki_fit([_SUB_CORE, _SUB_DEF, _SUB_OFF, _SUB_PROP])
    dna = to_dna(fit, T3Cat())
    res = parse_dna(dna, T3Cat())
    assert res.fit.ship_type_id == _LOKI               # hull preserved, not a subsystem
    assert sorted(res.fit.subsystems) == sorted(
        [_SUB_CORE, _SUB_DEF, _SUB_OFF, _SUB_PROP]
    )
    assert any(m.type_id == 2048 for m in res.fit.modules)


def test_t3_hull_is_always_first_dna_group():
    fit = _loki_fit([_SUB_DEF, _SUB_CORE, _SUB_PROP, _SUB_OFF])
    dna = to_dna(fit, T3Cat())
    assert dna.split(":", 1)[0] == str(_LOKI)


def test_t3_to_esi_items_tags_subsystem_slots():
    fit = _loki_fit([_SUB_CORE, _SUB_DEF, _SUB_OFF, _SUB_PROP])
    items = to_esi_items(fit)
    sub_flags = sorted(
        i["flag"] for i in items if i["type_id"] in
        (_SUB_CORE, _SUB_DEF, _SUB_OFF, _SUB_PROP)
    )
    assert sub_flags == ["SubSystemSlot0", "SubSystemSlot1",
                         "SubSystemSlot2", "SubSystemSlot3"]
    assert all(i["quantity"] == 1 for i in items if i["flag"].startswith("SubSystemSlot"))


def test_non_t3_dna_unchanged_regression():
    # A non-T3 fit with no subsystems must serialize exactly as before.
    dna = to_dna(_fit())
    assert dna.split(":", 1)[0] == "12015"             # hull first
    groups = dna.rstrip(":").split(":")
    assert "2048;1" in groups                           # DC II, one low
    assert "12058;5" in groups                          # 5 Hobgoblin II drones
    assert "216;1000" in groups                         # 1000 Plasma cargo
    # No bare subsystem tokens leaked in.
    assert all(";" in g for g in groups[1:])
