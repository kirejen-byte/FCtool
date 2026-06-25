# fleet_template_store.py
"""Fleet-template persistence — pure data model + JSON round-trip + CRUD.

`FleetTemplateStore` owns `fleet_templates.json` in `app_dir()`: a list of
reusable wing/squad/slot structures plus their assignment rules, rebalance
settings, and a cache of free-typed character names for the multibox
autocomplete. Writes are atomic (`app_io.atomic_write_json`) so a crash
mid-write cannot corrupt the file.

Rules reference wings/squads by NAME (not index) so they survive structural
reordering; `validate_template` flags rules whose wing/squad no longer exists
with `broken=True` (skipped at apply time, shown with a warning glyph).

Pure logic: no Tkinter, no ESI, no network.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import uuid4

from app_io import atomic_write_json
from app_log import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1


# ── Dataclasses ──────────────────────────────────────────────────────────────
@dataclass
class Slot:
    character: str | None  # named slot (exact, case-insensitive match); None = role/generic
    tag: str | None        # doctrine-tag role slot; None = generic
    role: str              # ESI role string


@dataclass
class Squad:
    name: str
    max_size: int | None   # advisory; enforced only by the live rebalancer
    slots: list[Slot] = field(default_factory=list)


@dataclass
class Wing:
    name: str
    max_size: int | None
    squads: list[Squad] = field(default_factory=list)


@dataclass
class RuleCondition:
    type: str   # "doctrine_tag" | "ship_type" | "ship_class" | "character"
    value: str


@dataclass
class RuleAction:
    role: str
    wing_name: str | None    # None = "anywhere"
    squad_name: str | None   # None = "anywhere"; non-None requires wing_name non-None


@dataclass
class AssignmentRule:
    priority: int
    condition: RuleCondition
    action: RuleAction
    broken: bool = False     # set by validate_template when wing/squad ref is missing


@dataclass
class RebalanceSettings:
    rebalance_interval_s: int = 60
    move_cooldown_s: int = 45
    bulk_apply_threshold: int = 5
    overflow_strategy: str = "least_populated"


@dataclass
class FleetTemplate:
    id: str
    name: str
    doctrine_id: str | None
    wings: list[Wing] = field(default_factory=list)
    rules: list[AssignmentRule] = field(default_factory=list)
    settings: RebalanceSettings = field(default_factory=RebalanceSettings)


# ── (de)serialization ────────────────────────────────────────────────────────
def _slot_to_dict(s: Slot) -> dict:
    return {"character": s.character, "tag": s.tag, "role": s.role}


def _slot_from_dict(d: dict) -> Slot:
    return Slot(character=d.get("character"), tag=d.get("tag"),
                role=d.get("role", "squad_member"))


def _squad_to_dict(sq: Squad) -> dict:
    return {"name": sq.name, "max_size": sq.max_size,
            "slots": [_slot_to_dict(s) for s in sq.slots]}


def _squad_from_dict(d: dict) -> Squad:
    return Squad(name=d.get("name", ""), max_size=d.get("max_size"),
                 slots=[_slot_from_dict(s) for s in d.get("slots", [])])


def _wing_to_dict(w: Wing) -> dict:
    return {"name": w.name, "max_size": w.max_size,
            "squads": [_squad_to_dict(s) for s in w.squads]}


def _wing_from_dict(d: dict) -> Wing:
    return Wing(name=d.get("name", ""), max_size=d.get("max_size"),
                squads=[_squad_from_dict(s) for s in d.get("squads", [])])


def _rule_to_dict(r: AssignmentRule) -> dict:
    return {
        "priority": r.priority,
        "condition": {"type": r.condition.type, "value": r.condition.value},
        "action": {"role": r.action.role, "wing_name": r.action.wing_name,
                   "squad_name": r.action.squad_name},
        # `broken` is derived at load, not persisted.
    }


def _rule_from_dict(d: dict) -> AssignmentRule:
    c = d.get("condition", {})
    a = d.get("action", {})
    return AssignmentRule(
        priority=d.get("priority", 0),
        condition=RuleCondition(c.get("type", "ship_type"), c.get("value", "")),
        action=RuleAction(a.get("role", "squad_member"),
                          a.get("wing_name"), a.get("squad_name")),
    )


def _settings_to_dict(s: RebalanceSettings) -> dict:
    return {"rebalance_interval_s": s.rebalance_interval_s,
            "move_cooldown_s": s.move_cooldown_s,
            "bulk_apply_threshold": s.bulk_apply_threshold,
            "overflow_strategy": s.overflow_strategy}


def _settings_from_dict(d: dict) -> RebalanceSettings:
    base = RebalanceSettings()
    return RebalanceSettings(
        rebalance_interval_s=d.get("rebalance_interval_s", base.rebalance_interval_s),
        move_cooldown_s=d.get("move_cooldown_s", base.move_cooldown_s),
        bulk_apply_threshold=d.get("bulk_apply_threshold", base.bulk_apply_threshold),
        overflow_strategy=d.get("overflow_strategy", base.overflow_strategy),
    )


def template_to_dict(t: FleetTemplate) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "doctrine_id": t.doctrine_id,
        "wings": [_wing_to_dict(w) for w in t.wings],
        "rules": [_rule_to_dict(r) for r in t.rules],
        "settings": _settings_to_dict(t.settings),
    }


def template_from_dict(d: dict) -> FleetTemplate:
    return FleetTemplate(
        id=d.get("id") or uuid4().hex,
        name=d.get("name", "Untitled"),
        doctrine_id=d.get("doctrine_id"),
        wings=[_wing_from_dict(w) for w in d.get("wings", [])],
        rules=[_rule_from_dict(r) for r in d.get("rules", [])],
        settings=_settings_from_dict(d.get("settings", {})),
    )


# append to fleet_template_store.py
import json


class FleetTemplateStore:
    """Owns fleet_templates.json: a list of FleetTemplate plus a character cache."""

    def __init__(self, path: str):
        self.path = path
        self.templates: list[FleetTemplate] = []
        self.cached_characters: list[str] = []

    # ── persistence ──────────────────────────────────────────────────────────
    def load(self) -> None:
        """Load from disk. Missing or corrupt file → empty store (never raises)."""
        self.templates = []
        self.cached_characters = []
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            log.exception("[fleet-templates] could not read %s; starting empty",
                          self.path)
            return
        for raw in data.get("templates", []):
            try:
                t = template_from_dict(raw)
                validate_template(t)
                self.templates.append(t)
            except Exception:
                log.exception("[fleet-templates] skipping malformed template in %s",
                              self.path)
        self.cached_characters = [c for c in data.get("cached_characters", [])
                                  if isinstance(c, str) and c.strip()]

    def save(self) -> None:
        """Atomically persist the store. Raises on serialization/IO error."""
        data = {
            "version": SCHEMA_VERSION,
            "templates": [template_to_dict(t) for t in self.templates],
            "cached_characters": self.cached_characters,
        }
        atomic_write_json(self.path, data)

    # ── template CRUD ────────────────────────────────────────────────────────
    def get_template(self, template_id: str) -> FleetTemplate | None:
        return next((t for t in self.templates if t.id == template_id), None)

    def add_template(self, name: str, doctrine_id: str | None = None) -> FleetTemplate:
        t = FleetTemplate(id=uuid4().hex, name=name or "Untitled",
                          doctrine_id=doctrine_id)
        self.templates.append(t)
        return t

    def rename_template(self, template_id: str, new_name: str) -> None:
        t = self.get_template(template_id)
        if t is not None and new_name.strip():
            t.name = new_name.strip()

    def delete_template(self, template_id: str) -> None:
        self.templates = [t for t in self.templates if t.id != template_id]

    def cache_character(self, name: str) -> bool:
        """Add a free-typed character name to the roster cache.

        Trims whitespace; de-dupes case-insensitively, preserving the first-seen
        casing. Returns True if a new name was added, False otherwise."""
        cleaned = (name or "").strip()
        if not cleaned:
            return False
        lowered = cleaned.lower()
        if any(c.lower() == lowered for c in self.cached_characters):
            return False
        self.cached_characters.append(cleaned)
        return True


# fleet_template_store.py — replace the placeholder validate_template
def validate_template(template: FleetTemplate) -> None:
    """Mark rules whose wing/squad reference no longer exists with broken=True.

    Rule semantics:
      - wing_name None & squad_name None  → "anywhere", never broken.
      - wing_name set, squad_name None    → broken iff that wing is missing.
      - wing_name set, squad_name set     → broken iff that (wing, squad) is missing.
      - wing_name None, squad_name set    → broken (a squad ref needs a wing).
    Mutates each rule's `broken` flag in place.
    """
    wing_names = {w.name for w in template.wings}
    squad_pairs = {(w.name, s.name) for w in template.wings for s in w.squads}
    for rule in template.rules:
        wn = rule.action.wing_name
        sn = rule.action.squad_name
        if wn is None and sn is None:
            rule.broken = False
        elif wn is None and sn is not None:
            rule.broken = True
        elif sn is None:
            rule.broken = wn not in wing_names
        else:
            rule.broken = (wn, sn) not in squad_pairs
