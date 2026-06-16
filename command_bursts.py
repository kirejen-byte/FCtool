"""Command-burst link tracking — pure domain data + logic.

Maps EVE command-burst charges to disciplines and evaluates whether a pilot's
hull can fit and is bonused for the bursts they link. No Tkinter, no network.

Domain reference verified 2026-06-16 (EVE University wiki + everef.net). See
docs/superpowers/specs/2026-06-16-booster-link-tracking-design.md.
"""
from __future__ import annotations

import re

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
