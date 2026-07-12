"""Pure overlay-layer computation for the star map (spec §5.2).

All game-data lookups are injectable for tests; defaults bind lazily to
system_coords / jump_range. Range/threat use TRUE 3D LY distances via
system_coords.systems_within_range — never 2D map distance (spec: color
systems, never draw a circle).
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _default_within(origin_id: int, range_ly: float, legal_only: bool = True):
    import system_coords
    return system_coords.systems_within_range(origin_id, range_ly, legal_only=legal_only)


def _default_legal(sid: int) -> bool:
    import system_coords
    return system_coords.is_legal_jump_destination(sid)


def _default_resolve(name: str):
    import system_coords
    return system_coords.resolve_name(name)


def _default_ship_table() -> dict:
    from jump_range import JumpRangeChecker
    return JumpRangeChecker.SHIP_RANGES


@dataclass(frozen=True)
class RangeOverlay:
    origin_id: int
    range_ly: float
    ship_label: str
    in_range: frozenset[int]
    illegal: frozenset[int]
    distances: dict[int, float] = field(default_factory=dict)

    def bright_set(self) -> frozenset[int]:
        return self.in_range | {self.origin_id}


def compute_range(origin_id: int, range_ly: float, ship_label: str = "",
                  within_fn=None, legal_fn=None) -> RangeOverlay:
    within = within_fn or _default_within
    legal = legal_fn or _default_legal
    in_sphere = within(origin_id, range_ly, legal_only=False)
    in_range, illegal, dists = set(), set(), {}
    for sid, ly in in_sphere:
        if sid == origin_id:
            continue
        dists[sid] = ly
        (in_range if legal(sid) else illegal).add(sid)
    return RangeOverlay(origin_id, range_ly, ship_label,
                        frozenset(in_range), frozenset(illegal), dists)


def compute_threat(staging_ids, range_ly: float, within_fn=None) -> frozenset[int]:
    """Union of systems inside `range_ly` of ANY hostile staging (staging incl.)."""
    within = within_fn or _default_within
    out: set[int] = set()
    for sid in staging_ids:
        out.add(sid)
        for other, _ly in within(sid, range_ly, legal_only=True):
            out.add(other)
    return frozenset(out)


def fleet_counts(members) -> dict[int, int]:
    counts: dict[int, int] = {}
    for m in members or ():
        sid = m.get("solar_system_id")
        if isinstance(sid, int):
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def resolve_staging(names, resolve_fn=None) -> dict[int, str]:
    resolve = resolve_fn or _default_resolve
    out: dict[int, str] = {}
    for name in names or ():
        sid = resolve(name)
        if sid is not None:
            out[sid] = name
    return out


def resolve_bridges(entries, resolve_fn=None) -> tuple[tuple[int, int], ...]:
    """Resolve config ``ansiblex_connections`` name pairs to a deterministic,
    deduped tuple of unordered ``(id_a, id_b)`` system-id pairs for the map's
    Ansiblex bridge layer.

    Mirrors the canonical parse the jump-range BFS consumes
    (``fc_gui._resolve_ansiblex_sync``): each entry is a 2-element
    ``[name_a, name_b]``; each name resolves via ``resolve_fn`` (default
    ``system_coords.resolve_name`` -- pure/local, NO ESI, so it is safe to call
    on the Tk thread). Unresolvable endpoints, self-pairs, malformed entries,
    and duplicate unordered pairs are dropped. The result is sorted and returned
    as a hashable tuple so it can travel in the render request dict and
    participate in the duplicate-render signature (``map_tab._request_sig``)."""
    resolve = resolve_fn or _default_resolve
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int]] = []
    for entry in entries or ():
        try:
            if len(entry) != 2:
                continue
            name_a, name_b = entry[0], entry[1]
        except (TypeError, KeyError, IndexError):
            continue
        ida = resolve(name_a)
        idb = resolve(name_b)
        if ida is None or idb is None or ida == idb:
            continue
        pair = (ida, idb) if ida < idb else (idb, ida)
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    out.sort()
    return tuple(out)


# Owner decision (spec §2.5): 5 grouped classes; labels carry the live LY value
# read from SHIP_RANGES at runtime so the Rorqual/Lancer fix chip auto-propagates.
_GROUPS = [
    ("Titan / Super", "Titan"),
    ("Dread / Carrier / FAX", "Dreadnought"),
    ("Command Carrier", "Command Carrier"),
    ("Black Ops / Lancer", "Black Ops"),
    ("JF / Rorqual", "Jump Freighter"),
]


def range_options(ship_table: dict | None = None) -> list[tuple[str, float]]:
    table = ship_table if ship_table is not None else _default_ship_table()
    opts = []
    for label, key in _GROUPS:
        ly = float(table.get(key, 6.0))
        opts.append((f"{label} ({ly:.1f} ly)", ly))   # .1f matches test labels exactly
    return opts


def threat_options(ship_table: dict | None = None) -> list[tuple[str, float]]:
    """Titan Bridge first (owner default): bridge range = titan jump range."""
    table = ship_table if ship_table is not None else _default_ship_table()
    tb = float(table.get("Titan", 6.0))
    return [(f"Titan Bridge ({tb:.1f} ly)", tb)] + range_options(table)


def ly_for_ship(ship_name: str, ship_table: dict | None = None) -> float:
    table = ship_table if ship_table is not None else _default_ship_table()
    if ship_name == "Titan Bridge":
        return float(table.get("Titan", 6.0))
    return float(table.get(ship_name, table.get("Titan", 6.0)))
