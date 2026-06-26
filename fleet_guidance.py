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
COMPOSITION_ROLE_ORDER = ("DPS", "Logistics", "Links", "Support - Webs", "Tackle")

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
    if mode in ("percent", "count") and member.ideal_min is not None:
        # Explicit override; min/max stored as-is (max may be None = no upper bound).
        return EffectiveIdeal(mode=mode, min=member.ideal_min, max=member.ideal_max, role=role)
    # Unset mode, or an explicit mode with a blank (None) min: fall through to the
    # tag default for this role (Links -> computed range; else TAG_DEFAULTS).
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
    # Ceil so a positive-percentage target never rounds down to 0 in a small fleet
    # (e.g. 25% of 2 -> 1, not 0). A minimum should round UP to the smallest pilot
    # count that reaches the percentage; this keeps small-fleet guidance meaningful.
    return int(math.ceil((pct / 100.0) * fleet_total))


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
    """Compute role-level composition guidance + a Defenders overlay rollup.

    ``get_fit(fit_id) -> Fit | None``. ``fleet_ship_counts`` maps hull type id ->
    live count; ``fleet_total`` is the current fleet size, or None when there is no
    live (boss) fleet.

    Composition targets (percent/count) are ROLE-LEVEL: the role's single ideal is
    applied once, ``current`` is the SUM of the role's hull counts, and every fit in
    the role carries that same role-level target/current/delta. So two Logistics
    fits (e.g. Basilisk + Scimitar) both show the same role shortfall (e.g. +4),
    rather than each independently targeting the full percentage.
    """
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

    # Group guided members by composition role (Defenders is a separate overlay).
    by_role: dict[str, list] = {}
    for m in members:
        role = _composition_role(m.tags)
        if role is None:
            continue
        f = get_fit(m.fit_id)
        if f is None:
            continue
        by_role.setdefault(role, []).append((m, f))

    fits: list[FitGuidance] = []
    roles: dict = {}
    for role, items in by_role.items():
        if role == "Links" and links_suppressed:
            continue
        # One role ideal: prefer an explicit per-member override (any member in the
        # role represents the whole role's target), else the first resolvable default.
        ideal = None
        for (m, _f) in items:
            if m.ideal_mode in ("percent", "count") and m.ideal_min is not None:
                cand = resolve_composition_ideal(m, links_range)
                if cand is not None:
                    ideal = cand
                    break
        if ideal is None:
            for (m, _f) in items:
                ideal = resolve_composition_ideal(m, links_range)
                if ideal is not None:
                    break
        if ideal is None:
            continue

        if has_live:
            tmin, tmax = _targets_in_pilots(ideal, fleet_total)
            current = sum(fleet_ship_counts.get(f.hull_type_id, 0) for (_m, f) in items)
            status, delta = compute_delta(current, tmin, tmax)
        else:
            tmin, tmax = _targets_in_pilots(ideal, fleet_total or 0)
            current, status, delta = None, "unknown", 0

        roles[role] = RoleRollup(role, tmin, tmax, current, status, delta)
        for (m, f) in items:
            fits.append(FitGuidance(
                fit_id=m.fit_id, hull_type_id=f.hull_type_id,
                label=(f.hull_name or f.name or str(f.hull_type_id)),
                role=role, mode=ideal.mode,
                target_min=tmin, target_max=tmax,
                current=current, status=status, delta=delta))

    roles["Defenders"] = _defenders_overlay(members, get_fit, fleet_ship_counts, has_live)
    return GuidanceReport(fits=fits, roles=roles,
                          links_suppressed=links_suppressed, has_live_fleet=has_live)


def _defenders_overlay(members, get_fit, fleet_ship_counts, has_live) -> RoleRollup:
    if has_live:
        hulls = set()
        for m in members:
            if DEFENDER_TAG in m.tags:
                f = get_fit(m.fit_id)
                if f is not None:
                    hulls.add(f.hull_type_id)
        current = sum(fleet_ship_counts.get(h, 0) for h in hulls)
        status, delta = compute_delta(current, DEFENDER_TARGET_MIN, None)
    else:
        current, status, delta = None, "unknown", 0
    return RoleRollup(DEFENDER_TAG, DEFENDER_TARGET_MIN, None, current, status, delta)
