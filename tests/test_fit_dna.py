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


def test_to_esi_items_merges_loaded_charge_with_cargo_of_same_type():
    # Ammo 215 is loaded in two guns AND carried in cargo. ESI must get exactly
    # ONE Cargo entry for 215 with the combined quantity (cargo + loaded count),
    # never two separate Cargo items for the same type_id.
    fit = ParsedFit(
        ship_type_id=12015, ship_name="Muninn",
        modules=[
            ParsedModule(2185, "720mm Howitzer Artillery II", "high",
                         charge_type_id=215, charge_name="EMP"),
            ParsedModule(2185, "720mm Howitzer Artillery II", "high",
                         charge_type_id=215, charge_name="EMP"),
        ],
        drones=[],
        cargo=[CargoStack(215, "EMP", 1000)],
        subsystems=[],
    )
    items = to_esi_items(fit)
    cargo_215 = [i for i in items if i["flag"] == "Cargo" and i["type_id"] == 215]
    assert len(cargo_215) == 1                         # exactly one merged entry
    assert cargo_215[0]["quantity"] == 1002            # 1000 cargo + 2 loaded charges


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


# ── Legacy bare-id normalisation (the MOTD-import "Tengu propulsion" bug) ──────
#
# A MOTD authored by an older tool/client carries T3 subsystems as BARE ids
# (29990:45631:45595:...), which the modern client mis-renders as the hull. The
# MOTD import path was emitting that raw DNA verbatim; the fix routes imported
# DNA through parse_dna → to_dna, which must NORMALISE the legacy form to the
# slot-sorted `id;1` form. These tests pin that round-trip.


def test_t3_legacy_bare_id_dna_normalizes_to_quantity_form():
    from fit_parser import parse_dna

    # Bare-id subsystems, deliberately scrambled, plus one low module.
    legacy = f"{_LOKI}:{_SUB_PROP}:{_SUB_OFF}:{_SUB_DEF}:{_SUB_CORE}:2048;1::"
    parsed = parse_dna(legacy, T3Cat()).fit
    assert parsed.ship_type_id == _LOKI                    # hull, not a subsystem
    assert sorted(parsed.subsystems) == sorted(
        [_SUB_CORE, _SUB_DEF, _SUB_OFF, _SUB_PROP])         # 4 subsystems recovered
    canon = to_dna(parsed, T3Cat())
    assert canon == (
        f"{_LOKI}:{_SUB_CORE};1:{_SUB_DEF};1:{_SUB_OFF};1:{_SUB_PROP};1:2048;1::"
    )
    groups = canon.rstrip(":").split(":")
    for sub in (_SUB_CORE, _SUB_DEF, _SUB_OFF, _SUB_PROP):
        assert str(sub) not in groups                       # only `id;1`, never bare


def test_real_catalog_normalizes_legacy_tengu_dna():
    # The exact reported hull (Tengu 29984) against the BUNDLED catalog, using
    # the real subsystem ids confirmed during investigation. Guards/​skips if the
    # catalog can't be built in this environment.
    try:
        import type_catalog
        from fit_parser import parse_dna
        cat = type_catalog.TypeCatalog(esi=None)
    except Exception as exc:                                # pragma: no cover
        import pytest
        pytest.skip(f"type catalog unavailable: {exc}")

    TENGU = 29984
    core, defen, offen, prop = 45625, 45589, 45601, 45613   # Core/Def/Off/Prop
    # Legacy bare-id Tengu, subsystems scrambled, plus a high + low module.
    legacy = f"{TENGU}:{prop}:{offen}:{defen}:{core}:405;1:506;1::"
    parsed = parse_dna(legacy, cat).fit
    if parsed.ship_type_id != TENGU or len(parsed.subsystems) != 4:
        import pytest
        pytest.skip("bundled catalog lacks Tengu subsystem data")
    canon = to_dna(parsed, cat)
    groups = canon.rstrip(":").split(":")
    assert groups[0] == str(TENGU)                          # hull first, not a sub
    # Subsystems normalised to `id;1` in Core→Def→Off→Prop order.
    assert groups[1:5] == [f"{core};1", f"{defen};1", f"{offen};1", f"{prop};1"]
    for sub in (core, defen, offen, prop):
        assert str(sub) not in groups                       # no bare-id leak


# ── Module-category cargo must be DROPPED from DNA (the "ghost slots" bug) ─────
#
# EVE fitting-DNA has no cargo section: the client decides slot-vs-cargo purely
# by an item's CATEGORY. Charges/drones/fighters route to cargo/bays, but ANY
# module-category item gets FITTED into a slot. So a MODULE carried as cargo
# (a spare low-slot module) would re-fit into a slot when the DNA link is
# clicked. to_dna must drop module-category cargo when a catalog is available,
# while still emitting cargo whose category is safe (charge/other/drone/fighter).


class _CargoCat:
    """Stub catalog: classifies the ids used by the cargo-filter tests.

    1001 -> a module carried as cargo (must be dropped from DNA).
    1002 -> a charge carried as cargo (must survive and round-trip to cargo).
    2048 -> a real low-slot module (routes to a fitted module).
    """

    def category_of(self, tid):
        return {1001: "module", 1002: "charge", 2048: "module"}.get(tid, "module")

    def slot_of(self, tid):
        return {1001: "low", 2048: "low"}.get(tid)

    def resolve_name(self, tid):
        return {1001: "Spare DC II", 1002: "EMP", 2048: "Damage Control II"}.get(
            tid, str(tid)
        )


def test_module_category_cargo_dropped_with_catalog():
    from fit_parser import parse_dna

    fit = ParsedFit(
        ship_type_id=12015, ship_name="Muninn",
        modules=[ParsedModule(2048, "Damage Control II", "low")],
        drones=[],
        cargo=[
            CargoStack(1001, "Spare DC II", 1),   # module-category -> must be dropped
            CargoStack(1002, "EMP", 500),         # charge-category -> must survive
        ],
        subsystems=[],
    )
    cat = _CargoCat()
    dna = to_dna(fit, cat)
    groups = dna.rstrip(":").split(":")

    # The module-cargo token is gone entirely; the charge-cargo token remains.
    assert "1001;1" not in groups
    assert not any(g.startswith("1001;") for g in groups)
    assert "1002;500" in groups

    # And it round-trips: 1001 lands in NEITHER modules NOR cargo; 1002 in cargo.
    back = parse_dna(dna, cat).fit
    assert all(m.type_id != 1001 for m in back.modules)
    assert all(c.type_id != 1001 for c in back.cargo)
    assert any(c.type_id == 1002 and c.quantity == 500 for c in back.cargo)


def test_module_category_cargo_kept_without_catalog():
    # Back-compat: with NO catalog, to_dna cannot classify, so it keeps ALL
    # cargo exactly as before (no behaviour change for catalog-less callers).
    fit = ParsedFit(
        ship_type_id=12015, ship_name="Muninn",
        modules=[ParsedModule(2048, "Damage Control II", "low")],
        drones=[],
        cargo=[CargoStack(1001, "Spare DC II", 1)],
        subsystems=[],
    )
    dna = to_dna(fit)
    groups = dna.rstrip(":").split(":")
    assert "1001;1" in groups


class _OfflineCat:
    """Stub catalog: ids 3001/3002 are low-slot modules (route to modules)."""

    def category_of(self, tid):
        return "module"

    def slot_of(self, tid):
        return {3001: "low", 3002: "low"}.get(tid, "low")

    def resolve_name(self, tid):
        return str(tid)


def test_offline_module_round_trips_distinct_types():
    from fit_parser import parse_dna

    fit = ParsedFit(
        ship_type_id=12015, ship_name="Muninn",
        modules=[
            ParsedModule(3001, "Online Mod", "low", offline=False),
            ParsedModule(3002, "Offline Mod", "low", offline=True),
        ],
        drones=[], cargo=[], subsystems=[],
    )
    cat = _OfflineCat()
    dna = to_dna(fit, cat)
    groups = dna.rstrip(":").split(":")

    assert "3001;1" in groups          # online emits a plain token
    assert "3002_;1" in groups         # offline emits the `_` suffix token

    back = parse_dna(dna, cat).fit
    online = [m for m in back.modules if m.type_id == 3001]
    offline = [m for m in back.modules if m.type_id == 3002]
    assert len(online) == 1 and online[0].offline is False
    assert len(offline) == 1 and offline[0].offline is True


def test_offline_and_online_same_type_emit_separate_tokens():
    # One offline + one online module of the SAME type id must produce TWO
    # tokens (X;1 and X_;1), never a merged X;2 that would lose offline state.
    fit = ParsedFit(
        ship_type_id=12015, ship_name="Muninn",
        modules=[
            ParsedModule(3001, "Mod", "low", offline=False),
            ParsedModule(3001, "Mod", "low", offline=True),
        ],
        drones=[], cargo=[], subsystems=[],
    )
    dna = to_dna(fit, _OfflineCat())
    groups = dna.rstrip(":").split(":")
    assert "3001;1" in groups
    assert "3001_;1" in groups
    assert "3001;2" not in groups


class _ChargeMergeCat:
    """Stub catalog: 4185 is a high-slot module, 4215 is a charge."""

    def category_of(self, tid):
        return {4185: "module", 4215: "charge"}.get(tid, "module")

    def slot_of(self, tid):
        return {4185: "high"}.get(tid)

    def resolve_name(self, tid):
        return str(tid)


def test_loaded_charge_and_cargo_of_same_type_merge_to_one_token():
    # Charge 4215 is loaded in two guns (count 2) AND carried as cargo (qty 3).
    # DNA must carry EXACTLY ONE token for 4215 with the summed quantity (5),
    # never two separate tokens for the same id.
    fit = ParsedFit(
        ship_type_id=12015, ship_name="Muninn",
        modules=[
            ParsedModule(4185, "Gun", "high", charge_type_id=4215, charge_name="EMP"),
            ParsedModule(4185, "Gun", "high", charge_type_id=4215, charge_name="EMP"),
        ],
        drones=[],
        cargo=[CargoStack(4215, "EMP", 3)],
        subsystems=[],
    )
    dna = to_dna(fit, _ChargeMergeCat())
    groups = dna.rstrip(":").split(":")
    tokens_4215 = [g for g in groups if g.startswith("4215;")]
    assert tokens_4215 == ["4215;5"]   # exactly one merged token, qty 3 + 2
