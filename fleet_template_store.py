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

SCHEMA_VERSION = 2


# ── Dataclasses ──────────────────────────────────────────────────────────────
@dataclass
class Slot:
    character: str | None       # named slot (exact, case-insensitive match); None = generic
    tag: str | None             # DEPRECATED in v2 (migrated to rules); kept for round-trip
    role: str                   # ESI role string
    character_id: int | None = None   # resolved id for a named slot (v2)


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
    # "doctrine_tag" | "ship_type" | "ship_class" | "character"
    #   | "capital" | "subcap" | "default"  (last three: value ignored, stored "")
    type: str
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
    # v2 pacing (used by the Phase-B executor/auto-sort loop):
    sync_active_s: int = 10
    sync_idle_s: int = 30
    move_spacing_ms: int = 400
    burst_cap: int = 25
    settle_s: int = 3
    bulk_apply_threshold: int = 5


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
    return {"character": s.character, "tag": s.tag, "role": s.role,
            "character_id": s.character_id}


def _slot_from_dict(d: dict) -> Slot:
    cid = d.get("character_id")
    return Slot(character=d.get("character"), tag=d.get("tag"),
                role=d.get("role", "squad_member"),
                character_id=cid if isinstance(cid, int) else None)


def _squad_to_dict(sq: Squad) -> dict:
    return {"name": sq.name, "max_size": sq.max_size,
            "slots": [_slot_to_dict(s) for s in sq.slots]}


def _squad_from_dict(d: dict) -> Squad:
    return Squad(name=d.get("name", ""), max_size=d.get("max_size"),
                 slots=[_slot_from_dict(s) for s in d.get("slots", [])])


def _wing_to_dict(w: Wing) -> dict:
    return {"name": w.name, "squads": [_squad_to_dict(s) for s in w.squads]}


def _wing_from_dict(d: dict) -> Wing:
    return Wing(name=d.get("name", ""), max_size=None,
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
    return {"sync_active_s": s.sync_active_s, "sync_idle_s": s.sync_idle_s,
            "move_spacing_ms": s.move_spacing_ms, "burst_cap": s.burst_cap,
            "settle_s": s.settle_s, "bulk_apply_threshold": s.bulk_apply_threshold}


def _settings_from_dict(d: dict) -> RebalanceSettings:
    base = RebalanceSettings()
    return RebalanceSettings(
        sync_active_s=d.get("sync_active_s", base.sync_active_s),
        sync_idle_s=d.get("sync_idle_s", base.sync_idle_s),
        move_spacing_ms=d.get("move_spacing_ms", base.move_spacing_ms),
        burst_cap=d.get("burst_cap", base.burst_cap),
        settle_s=d.get("settle_s", base.settle_s),
        bulk_apply_threshold=d.get("bulk_apply_threshold", base.bulk_apply_threshold),
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


def _migrate_template_v1(t: FleetTemplate) -> None:
    """In-place v1→v2 template migration.

    Each distinct doctrine tag T among a squad's slots becomes one routing rule
    `doctrine_tag=T → (that wing, that squad, role of the first slot bearing T)`,
    appended after existing rules with sequential priorities. Every slot's `tag`
    is then cleared (slots become generic). Named slots are untouched.
    """
    next_priority = (max((r.priority for r in t.rules), default=-1) + 1)
    for w in t.wings:
        for sq in w.squads:
            seen: dict[str, str] = {}   # tag -> role of its first slot (first-seen order)
            for slot in sq.slots:
                tag = (slot.tag or "").strip()
                if tag and tag not in seen:
                    seen[tag] = slot.role or "squad_member"
            for tag, role in seen.items():
                t.rules.append(AssignmentRule(
                    priority=next_priority,
                    condition=RuleCondition("doctrine_tag", tag),
                    action=RuleAction(role, w.name, sq.name)))
                next_priority += 1
            for slot in sq.slots:
                slot.tag = None


# append to fleet_template_store.py
import json


class FleetTemplateStore:
    """Owns fleet_templates.json: a list of FleetTemplate plus a character cache."""

    def __init__(self, path: str):
        self.path = path
        self.templates: list[FleetTemplate] = []
        # v2: list of {"name": str, "character_id": int | None}
        self.cached_characters: list[dict] = []
        # v2-additive UI state (window geometry etc.); optional, defaults {}.
        self.ui: dict = {}

    # ── persistence ──────────────────────────────────────────────────────────
    def load(self) -> None:
        """Load from disk. Missing/corrupt → empty store (never raises).

        Reads `version`: v1 (or missing) is migrated in memory (written back on
        the next natural save); a future version (>SCHEMA_VERSION) is refused —
        the store opens empty and the file is left untouched so nothing is
        corrupted."""
        self.templates = []
        self.cached_characters = []
        self.ui = {}
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            log.exception("[fleet-templates] could not read %s; starting empty",
                          self.path)
            return
        version = data.get("version", 1)
        if not isinstance(version, int):
            version = 1
        if version > SCHEMA_VERSION:
            log.warning("[fleet-templates] file version %s is newer than supported "
                        "%s; opening empty and leaving the file untouched.",
                        version, SCHEMA_VERSION)
            return
        for raw in data.get("templates", []):
            try:
                t = template_from_dict(raw)
                if version < 2:
                    _migrate_template_v1(t)
                validate_template(t)
                self.templates.append(t)
            except Exception:
                log.exception("[fleet-templates] skipping malformed template in %s",
                              self.path)
        self.cached_characters = self._normalize_cached(data.get("cached_characters", []))
        raw_ui = data.get("ui")
        self.ui = dict(raw_ui) if isinstance(raw_ui, dict) else {}

    @staticmethod
    def _normalize_cached(raw) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for entry in raw or []:
            if isinstance(entry, str):
                name, cid = entry.strip(), None
            elif isinstance(entry, dict):
                name = (entry.get("name") or "").strip()
                cid = entry.get("character_id")
                cid = cid if isinstance(cid, int) else None
            else:
                continue
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append({"name": name, "character_id": cid})
        return out

    def save(self) -> None:
        """Atomically persist the store. Raises on serialization/IO error."""
        data = {
            "version": SCHEMA_VERSION,
            "templates": [template_to_dict(t) for t in self.templates],
            "cached_characters": self.cached_characters,
            "ui": self.ui,
        }
        atomic_write_json(self.path, data)

    # ── template CRUD ────────────────────────────────────────────────────────
    def get_template(self, template_id: str) -> FleetTemplate | None:
        return next((t for t in self.templates if t.id == template_id), None)

    def add_template(self, name: str, doctrine_id: str | None = None) -> FleetTemplate:
        t = FleetTemplate(id=uuid4().hex, name=name or "Untitled",
                          doctrine_id=doctrine_id,
                          wings=[Wing(name="Wing 1", max_size=None,
                                      squads=[Squad(name="Squad 1", max_size=None,
                                                    slots=[])])])
        self.templates.append(t)
        return t

    def duplicate_template(self, template_id: str, *,
                           new_id: str | None = None) -> FleetTemplate | None:
        """Deep-copy an existing template (new id, ' (copy)' name suffix) and
        append it. Returns the copy, or None if the source id is unknown."""
        src = self.get_template(template_id)
        if src is None:
            return None
        copy = template_from_dict(template_to_dict(src))
        copy.id = new_id or uuid4().hex
        copy.name = f"{src.name} (copy)"
        self.templates.append(copy)
        return copy

    def rename_template(self, template_id: str, new_name: str) -> None:
        t = self.get_template(template_id)
        if t is not None and new_name.strip():
            t.name = new_name.strip()

    def delete_template(self, template_id: str) -> None:
        self.templates = [t for t in self.templates if t.id != template_id]

    def cache_character(self, name: str, character_id: int | None = None) -> bool:
        """Upsert a character into the roster cache.

        De-dupes case-insensitively on name, preserving first-seen casing. If the
        name already exists and a non-None `character_id` is supplied, the id is
        filled in (upgrading a previously-unresolved entry). Returns True only
        when a NEW name row was added."""
        cleaned = (name or "").strip()
        if not cleaned:
            return False
        lowered = cleaned.lower()
        for row in self.cached_characters:
            if row["name"].lower() == lowered:
                if character_id is not None and row.get("character_id") is None:
                    row["character_id"] = character_id
                return False
        self.cached_characters.append({"name": cleaned, "character_id": character_id})
        return True

    def cached_id(self, name: str) -> int | None:
        """Resolved character_id for a cached name (case-insensitive), or None."""
        lowered = (name or "").strip().lower()
        for row in self.cached_characters:
            if row["name"].lower() == lowered:
                return row.get("character_id")
        return None

    def cached_character_names(self) -> list[str]:
        """Flat list of cached names (for autocomplete providers)."""
        return [row["name"] for row in self.cached_characters]


# fleet_template_store.py — replace the placeholder validate_template
def validate_template(template: FleetTemplate) -> None:
    """Mark rules whose wing/squad reference no longer exists with broken=True,
    and mark every `default` rule after the first as broken (at most one default
    per template; it evaluates last regardless of priority — see the composer).

    Rule semantics:
      - wing_name None & squad_name None  → "anywhere", never broken (ref-wise).
      - wing_name set, squad_name None    → broken iff that wing is missing.
      - wing_name set, squad_name set     → broken iff that (wing, squad) is missing.
      - wing_name None, squad_name set    → broken (a squad ref needs a wing).
    Mutates each rule's `broken` flag in place.
    """
    wing_names = {w.name for w in template.wings}
    squad_pairs = {(w.name, s.name) for w in template.wings for s in w.squads}
    seen_default = False
    for rule in template.rules:
        wn = rule.action.wing_name
        sn = rule.action.squad_name
        if wn is None and sn is None:
            broken = False
        elif wn is None and sn is not None:
            broken = True
        elif sn is None:
            broken = wn not in wing_names
        else:
            broken = (wn, sn) not in squad_pairs
        if rule.condition.type == "default":
            if seen_default:
                broken = True
            seen_default = True
        rule.broken = broken


def build_template_from_live(live_members, live_structure, *, now,
                             new_id=None) -> FleetTemplate:
    """Build a NEW template from a live fleet snapshot (spec ask 4).

    Wings/squads are recreated by their (unclamped display) name in order.
    Every member sitting in a known squad becomes a named slot
    (character=name, character_id=id, role=member role) — squad commanders keep
    their squad_commander role. Members with no known squad (FC, wing
    commanders in EVE's boss/wing slot, just-joined pilots in the no-squad slot)
    do NOT produce squad slots. The caller pins these slots after import.

    `now` is a datetime (injected/frozen by tests); the template is named
    'Import YYYY-MM-DD HH:MM'. `new_id` overrides the generated uuid (tests).
    Pure: no Tk, no ESI, no network.
    """
    name = f"Import {now.strftime('%Y-%m-%d %H:%M')}"
    t = FleetTemplate(id=new_id or uuid4().hex, name=name, doctrine_id=None)

    # squad_id -> list of members (in roster order) that sit in a known squad.
    valid_squads = {s["id"] for w in live_structure.get("wings", [])
                    for s in w.get("squads", [])}
    by_squad: dict = {}
    for m in live_members:
        sid = m.get("squad_id")
        if sid in valid_squads:
            by_squad.setdefault(sid, []).append(m)

    for w in live_structure.get("wings", []):
        wing = Wing(name=w.get("name", ""), max_size=None, squads=[])
        for s in w.get("squads", []):
            squad = Squad(name=s.get("name", ""), max_size=None, slots=[])
            for m in by_squad.get(s.get("id"), []):
                squad.slots.append(Slot(
                    character=m.get("name") or None,
                    tag=None,
                    role=m.get("role") or "squad_member",
                    character_id=(m.get("character_id")
                                 if isinstance(m.get("character_id"), int)
                                 else None)))
            wing.squads.append(squad)
        t.wings.append(wing)
    return t
