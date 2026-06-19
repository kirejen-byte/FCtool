"""Fit serialization: DNA strings and ESI fitting items.

`to_dna` renders a `ParsedFit` as a fitting-DNA string for `<url=fitting:…>`
MOTD links. `to_esi_items` renders the same fit as the `items[]` body ESI's
character-fittings endpoint expects, using the **string** slot-flag enums
(`LoSlot0`, `HiSlot1`, `DroneBay`, `Cargo`, …) — never the legacy integer flags.
`esi_items_to_parsed` is the inverse, used by ESI in-game import.

Pure logic, Tk-free, network-free.
"""

from __future__ import annotations

from fit_models import (
    CargoStack,
    DroneStack,
    ParsedFit,
    ParsedModule,
    SLOT_HIGH,
    SLOT_LOW,
    SLOT_MED,
    SLOT_RIG,
    SLOT_SUBSYSTEM,
)

# Slot -> ESI string-flag prefix (a 0-based index is appended per section).
ESI_FLAGS: dict[str, str] = {
    "low": "LoSlot",
    "med": "MedSlot",
    "high": "HiSlot",
    "rig": "RigSlot",
    "subsystem": "SubSystemSlot",
}
DRONE_FLAG = "DroneBay"
CARGO_FLAG = "Cargo"
FIGHTER_FLAG = "FighterBay"

# Inverse of ESI_FLAGS, longest-prefix-first so "SubSystemSlot" wins over any
# shorter accidental overlap when matching a flag string.
_FLAG_PREFIX_TO_SLOT: list[tuple[str, str]] = sorted(
    ((prefix, slot) for slot, prefix in ESI_FLAGS.items()),
    key=lambda pair: len(pair[0]),
    reverse=True,
)

# The order modules are emitted in (DNA sections and ESI flag assignment).
_SLOT_EMIT_ORDER = [SLOT_HIGH, SLOT_MED, SLOT_LOW, SLOT_RIG, SLOT_SUBSYSTEM]

# Cargo whose category is one of these is "safe" to emit in fitting-DNA: the EVE
# client routes it to cargo/bays, not into a slot. Anything else (module/ship/
# subsystem/unknown) would re-fit into a slot when the DNA link is clicked
# ("ghost slots"), so it is dropped from DNA when a catalog can classify it.
_CARGO_SAFE_CATEGORIES = frozenset({"charge", "other", "drone", "fighter"})

# ── Tech III subsystem slot ordering ─────────────────────────────────────────
#
# A strategic cruiser's hull + subsystems must be encoded the way the live
# client (and pyfa's authoritative service/port/dna.py) does it: each subsystem
# as a ``typeID;1`` quantity item, sorted by the ``subSystemSlot`` attribute —
# Core (125) → Defensive (126) → Offensive (127) → Propulsion (128). Emitting
# subsystems as BARE ids in the ship token makes the modern client mis-render a
# subsystem as the hull (the reported "Loki Propulsion - …" bug).
#
# fit_types.json carries the SDE groupID (``g``) but not the subSystemSlot
# attribute, so we map groupID -> slot order. These four group IDs are constant
# across all four races (Loki/Tengu/Proteus/Legion).
_SUBSYSTEM_GROUP_ORDER: dict[int, int] = {
    958: 0,  # Core       (subSystemSlot 125)
    954: 1,  # Defensive  (subSystemSlot 126)
    956: 2,  # Offensive  (subSystemSlot 127)
    957: 3,  # Propulsion (subSystemSlot 128)
}

# Fallback when no catalog is available: the type-ID ranges are contiguous per
# slot in the current SDE. Used only to order subsystems for clients that pass
# no catalog; an id outside every range keeps its input position (stable sort).
_SUBSYSTEM_ID_RANGE_ORDER: list[tuple[range, int]] = [
    (range(45622, 45634), 0),  # Core
    (range(45586, 45598), 1),  # Defensive
    (range(45598, 45610), 2),  # Offensive
    (range(45610, 45622), 3),  # Propulsion
]
_SUBSYSTEM_ORDER_FALLBACK = len(_SUBSYSTEM_GROUP_ORDER)  # unknown slots sort last


def _subsystem_slot_rank(type_id: int, catalog=None) -> int:
    """Return the subSystemSlot sort rank for a subsystem type id.

    Prefers the catalog's group id (duck-typed ``group_of``) so any current or
    future subsystem sorts correctly; falls back to the bundled id ranges, then
    to a constant that parks unknown ids after the known slots (a stable sort
    then preserves their relative input order)."""
    if catalog is not None:
        group_of = getattr(catalog, "group_of", None)
        if callable(group_of):
            try:
                rank = _SUBSYSTEM_GROUP_ORDER.get(group_of(type_id))
            except Exception:
                rank = None
            if rank is not None:
                return rank
    for id_range, rank in _SUBSYSTEM_ID_RANGE_ORDER:
        if type_id in id_range:
            return rank
    return _SUBSYSTEM_ORDER_FALLBACK


def _sorted_subsystems(parsed: ParsedFit, catalog=None) -> list[int]:
    """Subsystem type ids ordered Core→Defensive→Offensive→Propulsion.

    A stable sort means unrecognized ids keep their relative input order and
    duplicates are preserved (callers may carry malformed fits)."""
    return sorted(
        parsed.subsystems,
        key=lambda type_id: _subsystem_slot_rank(type_id, catalog),
    )


def _modules_by_slot(parsed: ParsedFit) -> dict[str, list[ParsedModule]]:
    by_slot: dict[str, list[ParsedModule]] = {slot: [] for slot in _SLOT_EMIT_ORDER}
    for module in parsed.modules:
        by_slot.setdefault(module.slot, []).append(module)
    return by_slot


def to_dna(parsed: ParsedFit, catalog=None) -> str:
    """Render a `ParsedFit` as a fitting-DNA string, terminated with ``::``.

    The hull type id is always the first colon-group. Tech III subsystems follow
    the hull as ``typeID;1`` quantity items, sorted by subsystem slot (Core,
    Defensive, Offensive, Propulsion) — matching pyfa's generator and what the
    live client expects. They are NOT emitted as bare ids, which the modern
    client mis-renders as the hull. Pass an optional ``catalog`` exposing
    ``group_of(type_id)`` to order any subsystem id; without one, a bundled
    id-range table handles every current strategic-cruiser subsystem.
    """
    parts: list[str] = [str(parsed.ship_type_id)]
    parts.extend(f"{sub};1" for sub in _sorted_subsystems(parsed, catalog))

    by_slot = _modules_by_slot(parsed)

    # Modules, grouped by slot section then stacked by (type id, offline).
    #
    # NOTE: a module's `offline` state IS representable in DNA via a trailing `_`
    # on the id (`typeID_;qty`), which `parse_dna` reads back as `offline=True`,
    # so offline state round-trips through DNA. Online and offline instances of
    # the same type are emitted as SEPARATE tokens (never merged into one stack),
    # because they differ in identity. `to_esi_items` still drops offline: the
    # ESI items[] format has no offline field, and it is intentionally left
    # unchanged. `fit_content_hash` (fit_models.py) folds `offline` into a fit's
    # identity, consistent with DNA now preserving it.
    charge_counts: dict[int, int] = {}
    for slot in _SLOT_EMIT_ORDER:
        # Key on (type_id, offline) so online/offline of the same id each get
        # their own token; dict insertion order keeps first-seen token order.
        counts: dict[tuple[int, bool], int] = {}
        for module in by_slot.get(slot, []):
            key = (module.type_id, bool(module.offline))
            counts[key] = counts.get(key, 0) + 1
            if module.charge_type_id is not None:
                charge_counts[module.charge_type_id] = (
                    charge_counts.get(module.charge_type_id, 0) + 1
                )
        for (type_id, offline), count in counts.items():
            parts.append(f"{type_id}_;{count}" if offline else f"{type_id};{count}")

    # Drones are emitted before cargo, each as typeID;qty, and are NOT merged
    # with cargo/charges.
    for drone in parsed.drones:
        parts.append(f"{drone.type_id};{drone.quantity}")

    # Cargo + loaded charges, merged by type id so the SAME id carried in cargo
    # and loaded in guns yields ONE token with the summed quantity (mirrors
    # `to_esi_items`). First-seen order is preserved: cargo first, then
    # charge-only ids. Module-category cargo is dropped when a catalog can
    # classify it — EVE fitting-DNA has no cargo section, so a module carried as
    # cargo would otherwise re-fit into a slot ("ghost slots"). Without a catalog
    # (or one that cannot classify), all cargo is kept for back-compat.
    def _cargo_is_safe(type_id: int) -> bool:
        if catalog is None:
            return True
        category_of = getattr(catalog, "category_of", None)
        if not callable(category_of):
            return True  # no way to classify -> keep (back-compat)
        try:
            return category_of(type_id) in _CARGO_SAFE_CATEGORIES
        except Exception:
            return False  # cannot classify confidently -> drop (avoid ghost-fit)

    cargo_to_emit: dict[int, int] = {}
    for cargo in parsed.cargo:
        if not _cargo_is_safe(cargo.type_id):
            continue
        cargo_to_emit[cargo.type_id] = cargo_to_emit.get(cargo.type_id, 0) + cargo.quantity
    for type_id, count in charge_counts.items():
        cargo_to_emit[type_id] = cargo_to_emit.get(type_id, 0) + count
    for type_id, quantity in cargo_to_emit.items():
        parts.append(f"{type_id};{quantity}")

    return ":".join(parts) + "::"


def to_esi_items(parsed: ParsedFit, catalog=None) -> list[dict]:
    """Render a `ParsedFit` as ESI fitting ``items[]`` with string flag enums.

    Modules get sequential per-section flags (``HiSlot0``, ``HiSlot1``, …) and
    ``quantity=1``. Tech III subsystems (held on ``parsed.subsystems``, not on
    ``parsed.modules``) get ``SubSystemSlot0…3`` flags in subsystem-slot order
    (Core, Defensive, Offensive, Propulsion). Loaded charges are emitted as
    separate ``Cargo`` items (ESI does not bind charges to a module slot). Drones
    go to ``DroneBay``, cargo to ``Cargo``, fighters to ``FighterBay``. Integer
    flags are never emitted.
    """
    items: list[dict] = []
    by_slot = _modules_by_slot(parsed)

    charge_counts: dict[int, int] = {}
    for slot in _SLOT_EMIT_ORDER:
        prefix = ESI_FLAGS.get(slot)
        if prefix is None:
            continue
        for index, module in enumerate(by_slot.get(slot, [])):
            items.append(
                {"type_id": module.type_id, "flag": f"{prefix}{index}", "quantity": 1}
            )
            if module.charge_type_id is not None:
                charge_counts[module.charge_type_id] = (
                    charge_counts.get(module.charge_type_id, 0) + 1
                )

    subsystem_prefix = ESI_FLAGS[SLOT_SUBSYSTEM]
    for index, type_id in enumerate(_sorted_subsystems(parsed, catalog)):
        items.append(
            {"type_id": type_id, "flag": f"{subsystem_prefix}{index}", "quantity": 1}
        )

    for drone in parsed.drones:
        items.append(
            {"type_id": drone.type_id, "flag": DRONE_FLAG, "quantity": drone.quantity}
        )

    # Merge cargo + loaded-charge counts by type_id so the SAME ammo carried in
    # cargo and loaded in guns yields ONE Cargo item with the summed quantity,
    # never two separate Cargo entries for the same type. First-seen order is
    # preserved (parsed.cargo first, then charge-only types).
    cargo_totals: dict[int, int] = {}
    for cargo in parsed.cargo:
        cargo_totals[cargo.type_id] = cargo_totals.get(cargo.type_id, 0) + cargo.quantity
    for type_id, count in charge_counts.items():
        cargo_totals[type_id] = cargo_totals.get(type_id, 0) + count
    for type_id, quantity in cargo_totals.items():
        items.append({"type_id": type_id, "flag": CARGO_FLAG, "quantity": quantity})

    return items


def _slot_for_flag(flag: str) -> str | None:
    """Map an ESI slot flag (e.g. ``HiSlot1``) to a slot name, or ``None``."""
    for prefix, slot in _FLAG_PREFIX_TO_SLOT:
        if flag.startswith(prefix):
            return slot
    return None


def esi_items_to_parsed(esi_items: list[dict], catalog) -> ParsedFit:
    """Invert `to_esi_items`: route ESI fitting items back into a `ParsedFit`.

    Each ``{type_id, flag, quantity}`` is routed by flag prefix — slot flags
    (``Hi/Med/Lo/Rig/SubSystemSlot…``) become `ParsedModule`s with the mapped
    slot (stacked items expand to instances; charges are not bound back to
    modules), ``DroneBay`` becomes a `DroneStack`, ``FighterBay`` lands in cargo
    for v1, and ``Cargo`` becomes a `CargoStack`. Names resolve via `catalog`.

    The hull (``ship_type_id``/``ship_name``) is not encoded in ``items[]`` — the
    caller sets it from the ESI fitting's ``ship_type_id``.
    """
    modules: list[ParsedModule] = []
    drones: list[DroneStack] = []
    cargo: list[CargoStack] = []
    subsystems: list[int] = []

    for item in esi_items:
        type_id = item.get("type_id")
        flag = item.get("flag", "")
        quantity = item.get("quantity", 0)
        if type_id is None:
            continue
        name = catalog.resolve_name(type_id) or str(type_id)

        if flag == DRONE_FLAG:
            drones.append(DroneStack(type_id, name, quantity))
            continue
        if flag == FIGHTER_FLAG:
            # Fighters land in cargo for v1 (no fighter bay model yet).
            cargo.append(CargoStack(type_id, name, quantity))
            continue
        if flag == CARGO_FLAG:
            cargo.append(CargoStack(type_id, name, quantity))
            continue

        slot = _slot_for_flag(flag)
        if slot is None:
            # Unknown flag — treat as cargo so nothing is silently dropped.
            cargo.append(CargoStack(type_id, name, quantity))
            continue
        if slot == SLOT_SUBSYSTEM:
            for _ in range(max(quantity, 1)):
                subsystems.append(type_id)
            continue
        for _ in range(max(quantity, 1)):
            modules.append(ParsedModule(type_id=type_id, name=name, slot=slot))

    return ParsedFit(
        ship_type_id=0,
        ship_name="",
        modules=modules,
        drones=drones,
        cargo=cargo,
        subsystems=subsystems,
    )
