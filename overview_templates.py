"""FCTool-original "FC Standard" starter overview pack.

Generates a five-tab combat-FC overview pack programmatically from the bundled
SDE group/category tables (``inv_groups.json`` + ``inv_categories.json``), so no
YAML file needs to be bundled (owner decision D4, plan
``docs/superpowers/plans/2026-07-13-overview-manager-ui-p2.md`` Wave D3).

The content is ORIGINAL: presets are built by selecting SDE ``groupID``s from the
tables by category and name heuristics. Only the community *conventions* are
borrowed (tab archetypes Travel / Fleet / Targets / Logi / D-scan; "filter
friendlies on combat tabs") — no Z-S / E-UNI preset group lists are reproduced.

The pack is intentionally PARTIAL: it carries presets, tabs and global columns
only; appearance (flag/background/state colors/blinks), ship labels and user
settings are absent (``None``) so it imports cleanly as a partial overlay onto
whatever the client already has (research §A.2: partial files are valid imports).

Tables are the parsed JSON shapes:
  * ``groups``     : ``{"<groupID>": {"cat": <categoryID>, "name": str, "pub": bool}}``
  * ``categories`` : ``{"<categoryID>": <name>}``
The caller loads them; ``load_tables()`` is a convenience that reads the two
bundled JSONs (mirrors ``type_catalog.py``'s ``bundle_dir()`` load).
"""
from __future__ import annotations

import json
import os

import overview_schema
from overview_schema import BRACKET_SHOW_ALL, COLUMN_IDS, OverviewPack, Preset, TabConfig
from app_path import bundle_dir, resolve_data_file

# --- state IDs used by the template ---------------------------------------
# PROVENANCE (mirrors overview_schema.STATE_DEFS discipline): only 11 (pilot in
# your fleet) is research-attested. 12 (corp) and 14 (alliance) are the standard
# EVE standing/war state IDs — their meanings are PROVISIONAL here and the owner
# confirms them in-game. Labels never affect data: these ints pass straight
# through to the client, which is the final arbiter of their meaning.
STATE_FLEET = 11          # attested — pilot is in your fleet
STATE_CORP = 12           # provisional — pilot is in your corporation
STATE_ALLIANCE = 14       # provisional — pilot is in your alliance

# Friendlies to hide on the combat "Targets" tab (fleet + corp + alliance).
_FRIENDLY_STATES = [STATE_FLEET, STATE_CORP, STATE_ALLIANCE]

# Canonical preset names (FC-namespaced so a merge-import never collides with
# CCP-default or Z-S presets already in the client). Tabs bind to these exactly.
_PRESET_TRAVEL = "FC: Travel"
_PRESET_FLEET = "FC: Fleet"
_PRESET_TARGETS = "FC: Targets"
_PRESET_LOGI = "FC: Logistics"
_PRESET_DSCAN = "FC: D-Scan"

# Global column layout: full priority order + a combat-FC shown subset. Every
# id is a member of overview_schema.COLUMN_IDS (validate() checks membership).
_COLUMN_ORDER = list(COLUMN_IDS)
_OVERVIEW_COLUMNS = [
    "ICON", "TAG", "DISTANCE", "NAME", "TYPE",
    "CORPORATION", "ALLIANCE", "VELOCITY", "TRANSVERSALVELOCITY",
]


# --- table loading ---------------------------------------------------------

def _table_path(filename: str) -> str:
    """Resolve a bundled table path via the shared ``resolve_data_file`` with
    ``prefer="bundle"`` (``bundle_dir()`` first, this module's dir as the
    source-checkout fallback) — a pristine shipped SDE table, never locally
    overridden. When the file is absent everywhere, keep the historic
    unconditional bundle path so ``_load_json`` raises a clear FileNotFound."""
    return (resolve_data_file(filename, prefer="bundle")
            or os.path.join(bundle_dir(), filename))


def _load_json(filename: str) -> dict:
    with open(_table_path(filename), encoding="utf-8") as fh:
        return json.load(fh)


def load_tables() -> tuple[dict, dict]:
    """Load and parse the bundled ``(inv_groups, inv_categories)`` tables."""
    groups = _load_json("inv_groups.json")
    categories = _load_json("inv_categories.json")
    return groups, categories


# --- group selection helpers ----------------------------------------------

def _category_id(categories: dict, name: str, default: int) -> int:
    """Resolve a category NAME (case-insensitive) to its id via the table,
    falling back to ``default`` if the name is absent — robust to SDE id drift
    while keeping a hard-coded floor."""
    target = name.strip().lower()
    for cid, cname in (categories or {}).items():
        if isinstance(cname, str) and cname.strip().lower() == target:
            try:
                return int(cid)
            except (TypeError, ValueError):
                continue
    return default


def _groups_in_categories(groups: dict, cat_ids, *, published_only: bool = True) -> list:
    """Sorted int groupIDs whose ``cat`` is in ``cat_ids`` (optionally only
    ``pub`` groups)."""
    wanted = set(cat_ids)
    out = []
    for gid, info in (groups or {}).items():
        if not isinstance(info, dict):
            continue
        if info.get("cat") in wanted and (info.get("pub") or not published_only):
            try:
                out.append(int(gid))
            except (TypeError, ValueError):
                continue
    return sorted(out)


def _groups_by_name(groups: dict, names) -> list:
    """Sorted int groupIDs whose ``name`` exactly matches (case-insensitive) one
    of ``names``. Used for celestial/navigation groups that are unpublished in
    the SDE but are valid overview groups (Stargate, Station, Planet, ...)."""
    wanted = {n.strip().lower() for n in names}
    out = []
    for gid, info in (groups or {}).items():
        if not isinstance(info, dict):
            continue
        nm = info.get("name")
        if isinstance(nm, str) and nm.strip().lower() in wanted:
            try:
                out.append(int(gid))
            except (TypeError, ValueError):
                continue
    return sorted(out)


# --- pack builder ----------------------------------------------------------

def build_fc_standard(groups: dict, categories: dict) -> OverviewPack:
    """Build the FCTool-original "FC Standard" starter overview pack.

    ``groups``/``categories`` are the parsed ``inv_groups.json`` /
    ``inv_categories.json`` tables (use ``load_tables()`` to obtain them).
    Returns a partial :class:`overview_schema.OverviewPack` (presets + tabs +
    global columns; appearance/labels/user-settings absent).
    """
    cat = lambda name, default: _category_id(categories, name, default)  # noqa: E731
    cat_ship = cat("Ship", 6)
    cat_drone = cat("Drone", 18)
    cat_fighter = cat("Fighter", 87)
    cat_structure = cat("Structure", 65)
    cat_starbase = cat("Starbase", 23)
    cat_deployable = cat("Deployable", 22)
    cat_sovereignty = cat("Sovereignty Structures", 40)
    cat_orbital = cat("Orbitals", 46)

    # Building blocks -------------------------------------------------------
    ships = _groups_in_categories(groups, [cat_ship])
    drones = _groups_in_categories(groups, [cat_drone, cat_fighter])
    structures = _groups_in_categories(groups, [cat_structure])
    starbases = _groups_in_categories(groups, [cat_starbase])
    deployables = _groups_in_categories(groups, [cat_deployable])
    sov = _groups_in_categories(groups, [cat_sovereignty])
    orbitals = _groups_in_categories(groups, [cat_orbital])

    # Navigation celestials + hazards (unpublished-but-valid overview groups).
    warpables = _groups_by_name(groups, [
        "Stargate", "Station", "Planet", "Moon", "Asteroid Belt", "Sun",
        "Wormhole", "Beacon", "Warp Gate",
    ])
    hazards = _groups_by_name(groups, [
        "Mobile Warp Disruptor", "Force Field",
        "Cynosural Fields", "Covert Cynosural Field",
    ])

    def uniq(*chunks) -> list:
        merged = set()
        for chunk in chunks:
            merged.update(chunk)
        return sorted(merged)

    # Presets ---------------------------------------------------------------
    travel = Preset(
        name=_PRESET_TRAVEL,
        groups=uniq(warpables, structures, sov, orbitals, hazards),
    )
    fleet = Preset(
        name=_PRESET_FLEET,
        groups=uniq(ships, drones),
        always_shown_states=[STATE_FLEET],
    )
    targets = Preset(
        name=_PRESET_TARGETS,
        groups=uniq(ships),
        filtered_states=list(_FRIENDLY_STATES),   # hide fleet/corp/alliance
    )
    logi = Preset(
        name=_PRESET_LOGI,
        groups=uniq(ships),
        always_shown_states=[STATE_FLEET],         # fleet mates never filtered
    )
    dscan = Preset(
        name=_PRESET_DSCAN,
        groups=uniq(ships, drones, structures, starbases, deployables,
                    _groups_by_name(groups, [
                        "Wormhole", "Force Field",
                        "Cynosural Fields", "Covert Cynosural Field",
                    ])),
    )
    presets = [travel, fleet, targets, logi, dscan]

    # Tabs (all brackets = show-all sentinel; per-tab RGB accent colors) -----
    tabs = [
        TabConfig(index=0, name="Travel ✈", overview_preset=_PRESET_TRAVEL,
                  bracket_preset=BRACKET_SHOW_ALL, color=[0.40, 0.62, 1.00]),
        TabConfig(index=1, name="Fleet", overview_preset=_PRESET_FLEET,
                  bracket_preset=BRACKET_SHOW_ALL, color=[0.30, 0.80, 0.45]),
        TabConfig(index=2, name="Targets", overview_preset=_PRESET_TARGETS,
                  bracket_preset=BRACKET_SHOW_ALL, color=[0.75, 0.00, 0.00]),
        TabConfig(index=3, name="Logi", overview_preset=_PRESET_LOGI,
                  bracket_preset=BRACKET_SHOW_ALL, color=[0.20, 0.78, 0.80]),
        TabConfig(index=4, name="D-Scan", overview_preset=_PRESET_DSCAN,
                  bracket_preset=BRACKET_SHOW_ALL, color=[1.00, 0.62, 0.10]),
    ]

    return OverviewPack(
        presets=presets,
        tabs=tabs,
        column_order=list(_COLUMN_ORDER),
        overview_columns=list(_OVERVIEW_COLUMNS),
        # appearance / ship labels / user settings intentionally absent (partial pack)
    )
