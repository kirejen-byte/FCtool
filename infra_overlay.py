"""Pure badge/filter computation for the map infrastructure layer (plan §3.7,
docs/superpowers/plans/2026-07-11-infra-scan.md).

Stdlib-only. Per the architecture rule (plan §2) this module NEVER imports a
sibling infra_* module, fc_gui, map_tab, or tkinter -- entries/filters/model
arrive as plain dicts/lists/duck-typed objects from the caller (fc_gui.py,
Task 7). Every function here is a pure, deterministic computation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Priority order used only to break "top" ties in infra_badges(). Mirrors the
# CATEGORIES tuple in infra_parser.py (plan §3.2) by value -- duplicated
# deliberately here rather than imported, since infra_overlay must stay
# stdlib-pure and infra_* modules never import each other.
CATEGORIES = ("citadel", "engineering", "refinery", "gate", "flex", "npc", "unknown")

FILTER_DEFAULTS = {
    "enabled": True,
    "categories": {"citadel": True, "engineering": True, "refinery": True,
                   "gate": True, "flex": True, "npc": False, "unknown": True},
    "regions": None,          # None = all configured; else list[int]
    "stale_only": False,
    "sources": None,          # None = all; else list[str]
    "types": None,            # None = no per-type restriction beyond categories;
                              # else a collection of type_ids (see _entry_survives)
}


def _parse_iso(value) -> datetime | None:
    """Best-effort aware-datetime parse of a house-rule aware-UTC ISO string.
    Returns None on anything missing/unparsable rather than raising, so one
    malformed record can't take down the whole overlay computation.

    Naive (offset-less) values -- e.g. a hand-edited store file with a
    last_seen/now_iso missing its UTC offset -- are coerced to UTC rather
    than returned naive: comparing a naive and an aware datetime raises
    TypeError, which would otherwise take down the whole computation (the
    exact failure this docstring promises can't happen)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        log.warning("infra_overlay: unparsable timestamp %r", value)
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _is_stale(entry: dict, now: datetime | None, threshold: timedelta) -> bool:
    last_seen = _parse_iso(entry.get("last_seen"))
    if now is None or last_seen is None:
        return False
    return (now - last_seen) > threshold


def _pick_top(counts: dict) -> str | None:
    if not counts:
        return None
    # Canonical categories first (in CATEGORIES priority order) so max()'s
    # first-encountered-wins tie-break lands on the earlier canonical
    # category; any unexpected category name is appended (sorted, for
    # determinism) so it can still win outright on a strictly higher count.
    ordered = list(CATEGORIES) + sorted(c for c in counts if c not in CATEGORIES)
    return max(ordered, key=lambda cat: counts.get(cat, 0))


def _entry_survives(entry: dict, categories_filter: dict, regions_filter,
                    sources_filter, stale_only: bool, now, threshold,
                    types_filter=None) -> bool:
    """Single overlay-filter predicate (status!='dead' + category / region /
    source / stale-only / per-type). Shared by infra_badges and filter_entries so
    the map chips, hover tooltip and the right-click structure list apply IDENTICAL
    rules -- one source of truth. Does NOT consider system_id placement (that
    grouping rule is badge-specific and stays in infra_badges).

    ``types_filter`` (filters["types"]): None = no per-type restriction beyond the
    category toggles -- the default, byte-identical to the pre-type-filter
    behaviour. A non-None collection of type_ids restricts ONLY entries in a *known
    structure category* (category not in {"npc", "unknown"}): such an entry
    survives the type gate iff its ``type_id`` is in the collection. npc- and
    unknown-category entries are DELIBERATELY exempt -- they stay governed solely by
    their category toggles, so unticking "Fortizar" can never hide an NPC station,
    and an unknown-type manual (name-only) entry never vanishes when any type
    filter is active. This category-based gate is the stdlib-pure equivalent of
    "type_id in infra_parser.TYPE_CATEGORY": categorize() places exactly the
    TYPE_CATEGORY type_ids into the real categories and everything else into
    npc/unknown, so the two coincide for every parser-produced entry (and
    infra_overlay must not import infra_parser -- architecture rule, plan §2)."""
    if entry.get("status") == "dead":
        return False
    category = entry.get("category", "unknown")
    if not categories_filter.get(category, True):
        return False
    if types_filter is not None and category not in ("npc", "unknown") \
            and entry.get("type_id") not in types_filter:
        return False
    if regions_filter is not None and entry.get("region_id") not in regions_filter:
        return False
    if sources_filter is not None and entry.get("source") not in sources_filter:
        return False
    if stale_only and not _is_stale(entry, now, threshold):
        return False
    return True


def filter_entries(entries: list[dict], filters: dict, now_iso: str,
                   stale_days: int = 14) -> list[dict]:
    """Return the entries surviving the overlay filters, in input order -- the
    EXACT predicate infra_badges uses to build its per-system groups, exposed so
    a caller (fc_gui's right-click structure list) can show precisely what the
    chips show. Does NOT drop system_id-None entries -- placement is a
    badge-specific concern; pass already-placed entries (e.g. from
    InfraStore.by_system) when you want only located ones. Pure, deterministic."""
    now = _parse_iso(now_iso)
    threshold = timedelta(days=stale_days)
    categories_filter = filters.get("categories") or {}
    regions_filter = filters.get("regions")
    sources_filter = filters.get("sources")
    stale_only = filters.get("stale_only", False)
    types_filter = filters.get("types")
    return [e for e in entries
            if _entry_survives(e, categories_filter, regions_filter,
                               sources_filter, stale_only, now, threshold,
                               types_filter)]


def infra_badges(entries: list[dict], filters: dict, now_iso: str,
                 stale_days: int = 14, type_name_fn=None) -> dict[int, dict]:
    """system_id -> {"total": n, "counts": {category: n}, "top": category,
    "stale": bool, "type_counts": {display_type: n}}.

    Skips entries with system_id None; applies filters; 'stale' if ALL entries in
    the system have last_seen older than stale_days. ``type_counts`` tallies the
    SPECIFIC structure type of each FILTER-SURVIVING entry (what the map hover
    tooltip lists) -- keyed by ``type_name_fn(type_id, structure_id)`` when a
    resolver is injected (fc_gui passes infra_parser.type_name), else a stdlib
    fallback of ``"npc"`` for npc-category rows and ``str(type_id)`` otherwise, so
    the module stays stdlib-pure. ``type_counts`` is metadata only: the map's
    render-tuple fold (_infra_request_value) reads sid/total/top/stale, so it is
    byte-stable regardless of type_counts. Pure function, deterministic."""
    now = _parse_iso(now_iso)
    threshold = timedelta(days=stale_days)
    categories_filter = filters.get("categories") or {}
    regions_filter = filters.get("regions")
    sources_filter = filters.get("sources")
    stale_only = filters.get("stale_only", False)
    types_filter = filters.get("types")

    per_system: dict[int, list[dict]] = {}
    for entry in entries:
        system_id = entry.get("system_id")
        if system_id is None:
            continue
        if not _entry_survives(entry, categories_filter, regions_filter,
                               sources_filter, stale_only, now, threshold,
                               types_filter):
            continue
        per_system.setdefault(system_id, []).append(entry)

    badges: dict[int, dict] = {}
    for system_id, sys_entries in per_system.items():
        counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for e in sys_entries:
            cat = e.get("category", "unknown")
            counts[cat] = counts.get(cat, 0) + 1
            if type_name_fn is not None:
                tkey = type_name_fn(e.get("type_id"), e.get("structure_id"))
            else:
                tkey = "npc" if cat == "npc" else str(e.get("type_id"))
            type_counts[tkey] = type_counts.get(tkey, 0) + 1
        badges[system_id] = {
            "total": len(sys_entries),
            "counts": counts,
            "top": _pick_top(counts),
            "stale": all(_is_stale(e, now, threshold) for e in sys_entries),
            "type_counts": type_counts,
        }
    return badges


def gate_pairs(entries: list[dict]) -> list[tuple[str, str]]:
    """Unordered-deduped [(system_name, gate_to_system_name), ...] for status!='dead'
    gates -- the exact shape config['ansiblex_connections'] already uses."""
    seen: set[frozenset] = set()
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if entry.get("category") != "gate" or entry.get("status") == "dead":
            continue
        a, b = entry.get("system_name"), entry.get("gate_to_system_name")
        if not a or not b:
            continue
        key = frozenset((a, b))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((a, b))
    return pairs


def regions_catalog(model) -> list[tuple[int, str]]:
    """(region_id, display_name) sorted by name. Source of truth:
    model.region_anchors -- dict[int, tuple[name, lx, ly]] (map_data.py:42,
    populated from map_layout.json['regions'] at :118): name = anchors[rid][0].
    Fall back to f'Region {rid}' only for a region_id with no anchor entry.

    Region-id universe = union of {s.region_id for s in model.systems.values()}
    and model.region_anchors.keys(). model.systems carries region_id straight
    from system_coords.json while region_anchors comes from the separate
    map_layout.json['regions'] table (map_data.py:99-120) -- the two files can
    transiently disagree, so a region is listed even if only one side knows
    about it (verified: MapModel exposes no third, more authoritative list of
    regions to fall back on)."""
    anchors = getattr(model, "region_anchors", None) or {}
    systems = getattr(model, "systems", None) or {}
    region_ids = {s.region_id for s in systems.values()} | set(anchors.keys())
    catalog = [(rid, anchors[rid][0] if rid in anchors else f"Region {rid}")
               for rid in region_ids]
    catalog.sort(key=lambda pair: pair[1])
    return catalog
