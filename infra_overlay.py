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


def _gate_endpoints_from_name(name: str) -> tuple[str | None, str | None]:
    """(system, gate_to) endpoints parsed from an Ansiblex gate structure name
    ``"A » B - LABEL"`` -> ``("A", "B")`` (the destination is the text between the
    ``»`` and the first ``" - "`` that follows it).

    The in-game structure name is the GROUND TRUTH for a gate's endpoints;
    ``gate_to_system_name`` is a pre-parsed cache of it. The clipboard parser fills
    that cache (infra_parser); the ESI region scanner records only ``name`` (see
    infra_scan._resolved_row), so without this fallback every SCANNED Ansiblex gate
    is dropped from the bridge union and its hop never lights on the map/route.

    Duplicated BY VALUE from ``infra_parser._split_gate_name`` (the ``»`` branch) --
    infra_* modules never import each other (see CATEGORIES above); keep in
    lockstep. Returns ``(None, None)`` when the name lacks the ``»`` marker."""
    if not name or "»" not in name:
        return None, None
    a, _, rest = name.partition("»")
    rest = rest.strip()
    b = rest.split(" - ", 1)[0].strip() if " - " in rest else rest
    return (a.strip() or None), (b or None)


def gate_pairs(entries: list[dict]) -> list[tuple[str, str]]:
    """Unordered-deduped [(system_name, gate_to_system_name), ...] for status!='dead'
    gates -- the exact shape config['ansiblex_connections'] already uses.

    Endpoints come from the explicit ``system_name``/``gate_to_system_name`` fields
    when present (clipboard/config imports), else are parsed from the structure
    ``name`` (``"A » B - Ansiblex"``). The ESI scanner fills only ``name``, so this
    fallback is what lets SCANNED gates join the SAME bridge union that feeds both
    the map's bridge layer and the destination-route overlay."""
    seen: set[frozenset] = set()
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if entry.get("category") != "gate" or entry.get("status") == "dead":
            continue
        a, b = entry.get("system_name"), entry.get("gate_to_system_name")
        if not a or not b:                       # scanned gate: derive from the name
            na, nb = _gate_endpoints_from_name(entry.get("name") or "")
            a, b = (a or na), (b or nb)
        if not a or not b:
            continue
        key = frozenset((a, b))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((a, b))
    return pairs


def _gate_home(entry: dict) -> str | None:
    """The system a gate structure is PLACED in for chip/bridge purposes -- the
    same home endpoint :func:`gate_pairs` assigns to it: the explicit
    ``system_name`` when present, else the origin parsed from the structure
    ``name`` (``"A » B"`` -> ``A``). ``None`` when neither is known."""
    home = entry.get("system_name")
    if home:
        return home
    origin, _ = _gate_endpoints_from_name(entry.get("name") or "")
    return origin


def corroborated_gate_pairs(entries: list[dict]) -> list[tuple[str, str]]:
    """The :func:`gate_pairs` subset whose BOTH endpoints actually CONTAIN a live
    gate structure in the store -- the fix for the owner's phantom-bridge bug
    (2026-07-22).

    An Ansiblex bridge is a PAIR of structures, one anchored in each endpoint
    system. :func:`gate_pairs` emits a name pair for EVERY live gate, so a lone /
    one-way derivation -- a gate whose reciprocal end was never scanned, or (the
    observed case) whose reciprocal end collapsed onto the same scanned system --
    yields a bridge to a system that holds NO Ansiblex. This filter keeps a pair
    only when BOTH endpoints are among the systems that host a gate structure, so
    the map bridge layer (``fc_gui._get_map_bridges``) draws a bridge only when a
    gate exists on each end (owner's request: "don't show an ansiblex connection
    between 2 systems if one of them does not have an ansiblex in it").

    "Contains a gate" is judged by the store's own PLACEMENT (``_gate_home`` --
    ``system_name``, else the name-parsed origin), NEVER the name-parsed
    DESTINATION: a gate NAMED ``"K-6K16 » SVM-3K"`` but anchored in SVM-3K does
    NOT make K-6K16 gate-bearing. Endpoints compare case-insensitively. A gate
    still EXISTS as a chip in its own system regardless of this filter (it acts on
    the bridge union only, mirroring the reinforced-flag design where a suppressed
    bridge keeps its chip). ``reinforced_gate_pairs`` deliberately does NOT route
    through here -- it resolves flagged gates to id-pairs for SUPPRESSION matching
    and must emit a flagged gate's pair regardless of far-end corroboration.
    Pure/deterministic; input order preserved (as :func:`gate_pairs`)."""
    homes = set()
    for entry in (entries or ()):
        if entry.get("category") != "gate" or entry.get("status") == "dead":
            continue
        home = _gate_home(entry)
        if home:
            homes.add(home.casefold())
    return [(a, b) for a, b in gate_pairs(entries)
            if a.casefold() in homes and b.casefold() in homes]


def reinforced_gate_pairs(entries: list[dict]) -> list[tuple[str, str]]:
    """The ``gate_pairs`` subset for gates manually flagged ``reinforced``.

    Same shape / endpoint derivation as :func:`gate_pairs` (explicit
    ``system_name``/``gate_to_system_name`` fields, else parsed from the gate
    ``name``), restricted to entries whose ``reinforced`` flag is truthy. The
    manual reinforced/offline marker means the Ansiblex needs both ends online
    to bridge, so the map bridge layer (``fc_gui._get_map_bridges``) and the
    jump-range router resolve these to system-id pairs -- via the SAME resolver
    the bridge union uses -- and EXCLUDE the matching pair, dropping the line and
    the routing hop while flagged (reversible: clearing the flag re-includes it).

    ``gate_pairs`` itself is deliberately UNCHANGED (it still emits a reinforced
    gate's pair): a reinforced structure still EXISTS in space, so it stays a
    chip / in the by-system breakdown -- only the bridge is suppressed, and that
    suppression lives at the consumer, not in the pure union. Pure/deterministic."""
    return gate_pairs([e for e in (entries or ()) if e.get("reinforced")])


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
