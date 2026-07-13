"""Canonical overview-pack model + wire mapping.

Wire form = the parsed shape of an in-game overview YAML export: a dict of up
to 13 known top-level sections whose values are nested lists (list-of-pairs
style, no mappings). Facts: docs/superpowers/research/2026-07-12-overview-manager.md
(R §A.2 schema, §B.4 live correspondence).
"""
from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass, field

YAML_SECTIONS = (
    "backgroundOrder", "backgroundStates", "columnOrder", "flagOrder",
    "flagStates", "overviewColumns", "presets", "shipLabelOrder",
    "shipLabels", "stateBlinks", "stateColorsNameList", "tabSetup",
    "userSettings",
)
MAX_TABS = 20                      # CCP support article, June 2026 (R §A.3)
BRACKET_SHOW_ALL = "_BracketFilterShowAll"
COLUMN_IDS = [
    "ICON", "TAG", "DISTANCE", "NAME", "TYPE", "ALLIANCE", "CORPORATION",
    "FACTION", "MILITIA", "SIZE", "VELOCITY", "RADIALVELOCITY",
    "TRANSVERSALVELOCITY", "ANGULARVELOCITY",
]
_PRESET_KEYS = ("alwaysShownStates", "filteredStates", "groups")
_TAB_KEYS = ("bracket", "color", "name", "overview", "tabColumns")


@dataclass
class Preset:
    name: str
    groups: list = field(default_factory=list)
    filtered_states: list = field(default_factory=list)
    always_shown_states: list = field(default_factory=list)
    extra_pairs: list = field(default_factory=list)   # unknown [k, v] pairs, preserved


@dataclass
class TabConfig:
    index: int
    name: str = ""
    overview_preset: str = ""
    bracket_preset: str = BRACKET_SHOW_ALL
    color: list | None = None                 # [r, g, b] floats 0..1, or None
    tab_columns: list | None = None            # list[str] or None (= global columns)
    extra_pairs: list = field(default_factory=list)


@dataclass
class OverviewPack:
    presets: list | None = None                # list[Preset]
    tabs: list | None = None                   # list[TabConfig]
    flag_order: list | None = None
    flag_states: list | None = None
    background_order: list | None = None
    background_states: list | None = None
    state_blinks: list | None = None           # [["flag_13", True], ...]
    state_colors: list | None = None           # [["background_10", "white"], ...]
    column_order: list | None = None
    overview_columns: list | None = None
    ship_label_order: list | None = None
    ship_labels: list | None = None            # wire shape passthrough
    user_settings: list | None = None          # [["hideCorpTicker", True], ...]
    extras: dict = field(default_factory=dict)  # unknown top-level sections


def from_wire(wire: dict) -> OverviewPack:
    wire = dict(wire or {})
    pack = OverviewPack()

    if "presets" in wire:
        pack.presets = []
        for entry in wire.pop("presets") or []:
            name, pairs = entry[0], (entry[1] if len(entry) > 1 else [])
            known = {}
            extra = []
            for pair in pairs or []:
                if pair and pair[0] in _PRESET_KEYS:
                    known[pair[0]] = pair[1] if len(pair) > 1 else []
                else:
                    extra.append(list(pair))
            pack.presets.append(Preset(
                name=name,
                groups=list(known.get("groups") or []),
                filtered_states=list(known.get("filteredStates") or []),
                always_shown_states=list(known.get("alwaysShownStates") or []),
                extra_pairs=extra,
            ))

    if "tabSetup" in wire:
        pack.tabs = []
        for entry in wire.pop("tabSetup") or []:
            idx, pairs = entry[0], (entry[1] if len(entry) > 1 else [])
            known = {}
            extra = []
            for pair in pairs or []:
                if pair and pair[0] in _TAB_KEYS:
                    known[pair[0]] = pair[1] if len(pair) > 1 else None
                else:
                    extra.append(list(pair))
            pack.tabs.append(TabConfig(
                index=idx,
                name=known.get("name") or "",
                overview_preset=known.get("overview") or "",
                bracket_preset=known.get("bracket") or BRACKET_SHOW_ALL,
                color=known.get("color"),
                tab_columns=known.get("tabColumns"),
                extra_pairs=extra,
            ))

    simple = {
        "flagOrder": "flag_order", "flagStates": "flag_states",
        "backgroundOrder": "background_order",
        "backgroundStates": "background_states",
        "stateBlinks": "state_blinks", "stateColorsNameList": "state_colors",
        "columnOrder": "column_order", "overviewColumns": "overview_columns",
        "shipLabelOrder": "ship_label_order", "shipLabels": "ship_labels",
        "userSettings": "user_settings",
    }
    for wire_key, attr in simple.items():
        if wire_key in wire:
            setattr(pack, attr, wire.pop(wire_key))

    pack.extras = wire            # everything left over, verbatim
    return pack


def to_wire(pack: OverviewPack) -> dict:
    wire = {}
    if pack.presets is not None:
        entries = []
        for p in pack.presets:
            pairs = [["alwaysShownStates", list(p.always_shown_states)],
                     ["filteredStates", list(p.filtered_states)],
                     ["groups", list(p.groups)]]
            pairs.extend([list(x) for x in p.extra_pairs])
            entries.append([p.name, pairs])
        wire["presets"] = entries
    if pack.tabs is not None:
        entries = []
        for t in pack.tabs:
            pairs = [["bracket", t.bracket_preset], ["color", t.color],
                     ["name", t.name], ["overview", t.overview_preset]]
            if t.tab_columns is not None:
                pairs.append(["tabColumns", list(t.tab_columns)])
            pairs.extend([list(x) for x in t.extra_pairs])
            entries.append([t.index, pairs])
        wire["tabSetup"] = entries

    simple = {
        "flag_order": "flagOrder", "flag_states": "flagStates",
        "background_order": "backgroundOrder",
        "background_states": "backgroundStates",
        "state_blinks": "stateBlinks", "state_colors": "stateColorsNameList",
        "column_order": "columnOrder", "overview_columns": "overviewColumns",
        "ship_label_order": "shipLabelOrder", "ship_labels": "shipLabels",
        "user_settings": "userSettings",
    }
    for attr, wire_key in simple.items():
        val = getattr(pack, attr)
        if val is not None:
            wire[wire_key] = val

    wire.update(pack.extras or {})
    return wire


# --- state IDs -------------------------------------------------------------
# PROVENANCE DISCIPLINE: the research doc ATTESTS meanings only for IDs
# 9, 10, 11 and 44 (R §A.2). Every other label below is a PROVISIONAL
# community-recollected hint (Z-S Customizer README / kormat annotations /
# the live-corpus observation that IDs <= 53 occur) and must be treated as
# UI-label guesswork until cross-checked against the golden export — Task 1
# (G1) records every ID it sees and its in-game meaning; that cross-check is
# authoritative and UPDATES this table. Labels never affect data: unknown or
# mislabeled IDs render via state_name() and always pass through untouched.
# G1 corroboration (2026-07-12): every ID below occurs in the real export
# (none phantom); the export ALSO uses IDs 50, 66, 68 which are deliberately
# absent here (no attested meaning — they render as "State N"; label them
# from in-game observation when convenient and extend this table).
STATE_DEFS = {
    9:  "Pilot has security status below -5",
    10: "Pilot has security status below 0",
    11: "Pilot is in your fleet",
    12: "Pilot is in your corporation",
    13: "Pilot is at war with your corporation/alliance",
    14: "Pilot is in your alliance",
    15: "Pilot has excellent standing",
    16: "Pilot has good standing",
    17: "Pilot has neutral standing",
    18: "Pilot has bad standing",
    19: "Pilot has terrible standing",
    20: "Pilot is in your militia",
    21: "Pilot is in allied militia",
    36: "Pilot has a limited engagement with you",
    37: "Pilot is a criminal",
    44: "Pilot is at war with your militia",
    45: "Pilot is a suspect",
    48: "Pilot has a kill right on them",
    49: "Pilot is an interesting agent",
    51: "Pilot is at war with you (mutual)",
    52: "Pilot is a fugitive",
    53: "Pilot is an outlaw",
}


def state_name(state_id) -> str:
    return STATE_DEFS.get(state_id, f"State {state_id}")


def unescape_markup(name: str) -> str:
    """Undo CCP's export HTML-escaping bug exactly once (R §A.10)."""
    if isinstance(name, str) and ("&lt;" in name or "&gt;" in name or "&amp;" in name):
        return html.unescape(name)
    return name


def fingerprint(pack: OverviewPack) -> str:
    payload = json.dumps(to_wire(pack), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def validate(pack: OverviewPack) -> list:
    """Structural warnings only. The client is the final validator; FCTool
    must never be stricter than the client (spec §4.1), so callers treat
    these as advisory badges, never as blockers."""
    warnings = []
    if pack.tabs is not None and len(pack.tabs) > MAX_TABS:
        warnings.append(
            f"{len(pack.tabs)} tabs configured; the client supports {MAX_TABS}")
    preset_names = {p.name for p in (pack.presets or [])}
    for p in pack.presets or []:
        for coll, label in ((p.groups, "groups"),
                            (p.filtered_states, "filteredStates"),
                            (p.always_shown_states, "alwaysShownStates")):
            bad = [x for x in coll if not isinstance(x, int)]
            if bad:
                warnings.append(
                    f"preset {p.name!r}: non-integer {label} entries {bad!r}")
    if pack.presets is not None:
        for t in pack.tabs or []:
            if t.overview_preset and t.overview_preset not in preset_names:
                warnings.append(
                    f"tab {t.index} ({t.name!r}) references missing preset "
                    f"{t.overview_preset!r}")
    for col_list in (pack.column_order, pack.overview_columns):
        for c in col_list or []:
            if c not in COLUMN_IDS:
                warnings.append(f"unknown column id {c!r} (passed through)")
    return warnings
