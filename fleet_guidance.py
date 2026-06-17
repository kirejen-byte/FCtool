"""Pure, Tk-free fleet-composition guidance for FCTool.

Computes per-fit "ideal %/#" targets for a doctrine and compares them against the
live fleet (matched by hull type id). No Tkinter, no network, no store import — it
takes a ``get_fit`` callable and a catalog exposing ``group_of``/``resolve_name``.
See docs/superpowers/specs/2026-06-16-doctrine-fleet-guidance-design.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

COMMAND_BURST_GROUP_ID = 1770
DEFENDER_LAUNCHER_TYPE_ID = 44102
BURST_TARGET_MIN = 9
BURST_TARGET_MAX = 18
COMMAND_SHIP_SKIP_FRACTION = 0.35
DEFENDER_TAG = "Defenders"

# Composition roles resolved per-fit, in priority order. "Defenders" is NOT here —
# it is an additive fleet-wide overlay (see compute_fleet_guidance).
COMPOSITION_ROLE_ORDER = ("DPS", "Logistics", "Links", "Support - Webs")

# Tag -> (mode, min, max) default. "Links" has no static default (computed).
TAG_DEFAULTS: dict[str, tuple[str, int, int | None]] = {
    "DPS": ("percent", 50, 60),
    "Logistics": ("percent", 25, 35),
    "Support - Webs": ("count", 2, 6),
}
DEFENDER_TARGET_MIN = 8


def count_command_bursts(parsed, catalog) -> int:
    """Number of fitted Command Burst modules (group 1770; name fallback)."""
    n = 0
    for m in parsed.modules:
        gid = None
        try:
            gid = catalog.group_of(m.type_id)
        except Exception:
            gid = None
        if gid == COMMAND_BURST_GROUP_ID:
            n += 1
        elif gid is None and "command burst" in (m.name or "").lower():
            n += 1
    return n


def has_defender_launcher(parsed, catalog) -> bool:
    """True if the fit mounts a Defender Launcher (type 44102, or name match)."""
    for m in parsed.modules:
        if m.type_id == DEFENDER_LAUNCHER_TYPE_ID:
            return True
        name = (m.name or "")
        if not name or name == str(m.type_id):
            try:
                name = catalog.resolve_name(m.type_id) or ""
            except Exception:
                name = ""
        low = name.lower()
        if "defender" in low and "launcher" in low:
            return True
    return False


def links_ideal_range(links_parsed_fits, catalog) -> tuple[int, int] | None:
    """Ideal (min, max) number of links ships to secure 9..18 total command bursts.

    avg = mean burst count across the doctrine's links fits; min = ceil(9/avg),
    max = ceil(18/avg). Returns None when there are no links fits or they carry no
    bursts (avg 0)."""
    if not links_parsed_fits:
        return None
    counts = [count_command_bursts(p, catalog) for p in links_parsed_fits]
    total = sum(counts)
    if total <= 0:
        return None
    avg = total / len(counts)
    lo = math.ceil(BURST_TARGET_MIN / avg)
    hi = math.ceil(BURST_TARGET_MAX / avg)
    return (lo, hi)
