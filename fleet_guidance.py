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


@dataclass
class EffectiveIdeal:
    mode: str          # "percent" | "count"
    min: int
    max: int | None
    role: str          # the composition tag it came from


def _composition_role(tags) -> str | None:
    for role in COMPOSITION_ROLE_ORDER:
        if role in tags:
            return role
    return None


def resolve_composition_ideal(member, links_range) -> EffectiveIdeal | None:
    """Effective composition ideal for a member, or None if it has none.

    Explicit value wins; "off" disables; otherwise the tag default (Links uses the
    precomputed ``links_range``). Defenders is NOT a composition role here."""
    role = _composition_role(member.tags)
    if role is None:
        return None
    mode = member.ideal_mode
    if mode == "off":
        return None
    if mode in ("percent", "count"):
        # Explicit override; min/max stored as-is (max may be None = no upper bound).
        return EffectiveIdeal(mode=mode, min=member.ideal_min, max=member.ideal_max, role=role)
    # Unset -> tag default.
    if role == "Links":
        if links_range is None:
            return None
        return EffectiveIdeal(mode="count", min=links_range[0], max=links_range[1], role=role)
    default = TAG_DEFAULTS.get(role)
    if default is None:
        return None
    dmode, dmin, dmax = default
    return EffectiveIdeal(mode=dmode, min=dmin, max=dmax, role=role)


def percent_to_pilots(pct, fleet_total) -> int:
    return int(round((pct / 100.0) * fleet_total))


def compute_delta(current, target_min, target_max):
    """Return (status, delta): under (+need), in (0), over (-excess)."""
    if current < target_min:
        return ("under", target_min - current)
    if target_max is not None and current > target_max:
        return ("over", -(current - target_max))
    return ("in", 0)


@dataclass
class FitGuidance:
    fit_id: str
    hull_type_id: int
    label: str
    role: str
    mode: str
    target_min: int
    target_max: int | None
    current: int | None
    status: str          # "under" | "in" | "over" | "unknown"
    delta: int


@dataclass
class RoleRollup:
    tag: str
    target_min: int
    target_max: int | None
    current: int | None
    status: str
    delta: int


@dataclass
class GuidanceReport:
    fits: list[FitGuidance]
    roles: dict
    links_suppressed: bool
    has_live_fleet: bool


def _targets_in_pilots(ideal: EffectiveIdeal, fleet_total):
    if ideal.mode == "percent":
        tmin = percent_to_pilots(ideal.min, fleet_total)
        tmax = percent_to_pilots(ideal.max, fleet_total) if ideal.max is not None else None
        return tmin, tmax
    return ideal.min, ideal.max


def compute_fleet_guidance(doctrine, get_fit, catalog, fleet_ship_counts,
                           fleet_total, command_ship_fraction=0.0) -> GuidanceReport:
    """Compute per-fit composition guidance + a Defenders overlay rollup.

    ``get_fit(fit_id) -> Fit | None``. ``fleet_ship_counts`` maps hull type id ->
    live count; ``fleet_total`` is the current fleet size, or None when there is no
    live (boss) fleet."""
    has_live = fleet_total is not None
    members = list(getattr(doctrine, "members", []))

    # Precompute the links range from the doctrine's links fits.
    links_parsed = []
    for m in members:
        if "Links" in m.tags:
            f = get_fit(m.fit_id)
            if f is not None:
                links_parsed.append(f.parsed)
    links_range = links_ideal_range(links_parsed, catalog)
    links_suppressed = command_ship_fraction > COMMAND_SHIP_SKIP_FRACTION

    fits: list[FitGuidance] = []
    for m in members:
        f = get_fit(m.fit_id)
        if f is None:
            continue
        ideal = resolve_composition_ideal(m, links_range)
        if ideal is None:
            continue
        if ideal.role == "Links" and links_suppressed:
            continue
        if has_live:
            tmin, tmax = _targets_in_pilots(ideal, fleet_total)
            current = fleet_ship_counts.get(f.hull_type_id, 0)
            status, delta = compute_delta(current, tmin, tmax)
        else:
            tmin, tmax = _targets_in_pilots(ideal, fleet_total or 0)
            current, status, delta = None, "unknown", 0
        fits.append(FitGuidance(
            fit_id=m.fit_id, hull_type_id=f.hull_type_id,
            label=(f.hull_name or f.name or str(f.hull_type_id)),
            role=ideal.role, mode=ideal.mode,
            target_min=tmin, target_max=tmax,
            current=current, status=status, delta=delta))

    roles = _rollup_by_role(fits, has_live)
    roles["Defenders"] = _defenders_overlay(members, get_fit, fleet_ship_counts, has_live)
    return GuidanceReport(fits=fits, roles=roles,
                          links_suppressed=links_suppressed, has_live_fleet=has_live)


def _rollup_by_role(fits, has_live) -> dict:
    out: dict = {}
    grouped: dict[str, list[FitGuidance]] = {}
    for f in fits:
        grouped.setdefault(f.role, []).append(f)
    for role, group in grouped.items():
        tmin = sum(g.target_min for g in group)
        tmax = None if any(g.target_max is None for g in group) else sum(g.target_max for g in group)
        if has_live:
            current = sum(g.current for g in group)
            status, delta = compute_delta(current, tmin, tmax)
        else:
            current, status, delta = None, "unknown", 0
        out[role] = RoleRollup(role, tmin, tmax, current, status, delta)
    return out


def _defenders_overlay(members, get_fit, fleet_ship_counts, has_live) -> RoleRollup:
    hulls = set()
    for m in members:
        if DEFENDER_TAG in m.tags:
            f = get_fit(m.fit_id)
            if f is not None:
                hulls.add(f.hull_type_id)
    if has_live:
        current = sum(fleet_ship_counts.get(h, 0) for h in hulls)
        status, delta = compute_delta(current, DEFENDER_TARGET_MIN, None)
    else:
        current, status, delta = None, "unknown", 0
    return RoleRollup(DEFENDER_TAG, DEFENDER_TARGET_MIN, None, current, status, delta)
