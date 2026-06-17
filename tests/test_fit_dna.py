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
