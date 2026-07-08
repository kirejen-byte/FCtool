"""Fit / doctrine domain dataclasses + content hashing.

Pure data model for the Fittings tab: parsed fits (`ParsedModule`,
`DroneStack`, `CargoStack`, `ParsedFit`), stored library fits (`Fit`), and
doctrines (`DoctrineMember`, `Doctrine`). Also the default tag vocabulary and a
stable, order-independent `fit_content_hash` used to de-dupe fits on import.

No Tkinter, no network. JSON (de)serialization helpers (`to_dict`/`from_dict`)
keep persistence concerns out of the storage layer's hot path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# ── Fitting slots ────────────────────────────────────────────────────────────
SLOT_HIGH = "high"
SLOT_MED = "med"
SLOT_LOW = "low"
SLOT_RIG = "rig"
SLOT_SUBSYSTEM = "subsystem"
SLOT_SERVICE = "service"

# ── Default tag vocabulary (stored in the library file; user-extensible) ──────
DEFAULT_TAGS = [
    "DPS",
    "Links",
    "Logi",
    "EWAR",
    "Webs",
    "Defenders",
    "Tackle",
    "Special",
]


# ── Parsed fit (derived from an EFT/DNA/pyfa/ESI source) ──────────────────────
@dataclass
class ParsedModule:
    type_id: int
    name: str
    slot: str  # one of the SLOT_* values
    charge_type_id: int | None = None
    charge_name: str | None = None
    offline: bool = False


@dataclass
class DroneStack:
    type_id: int
    name: str
    quantity: int


@dataclass
class CargoStack:
    type_id: int
    name: str
    quantity: int


@dataclass
class ParsedFit:
    ship_type_id: int
    ship_name: str
    modules: list[ParsedModule]
    drones: list[DroneStack]
    cargo: list[CargoStack]
    subsystems: list[int]
    name_hint: str | None = None  # fit name parsed from an EFT header, if any


# ── Stored library fit ────────────────────────────────────────────────────────
@dataclass
class Fit:
    id: str
    name: str
    hull_type_id: int
    hull_name: str
    source: str  # one of {"eft", "dna", "pyfa", "esi"}
    raw_text: str
    parsed: ParsedFit
    dna: str
    notes: str
    esi_fitting_ids: dict[int, int]  # character_id -> fitting_id
    created: str
    modified: str


# ── Doctrines ─────────────────────────────────────────────────────────────────
@dataclass
class DoctrineMember:
    fit_id: str
    tags: list[str]
    order: int
    ideal_mode: str | None = None      # "percent" | "count" | "off" | None
    ideal_min: int | None = None
    ideal_max: int | None = None       # None = no upper bound (e.g. defenders)
    # Per-fit market seed target (units of THIS fit that count as "fully seeded").
    # None = "inherit the doctrine's seed_target (which itself falls back to the
    # global config["market"]["seed_target"] default)". Lets a doctrine seed e.g.
    # 50 stabbers / 20 scythes / 10 bifrosts rather than a flat N of every hull.
    # Resolved via fc_gui._market_seed_target(doctrine, member).
    seed_target: int | None = None


@dataclass
class Doctrine:
    id: str
    name: str
    description: str
    members: list[DoctrineMember]
    created: str
    modified: str
    # Per-doctrine ideal-% exemptions. Tagged-union entry dicts:
    #   {"kind": "capital"} | {"kind": "group", "id": int, "name": str}
    #   | {"kind": "type", "id": int, "name": str}
    # Semantics: None = "use STANDARD_EXEMPTIONS" (never customized); [] = explicitly
    # none; [...] = that explicit list. See fleet_guidance.effective_exemptions.
    exemptions: list[dict] | None = None
    # Per-doctrine market seed target (how many of each fit counts as "fully
    # seeded"). None = "use the global config["market"]["seed_target"] default"
    # — a throwaway-frigate doctrine and a battleship doctrine can want very
    # different targets. Resolved via fc_gui._market_seed_target.
    seed_target: int | None = None


# ── Content hashing (order-independent, for de-dupe) ──────────────────────────
def fit_content_hash(parsed: ParsedFit) -> str:
    """Return a stable, order-independent hash of a fit's contents.

    Canonicalizes the hull plus sorted module/drone/cargo/subsystem tuples so
    two fits with identical contents in a different order hash the same. Slot
    and loaded-charge identity are part of a module's identity; instance order
    and display names are not.
    """
    modules = sorted(
        (m.type_id, m.slot, m.charge_type_id, bool(m.offline))
        for m in parsed.modules
    )
    drones = sorted((d.type_id, d.quantity) for d in parsed.drones)
    cargo = sorted((c.type_id, c.quantity) for c in parsed.cargo)
    subsystems = sorted(parsed.subsystems)
    canonical = (parsed.ship_type_id, modules, drones, cargo, subsystems)
    return hashlib.sha1(repr(canonical).encode("utf-8")).hexdigest()


# ── JSON (de)serialization helpers ────────────────────────────────────────────
def parsed_fit_to_dict(parsed: ParsedFit) -> dict:
    return {
        "ship_type_id": parsed.ship_type_id,
        "ship_name": parsed.ship_name,
        "modules": [
            {
                "type_id": m.type_id,
                "name": m.name,
                "slot": m.slot,
                "charge_type_id": m.charge_type_id,
                "charge_name": m.charge_name,
                "offline": m.offline,
            }
            for m in parsed.modules
        ],
        "drones": [
            {"type_id": d.type_id, "name": d.name, "quantity": d.quantity}
            for d in parsed.drones
        ],
        "cargo": [
            {"type_id": c.type_id, "name": c.name, "quantity": c.quantity}
            for c in parsed.cargo
        ],
        "subsystems": list(parsed.subsystems),
        "name_hint": parsed.name_hint,
    }


def parsed_fit_from_dict(data: dict) -> ParsedFit:
    return ParsedFit(
        ship_type_id=data["ship_type_id"],
        ship_name=data.get("ship_name", ""),
        modules=[
            ParsedModule(
                type_id=m["type_id"],
                name=m.get("name", ""),
                slot=m.get("slot", ""),
                charge_type_id=m.get("charge_type_id"),
                charge_name=m.get("charge_name"),
                offline=m.get("offline", False),
            )
            for m in data.get("modules", [])
        ],
        drones=[
            DroneStack(type_id=d["type_id"], name=d.get("name", ""),
                       quantity=d.get("quantity", 0))
            for d in data.get("drones", [])
        ],
        cargo=[
            CargoStack(type_id=c["type_id"], name=c.get("name", ""),
                       quantity=c.get("quantity", 0))
            for c in data.get("cargo", [])
        ],
        subsystems=list(data.get("subsystems", [])),
        name_hint=data.get("name_hint"),
    )


def fit_to_dict(fit: Fit) -> dict:
    return {
        "id": fit.id,
        "name": fit.name,
        "hull_type_id": fit.hull_type_id,
        "hull_name": fit.hull_name,
        "source": fit.source,
        "raw_text": fit.raw_text,
        "parsed": parsed_fit_to_dict(fit.parsed),
        "dna": fit.dna,
        "notes": fit.notes,
        # JSON object keys are always strings; coerce character ids on the way out.
        "esi_fitting_ids": {str(k): v for k, v in fit.esi_fitting_ids.items()},
        "created": fit.created,
        "modified": fit.modified,
    }


def fit_from_dict(data: dict) -> Fit:
    raw_ids = data.get("esi_fitting_ids", {}) or {}
    esi_fitting_ids: dict[int, int] = {}
    for k, v in raw_ids.items():
        try:
            esi_fitting_ids[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return Fit(
        id=data["id"],
        name=data.get("name", ""),
        hull_type_id=data.get("hull_type_id", 0),
        hull_name=data.get("hull_name", ""),
        source=data.get("source", ""),
        raw_text=data.get("raw_text", ""),
        parsed=parsed_fit_from_dict(data.get("parsed", {})),
        dna=data.get("dna", ""),
        notes=data.get("notes", ""),
        esi_fitting_ids=esi_fitting_ids,
        created=data.get("created", ""),
        modified=data.get("modified", ""),
    )


def doctrine_member_to_dict(member: DoctrineMember) -> dict:
    d = {"fit_id": member.fit_id, "tags": list(member.tags), "order": member.order}
    if member.ideal_mode is not None:
        d["ideal_mode"] = member.ideal_mode
    if member.ideal_min is not None:
        d["ideal_min"] = member.ideal_min
    if member.ideal_max is not None:
        d["ideal_max"] = member.ideal_max
    # Serialize seed_target ONLY when overridden (None omitted so absence ==
    # "inherit the doctrine seed target"; older libraries without the key load
    # back as None, unaffected). Mirrors the doctrine-level seed_target/exemptions.
    if member.seed_target is not None:
        d["seed_target"] = member.seed_target
    return d


def doctrine_member_from_dict(d: dict) -> DoctrineMember:
    return DoctrineMember(
        fit_id=d["fit_id"],
        tags=list(d.get("tags", [])),
        order=d.get("order", 0),
        ideal_mode=d.get("ideal_mode"),
        ideal_min=d.get("ideal_min"),
        ideal_max=d.get("ideal_max"),
        seed_target=d.get("seed_target"),
    )


def doctrine_to_dict(doctrine: Doctrine) -> dict:
    d = {
        "id": doctrine.id,
        "name": doctrine.name,
        "description": doctrine.description,
        "members": [doctrine_member_to_dict(m) for m in doctrine.members],
        "created": doctrine.created,
        "modified": doctrine.modified,
    }
    # Serialize exemptions ONLY when customized (None omitted so absence == "use
    # STANDARD_EXEMPTIONS"; an explicit [] is preserved to mean "no exemptions").
    if doctrine.exemptions is not None:
        d["exemptions"] = [dict(e) for e in doctrine.exemptions]
    # Serialize seed_target ONLY when overridden (None omitted so absence == "use
    # the global market seed_target default"; older libraries without the key load
    # back as None, unaffected).
    if doctrine.seed_target is not None:
        d["seed_target"] = doctrine.seed_target
    return d


def doctrine_from_dict(data: dict) -> Doctrine:
    return Doctrine(
        id=data["id"],
        name=data.get("name", ""),
        description=data.get("description", ""),
        members=[doctrine_member_from_dict(m) for m in data.get("members", [])],
        created=data.get("created", ""),
        modified=data.get("modified", ""),
        exemptions=data.get("exemptions"),
        seed_target=data.get("seed_target"),
    )
