"""Pure label-derivation core for the Eve-O Preview activity overlay.

No Tk, no ctypes, no network. CharState is produced by the fc_gui ESI poller
(Phase 2) or left mostly-unknown (Phase 1, manual overrides only). label_for
turns a CharState + ordered rules + overrides into the string drawn on that
character's thumbnail (or '' for no label).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CharState:
    """Snapshot of one character's ESI-derived activity. Fields default to the
    'unknown' shape used in Phase 1 (no poller yet) so overrides still work."""
    character_id: int
    name: str
    online: bool | None = None          # None = unknown (no scope / not fetched)
    ship_type_id: int | None = None
    ship_type_name: str = ""
    ship_group: str = ""                 # via ship_classes.get_group_name
    is_capital: bool | None = None
    solar_system_id: int | None = None
    system_name: str = ""
    docked: bool = False                 # station_id or structure_id present


@dataclass
class OverlayRule:
    when: str      # 'ship_group'|'ship_type'|'system'|'docked'|'offline'|'capital'|'subcap'
    value: str     # match value ('' for docked/offline/capital/subcap)
    label: str


def _fill(label: str, state: CharState) -> str:
    """Fill {ship} {group} {system} placeholders from state."""
    return (
        label.replace("{ship}", state.ship_type_name or "")
             .replace("{group}", state.ship_group or "")
             .replace("{system}", state.system_name or "")
    )


def _rule_matches(rule: OverlayRule, state: CharState) -> bool:
    when = rule.when
    val = (rule.value or "").strip().lower()
    if when == "ship_group":
        return bool(state.ship_group) and state.ship_group.lower() == val
    if when == "ship_type":
        return bool(state.ship_type_name) and state.ship_type_name.lower() == val
    if when == "system":
        return bool(state.system_name) and state.system_name.lower() == val
    if when == "docked":
        return state.docked is True
    if when == "offline":
        return state.online is False    # unknown (None) must not match
    if when == "capital":
        return state.is_capital is True
    if when == "subcap":
        return state.is_capital is False
    return False


def label_for(state: CharState, rules, overrides) -> str:
    """Resolve the label to draw for `state`.

    Precedence: an override for this character (keyed by lowercased name) wins
    outright — even when its value is '' (empty override = 'hide this char').
    Otherwise the first matching rule's label (placeholder-filled) is used.
    No override and no matching rule → '' (draw nothing)."""
    key = (state.name or "").strip().lower()
    if overrides and key in {k.strip().lower(): v for k, v in overrides.items()}:
        norm = {k.strip().lower(): v for k, v in overrides.items()}
        return _fill(norm[key] or "", state)
    for rule in rules or ():
        if _rule_matches(rule, state):
            return _fill(rule.label, state)
    return ""


def seed_rules() -> list[OverlayRule]:
    """Default rules created once when the feature is first enabled."""
    return [
        OverlayRule("ship_group", "Force Recon Ship", "Cyno"),
        OverlayRule("ship_group", "Black Ops", "Bridger"),
        OverlayRule("capital", "", "{group}"),
    ]
