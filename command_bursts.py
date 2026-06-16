"""Command-burst link tracking — pure domain data + logic.

Maps EVE command-burst charges to disciplines and evaluates whether a pilot's
hull can fit and is bonused for the bursts they link. No Tkinter, no network.

Domain reference verified 2026-06-16 (EVE University wiki + everef.net). See
docs/superpowers/specs/2026-06-16-booster-link-tracking-design.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# ── Disciplines ──────────────────────────────────────────────────────────────
SHIELD = "shield"
ARMOR = "armor"
SKIRMISH = "skirmish"
INFORMATION = "information"

DISCIPLINES = (SHIELD, ARMOR, SKIRMISH, INFORMATION)

DISCIPLINE_LABEL = {
    SHIELD: "Shield",
    ARMOR: "Armor",
    SKIRMISH: "Skirmish",
    INFORMATION: "Information",
}

# ── The 12 charges (canonical in-game names) ─────────────────────────────────
DISCIPLINE_CHARGES: dict[str, tuple[str, ...]] = {
    SHIELD: (
        "Active Shielding Charge",
        "Shield Extension Charge",
        "Shield Harmonizing Charge",
    ),
    ARMOR: (
        "Armor Energizing Charge",
        "Armor Reinforcement Charge",
        "Rapid Repair Charge",
    ),
    SKIRMISH: (
        "Evasive Maneuvers Charge",
        "Interdiction Maneuvers Charge",
        "Rapid Deployment Charge",
    ),
    INFORMATION: (
        "Electronic Superiority Charge",
        "Sensor Optimization Charge",
        "Electronic Hardening Charge",
    ),
}

CHARGE_TO_DISCIPLINE: dict[str, str] = {
    charge.lower(): discipline
    for discipline, charges in DISCIPLINE_CHARGES.items()
    for charge in charges
}

_CANONICAL_CHARGE: dict[str, str] = {
    charge.lower(): charge
    for charges in DISCIPLINE_CHARGES.values()
    for charge in charges
}

_WS = re.compile(r"\s+")


def parse_charges(message: str) -> set[tuple[str, str]]:
    """Return the set of (discipline, canonical_charge_name) found in a message.

    EVE chat stores dragged charges as plain text (no type IDs). Charges are
    double-space separated with free-typed text around them, so we match each
    known charge name as a case-insensitive, whitespace-normalized substring.
    No charge name is a substring of another, so substring matching is safe.
    """
    if not message:
        return set()
    haystack = _WS.sub(" ", message).strip().lower()
    found: set[tuple[str, str]] = set()
    for lower_name, canonical in _CANONICAL_CHARGE.items():
        if lower_name in haystack:
            found.add((CHARGE_TO_DISCIPLINE[lower_name], canonical))
    return found


# ── Hull bonuses + verdict logic ─────────────────────────────────────────────
class Verdict(Enum):
    BONUSED = "bonused"
    BONUSED_CONDITIONAL = "bonused_conditional"  # T3C: needs Support Processor subsystem
    FITS_NO_BONUS = "fits_no_bonus"
    CANT_FIT = "cant_fit"
    UNKNOWN = "unknown"


# hull type_id -> frozenset of bonused disciplines (verified 2026-06-16).
HULL_BURST_BONUS: dict[int, frozenset[str]] = {
    # Command Ships
    22448: frozenset({ARMOR, INFORMATION}),    # Absolution
    22474: frozenset({ARMOR, INFORMATION}),    # Damnation
    22470: frozenset({SHIELD, INFORMATION}),   # Nighthawk
    22446: frozenset({SHIELD, INFORMATION}),   # Vulture
    22466: frozenset({ARMOR, SKIRMISH}),       # Astarte
    22442: frozenset({ARMOR, SKIRMISH}),       # Eos
    22444: frozenset({SHIELD, SKIRMISH}),      # Sleipnir
    22468: frozenset({SHIELD, SKIRMISH}),      # Claymore
    # Command Destroyers
    37481: frozenset({ARMOR, INFORMATION}),    # Pontifex
    37482: frozenset({SHIELD, INFORMATION}),   # Stork
    37483: frozenset({ARMOR, SKIRMISH}),       # Magus
    37480: frozenset({SHIELD, SKIRMISH}),      # Bifrost
    # Strategic Cruisers — bonus is subsystem-conditional (see T3C_HULL_IDS)
    29986: frozenset({ARMOR, INFORMATION, SKIRMISH}),   # Legion
    29984: frozenset({SHIELD, INFORMATION, SKIRMISH}),  # Tengu
    29988: frozenset({ARMOR, INFORMATION, SKIRMISH}),   # Proteus
    29990: frozenset({ARMOR, SHIELD, SKIRMISH}),        # Loki
}

T3C_HULL_IDS: frozenset[int] = frozenset({29986, 29984, 29988, 29990})

# Ship group_ids that can fit a command burst module at all (CCP hull restriction).
# 540/1534/963 already exist in ship_classes.py; the rest are from recall and
# MUST be confirmed against the SDE during implementation before relying on them.
BURST_CAPABLE_GROUP_IDS: frozenset[int] = frozenset({
    540,   # Command Ship
    1534,  # Command Destroyer
    963,   # Strategic Cruiser
    1201,  # Combat Battlecruiser
    941,   # Industrial Command Ship
    883,   # Capital Industrial Ship
    547,   # Carrier
    1538,  # Force Auxiliary
    659,   # Supercarrier
    30,    # Titan
})


def evaluate_discipline(discipline: str, ship_type_id, group_id) -> Verdict:
    """Verdict for one discipline a pilot links, given their hull + group.

    None ship_type_id -> UNKNOWN (no roster/ship data). Known bonused hulls
    (CS/CD/T3C) can always fit; T3C bonus is reported as conditional. Unknown
    hulls fall back to group-based fittability.
    """
    if ship_type_id is None:
        return Verdict.UNKNOWN
    bonus = HULL_BURST_BONUS.get(ship_type_id)
    if bonus is None:
        if group_id is None:
            return Verdict.UNKNOWN
        return Verdict.FITS_NO_BONUS if group_id in BURST_CAPABLE_GROUP_IDS else Verdict.CANT_FIT
    if discipline in bonus:
        if ship_type_id in T3C_HULL_IDS:
            return Verdict.BONUSED_CONDITIONAL
        return Verdict.BONUSED
    return Verdict.FITS_NO_BONUS


# ── Render model + text/glyph helpers ────────────────────────────────────────
MAX_CHARGES = 3

VERDICT_GLYPH = {
    Verdict.BONUSED: "✓",              # ✓
    Verdict.BONUSED_CONDITIONAL: "✓",  # ✓ (tooltip carries the caveat)
    Verdict.FITS_NO_BONUS: "⚠",        # ⚠
    Verdict.CANT_FIT: "✗",             # ✗
    Verdict.UNKNOWN: "?",
}


@dataclass
class DisciplineCell:
    discipline: str
    charges: list[str]
    verdict: Verdict


@dataclass
class PilotRow:
    name: str
    ship_type_id: int | None
    charge_count: int
    over_limit: bool
    cells: list[DisciplineCell] = field(default_factory=list)


def build_pilot_rows(snapshot, name_to_ship_type_id, group_of) -> list[PilotRow]:
    """Build the per-pilot render model.

    snapshot: list of (pilot_name, set[(discipline, charge)]).
    name_to_ship_type_id: dict of lowercased pilot name -> hull type_id (roster).
    group_of: callable(type_id) -> group_id | None (e.g. ship_classes.get_group_id).
    Cells are emitted in DISCIPLINES order, only for disciplines the pilot linked.
    """
    rows: list[PilotRow] = []
    for name, charges in snapshot:
        ship_type_id = name_to_ship_type_id.get(name.lower())
        group_id = group_of(ship_type_id) if ship_type_id is not None else None
        by_disc: dict[str, list[str]] = {}
        for disc, charge in charges:
            by_disc.setdefault(disc, []).append(charge)
        cells = [
            DisciplineCell(disc, sorted(by_disc[disc]),
                           evaluate_discipline(disc, ship_type_id, group_id))
            for disc in DISCIPLINES if disc in by_disc
        ]
        rows.append(PilotRow(
            name=name,
            ship_type_id=ship_type_id,
            charge_count=len(charges),
            over_limit=len(charges) > MAX_CHARGES,
            cells=cells,
        ))
    rows.sort(key=lambda r: r.name.lower())
    return rows


def verdict_text(verdict: Verdict, discipline_label: str, charges, ship_name=None) -> str:
    base = f"{discipline_label}: {', '.join(charges)}"
    if verdict is Verdict.BONUSED:
        return f"{base} — bonused"
    if verdict is Verdict.BONUSED_CONDITIONAL:
        return f"{base} — bonused (requires Offensive – Support Processor subsystem)"
    if verdict is Verdict.FITS_NO_BONUS:
        who = ship_name or "ship"
        return f"{base} — fits but {who} is not bonused for {discipline_label}"
    if verdict is Verdict.CANT_FIT:
        who = ship_name or "ship"
        return f"{base} — {who} cannot fit a command burst"
    return f"{base} — ship unknown"
