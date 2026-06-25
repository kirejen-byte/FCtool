# Fleet Templates & Live Fleet Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Fleet Templates window to FCTool that lets an FC build reusable wing/squad/slot structures offline, apply them to a live fleet with one click via ESI, auto-assign pilots by rules (ship type/class/character/doctrine tag), support multiboxing with named + generic slots, and enforce optional size caps with a pausable, ESI-frugal background rebalancer.

**Architecture:** Four new files. Three are pure (no Tk, no network) and fully unit-tested: `fleet_template_store.py` (data model + JSON persistence), `fleet_composer.py` (pilot→slot matching + rebalance planning), `fleet_esi.py` (ESI fleet-structure writes behind a thin session adapter, tested with a fake session). One is the Tkinter view `fleet_template_window.py` that wires them together. `fc_gui.py` gains only a button plus a handful of provider callables.

**Tech Stack:** Python 3.13, Tkinter/ttk, `requests` (via existing `ESIAuth._session`), the existing `rate_limiter.rate_limit("esi")`, `app_io.atomic_write_json`, and the existing `FittingsStore` / `Doctrine` / `Fit` models.

**Source of truth:** `docs/superpowers/specs/2026-06-25-fleet-templates-design.md`. Read it before starting; every task below maps to a section of that spec.

---

## Key facts established from the codebase (do not re-derive)

- **ESI scope already granted.** `esi_auth.SCOPES` already contains `esi-fleets.read_fleet.v1` and `esi-fleets.write_fleet.v1` (esi_auth.py:40-41). Moving members and creating/renaming/deleting wings & squads all use `write_fleet`. **No new scope is needed.** Older tokens minted before fleet scopes were added won't carry them — gate on `auth.has_scope("esi-fleets.write_fleet.v1")` (esi_auth.py:538) and prompt re-auth, exactly as the fittings flow does (fc_gui.py:4383).
- **Authenticated requests.** `ESIAuth` exposes `.access_token` (property, auto-refreshes), `.character_id`, `.character_name`, `.is_authenticated`, `.has_scope(...)`, and a live `requests.Session` at `._session`. The codebase already reaches `acct._session.post(...)` directly (fc_gui.py:5466), so using `._session` in an adapter is an established pattern.
- **Reading fleet structure & members.** `auth.get_fleet_info()` → `{"fleet_id","fleet_boss_id","role"}`; `auth.is_boss(info, character_id)`; `auth.get_fleet_members(fleet_id)` → list of `{character_id, ship_type_id, role, squad_id, wing_id, join_time, ...}` (no names). Wings come from `GET /fleets/{id}/wings/` (not yet wrapped — `fleet_esi.get_wings` adds it).
- **Name resolution.** `from zkill_monitor import resolve_name`; `resolve_name(char_id, "character")` and `resolve_name(type_id, "type")`. The window resolves ids→names *before* calling the pure composer, so the composer stays resolver-free.
- **Ship class label.** `ship_classes.get_group_id(type_id)` → group_id; map to a human label for `ship_class` rule conditions (see the Task D5 helper `ship_class_label`, resolved off the Tk thread).
- **Doctrine + tags.** `self.fittings` is a `FittingsStore` (built at fc_gui.py:580). `fittings.list_doctrines()`, `fittings.get_doctrine(id)`, `fittings.get_fit(fit_id)`, `fittings.tags` (list[str]). A `Doctrine` has `.members: list[DoctrineMember]`; a `DoctrineMember` has `.fit_id` and `.tags: list[str]`; a `Fit` has `.hull_type_id`. The active doctrine id/name lives in `config["fleet"]["active_doctrine"]` (fc_gui.py:1538).
- **Persistence helpers.** `from app_io import atomic_write_json`; `from app_path import app_dir`. The fittings library lives at `os.path.join(app_dir(), "fittings_library.json")`; the new store lives beside it at `fleet_templates.json`.
- **ESI wing/squad name limit is 10 characters.** ESI rejects longer names. `fleet_esi` clamps every wing/squad name to `name[:10]`.
- **ESI role strings** (identical in template and ESI): `fleet_commander`, `wing_commander`, `squad_commander`, `squad_member`. For `move_member`: `fleet_commander` sends no wing/squad; `wing_commander` sends `wing_id` only; `squad_commander`/`squad_member` send both `wing_id` and `squad_id`.

---

## File structure

| File | Responsibility | Tk? | Net? |
|---|---|---|---|
| `fleet_template_store.py` (new) | Dataclasses (`FleetTemplate`/`Wing`/`Squad`/`Slot`/`RuleCondition`/`RuleAction`/`AssignmentRule`/`RebalanceSettings`), JSON round-trip, template CRUD, rule validation, character cache | No | No |
| `fleet_composer.py` (new) | `compose()` (named→rule→generic→unassigned matching + diff/skip), `plan_rebalance()`, `summarize_moves()`, tag index | No | No |
| `fleet_esi.py` (new) | `FleetESIError`, `_call` retry logic, `create_wing/create_squad/get_wings/move_member/rename_wing/rename_squad/delete_wing/delete_squad`, `AuthEsiSession` adapter | No | Yes |
| `fleet_template_window.py` (new) | `FleetTemplateWindow` Toplevel: tree, Rules/Settings tabs, mode toggle, apply flow, rebalancer loop, drag-drop, context menus | Yes | via providers |
| `fc_gui.py` (modify) | "Fleet Templates" button in Fleet Management header; build provider callables; open the window | Yes | — |
| `tests/test_fleet_template_store.py` (new) | Round-trip, broken-rule detection, cache dedup | — | — |
| `tests/test_fleet_composer.py` (new) | All §12 matching cases + rebalance planning | — | — |
| `tests/test_fleet_esi.py` (new) | Retry/raise behaviour with a fake session | — | — |

---

# Phase A — `fleet_template_store.py` (pure data model + persistence)

### Task A1: Dataclasses + JSON (de)serialization

**Files:**
- Create: `fleet_template_store.py`
- Test: `tests/test_fleet_template_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_template_store.py
from fleet_template_store import (
    FleetTemplate, Wing, Squad, Slot, RuleCondition, RuleAction,
    AssignmentRule, RebalanceSettings, template_to_dict, template_from_dict,
)


def _sample_template():
    return FleetTemplate(
        id="t1",
        name="Standard Armor Fleet",
        doctrine_id="d1",
        wings=[Wing(name="Alpha Wing", max_size=None, squads=[
            Squad(name="Logi Squad", max_size=10, slots=[
                Slot(character="Kyra Dawnfall", tag=None, role="squad_commander"),
                Slot(character=None, tag="Logistics", role="squad_member"),
                Slot(character=None, tag=None, role="squad_member"),
            ]),
        ])],
        rules=[
            AssignmentRule(priority=0,
                           condition=RuleCondition("doctrine_tag", "Links"),
                           action=RuleAction("squad_commander", "Alpha Wing", "Logi Squad")),
            AssignmentRule(priority=1,
                           condition=RuleCondition("ship_type", "Damnation"),
                           action=RuleAction("squad_commander", "Alpha Wing", None)),
        ],
        settings=RebalanceSettings(),
    )


def test_template_round_trips_through_dict():
    t = _sample_template()
    again = template_from_dict(template_to_dict(t))
    assert again == t


def test_defaults_on_rebalance_settings():
    s = RebalanceSettings()
    assert s.rebalance_interval_s == 60
    assert s.move_cooldown_s == 45
    assert s.bulk_apply_threshold == 5
    assert s.overflow_strategy == "least_populated"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_template_store.py::test_template_round_trips_through_dict -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fleet_template_store'`.

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_template_store.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_template_store.py tests/test_fleet_template_store.py
git commit -m "feat(fleet-templates): data model + JSON round-trip for fleet_template_store"
```

---

### Task A2: Store load/save + template CRUD

**Files:**
- Modify: `fleet_template_store.py`
- Test: `tests/test_fleet_template_store.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fleet_template_store.py
from fleet_template_store import FleetTemplateStore


def test_add_get_rename_delete_and_persist(tmp_path):
    path = str(tmp_path / "fleet_templates.json")
    store = FleetTemplateStore(path)
    store.load()                       # missing file → empty, no error
    t = store.add_template("My Fleet")
    assert store.get_template(t.id) is t
    store.rename_template(t.id, "Renamed Fleet")
    store.save()

    reloaded = FleetTemplateStore(path)
    reloaded.load()
    assert len(reloaded.templates) == 1
    assert reloaded.templates[0].name == "Renamed Fleet"

    reloaded.delete_template(t.id)
    reloaded.save()
    assert reloaded.get_template(t.id) is None

    fresh = FleetTemplateStore(path)
    fresh.load()
    assert fresh.templates == []


def test_load_corrupt_file_is_empty_not_crash(tmp_path):
    path = tmp_path / "fleet_templates.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = FleetTemplateStore(str(path))
    store.load()
    assert store.templates == []
    assert store.cached_characters == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_template_store.py::test_add_get_rename_delete_and_persist -v`
Expected: FAIL with `ImportError: cannot import name 'FleetTemplateStore'`.

- [ ] **Step 3: Write minimal implementation**

```python
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
            t = template_from_dict(raw)
            validate_template(t)
            self.templates.append(t)
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
```

Add a temporary stub so the load() call resolves; the real one lands in Task A3:

```python
# append to fleet_template_store.py (replaced with full logic in Task A3)
def validate_template(template: FleetTemplate) -> None:
    """Placeholder — full implementation in Task A3."""
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_template_store.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_template_store.py tests/test_fleet_template_store.py
git commit -m "feat(fleet-templates): store load/save + template CRUD"
```

---

### Task A3: Rule validation (broken-ref detection)

**Files:**
- Modify: `fleet_template_store.py` (replace the `validate_template` stub)
- Test: `tests/test_fleet_template_store.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fleet_template_store.py
from fleet_template_store import validate_template


def test_validate_flags_broken_wing_and_squad_refs():
    t = FleetTemplate(
        id="t", name="n", doctrine_id=None,
        wings=[Wing("Alpha Wing", None, [Squad("Logi Squad", None, [])])],
        rules=[
            AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                           RuleAction("squad_commander", "Alpha Wing", "Logi Squad")),
            AssignmentRule(1, RuleCondition("ship_type", "Guardian"),
                           RuleAction("squad_member", "Ghost Wing", None)),   # wing gone
            AssignmentRule(2, RuleCondition("ship_type", "Scimitar"),
                           RuleAction("squad_member", "Alpha Wing", "Ghost Squad")),  # squad gone
            AssignmentRule(3, RuleCondition("ship_type", "Eos"),
                           RuleAction("squad_member", None, None)),           # anywhere = OK
            AssignmentRule(4, RuleCondition("ship_type", "Sleipnir"),
                           RuleAction("squad_member", None, "Logi Squad")),   # squad w/o wing = broken
        ],
    )
    validate_template(t)
    assert [r.broken for r in t.rules] == [False, True, True, False, True]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_template_store.py::test_validate_flags_broken_wing_and_squad_refs -v`
Expected: FAIL (the stub leaves every `broken` False, so the list is all-False).

- [ ] **Step 3: Write the implementation** (replace the stub)

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_template_store.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_template_store.py tests/test_fleet_template_store.py
git commit -m "feat(fleet-templates): validate_template flags broken wing/squad rule refs"
```

---

### Task A4: Character cache (case-insensitive dedup)

**Files:**
- Modify: `fleet_template_store.py`
- Test: `tests/test_fleet_template_store.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fleet_template_store.py
def test_cache_character_dedups_case_insensitively(tmp_path):
    store = FleetTemplateStore(str(tmp_path / "f.json"))
    assert store.cache_character("Kyra Dawnfall") is True
    assert store.cache_character("  kyra dawnfall  ") is False   # dup (ci, trimmed)
    assert store.cache_character("Alt Pilot") is True
    assert store.cache_character("") is False
    assert store.cache_character("   ") is False
    assert store.cached_characters == ["Kyra Dawnfall", "Alt Pilot"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_template_store.py::test_cache_character_dedups_case_insensitively -v`
Expected: FAIL with `AttributeError: 'FleetTemplateStore' object has no attribute 'cache_character'`.

- [ ] **Step 3: Write the implementation**

```python
# fleet_template_store.py — add method to FleetTemplateStore
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_template_store.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_template_store.py tests/test_fleet_template_store.py
git commit -m "feat(fleet-templates): cached_characters dedup"
```

---

# Phase B — `fleet_composer.py` (pure matching + rebalance planning)

The composer takes a template, the **enriched** live member list (the window resolves ids→names first), and the live fleet structure, and returns a `ComposeResult`. It also plans a single rebalance move. No Tk, no ESI.

**Enriched member dict shape** the composer consumes (built by the window in Task D5):
```python
{"character_id": int, "name": str, "ship_type_id": int|None,
 "ship_type_name": str, "ship_class": str|None,   # ship_class pre-resolved off the Tk thread
 "wing_id": int|None, "squad_id": int|None,
 "role": str, "join_time": str}   # join_time is the ISO8601 string ESI returns
```

> **Why `ship_class` is pre-resolved:** mapping a hull to its class label needs `ship_classes.get_group_id`, which makes a blocking ESI call on cache-miss. `compose()` must stay network-free and is called on the Tk main thread, so the window resolves the class label inside `_enrich_members` (which runs on the background sync worker, Task D5) and the composer only reads the cached `ship_class` field.

**Live structure shape:**
```python
{"wings": [{"id": int, "name": str, "squads": [{"id": int, "name": str}, ...]}, ...]}
```

### Task B1: Result dataclasses + doctrine tag index

**Files:**
- Create: `fleet_composer.py`
- Test: `tests/test_fleet_composer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_composer.py
from fleet_composer import Move, ComposeResult, build_tag_index


class _FakeFit:
    def __init__(self, fit_id, hull_type_id):
        self.id = fit_id
        self.hull_type_id = hull_type_id


class _FakeMember:
    def __init__(self, fit_id, tags):
        self.fit_id = fit_id
        self.tags = tags


class _FakeDoctrine:
    def __init__(self, members):
        self.members = members


class _FakeFittings:
    def __init__(self, fits):
        self._fits = {f.id: f for f in fits}

    def get_fit(self, fit_id):
        return self._fits.get(fit_id)


def test_build_tag_index_unions_tags_per_hull():
    fits = [_FakeFit("f-dam", 22474), _FakeFit("f-guard", 11987)]
    doctrine = _FakeDoctrine([
        _FakeMember("f-dam", ["Links"]),
        _FakeMember("f-dam", ["DPS"]),       # same hull, second member → union
        _FakeMember("f-guard", ["Logistics"]),
    ])
    idx = build_tag_index(doctrine, _FakeFittings(fits))
    assert idx[22474] == {"Links", "DPS"}
    assert idx[11987] == {"Logistics"}


def test_build_tag_index_empty_without_doctrine():
    assert build_tag_index(None, _FakeFittings([])) == {}


def test_move_defaults_to_executable():
    m = Move(pilot_id=1, pilot_name="X", target_wing_name="W",
             target_squad_name="S", target_role="squad_member")
    assert m.skip_reason is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_composer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fleet_composer'`.

- [ ] **Step 3: Write minimal implementation**

```python
# fleet_composer.py
"""Fleet composition matching — pure logic, no Tk, no ESI, no network.

`compose(template, live_members, live_structure, ...)` assigns live pilots to
the template's slots in precedence order — named slots, then rule-driven role
slots, then generic slots, then Unassigned — diffs each assignment against the
pilot's current ESI placement, and returns a `ComposeResult` whose executable
moves (`skip_reason is None`) are exactly the ESI writes the apply step issues.

`plan_rebalance(...)` returns a single overflow move for the size-cap rebalancer
(at most one per tick, by design).

All targets are expressed by wing/squad NAME; the caller resolves names to live
ESI ids (creating wings/squads as needed) at apply time.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# compose() synthesizes an implicit doctrine_tag rule per tagged slot, so it
# needs the store's rule dataclasses.
from fleet_template_store import AssignmentRule, RuleCondition, RuleAction

# Lowest-priority sentinel so an implicit tag-rule never outranks a user rule.
_IMPLICIT_PRIORITY = 1_000_000


@dataclass
class Move:
    pilot_id: int
    pilot_name: str
    target_wing_name: str | None
    target_squad_name: str | None
    target_role: str
    skip_reason: str | None = None   # None => executable ESI write; else informational


@dataclass
class ComposeResult:
    moves: list[Move] = field(default_factory=list)        # all (incl. already-correct)
    unassigned: list[dict] = field(default_factory=list)   # enriched member dicts
    warnings: list[str] = field(default_factory=list)

    @property
    def executable(self) -> list[Move]:
        return [m for m in self.moves if m.skip_reason is None]


def build_tag_index(doctrine, fittings) -> dict[int, set[str]]:
    """ship_type_id → union of doctrine tags whose fit uses that hull.

    Empty dict when no doctrine/fittings (so doctrine_tag rules never fire —
    the "no doctrine active" inactive state)."""
    index: dict[int, set[str]] = {}
    if not doctrine or not fittings:
        return index
    for member in getattr(doctrine, "members", []):
        fit = fittings.get_fit(member.fit_id)
        if fit is None:
            continue
        index.setdefault(fit.hull_type_id, set()).update(member.tags)
    return index
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_composer.py -v`
Expected: PASS (three tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_composer.py tests/test_fleet_composer.py
git commit -m "feat(fleet-templates): composer result types + doctrine tag index"
```

---

### Task B2: `compose()` — named slots, rules, generic fill, diff, warnings

This is the core. It is built in one task because the passes are interdependent, but the test suite covers each pass and every §7/§12 edge case separately.

**Files:**
- Modify: `fleet_composer.py`
- Test: `tests/test_fleet_composer.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_fleet_composer.py
from fleet_composer import compose
from fleet_template_store import (
    FleetTemplate, Wing, Squad, Slot, RuleCondition, RuleAction,
    AssignmentRule, RebalanceSettings,
)


def _member(cid, name, tname, role="squad_member", wing_id=None, squad_id=None,
            join="2026-01-01T00:00:00Z", ship_type_id=0):
    return {"character_id": cid, "name": name, "ship_type_id": ship_type_id,
            "ship_type_name": tname, "role": role, "wing_id": wing_id,
            "squad_id": squad_id, "join_time": join}


# Empty live structure (no wings exist yet) for tests that only check targeting.
_EMPTY_STRUCT = {"wings": []}


def _template(wings, rules=None):
    return FleetTemplate(id="t", name="n", doctrine_id=None, wings=wings,
                         rules=rules or [], settings=RebalanceSettings())


def test_named_slot_exact_match_case_insensitive():
    t = _template([Wing("Alpha Wing", None, [Squad("Logi Squad", None, [
        Slot(character="Kyra Dawnfall", tag=None, role="squad_commander"),
    ])])])
    members = [_member(1, "kyra dawnfall", "Archon")]
    res = compose(t, members, _EMPTY_STRUCT)
    assert len(res.executable) == 1
    mv = res.executable[0]
    assert (mv.pilot_id, mv.target_wing_name, mv.target_squad_name, mv.target_role) \
        == (1, "Alpha Wing", "Logi Squad", "squad_commander")
    assert res.unassigned == []


def test_named_slot_missing_pilot_leaves_slot_empty_no_move():
    t = _template([Wing("Alpha Wing", None, [Squad("Logi Squad", None, [
        Slot(character="Absent Pilot", tag=None, role="squad_commander"),
    ])])])
    res = compose(t, [_member(1, "Someone Else", "Rifter")], _EMPTY_STRUCT)
    # The named slot generates no move; the unmatched pilot is Unassigned.
    assert res.executable == []
    assert [m["character_id"] for m in res.unassigned] == [1]


def test_rule_priority_first_matching_rule_wins():
    t = _template(
        [Wing("Alpha Wing", None, [Squad("Cmd Squad", None, [
            Slot(character=None, tag="Links", role="squad_commander"),
        ])])],
        rules=[
            AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                           RuleAction("squad_commander", "Alpha Wing", "Cmd Squad")),
            AssignmentRule(1, RuleCondition("ship_type", "Damnation"),
                           RuleAction("squad_member", "Alpha Wing", "Cmd Squad")),
        ],
    )
    res = compose(t, [_member(1, "Boss", "Damnation")], _EMPTY_STRUCT)
    assert res.executable[0].target_role == "squad_commander"   # priority-0 rule won


def test_six_damnations_five_sc_slots_warns_about_one_unplaced():
    squads = [Squad(f"S{i}", None, [
        Slot(character=None, tag="Links", role="squad_commander"),
    ]) for i in range(5)]
    # one generic slot for the overflow pilot to land in as a member
    squads.append(Squad("Spare", None, [Slot(character=None, tag=None, role="squad_member")]))
    t = _template([Wing("Alpha Wing", None, squads)],
                  rules=[AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                                        RuleAction("squad_commander", None, None))])
    members = [_member(i, f"Pilot{i}", "Damnation", join=f"2026-01-0{i+1}T00:00:00Z")
               for i in range(6)]
    res = compose(t, members, _EMPTY_STRUCT)
    sc = [m for m in res.executable if m.target_role == "squad_commander"]
    assert len(sc) == 5
    assert any("Damnation" in w and "unplaced" in w for w in res.warnings)


def test_already_correct_position_is_skipped():
    struct = {"wings": [{"id": 100, "name": "Alpha Wing",
                         "squads": [{"id": 200, "name": "Logi Squad"}]}]}
    t = _template([Wing("Alpha Wing", None, [Squad("Logi Squad", None, [
        Slot(character="Kyra", tag=None, role="squad_member"),
    ])])])
    members = [_member(1, "Kyra", "Guardian", role="squad_member",
                       wing_id=100, squad_id=200)]
    res = compose(t, members, struct)
    assert res.executable == []                       # no ESI write needed
    assert any(m.skip_reason == "already_correct" for m in res.moves)


def test_doctrine_tag_rule_inactive_without_doctrine():
    t = _template([Wing("Alpha Wing", None, [Squad("Cmd", None, [
        Slot(character=None, tag=None, role="squad_commander"),
    ])])],
        rules=[AssignmentRule(0, RuleCondition("doctrine_tag", "Links"),
                              RuleAction("squad_commander", "Alpha Wing", "Cmd"))])
    res = compose(t, [_member(1, "P", "Damnation")], _EMPTY_STRUCT)  # no doctrine
    # The doctrine_tag rule cannot fire → the SC slot is generic-filled instead.
    assert res.executable[0].target_role == "squad_commander"
    assert res.executable[0].pilot_id == 1


def test_broken_rule_is_skipped_and_does_not_crash():
    t = _template([Wing("Alpha Wing", None, [Squad("Cmd", None, [
        Slot(character=None, tag=None, role="squad_member"),
    ])])],
        rules=[AssignmentRule(0, RuleCondition("ship_type", "Damnation"),
                              RuleAction("squad_commander", "Ghost", None), broken=True)])
    res = compose(t, [_member(1, "P", "Damnation")], _EMPTY_STRUCT)
    # Broken rule ignored; pilot still generic-fills the member slot.
    assert res.executable[0].pilot_id == 1


def test_generic_slots_fill_in_tree_order_by_join_time():
    t = _template([Wing("W", None, [Squad("S", None, [
        Slot(character=None, tag=None, role="squad_member"),
        Slot(character=None, tag=None, role="squad_member"),
    ])])])
    members = [_member(2, "Late", "Rifter", join="2026-02-01T00:00:00Z"),
               _member(1, "Early", "Rifter", join="2026-01-01T00:00:00Z")]
    res = compose(t, members, _EMPTY_STRUCT)
    # Longest-serving (Early, join Jan) fills the first slot.
    assert [m.pilot_id for m in res.executable] == [1, 2]


def test_pilot_with_no_slot_or_rule_match_is_unassigned():
    t = _template([Wing("W", None, [Squad("S", None, [
        Slot(character="OnlyThisGuy", tag=None, role="squad_member"),
    ])])])
    res = compose(t, [_member(9, "Nobody", "Rifter")], _EMPTY_STRUCT)
    assert res.executable == []
    assert [m["character_id"] for m in res.unassigned] == [9]


def test_ship_class_condition_uses_pre_resolved_ship_class():
    t = _template([Wing("W", None, [Squad("S", None, [
        Slot(character=None, tag="Logistics", role="squad_member"),
    ])])],
        rules=[AssignmentRule(0, RuleCondition("ship_class", "Logistics Cruiser"),
                              RuleAction("squad_member", "W", "S"))])
    m = _member(1, "Logi Guy", "Guardian")
    m["ship_class"] = "Logistics Cruiser"      # pre-resolved by the window
    res = compose(t, [m], _EMPTY_STRUCT)
    assert res.executable[0].pilot_id == 1


def test_rule_slot_with_no_match_emits_unfilled_warning():
    # A tagged role slot, no rules and no doctrine → nothing can fill it (§7).
    t = _template([Wing("W", None, [Squad("S", None, [
        Slot(character=None, tag="Logistics", role="squad_member"),
    ])])])
    res = compose(t, [_member(1, "DPS Guy", "Megathron")], _EMPTY_STRUCT)
    assert res.executable == []                       # tag slot stays empty, no ESI call
    assert any("unfilled" in w for w in res.warnings)
    assert [m["character_id"] for m in res.unassigned] == [1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fleet_composer.py -k "named or rule or damnations or already or doctrine or broken or generic or unassigned or ship_class or unfilled" -v`
Expected: FAIL with `ImportError: cannot import name 'compose'` (plus the helper imports).

- [ ] **Step 3: Write the implementation**

```python
# append to fleet_composer.py
def _pilot_matches(condition, member, tag_index) -> bool:
    t, v = condition.type, condition.value
    if t == "character":
        return (member.get("name") or "").lower() == v.lower()
    if t == "ship_type":
        return (member.get("ship_type_name") or "").lower() == v.lower()
    if t == "ship_class":
        # Pre-resolved off the Tk thread in _enrich_members → no network here.
        return (member.get("ship_class") or "").lower() == v.lower()
    if t == "doctrine_tag":
        return v in tag_index.get(member.get("ship_type_id"), set())
    return False


def _current_placement(member, id_to_names):
    """(wing_name, squad_name, role) for a member's current ESI position, or
    (None, None, role) if its wing/squad id isn't in the known structure."""
    key = (member.get("wing_id"), member.get("squad_id"))
    wname, sname = id_to_names.get(key, (None, None))
    return wname, sname, member.get("role")


def compose(template, live_members, live_structure, *, doctrine=None,
            fittings=None) -> ComposeResult:
    """Assign pilots to template slots and diff against current placement.

    See module docstring for the member/structure dict shapes. Ship-class rule
    conditions read each member's pre-resolved `ship_class` field (resolved off
    the Tk thread in the window's _enrich_members), so compose stays
    network-free and safe to call on the UI thread.
    """
    result = ComposeResult()
    tag_index = build_tag_index(doctrine, fittings)

    # id → names for the diff step.
    id_to_names: dict[tuple, tuple] = {}
    for w in live_structure.get("wings", []):
        for s in w.get("squads", []):
            id_to_names[(w["id"], s["id"])] = (w["name"], s["name"])

    # Pool ordered by join_time ascending (longest-serving first). ESI join_time
    # is an ISO8601 string; lexical sort matches chronological order.
    pool = sorted(live_members, key=lambda m: m.get("join_time") or "")
    claimed: set = set()
    assignment: dict = {}   # character_id → (wing_name, squad_name, role)

    # Flatten slots in tree order.
    flat = [(w.name, s.name, slot)
            for w in template.wings for s in w.squads for slot in s.slots]

    by_name: dict[str, list] = {}
    for m in pool:
        by_name.setdefault((m.get("name") or "").lower(), []).append(m)

    # Pass 1 — named slots.
    for wname, sname, slot in flat:
        if not slot.character:
            continue
        cand = next((c for c in by_name.get(slot.character.lower(), [])
                     if c["character_id"] not in claimed), None)
        if cand is not None:
            assignment[cand["character_id"]] = (wname, sname, slot.role)
            claimed.add(cand["character_id"])

    # Pass 2 — rule-driven role slots (slot.tag set, slot.character None).
    user_rules = sorted((r for r in template.rules if not r.broken),
                        key=lambda r: r.priority)
    for wname, sname, slot in flat:
        if slot.character or slot.tag is None:
            continue
        candidate_rules = [
            r for r in user_rules
            if r.action.role == slot.role
            and r.action.wing_name in (None, wname)
            and r.action.squad_name in (None, sname)
        ]
        # Implicit lowest-priority rule from the slot's own doctrine tag.
        candidate_rules.append(AssignmentRule(
            _IMPLICIT_PRIORITY,
            RuleCondition("doctrine_tag", slot.tag),
            RuleAction(slot.role, wname, sname)))
        for rule in candidate_rules:
            pilot = next((m for m in pool if m["character_id"] not in claimed
                          and _pilot_matches(rule.condition, m, tag_index)), None)
            if pilot is not None:
                assignment[pilot["character_id"]] = (wname, sname, slot.role)
                claimed.add(pilot["character_id"])
                break
        else:
            result.warnings.append(
                f"1 slot unfilled (no match): {wname}/{sname} [{slot.tag}]")

    # Pass 2b — warn about pilots a user rule matched but had no open slot for.
    for rule in user_rules:
        leftover = [m for m in pool if m["character_id"] not in claimed
                    and _pilot_matches(rule.condition, m, tag_index)]
        if leftover:
            result.warnings.append(
                f"{len(leftover)} {rule.condition.value} unplaced by "
                f"{rule.action.role} rule (no open slot).")

    # Pass 3 — generic slots (character None, tag None), tree order.
    for wname, sname, slot in flat:
        if slot.character or slot.tag is not None:
            continue
        pilot = next((m for m in pool if m["character_id"] not in claimed), None)
        if pilot is not None:
            assignment[pilot["character_id"]] = (wname, sname, slot.role)
            claimed.add(pilot["character_id"])

    # Build moves (diff each assignment vs current placement).
    member_by_id = {m["character_id"]: m for m in pool}
    for cid, (wname, sname, role) in assignment.items():
        m = member_by_id[cid]
        cur = _current_placement(m, id_to_names)
        skip = "already_correct" if cur == (wname, sname, role) else None
        result.moves.append(Move(
            pilot_id=cid, pilot_name=m.get("name", ""),
            target_wing_name=wname, target_squad_name=sname,
            target_role=role, skip_reason=skip))

    # Pass 5 — leftover pool → Unassigned.
    result.unassigned = [m for m in pool if m["character_id"] not in claimed]
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fleet_composer.py -v`
Expected: PASS (all compose tests + the Task B1 tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_composer.py tests/test_fleet_composer.py
git commit -m "feat(fleet-templates): compose() matching + diff + warnings"
```

---

### Task B3: `summarize_moves()` for the confirm dialog

**Files:**
- Modify: `fleet_composer.py`
- Test: `tests/test_fleet_composer.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fleet_composer.py
from fleet_composer import summarize_moves


def test_summarize_counts_repositions_role_changes_and_unfilled():
    res = ComposeResult(
        moves=[
            Move(1, "A", "W", "S1", "squad_member"),                 # reposition
            Move(2, "B", "W", "S1", "squad_commander"),              # reposition
            Move(3, "C", "W", "S1", "squad_member", skip_reason="already_correct"),
        ],
        unassigned=[{"character_id": 9}],
        warnings=["1 slot unfilled (no match): W/S2 [Logistics]"],
    )
    s = summarize_moves(res)
    assert s["executable"] == 2
    assert s["unfilled"] == 1
    assert s["unassigned"] == 1
    assert s["esi_calls"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_composer.py::test_summarize_counts_repositions_role_changes_and_unfilled -v`
Expected: FAIL with `ImportError: cannot import name 'summarize_moves'`.

- [ ] **Step 3: Write the implementation**

```python
# append to fleet_composer.py
def summarize_moves(result: ComposeResult) -> dict:
    """Counts for the apply confirm dialog. `esi_calls` is the move count;
    the apply layer adds wing/squad creates on top, so this is a lower bound."""
    executable = len(result.executable)
    unfilled = sum(1 for w in result.warnings if "unfilled" in w)
    return {
        "executable": executable,
        "unfilled": unfilled,
        "unassigned": len(result.unassigned),
        "esi_calls": executable,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_composer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fleet_composer.py tests/test_fleet_composer.py
git commit -m "feat(fleet-templates): summarize_moves for confirm dialog"
```

---

### Task B4: `plan_rebalance()` — one overflow move per tick

**Files:**
- Modify: `fleet_composer.py`
- Test: `tests/test_fleet_composer.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fleet_composer.py
from fleet_composer import plan_rebalance, RebalanceAction


def _struct(wings):
    return {"wings": wings}


def test_rebalance_moves_last_joined_overflow_to_undercap_squad_same_wing():
    # Wing W: S1 cap 2 has 3 members; S2 cap 5 has 0 → move newest from S1 to S2.
    struct = _struct([{"id": 1, "name": "W", "squads": [
        {"id": 10, "name": "S1"}, {"id": 11, "name": "S2"}]}])
    members = [
        {"character_id": 1, "name": "A", "wing_id": 1, "squad_id": 10,
         "join_time": "2026-01-01T00:00:00Z"},
        {"character_id": 2, "name": "B", "wing_id": 1, "squad_id": 10,
         "join_time": "2026-01-02T00:00:00Z"},
        {"character_id": 3, "name": "C", "wing_id": 1, "squad_id": 10,
         "join_time": "2026-01-03T00:00:00Z"},   # newest → overflow
    ]
    max_sizes = {("W", "S1"): 2, ("W", "S2"): 5}
    act = plan_rebalance(members, struct, max_sizes=max_sizes)
    assert isinstance(act, RebalanceAction)
    assert act.pilot_id == 3
    assert act.target_wing_name == "W"
    assert act.target_squad_name == "S2"
    assert act.create_squad is False


def test_rebalance_returns_none_when_all_within_cap():
    struct = _struct([{"id": 1, "name": "W", "squads": [{"id": 10, "name": "S1"}]}])
    members = [{"character_id": 1, "name": "A", "wing_id": 1, "squad_id": 10,
                "join_time": "2026-01-01T00:00:00Z"}]
    assert plan_rebalance(members, struct, max_sizes={("W", "S1"): 5}) is None


def test_rebalance_signals_create_when_no_undercap_target_exists():
    struct = _struct([{"id": 1, "name": "W", "squads": [{"id": 10, "name": "S1"}]}])
    members = [{"character_id": i, "name": str(i), "wing_id": 1, "squad_id": 10,
                "join_time": f"2026-01-0{i}T00:00:00Z"} for i in (1, 2, 3)]
    act = plan_rebalance(members, struct, max_sizes={("W", "S1"): 2})
    assert act.pilot_id == 3
    assert act.target_wing_name == "W"
    assert act.create_squad is True
    assert act.target_squad_name is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_composer.py -k rebalance -v`
Expected: FAIL with `ImportError: cannot import name 'plan_rebalance'`.

- [ ] **Step 3: Write the implementation**

```python
# append to fleet_composer.py
@dataclass
class RebalanceAction:
    pilot_id: int
    pilot_name: str
    source_wing_name: str
    target_wing_name: str
    target_squad_name: str | None   # None + create_squad=True → make a new squad here
    create_squad: bool = False


def plan_rebalance(live_members, live_structure, *, max_sizes) -> "RebalanceAction | None":
    """Return at most ONE overflow move, or None if every squad is within cap.

    Order of preference for the target (spec §9): an under-cap squad in the SAME
    wing → the least-populated squad across all wings → signal create_squad in
    the same wing. The overflow pilot is the last-joined in the over-cap squad.
    `max_sizes`: {(wing_name, squad_name): cap or None}. None means uncapped.

    NOTE: `RebalanceSettings.overflow_strategy` is reserved for v1. The only
    documented/default value is "least_populated", which is exactly this
    target-selection order, so the field is persisted (round-tripped) but not
    branched on yet and is intentionally not exposed in the Settings tab. A
    future strategy (e.g. "fill_first") would add a branch here.
    """
    # Index structure.
    wings = live_structure.get("wings", [])
    sid_to_names: dict[int, tuple] = {}
    wname_by_id: dict[int, str] = {}
    squads_by_wing: dict[str, list] = {}
    for w in wings:
        wname_by_id[w["id"]] = w["name"]
        squads_by_wing[w["name"]] = []
        for s in w.get("squads", []):
            sid_to_names[s["id"]] = (w["name"], s["name"])
            squads_by_wing[w["name"]].append(s["name"])

    # Population per (wing_name, squad_name).
    pop: dict[tuple, list] = {}
    for m in live_members:
        names = sid_to_names.get(m.get("squad_id"))
        if names:
            pop.setdefault(names, []).append(m)

    # First over-cap squad in tree order.
    for w in wings:
        for s in w.get("squads", []):
            key = (w["name"], s["name"])
            cap = max_sizes.get(key)
            members = pop.get(key, [])
            if cap is None or len(members) <= cap:
                continue
            overflow = max(members, key=lambda m: m.get("join_time") or "")

            # (i) under-cap squad in the same wing
            same_wing = [(w["name"], sn) for sn in squads_by_wing[w["name"]]
                         if sn != s["name"]]
            under = [k for k in same_wing
                     if max_sizes.get(k) is None or len(pop.get(k, [])) < max_sizes[k]]
            if under:
                target = min(under, key=lambda k: len(pop.get(k, [])))
                return RebalanceAction(overflow["character_id"],
                                       overflow.get("name", ""), w["name"],
                                       target[0], target[1], create_squad=False)

            # (ii) least-populated under-cap squad across all wings
            all_other = [k for k in pop.keys() | set(max_sizes.keys())
                         if k != key]
            under_any = [k for k in all_other
                         if max_sizes.get(k) is None or len(pop.get(k, [])) < max_sizes[k]]
            if under_any:
                target = min(under_any, key=lambda k: len(pop.get(k, [])))
                return RebalanceAction(overflow["character_id"],
                                       overflow.get("name", ""), w["name"],
                                       target[0], target[1], create_squad=False)

            # (iii) no under-cap squad anywhere → create one in the same wing
            return RebalanceAction(overflow["character_id"], overflow.get("name", ""),
                                   w["name"], w["name"], None, create_squad=True)

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_composer.py -v`
Expected: PASS (all composer tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_composer.py tests/test_fleet_composer.py
git commit -m "feat(fleet-templates): plan_rebalance one-move-per-tick overflow planner"
```

---

# Phase C — `fleet_esi.py` (ESI fleet-structure writes)

A thin layer over an injectable `session` so it is fully unit-testable with a fake. `session.request(method, path, json=None)` returns a response with `.status_code`, `.ok`, `.json()`, `.text`. The production `AuthEsiSession` wraps an `ESIAuth`.

### Task C1: `FleetESIError` + `_call` retry/raise logic

**Files:**
- Create: `fleet_esi.py`
- Test: `tests/test_fleet_esi.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_esi.py
import pytest
from fleet_esi import FleetESIError, _call


class FakeResp:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class FakeSession:
    """Returns/raises queued items in order on each .request() call."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_call_returns_response_on_2xx():
    sess = FakeSession([FakeResp(204)])
    resp = _call(sess, "PUT", "/x/", json={"a": 1}, expect=(204,))
    assert resp.status_code == 204
    assert sess.calls == [("PUT", "/x/", {"a": 1})]


def test_call_retries_once_on_5xx_then_succeeds():
    sess = FakeSession([FakeResp(500), FakeResp(201, {"wing_id": 7})])
    resp = _call(sess, "POST", "/w/", expect=(201,))
    assert resp.json()["wing_id"] == 7
    assert len(sess.calls) == 2


def test_call_raises_boss_lost_on_403():
    sess = FakeSession([FakeResp(403)])
    with pytest.raises(FleetESIError) as ei:
        _call(sess, "PUT", "/x/", expect=(204,))
    assert ei.value.reason == "boss_lost"


def test_call_raises_not_found_on_404():
    sess = FakeSession([FakeResp(404)])
    with pytest.raises(FleetESIError) as ei:
        _call(sess, "PUT", "/x/", expect=(204,))
    assert ei.value.reason == "not_found"


def test_call_raises_after_second_5xx_failure():
    sess = FakeSession([FakeResp(502), FakeResp(503)])
    with pytest.raises(FleetESIError) as ei:
        _call(sess, "POST", "/w/", expect=(201,))
    assert ei.value.reason == "http_error"
    assert ei.value.status == 503


def test_call_retries_once_on_network_exception():
    sess = FakeSession([RuntimeError("conn reset"), FakeResp(204)])
    resp = _call(sess, "PUT", "/x/", expect=(204,))
    assert resp.status_code == 204
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_esi.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fleet_esi'`.

- [ ] **Step 3: Write minimal implementation**

```python
# fleet_esi.py
"""ESI fleet-structure writes (move members, create/rename/delete wings & squads).

Every call goes through an injectable `session` whose `.request(method, path,
json=None)` returns a requests-style response (`.status_code`, `.ok`, `.json()`,
`.text`). `AuthEsiSession` adapts an `ESIAuth` for production; tests pass a fake.

Error policy (spec §3): retry ONCE on a 5xx or a network exception; raise
`FleetESIError("boss_lost")` on 403, `FleetESIError("not_found")` on 404, and
`FleetESIError("http_error", status=...)` on any other non-expected status or a
second failure. The caller (`fleet_template_window`) handles `FleetESIError`.

ESI wing/squad names are capped at 10 characters; every name is clamped.
"""
from __future__ import annotations

_NAME_MAX = 10


class FleetESIError(Exception):
    def __init__(self, reason: str, status: int | None = None, detail: str = ""):
        super().__init__(f"{reason} (status={status}) {detail}".strip())
        self.reason = reason      # "boss_lost" | "not_found" | "http_error" | "no_token" | "network"
        self.status = status
        self.detail = detail


def _call(session, method: str, path: str, *, json=None, expect=(200, 201, 204)):
    """Single ESI call with retry-once-on-5xx/network. Returns the response."""
    last_exc = None
    for attempt in (1, 2):
        try:
            resp = session.request(method, path, json=json)
        except FleetESIError:
            raise
        except Exception as e:   # network/transport error
            last_exc = e
            if attempt == 2:
                raise FleetESIError("network", detail=str(e))
            continue
        if resp.status_code in expect:
            return resp
        if resp.status_code == 403:
            raise FleetESIError("boss_lost", status=403)
        if resp.status_code == 404:
            raise FleetESIError("not_found", status=404)
        if 500 <= resp.status_code < 600 and attempt == 1:
            continue   # retry once on 5xx
        raise FleetESIError("http_error", status=resp.status_code,
                            detail=getattr(resp, "text", ""))
    # Unreachable: loop either returns or raises.
    raise FleetESIError("network", detail=str(last_exc))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_esi.py -v`
Expected: PASS (six tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_esi.py tests/test_fleet_esi.py
git commit -m "feat(fleet-templates): fleet_esi error policy + _call retry logic"
```

---

### Task C2: Structure write wrappers (create/get/move/rename/delete)

**Files:**
- Modify: `fleet_esi.py`
- Test: `tests/test_fleet_esi.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fleet_esi.py
import fleet_esi


def test_create_wing_returns_id_and_renames_when_named():
    sess = FakeSession([FakeResp(201, {"wing_id": 5}), FakeResp(204)])
    wid = fleet_esi.create_wing(sess, 999, "Alpha Wing")
    assert wid == 5
    # second call renames the new wing, name clamped to 10 chars
    assert sess.calls[1] == ("PUT", "/fleets/999/wings/5/", {"name": "Alpha Wing"})


def test_create_wing_skips_rename_for_blank_name():
    sess = FakeSession([FakeResp(201, {"wing_id": 5})])
    wid = fleet_esi.create_wing(sess, 999, "")
    assert wid == 5
    assert len(sess.calls) == 1


def test_create_squad_clamps_long_name_to_ten_chars():
    sess = FakeSession([FakeResp(201, {"squad_id": 8}), FakeResp(204)])
    sid = fleet_esi.create_squad(sess, 999, 5, "Logistics Wing Squad")
    assert sid == 8
    assert sess.calls[1] == ("PUT", "/fleets/999/squads/8/", {"name": "Logistics "})


def test_move_member_squad_member_sends_wing_and_squad():
    sess = FakeSession([FakeResp(204)])
    fleet_esi.move_member(sess, 999, 42, wing_id=5, squad_id=8, role="squad_member")
    assert sess.calls[0] == ("PUT", "/fleets/999/members/42/",
                             {"role": "squad_member", "wing_id": 5, "squad_id": 8})


def test_move_member_fleet_commander_sends_role_only():
    sess = FakeSession([FakeResp(204)])
    fleet_esi.move_member(sess, 999, 42, wing_id=None, squad_id=None,
                          role="fleet_commander")
    assert sess.calls[0] == ("PUT", "/fleets/999/members/42/",
                             {"role": "fleet_commander"})


def test_move_member_wing_commander_sends_wing_only():
    sess = FakeSession([FakeResp(204)])
    fleet_esi.move_member(sess, 999, 42, wing_id=5, squad_id=None,
                          role="wing_commander")
    assert sess.calls[0] == ("PUT", "/fleets/999/members/42/",
                             {"role": "wing_commander", "wing_id": 5})


def test_get_wings_returns_parsed_list():
    payload = [{"id": 1, "name": "W", "squads": [{"id": 2, "name": "S"}]}]
    sess = FakeSession([FakeResp(200, payload)])
    assert fleet_esi.get_wings(sess, 999) == payload


def test_delete_wing_and_squad():
    sess = FakeSession([FakeResp(204), FakeResp(204)])
    fleet_esi.delete_wing(sess, 999, 5)
    fleet_esi.delete_squad(sess, 999, 8)
    assert sess.calls == [("DELETE", "/fleets/999/wings/5/", None),
                          ("DELETE", "/fleets/999/squads/8/", None)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_esi.py -k "create or move or get_wings or delete" -v`
Expected: FAIL with `AttributeError: module 'fleet_esi' has no attribute 'create_wing'`.

- [ ] **Step 3: Write the implementation**

```python
# append to fleet_esi.py
def get_wings(session, fleet_id: int) -> list:
    """GET /fleets/{id}/wings/ → list of {id, name, squads:[{id, name}]}."""
    resp = _call(session, "GET", f"/fleets/{fleet_id}/wings/", expect=(200,))
    data = resp.json()
    return data if isinstance(data, list) else []


def create_wing(session, fleet_id: int, name: str | None = None) -> int:
    """POST a new wing; rename it if `name` is given. Returns the new wing_id."""
    resp = _call(session, "POST", f"/fleets/{fleet_id}/wings/", expect=(201, 200))
    wing_id = resp.json().get("wing_id")
    if name and name.strip():
        rename_wing(session, fleet_id, wing_id, name)
    return wing_id


def create_squad(session, fleet_id: int, wing_id: int, name: str | None = None) -> int:
    """POST a new squad into a wing; rename it if `name` is given. Returns squad_id."""
    resp = _call(session, "POST", f"/fleets/{fleet_id}/wings/{wing_id}/squads/",
                 expect=(201, 200))
    squad_id = resp.json().get("squad_id")
    if name and name.strip():
        rename_squad(session, fleet_id, squad_id, name)
    return squad_id


def rename_wing(session, fleet_id: int, wing_id: int, name: str) -> None:
    _call(session, "PUT", f"/fleets/{fleet_id}/wings/{wing_id}/",
          json={"name": name[:_NAME_MAX]}, expect=(204,))


def rename_squad(session, fleet_id: int, squad_id: int, name: str) -> None:
    _call(session, "PUT", f"/fleets/{fleet_id}/squads/{squad_id}/",
          json={"name": name[:_NAME_MAX]}, expect=(204,))


def delete_wing(session, fleet_id: int, wing_id: int) -> None:
    _call(session, "DELETE", f"/fleets/{fleet_id}/wings/{wing_id}/", expect=(204,))


def delete_squad(session, fleet_id: int, squad_id: int) -> None:
    _call(session, "DELETE", f"/fleets/{fleet_id}/squads/{squad_id}/", expect=(204,))


def move_member(session, fleet_id: int, member_id: int, *, wing_id, squad_id,
                role: str) -> None:
    """PUT /fleets/{id}/members/{member_id}/ — move a pilot to a role/position.

    Body shape by role: fleet_commander → role only; wing_commander → +wing_id;
    squad_commander / squad_member → +wing_id +squad_id."""
    body: dict = {"role": role}
    if wing_id is not None:
        body["wing_id"] = wing_id
    if squad_id is not None:
        body["squad_id"] = squad_id
    _call(session, "PUT", f"/fleets/{fleet_id}/members/{member_id}/",
          json=body, expect=(204,))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_esi.py -v`
Expected: PASS (all fleet_esi tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_esi.py tests/test_fleet_esi.py
git commit -m "feat(fleet-templates): fleet_esi structure write wrappers"
```

---

### Task C3: `AuthEsiSession` production adapter

**Files:**
- Modify: `fleet_esi.py`
- Test: `tests/test_fleet_esi.py`

- [ ] **Step 1: Write the failing test** (no network — a fake auth)

```python
# append to tests/test_fleet_esi.py
class _FakeRequestsSession:
    def __init__(self):
        self.last = None

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.last = {"method": method, "url": url, "headers": headers,
                     "json": json, "timeout": timeout}
        return FakeResp(204)


class _FakeAuth:
    def __init__(self, token="tok"):
        self.access_token = token
        self._session = _FakeRequestsSession()


def test_auth_session_builds_authorized_request(monkeypatch):
    # Don't actually sleep in the rate limiter during tests.
    monkeypatch.setattr("rate_limiter.rate_limit", lambda *a, **k: None)
    auth = _FakeAuth()
    sess = fleet_esi.AuthEsiSession(auth)
    resp = sess.request("PUT", "/fleets/1/members/2/", json={"role": "squad_member"})
    assert resp.status_code == 204
    call = auth._session.last
    assert call["method"] == "PUT"
    assert call["url"].endswith("/fleets/1/members/2/")
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["json"] == {"role": "squad_member"}


def test_auth_session_raises_no_token_when_unauthenticated(monkeypatch):
    monkeypatch.setattr("rate_limiter.rate_limit", lambda *a, **k: None)
    auth = _FakeAuth(token=None)
    sess = fleet_esi.AuthEsiSession(auth)
    with pytest.raises(FleetESIError) as ei:
        sess.request("GET", "/fleets/1/wings/")
    assert ei.value.reason == "no_token"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_esi.py -k auth_session -v`
Expected: FAIL with `AttributeError: module 'fleet_esi' has no attribute 'AuthEsiSession'`.

- [ ] **Step 3: Write the implementation**

```python
# append to fleet_esi.py
class AuthEsiSession:
    """Adapts an ESIAuth into the `session` protocol `_call` expects.

    Each request applies the ESI rate limiter, attaches the bearer token, and
    calls through the auth's live requests.Session. Raises FleetESIError
    ("no_token") if the auth has no valid token (e.g. not the fleet boss yet)."""

    def __init__(self, auth):
        self._auth = auth

    def request(self, method, path, json=None):
        from rate_limiter import rate_limit
        from esi_constants import ESI_BASE
        token = self._auth.access_token
        if not token:
            raise FleetESIError("no_token")
        rate_limit("esi")
        return self._auth._session.request(
            method, f"{ESI_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json, timeout=10)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_esi.py -v`
Expected: PASS (all fleet_esi tests).

- [ ] **Step 5: Commit**

```bash
git add fleet_esi.py tests/test_fleet_esi.py
git commit -m "feat(fleet-templates): AuthEsiSession production adapter"
```

---

# Phase D — `fleet_template_window.py` (Tkinter view)

The window wires the pure modules to widgets. Interaction-heavy parts (drag-drop, context menus) are verified by a headless smoke test (construct under a withdrawn root, drive the public methods) plus the manual checklist in Phase F. Pure decision helpers already live in the composer, so the window holds no untested logic.

> **Style:** match existing fc_gui conventions — `BG_DARK`, `BG_PANEL`, `BG_ENTRY`, `FG_TEXT`, `FG_DIM`, `FG_ACCENT`, `FG_GREEN`, `FG_YELLOW`, `FG_RED`, `BORDER_COLOR` constants (import them from `fc_gui` is circular — instead define a small local palette mirroring those hex values, see Step 3) and ttk styles `"Dark.TButton"` / `"Red.TButton"`. Use `("Consolas", N)` fonts.

### Task D1: Window scaffold, palette, mode toggle, template selector + headless smoke

**Files:**
- Create: `fleet_template_window.py`
- Test: `tests/test_fleet_template_window.py`

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_fleet_template_window.py
import os
import pytest

# Skip the whole module when there is no display (CI headless without Tk).
tk = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    r.withdraw()
    yield r
    r.destroy()


def _store(tmp_path):
    from fleet_template_store import FleetTemplateStore
    s = FleetTemplateStore(str(tmp_path / "fleet_templates.json"))
    s.load()
    s.add_template("Test Fleet")
    return s


class _FakeFittings:
    tags = ["DPS", "Links", "Logistics"]

    def list_doctrines(self):
        return []

    def get_doctrine(self, _id):
        return None

    def get_fit(self, _id):
        return None


def test_window_builds_and_defaults_to_template_mode(root, tmp_path):
    from fleet_template_window import FleetTemplateWindow
    win = FleetTemplateWindow(
        root,
        store=_store(tmp_path),
        fittings=_FakeFittings(),
        config={},
        esi_session_provider=lambda: None,
        fleet_info_provider=lambda: None,
        doctrine_provider=lambda: None,
        character_names_provider=lambda: ["Kyra Dawnfall"],
    )
    assert win.mode == "template"
    # Apply disabled in template mode, Rebalance disabled.
    assert str(win._apply_btn["state"]) == "disabled"
    win.destroy()


def test_mode_toggle_to_live_enables_apply(root, tmp_path):
    from fleet_template_window import FleetTemplateWindow
    win = FleetTemplateWindow(
        root, store=_store(tmp_path), fittings=_FakeFittings(), config={},
        esi_session_provider=lambda: None,
        fleet_info_provider=lambda: {"fleet_id": 1, "is_boss": True},
        doctrine_provider=lambda: None,
        character_names_provider=lambda: [],
    )
    win.set_mode("live")
    assert win.mode == "live"
    assert str(win._apply_btn["state"]) == "normal"
    win.destroy()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fleet_template_window'` (or skip if no display — run locally where Tk is available).

- [ ] **Step 3: Write the scaffold**

```python
# fleet_template_window.py
"""Fleet Templates window — Tkinter view over the pure fleet_* modules.

Owns a Toplevel with a Template/Live mode toggle, a wing/squad/slot tree, a
right-hand Members/Rules/Settings notebook, a hybrid apply flow, and a pausable
size-cap rebalancer. All matching/persistence/ESI logic lives in
fleet_template_store / fleet_composer / fleet_esi; this module is widgets + glue.

Constructed by fc_gui with provider callables so it never reaches back into the
main app's mutable state directly:
  esi_session_provider()    -> fleet_esi.AuthEsiSession | None (current boss)
  fleet_info_provider()     -> {"fleet_id": int, "is_boss": bool} | None
  doctrine_provider()       -> Doctrine | None (active doctrine)
  character_names_provider() -> list[str] (authed Characters-tab names)
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import fleet_composer
import fleet_esi
from fleet_template_store import (
    Wing, Squad, Slot, RuleCondition, RuleAction, AssignmentRule, validate_template,
)

# Palette mirroring fc_gui's (importing fc_gui here would be circular).
BG_DARK = "#1a1a1a"
BG_PANEL = "#252525"
BG_ENTRY = "#2d2d2d"
FG_TEXT = "#d0d0d0"
FG_DIM = "#808080"
FG_ACCENT = "#4ea1d3"
FG_GREEN = "#5fb85f"
FG_YELLOW = "#d6b656"
FG_RED = "#d35f5f"
BORDER_COLOR = "#3a3a3a"

ROLE_VALUES = ["squad_member", "squad_commander", "wing_commander", "fleet_commander"]
ROLE_ABBR = {"squad_member": "", "squad_commander": "SC",
             "wing_commander": "WC", "fleet_commander": "FC"}
CONDITION_TYPES = ["ship_type", "ship_class", "character", "doctrine_tag"]


class FleetTemplateWindow:
    def __init__(self, root, *, store, fittings, config, esi_session_provider,
                 fleet_info_provider, doctrine_provider, character_names_provider):
        self.store = store
        self.fittings = fittings
        self.config = config
        self._esi_session_provider = esi_session_provider
        self._fleet_info_provider = fleet_info_provider
        self._doctrine_provider = doctrine_provider
        self._character_names_provider = character_names_provider

        self.mode = "template"
        self._current_template_id = (store.templates[0].id if store.templates else None)
        self._rebalance_on = False
        self._rebalance_after_id = None
        self._sync_after_id = None
        self._last_write_monotonic = 0.0
        self._live_members: list[dict] = []      # enriched dicts (Task D5)
        self._live_structure: dict = {"wings": []}
        self._last_preview = None                 # cached ComposeResult (Tasks D5/D10)
        self._undo_stack: list[dict] = []         # template-mode undo (Task D9)

        self.win = tk.Toplevel(root)
        self.win.title("Fleet Templates")
        self.win.configure(bg=BG_DARK)
        self.win.geometry("980x640")
        self.win.protocol("WM_DELETE_WINDOW", self.destroy)

        self._build_header()
        self._build_body()
        self._build_footer()
        self._refresh_template_selector()
        self.set_mode("template")

    # ── construction ─────────────────────────────────────────────────────────
    def _build_header(self):
        bar = tk.Frame(self.win, bg=BG_PANEL)
        bar.pack(fill=tk.X, side=tk.TOP)
        tk.Label(bar, text="Fleet Templates", font=("Consolas", 12, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).pack(side=tk.LEFT, padx=10, pady=6)

        self._mode_btn = ttk.Button(bar, text="Mode: Template",
                                    style="Dark.TButton", command=self._toggle_mode)
        self._mode_btn.pack(side=tk.RIGHT, padx=10)

        sel = tk.Frame(self.win, bg=BG_DARK)
        sel.pack(fill=tk.X, side=tk.TOP)
        tk.Label(sel, text="Template:", font=("Consolas", 10), fg=FG_DIM,
                 bg=BG_DARK).pack(side=tk.LEFT, padx=(10, 4), pady=4)
        self._template_var = tk.StringVar()
        self._template_combo = ttk.Combobox(sel, textvariable=self._template_var,
                                            state="readonly", width=32)
        self._template_combo.pack(side=tk.LEFT, padx=2)
        self._template_combo.bind("<<ComboboxSelected>>", self._on_template_selected)
        ttk.Button(sel, text="New", style="Dark.TButton",
                   command=self._new_template).pack(side=tk.LEFT, padx=2)
        ttk.Button(sel, text="Rename", style="Dark.TButton",
                   command=self._rename_template).pack(side=tk.LEFT, padx=2)
        ttk.Button(sel, text="Delete", style="Red.TButton",
                   command=self._delete_template).pack(side=tk.LEFT, padx=2)

    def _build_body(self):
        body = tk.Frame(self.win, bg=BG_DARK)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        # Left: tree (filled in Task D2). Right: notebook (Tasks D3/D4).
        self._tree_frame = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.RIDGE)
        self._tree_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        self._panel = ttk.Notebook(body)
        self._panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        self._members_tab = tk.Frame(self._panel, bg=BG_PANEL)
        self._rules_tab = tk.Frame(self._panel, bg=BG_PANEL)
        self._settings_tab = tk.Frame(self._panel, bg=BG_PANEL)
        self._panel.add(self._members_tab, text="Members")
        self._panel.add(self._rules_tab, text="Rules")
        self._panel.add(self._settings_tab, text="Settings")
        self._build_tree()         # Task D2
        self._build_rules_tab()    # Task D3
        self._build_settings_tab() # Task D4

    def _build_footer(self):
        bar = tk.Frame(self.win, bg=BG_PANEL)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self._save_btn = ttk.Button(bar, text="Save Template",
                                    style="Dark.TButton", command=self._save)
        self._save_btn.pack(side=tk.LEFT, padx=8, pady=6)
        self._rebalance_btn = ttk.Button(bar, text="Rebalance: OFF",
                                         style="Dark.TButton",
                                         command=self._toggle_rebalance)
        self._rebalance_btn.pack(side=tk.LEFT, padx=8)
        self._status = tk.Label(bar, text="", font=("Consolas", 9),
                                fg=FG_DIM, bg=BG_PANEL)
        self._status.pack(side=tk.LEFT, padx=10)
        self._apply_btn = ttk.Button(bar, text="Apply Template",
                                     style="Dark.TButton", command=self._apply)
        self._apply_btn.pack(side=tk.RIGHT, padx=8)

    # ── template selector ────────────────────────────────────────────────────
    def current_template(self):
        return self.store.get_template(self._current_template_id)

    def _refresh_template_selector(self):
        names = [t.name for t in self.store.templates]
        self._template_combo["values"] = names
        t = self.current_template()
        if t is not None:
            self._template_var.set(t.name)

    def _on_template_selected(self, _evt=None):
        name = self._template_var.get()
        match = next((t for t in self.store.templates if t.name == name), None)
        if match is not None:
            self._current_template_id = match.id
            self._reload_tree()
            self._reload_rules()
            self._reload_settings()

    def _new_template(self):
        name = simpledialog.askstring("New Template", "Template name:",
                                      parent=self.win)
        if not name:
            return
        t = self.store.add_template(name)
        self._current_template_id = t.id
        self.store.save()
        self._refresh_template_selector()
        self._on_template_selected()

    def _rename_template(self):
        t = self.current_template()
        if t is None:
            return
        name = simpledialog.askstring("Rename Template", "New name:",
                                      initialvalue=t.name, parent=self.win)
        if name:
            self.store.rename_template(t.id, name)
            self.store.save()
            self._refresh_template_selector()

    def _delete_template(self):
        t = self.current_template()
        if t is None:
            return
        if not messagebox.askyesno("Delete Template",
                                   f"Delete '{t.name}'?", parent=self.win):
            return
        self.store.delete_template(t.id)
        self.store.save()
        self._current_template_id = (self.store.templates[0].id
                                     if self.store.templates else None)
        self._refresh_template_selector()
        self._on_template_selected()

    # ── mode toggle ──────────────────────────────────────────────────────────
    def _toggle_mode(self):
        self.set_mode("live" if self.mode == "template" else "template")

    def set_mode(self, mode: str):
        self.mode = mode
        self._mode_btn.config(text=f"Mode: {mode.capitalize()}")
        live = (mode == "live")
        self._apply_btn.config(state="normal" if live else "disabled")
        self._rebalance_btn.config(state="normal" if live else "disabled")
        self._save_btn.config(state="disabled" if live else "normal")
        if live:
            self._enter_live_mode()    # Task D5
        else:
            self._exit_live_mode()     # Task D5

    def _save(self):
        t = self.current_template()
        if t is not None:
            validate_template(t)
            self.store.save()
            self._status.config(text="Saved.", fg=FG_GREEN)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def destroy(self):
        for after_id in (self._rebalance_after_id, self._sync_after_id):
            if after_id:
                try:
                    self.win.after_cancel(after_id)
                except Exception:
                    pass
        try:
            self.win.destroy()
        except Exception:
            pass
```

Add the placeholder methods referenced above so the scaffold imports and the smoke test passes; each is fleshed out in its own task. **These are intentional stubs, replaced in the named tasks — not permanent placeholders.**

```python
# fleet_template_window.py — temporary stubs (each replaced in Tasks D2–D10;
# the drag handlers + _manual_assign are introduced as stubs in Task D2's
# append and fleshed out in Tasks D5/D8)
    def _build_tree(self): pass                 # Task D2
    def _reload_tree(self): pass                # Task D2
    def _build_rules_tab(self): pass            # Task D3
    def _reload_rules(self): pass               # Task D3
    def _build_settings_tab(self): pass         # Task D4
    def _reload_settings(self): pass            # Task D4
    def _enter_live_mode(self): pass            # Task D5
    def _exit_live_mode(self): pass             # Task D5
    def _apply(self): pass                       # Task D6
    def _toggle_rebalance(self): pass            # Task D7
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS (or SKIP if the machine has no display; run on the dev box where Tk works).

- [ ] **Step 5: Commit**

```bash
git add fleet_template_window.py tests/test_fleet_template_window.py
git commit -m "feat(fleet-templates): window scaffold + mode toggle + template selector"
```

---

### Task D2: Wing/squad/slot tree (render + context menu + keyboard)

**Files:**
- Modify: `fleet_template_window.py` (replace `_build_tree`/`_reload_tree` stubs)

- [ ] **Step 1: Replace the tree stubs with the implementation**

```python
# fleet_template_window.py — replace the _build_tree / _reload_tree stubs
    def _build_tree(self):
        wrap = tk.Frame(self._tree_frame, bg=BG_PANEL)
        wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._tree = ttk.Treeview(wrap, show="tree", selectmode="browse")
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        # node-id → ("wing"|"squad"|"slot"|"unassigned", path tuple)
        self._node_meta: dict[str, tuple] = {}
        self._tree.bind("<Button-3>", self._on_tree_right_click)
        self._tree.bind("<F2>", lambda e: self._rename_selected())
        self._tree.bind("<Delete>", lambda e: self._delete_selected())
        # Drag-drop bindings (Task D8).
        self._tree.bind("<ButtonPress-1>", self._on_drag_start)
        self._tree.bind("<B1-Motion>", self._on_drag_motion)
        self._tree.bind("<ButtonRelease-1>", self._on_drag_drop)
        self._drag_item = None

        add = tk.Frame(self._tree_frame, bg=BG_PANEL)
        add.pack(fill=tk.X)
        ttk.Button(add, text="+ Add Wing", style="Dark.TButton",
                   command=self._add_wing).pack(side=tk.LEFT, padx=4, pady=2)
        self._reload_tree()

    def _slot_label(self, slot: Slot) -> str:
        abbr = ROLE_ABBR.get(slot.role, "")
        suffix = f" [{abbr}]" if abbr else ""
        if slot.character:
            return f"● {slot.character}{suffix}"
        if slot.tag:
            return f"◈ {slot.tag}{suffix}"
        return f"○ (empty){suffix}"

    def _reload_tree(self):
        self._tree.delete(*self._tree.get_children())
        self._node_meta.clear()
        t = self.current_template()
        if t is None:
            return
        for wi, wing in enumerate(t.wings):
            cap = f"  (max {wing.max_size})" if wing.max_size else ""
            wid = self._tree.insert("", "end", text=f"▼ {wing.name}{cap}", open=True)
            self._node_meta[wid] = ("wing", (wi,))
            for si, squad in enumerate(wing.squads):
                scap = f"  (max {squad.max_size})" if squad.max_size else ""
                sid = self._tree.insert(wid, "end",
                                        text=f"▼ {squad.name}{scap}", open=True)
                self._node_meta[sid] = ("squad", (wi, si))
                for li, slot in enumerate(squad.slots):
                    nid = self._tree.insert(sid, "end", text=self._slot_label(slot))
                    self._node_meta[nid] = ("slot", (wi, si, li))
        if self.mode == "live":
            self._render_unassigned()   # Task D5

    # ── structural edits ─────────────────────────────────────────────────────
    def _add_wing(self):
        t = self.current_template()
        if t is None:
            return
        t.wings.append(Wing(name=f"Wing {len(t.wings) + 1}", max_size=None, squads=[]))
        self._after_structure_change()

    def _add_squad(self, wi):
        t = self.current_template()
        t.wings[wi].squads.append(
            Squad(name=f"Squad {len(t.wings[wi].squads) + 1}", max_size=None, slots=[]))
        self._after_structure_change()

    def _add_slot(self, wi, si):
        t = self.current_template()
        t.wings[wi].squads[si].slots.append(
            Slot(character=None, tag=None, role="squad_member"))
        self._after_structure_change()

    def _after_structure_change(self):
        t = self.current_template()
        if t is not None:
            validate_template(t)
        self._reload_tree()
        self._reload_rules()      # wing/squad dropdowns may have changed
        self.store.save()

    def _selected_meta(self):
        sel = self._tree.selection()
        if not sel:
            return None, None
        return sel[0], self._node_meta.get(sel[0])

    def _on_tree_right_click(self, event):
        item = self._tree.identify_row(event.y)
        if item:
            self._tree.selection_set(item)
        meta = self._node_meta.get(item)
        menu = tk.Menu(self.win, tearoff=0, bg=BG_PANEL, fg=FG_TEXT)
        if meta is None:
            menu.add_command(label="Add Wing", command=self._add_wing)
        else:
            kind, path = meta
            if kind == "wing":
                wi = path[0]
                menu.add_command(label="Rename", command=lambda: self._rename_selected())
                menu.add_command(label="Add Squad", command=lambda: self._add_squad(wi))
                menu.add_command(label="Set max size",
                                 command=lambda: self._set_max_size("wing", path))
                menu.add_separator()
                menu.add_command(label="Delete Wing",
                                 command=lambda: self._delete_selected())
            elif kind == "squad":
                wi, si = path
                menu.add_command(label="Rename", command=lambda: self._rename_selected())
                menu.add_command(label="Add Slot",
                                 command=lambda: self._add_slot(wi, si))
                menu.add_command(label="Set max size",
                                 command=lambda: self._set_max_size("squad", path))
                menu.add_separator()
                menu.add_command(label="Delete Squad",
                                 command=lambda: self._delete_selected())
            elif kind == "slot":
                menu.add_command(label="Edit slot…",
                                 command=lambda: self._edit_slot(path))
                menu.add_command(label="Delete Slot",
                                 command=lambda: self._delete_selected())
            elif kind == "unassigned" and self.mode == "live":
                menu.add_command(label="Move to squad…",
                                 command=lambda: self._manual_assign(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _rename_selected(self):
        item, meta = self._selected_meta()
        if not meta:
            return
        kind, path = meta
        t = self.current_template()
        if kind == "wing":
            obj = t.wings[path[0]]
        elif kind == "squad":
            obj = t.wings[path[0]].squads[path[1]]
        else:
            return
        new = simpledialog.askstring("Rename", "Name (max 10 chars on ESI):",
                                     initialvalue=obj.name, parent=self.win)
        if new:
            obj.name = new
            self._after_structure_change()

    def _set_max_size(self, kind, path):
        t = self.current_template()
        obj = t.wings[path[0]] if kind == "wing" else t.wings[path[0]].squads[path[1]]
        val = simpledialog.askinteger("Max size",
                                      "Max members (blank/0 = no cap):",
                                      initialvalue=obj.max_size or 0,
                                      minvalue=0, parent=self.win)
        obj.max_size = val if val else None
        self._after_structure_change()

    def _edit_slot(self, path):
        wi, si, li = path
        slot = self.current_template().wings[wi].squads[si].slots[li]
        SlotEditor(self.win, slot, self.fittings, self._character_names_provider(),
                   on_ok=lambda: self._after_structure_change())

    def _delete_selected(self):
        item, meta = self._selected_meta()
        if not meta:
            return
        kind, path = meta
        t = self.current_template()
        if kind == "wing":
            if t.wings[path[0]].squads and not messagebox.askyesno(
                    "Delete Wing", "Wing is not empty. Delete anyway?",
                    parent=self.win):
                return
            del t.wings[path[0]]
        elif kind == "squad":
            del t.wings[path[0]].squads[path[1]]
        elif kind == "slot":
            del t.wings[path[0]].squads[path[1]].slots[path[2]]
        else:
            return
        self._after_structure_change()
```

Add the `SlotEditor` dialog and drag-drop / manual-assign stubs (drag-drop fleshed out in Task D8):

```python
# fleet_template_window.py — append SlotEditor + interaction stubs
    def _on_drag_start(self, event): self._drag_item = self._tree.identify_row(event.y)
    def _on_drag_motion(self, event): pass            # Task D8
    def _on_drag_drop(self, event): self._drag_item = None   # Task D8
    def _manual_assign(self, path): pass              # Task D5


class SlotEditor:
    """Modal: edit a slot's type (named/role/generic), tag, and role."""
    def __init__(self, parent, slot, fittings, character_names, *, on_ok):
        self.slot = slot
        self.on_ok = on_ok
        self.win = tk.Toplevel(parent)
        self.win.title("Edit Slot")
        self.win.configure(bg=BG_PANEL)
        self.win.transient(parent)
        self.win.grab_set()

        tk.Label(self.win, text="Character (named slot):", bg=BG_PANEL,
                 fg=FG_TEXT, font=("Consolas", 9)).grid(row=0, column=0, sticky="w",
                                                        padx=6, pady=4)
        self._char = ttk.Combobox(self.win, values=sorted(character_names), width=28)
        self._char.set(slot.character or "")
        self._char.grid(row=0, column=1, padx=6, pady=4)

        tk.Label(self.win, text="Doctrine tag (role slot):", bg=BG_PANEL,
                 fg=FG_TEXT, font=("Consolas", 9)).grid(row=1, column=0, sticky="w",
                                                        padx=6, pady=4)
        self._tag = ttk.Combobox(self.win, values=[""] + list(getattr(fittings, "tags", [])),
                                 width=28, state="readonly")
        self._tag.set(slot.tag or "")
        self._tag.grid(row=1, column=1, padx=6, pady=4)

        tk.Label(self.win, text="Role:", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self._role = ttk.Combobox(self.win, values=ROLE_VALUES, width=28,
                                  state="readonly")
        self._role.set(slot.role)
        self._role.grid(row=2, column=1, padx=6, pady=4)

        btns = tk.Frame(self.win, bg=BG_PANEL)
        btns.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="OK", style="Dark.TButton",
                   command=self._ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=self.win.destroy).pack(side=tk.LEFT, padx=4)

    def _ok(self):
        char = self._char.get().strip()
        tag = self._tag.get().strip()
        # Named takes precedence; a named slot ignores tag. Generic = both blank.
        self.slot.character = char or None
        self.slot.tag = (tag or None) if not char else None
        self.slot.role = self._role.get() or "squad_member"
        self.win.destroy()
        self.on_ok()
```

- [ ] **Step 2: Re-run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS (tree builds; no regression).

- [ ] **Step 3: Manual sanity (dev box)**

Open the app, open Fleet Templates (after Task E1), add a wing → squad → slot, right-click each level, edit a slot. Confirm tree updates and `fleet_templates.json` is written.

- [ ] **Step 4: Commit**

```bash
git add fleet_template_window.py
git commit -m "feat(fleet-templates): wing/squad/slot tree + context menu + slot editor"
```

---

### Task D3: Rules tab (add/edit/reorder/delete + doctrine-tag greying)

**Files:**
- Modify: `fleet_template_window.py` (replace `_build_rules_tab`/`_reload_rules` stubs)

- [ ] **Step 1: Replace the rules stubs**

```python
# fleet_template_window.py — replace _build_rules_tab / _reload_rules
    def _build_rules_tab(self):
        top = tk.Frame(self._rules_tab, bg=BG_PANEL)
        top.pack(fill=tk.X)
        ttk.Button(top, text="+ Add Rule", style="Dark.TButton",
                   command=self._add_rule).pack(side=tk.LEFT, padx=6, pady=4)
        ttk.Button(top, text="Test Rules", style="Dark.TButton",
                   command=self._test_rules).pack(side=tk.LEFT, padx=2)
        self._rules_hint = tk.Label(top, text="", font=("Consolas", 8),
                                    fg=FG_YELLOW, bg=BG_PANEL)
        self._rules_hint.pack(side=tk.LEFT, padx=6)
        self._rules_list = tk.Frame(self._rules_tab, bg=BG_PANEL)
        self._rules_list.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._reload_rules()

    def _wing_names(self):
        t = self.current_template()
        return [""] + [w.name for w in (t.wings if t else [])]

    def _squad_names(self):
        t = self.current_template()
        names = [""]
        for w in (t.wings if t else []):
            names += [s.name for s in w.squads]
        return names

    def _reload_rules(self):
        for child in self._rules_list.winfo_children():
            child.destroy()
        t = self.current_template()
        if t is None:
            return
        doctrine_active = self._doctrine_provider() is not None
        self._rules_hint.config(
            text="" if doctrine_active else "No doctrine active — tag rules inactive")
        t.rules.sort(key=lambda r: r.priority)
        for idx, rule in enumerate(t.rules):
            self._render_rule_row(idx, rule, doctrine_active)

    def _render_rule_row(self, idx, rule, doctrine_active):
        row = tk.Frame(self._rules_list, bg=BG_PANEL)
        row.pack(fill=tk.X, pady=1)
        inactive = (rule.condition.type == "doctrine_tag" and not doctrine_active)
        fg = FG_DIM if (inactive or rule.broken) else FG_TEXT

        ttk.Button(row, text="↑", width=2, style="Dark.TButton",
                   command=lambda: self._move_rule(idx, -1)).pack(side=tk.LEFT)
        ttk.Button(row, text="↓", width=2, style="Dark.TButton",
                   command=lambda: self._move_rule(idx, +1)).pack(side=tk.LEFT)

        warn = "⚠ " if rule.broken else ""
        tk.Label(row, text=f"{warn}IF", fg=fg, bg=BG_PANEL,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=2)

        ctype = ttk.Combobox(row, values=CONDITION_TYPES, width=11, state="readonly")
        ctype.set(rule.condition.type)
        ctype.pack(side=tk.LEFT, padx=1)
        ctype.bind("<<ComboboxSelected>>",
                   lambda e: self._update_rule(idx, ctype=ctype.get()))

        cval = ttk.Combobox(row, width=16, values=self._condition_values(rule.condition.type))
        cval.set(rule.condition.value)
        cval.pack(side=tk.LEFT, padx=1)
        cval.bind("<FocusOut>", lambda e: self._update_rule(idx, cval=cval.get()))
        cval.bind("<<ComboboxSelected>>", lambda e: self._update_rule(idx, cval=cval.get()))

        tk.Label(row, text="→", fg=fg, bg=BG_PANEL).pack(side=tk.LEFT, padx=2)

        role = ttk.Combobox(row, values=ROLE_VALUES, width=15, state="readonly")
        role.set(rule.action.role)
        role.pack(side=tk.LEFT, padx=1)
        role.bind("<<ComboboxSelected>>", lambda e: self._update_rule(idx, role=role.get()))

        wing = ttk.Combobox(row, values=self._wing_names(), width=10, state="readonly")
        wing.set(rule.action.wing_name or "")
        wing.pack(side=tk.LEFT, padx=1)
        wing.bind("<<ComboboxSelected>>", lambda e: self._update_rule(idx, wing=wing.get()))

        squad = ttk.Combobox(row, values=self._squad_names(), width=10, state="readonly")
        squad.set(rule.action.squad_name or "")
        squad.pack(side=tk.LEFT, padx=1)
        squad.bind("<<ComboboxSelected>>", lambda e: self._update_rule(idx, squad=squad.get()))

        ttk.Button(row, text="✕", width=2, style="Red.TButton",
                   command=lambda: self._delete_rule(idx)).pack(side=tk.LEFT, padx=2)

    def _condition_values(self, ctype):
        if ctype == "doctrine_tag":
            return list(getattr(self.fittings, "tags", []))
        if ctype == "character":
            return sorted(self._character_names_provider())
        return []   # ship_type / ship_class: free text

    def _add_rule(self):
        t = self.current_template()
        if t is None:
            return
        t.rules.append(AssignmentRule(
            priority=len(t.rules),
            condition=RuleCondition("ship_type", ""),
            action=RuleAction("squad_member", None, None)))
        self._renumber_and_save()

    def _update_rule(self, idx, *, ctype=None, cval=None, role=None, wing=None, squad=None):
        t = self.current_template()
        if t is None or idx >= len(t.rules):
            return
        r = t.rules[idx]
        if ctype is not None:
            r.condition.type = ctype
        if cval is not None:
            r.condition.value = cval
        if role is not None:
            r.action.role = role
        if wing is not None:
            r.action.wing_name = wing or None
        if squad is not None:
            r.action.squad_name = squad or None
        validate_template(t)
        self.store.save()
        self._reload_rules()

    def _move_rule(self, idx, delta):
        t = self.current_template()
        j = idx + delta
        if t is None or not (0 <= j < len(t.rules)):
            return
        t.rules[idx], t.rules[j] = t.rules[j], t.rules[idx]
        self._renumber_and_save()

    def _delete_rule(self, idx):
        t = self.current_template()
        if t is None or idx >= len(t.rules):
            return
        del t.rules[idx]
        self._renumber_and_save()

    def _renumber_and_save(self):
        t = self.current_template()
        for i, r in enumerate(t.rules):
            r.priority = i
        validate_template(t)
        self.store.save()
        self._reload_rules()

    def _test_rules(self):
        """Preview the rules. Live mode: dry-run compose() against the real fleet
        and report move/unfilled/unassigned counts + warnings. Template mode:
        static validation summary (broken-ref count)."""
        t = self.current_template()
        if t is None:
            return
        if self.mode != "live":
            broken = sum(1 for r in t.rules if r.broken)
            messagebox.showinfo(
                "Test Rules",
                f"{len(t.rules)} rules, {broken} broken (⚠).\n"
                "Switch to Live mode to dry-run against the real fleet.",
                parent=self.win)
            return
        res = self._compose_preview()
        if res is None:
            return
        s = fleet_composer.summarize_moves(res)
        lines = [f"{s['executable']} moves, {s['unfilled']} slots unfilled, "
                 f"{s['unassigned']} unassigned."]
        lines += res.warnings[:10]
        messagebox.showinfo("Test Rules", "\n".join(lines), parent=self.win)
```

- [ ] **Step 2: Re-run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS.

- [ ] **Step 3: Manual sanity (dev box)**

Add two rules, reorder with ↑/↓, point a rule at a wing then delete that wing → reopen Rules tab → rule shows `⚠`. With no doctrine selected, a `doctrine_tag` rule renders greyed.

- [ ] **Step 4: Commit**

```bash
git add fleet_template_window.py
git commit -m "feat(fleet-templates): rules tab (add/edit/reorder/delete + tag greying)"
```

---

### Task D4: Settings tab (rebalance interval, cooldown, bulk threshold)

**Files:**
- Modify: `fleet_template_window.py` (replace `_build_settings_tab`/`_reload_settings` stubs)

- [ ] **Step 1: Replace the settings stubs**

```python
# fleet_template_window.py — replace _build_settings_tab / _reload_settings
    def _build_settings_tab(self):
        self._settings_vars = {}
        fields = [
            ("rebalance_interval_s", "Rebalance interval (s)", 10, 600),
            ("move_cooldown_s", "Move cooldown (s)", 30, 600),
            ("bulk_apply_threshold", "Bulk apply threshold (moves)", 1, 100),
        ]
        for i, (key, label, lo, hi) in enumerate(fields):
            tk.Label(self._settings_tab, text=label, bg=BG_PANEL, fg=FG_TEXT,
                     font=("Consolas", 9)).grid(row=i, column=0, sticky="w",
                                                padx=8, pady=6)
            var = tk.IntVar()
            self._settings_vars[key] = (var, lo, hi)
            tk.Spinbox(self._settings_tab, from_=lo, to=hi, textvariable=var,
                       width=8, bg=BG_ENTRY, fg=FG_TEXT,
                       command=self._on_settings_changed).grid(row=i, column=1,
                                                               padx=8, pady=6)
        tk.Label(self._settings_tab,
                 text="Each pilot move triggers a ~30 s EVE session timer.\n"
                      "Cooldown ≥ 45 s keeps the rebalancer under that limit.",
                 bg=BG_PANEL, fg=FG_DIM, font=("Consolas", 8),
                 justify=tk.LEFT).grid(row=len(fields), column=0, columnspan=2,
                                       sticky="w", padx=8, pady=8)
        self._reload_settings()

    def _reload_settings(self):
        t = self.current_template()
        if t is None:
            return
        s = t.settings
        self._settings_vars["rebalance_interval_s"][0].set(s.rebalance_interval_s)
        self._settings_vars["move_cooldown_s"][0].set(s.move_cooldown_s)
        self._settings_vars["bulk_apply_threshold"][0].set(s.bulk_apply_threshold)

    def _on_settings_changed(self):
        t = self.current_template()
        if t is None:
            return
        for key, (var, lo, hi) in self._settings_vars.items():
            try:
                val = max(lo, min(hi, int(var.get())))
            except (tk.TclError, ValueError):
                continue
            setattr(t.settings, key, val)
        self.store.save()
```

- [ ] **Step 2: Re-run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add fleet_template_window.py
git commit -m "feat(fleet-templates): settings tab (interval/cooldown/threshold)"
```

---

### Task D5: Live-mode load + enriched members + Unassigned + compose preview

**Files:**
- Modify: `fleet_template_window.py` (replace `_enter_live_mode`/`_exit_live_mode`/`_manual_assign` stubs; add helpers)

- [ ] **Step 1: Replace the live-mode stubs + add helpers**

```python
# fleet_template_window.py — replace _enter_live_mode / _exit_live_mode / _manual_assign
    def ship_class_label(self, type_id):
        """Human label for a hull's group (for ship_class rule conditions)."""
        if not type_id:
            return None
        try:
            import ship_classes
            gid = ship_classes.get_group_id(type_id)
        except Exception:
            return None
        return _GROUP_LABELS.get(gid)

    def _enrich_members(self, raw_members):
        """ESI member dicts → composer-shaped dicts. Runs on the background sync
        worker (see _sync_live), so the name/ship-class resolution here may hit
        ESI without blocking the UI thread."""
        from zkill_monitor import resolve_name
        out = []
        for m in raw_members:
            cid = m.get("character_id")
            tid = m.get("ship_type_id")
            out.append({
                "character_id": cid,
                "name": resolve_name(cid, "character") if cid else "",
                "ship_type_id": tid,
                "ship_type_name": resolve_name(tid, "type") if tid else "",
                "ship_class": self.ship_class_label(tid),   # pre-resolve off the Tk thread
                "role": m.get("role", "squad_member"),
                "wing_id": m.get("wing_id"),
                "squad_id": m.get("squad_id"),
                "join_time": m.get("join_time") or "",
            })
        return out

    def _enter_live_mode(self):
        info = self._fleet_info_provider()
        if not info or not info.get("is_boss"):
            messagebox.showwarning(
                "Live mode unavailable",
                "The selected FC character must be the current fleet boss to "
                "read and manage fleet structure.", parent=self.win)
            self.set_mode("template")
            return
        self._fleet_id = info["fleet_id"]
        self._sync_live(initial=True)
        self._schedule_sync()

    def _exit_live_mode(self):
        if self._sync_after_id:
            try:
                self.win.after_cancel(self._sync_after_id)
            except Exception:
                pass
            self._sync_after_id = None
        self._reload_tree()   # revert to stored template

    def _schedule_sync(self):
        # UI sync read every ~30 s (spec §9 budget). Main-thread timer.
        self._sync_after_id = self.win.after(30_000, self._sync_tick)

    def _post(self, fn, *args):
        """Schedule fn on the Tk thread from a worker; ignore TclError if the
        window was destroyed mid-flight (the daemon worker outlived the GUI).
        ALL worker→UI dispatches go through this, never a bare self.win.after."""
        try:
            self.win.after(0, fn, *args)
        except tk.TclError:
            pass

    def _sync_tick(self):
        if self.mode != "live":
            return
        self._sync_live(initial=False)
        self._schedule_sync()

    def _sync_live(self, *, initial):
        session = self._esi_session_provider()
        if session is None:
            self._status.config(text="No fleet-boss session.", fg=FG_RED)
            return

        def worker():
            err = None
            structure, members = {"wings": []}, []
            try:
                structure = {"wings": fleet_esi.get_wings(session, self._fleet_id)}
                # Enrich (name + ship_class resolution) HERE, off the Tk thread —
                # ship_class_label may hit ESI on cache-miss. Never enrich in _done.
                members = self._enrich_members(self._read_members(session))
            except fleet_esi.FleetESIError as e:
                err = e
            self._post(_done, structure, members, err)

        def _done(structure, members, err):
            if err is not None:
                self._status.config(text=f"Sync failed: {err.reason}", fg=FG_RED)
                return
            self._live_structure = structure
            self._live_members = members          # already enriched on the worker
            self._status.config(text="● Synced", fg=FG_GREEN)
            self._reload_tree()

        threading.Thread(target=worker, daemon=True).start()

    def _read_members(self, session):
        """GET /fleets/{id}/members/ via the session adapter."""
        resp = fleet_esi._call(session, "GET",
                               f"/fleets/{self._fleet_id}/members/", expect=(200,))
        data = resp.json()
        return data if isinstance(data, list) else []

    def _compose_preview(self):
        """Compose against the cached live snapshot and cache the result on
        self._last_preview so the tree + Unassigned render share one compose()
        call per reload (no network — ship_class is pre-resolved)."""
        t = self.current_template()
        if t is None:
            self._last_preview = None
            return None
        self._last_preview = fleet_composer.compose(
            t, self._live_members, self._live_structure,
            doctrine=self._doctrine_provider(), fittings=self.fittings)
        return self._last_preview

    def _render_unassigned(self):
        # Task D5 calls compose() here directly; Task D10 switches this to the
        # cached self._last_preview so the tree + Unassigned share one compose().
        res = self._compose_preview()
        if res is None or not res.unassigned:
            return
        head = self._tree.insert("", "end", text="── Unassigned ──", open=True)
        self._node_meta[head] = ("unassigned_header", ())
        for m in res.unassigned:
            nid = self._tree.insert(head, "end",
                                    text=f"· {m['name']} — {m['ship_type_name']}")
            self._node_meta[nid] = ("unassigned", (m["character_id"],))

    def _manual_assign(self, path):
        # path = (character_id,). Prompt for wing/squad then queue a single move.
        messagebox.showinfo("Manual move",
                            "Use drag-and-drop onto a squad to move this pilot.",
                            parent=self.win)
```

Add the group-label map near the top-level constants:

```python
# fleet_template_window.py — add near CONDITION_TYPES
# group_id → ship_class rule label (covers the doctrine-relevant hull groups).
_GROUP_LABELS = {
    540: "Command Ship", 1534: "Command Destroyer", 832: "Logistics Cruiser",
    1527: "Logistics Frigate", 963: "Strategic Cruiser", 547: "Carrier",
    1538: "Force Auxiliary", 485: "Dreadnought", 30: "Titan", 659: "Supercarrier",
    1201: "Combat Battlecruiser", 419: "Combat Battlecruiser",
}
```

- [ ] **Step 2: Re-run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS (live mode with `is_boss: True` and a null session shows "No fleet-boss session" without crashing).

- [ ] **Step 3: Commit**

```bash
git add fleet_template_window.py
git commit -m "feat(fleet-templates): live-mode sync, member enrichment, unassigned + preview"
```

---

### Task D6: Apply flow (hybrid: auto ≤ threshold, confirm > threshold)

**Files:**
- Modify: `fleet_template_window.py` (replace `_apply` stub; add `_resolve_target` + execute loop)

- [ ] **Step 1: Replace the `_apply` stub + add the executor**

```python
# fleet_template_window.py — replace _apply
    def _apply(self):
        if self.mode != "live":
            return
        session = self._esi_session_provider()
        if session is None:
            messagebox.showwarning("Apply", "No fleet-boss session.", parent=self.win)
            return
        res = self._compose_preview()
        if res is None:
            return
        summary = fleet_composer.summarize_moves(res)
        t = self.current_template()
        threshold = t.settings.bulk_apply_threshold
        if summary["executable"] == 0:
            self._status.config(text="Nothing to apply — fleet already matches.",
                                fg=FG_GREEN)
            return
        if summary["executable"] > threshold:
            msg = (f"{summary['executable']} moves required "
                   f"({summary['unfilled']} slots unfilled, "
                   f"{summary['unassigned']} unassigned).\n"
                   f"ESI calls: ~{summary['esi_calls']} (+ wing/squad creates)\n"
                   f"Estimated time: ~{summary['executable'] * 0.5:.0f}s\n\nApply now?")
            if not messagebox.askyesno("Confirm apply", msg, parent=self.win):
                return
        self._execute_moves(session, res.executable)

    def _execute_moves(self, session, moves):
        # Resolve target wing/squad names → live ids (creating as needed) lazily,
        # caching within this apply run.
        fleet_id = self._fleet_id
        name_to_wing = {w["name"]: w["id"]
                        for w in self._live_structure.get("wings", [])}
        name_to_squad = {(w["name"], s["name"]): s["id"]
                         for w in self._live_structure.get("wings", [])
                         for s in w.get("squads", [])}

        def worker():
            done = skipped = 0
            abort = None
            for i, mv in enumerate(moves):
                try:
                    wing_id = squad_id = None
                    if mv.target_wing_name is not None:
                        wing_id = name_to_wing.get(mv.target_wing_name)
                        if wing_id is None:
                            wing_id = fleet_esi.create_wing(session, fleet_id,
                                                            mv.target_wing_name)
                            name_to_wing[mv.target_wing_name] = wing_id
                    if mv.target_squad_name is not None and wing_id is not None:
                        key = (mv.target_wing_name, mv.target_squad_name)
                        squad_id = name_to_squad.get(key)
                        if squad_id is None:
                            squad_id = fleet_esi.create_squad(session, fleet_id,
                                                              wing_id, mv.target_squad_name)
                            name_to_squad[key] = squad_id
                    fleet_esi.move_member(session, fleet_id, mv.pilot_id,
                                          wing_id=wing_id, squad_id=squad_id,
                                          role=mv.target_role)
                    done += 1
                    self._post(lambda d=done, n=len(moves):
                               self._status.config(text=f"Moving pilots… {d}/{n}",
                                                   fg=FG_ACCENT))
                except fleet_esi.FleetESIError as e:
                    if e.reason == "boss_lost":
                        abort = e
                        break
                    skipped += 1   # 404 pilot-left / other → skip, continue
                import time
                time.sleep(0.5)    # sequential pacing (spec §8)
            self._post(_finish, done, skipped, abort)

        def _finish(done, skipped, abort):
            # Stamp the rebalancer cooldown so an apply burst delays the next
            # rebalancer move by move_cooldown_s (session-timer safety).
            if done:
                import time
                self._last_write_monotonic = time.monotonic()
            if abort is not None:
                messagebox.showerror("Apply aborted",
                                     "Lost fleet-boss role (403). No further moves "
                                     "were made.", parent=self.win)
                self._status.config(text="Apply aborted — boss lost.", fg=FG_RED)
            else:
                self._status.config(
                    text=f"Applied {done} moves. {skipped} skipped.", fg=FG_GREEN)
            self._sync_live(initial=False)

        threading.Thread(target=worker, daemon=True).start()
```

- [ ] **Step 2: Re-run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add fleet_template_window.py
git commit -m "feat(fleet-templates): hybrid apply flow + sequential ESI executor"
```

---

### Task D7: Rebalancer loop + toggle (one move per cooldown)

**Files:**
- Modify: `fleet_template_window.py` (replace `_toggle_rebalance` stub; add the loop)

- [ ] **Step 1: Replace the `_toggle_rebalance` stub + add the loop**

```python
# fleet_template_window.py — replace _toggle_rebalance
    def _toggle_rebalance(self):
        if self.mode != "live":
            return
        self._rebalance_on = not self._rebalance_on
        self._rebalance_btn.config(
            text=f"Rebalance: {'ON' if self._rebalance_on else 'OFF'}")
        if self._rebalance_on:
            self._schedule_rebalance(immediate=True)
        elif self._rebalance_after_id:
            try:
                self.win.after_cancel(self._rebalance_after_id)
            except Exception:
                pass
            self._rebalance_after_id = None

    def _schedule_rebalance(self, *, immediate=False):
        t = self.current_template()
        interval_ms = (t.settings.rebalance_interval_s if t else 60) * 1000
        delay = 100 if immediate else interval_ms
        self._rebalance_after_id = self.win.after(delay, self._rebalance_tick)

    def _rebalance_tick(self):
        if not self._rebalance_on or self.mode != "live":
            return
        session = self._esi_session_provider()
        t = self.current_template()
        if session is None or t is None:
            self._schedule_rebalance()
            return

        # Cooldown gate (monotonic). One write per move_cooldown_s.
        import time
        now = time.monotonic()
        cooldown = t.settings.move_cooldown_s
        if now - self._last_write_monotonic < cooldown:
            self._schedule_rebalance()
            return

        max_sizes = {(w.name, s.name): s.max_size
                     for w in t.wings for s in w.squads}

        def worker():
            err = None
            action = None
            structure, members = self._live_structure, []
            try:
                structure = {"wings": fleet_esi.get_wings(session, self._fleet_id)}
                members = self._read_members(session)
                action = fleet_composer.plan_rebalance(
                    members, structure, max_sizes=max_sizes)
            except fleet_esi.FleetESIError as e:
                err = e
            self._post(_apply_action, structure, action, err)

        def _apply_action(structure, action, err):
            self._live_structure = structure
            if err is not None:
                if err.reason in ("boss_lost",):
                    self._rebalance_on = False
                    self._rebalance_btn.config(text="Rebalance: OFF")
                    messagebox.showerror("Rebalancer stopped",
                                         "Lost fleet-boss role (403).", parent=self.win)
                    return
                self._schedule_rebalance()
                return
            if action is None:
                self._status.config(text="Rebalancer: all squads within cap.",
                                    fg=FG_DIM)
                self._schedule_rebalance()
                return
            self._execute_rebalance(session, action)

        threading.Thread(target=worker, daemon=True).start()

    def _execute_rebalance(self, session, action):
        def worker():
            err = None
            try:
                name_to_wing = {w["name"]: w["id"]
                                for w in self._live_structure.get("wings", [])}
                wing_id = name_to_wing.get(action.target_wing_name)
                squad_id = None
                if action.create_squad:
                    squad_id = fleet_esi.create_squad(session, self._fleet_id,
                                                      wing_id, "Overflow")
                else:
                    for w in self._live_structure.get("wings", []):
                        if w["name"] != action.target_wing_name:
                            continue
                        for s in w.get("squads", []):
                            if s["name"] == action.target_squad_name:
                                squad_id = s["id"]
                fleet_esi.move_member(session, self._fleet_id, action.pilot_id,
                                      wing_id=wing_id, squad_id=squad_id,
                                      role="squad_member")
            except fleet_esi.FleetESIError as e:
                err = e
            self._post(_done, err)

        def _done(err):
            import time
            self._last_write_monotonic = time.monotonic()
            if err is None:
                self._status.config(
                    text=f"Rebalancer: moved {action.pilot_name} → "
                         f"{action.target_wing_name}", fg=FG_GREEN)
            else:
                self._status.config(text=f"Rebalancer error: {err.reason}", fg=FG_RED)
            self._schedule_rebalance()

        threading.Thread(target=worker, daemon=True).start()
```

- [ ] **Step 2: Re-run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add fleet_template_window.py
git commit -m "feat(fleet-templates): size-cap rebalancer loop + ON/OFF toggle"
```

---

### Task D8: Drag-and-drop (slot/pilot → squad)

**Files:**
- Modify: `fleet_template_window.py` (replace the three drag stubs)

- [ ] **Step 1: Replace the drag stubs**

```python
# fleet_template_window.py — replace _on_drag_start / _on_drag_motion / _on_drag_drop
    def _on_drag_start(self, event):
        item = self._tree.identify_row(event.y)
        meta = self._node_meta.get(item)
        # Slots and unassigned pilots are always draggable; whole squads are
        # draggable in template mode only (live tree mirrors ESI structure).
        draggable = {"slot", "unassigned"}
        if self.mode == "template":
            draggable.add("squad")
        if meta and meta[0] in draggable:
            self._drag_item = item
        else:
            self._drag_item = None

    def _on_drag_motion(self, event):
        if self._drag_item:
            target = self._tree.identify_row(event.y)
            self._tree.selection_set(target) if target else None

    def _on_drag_drop(self, event):
        src = self._drag_item
        self._drag_item = None
        if not src:
            return
        dst = self._tree.identify_row(event.y)
        src_meta = self._node_meta.get(src)
        dst_meta = self._node_meta.get(dst)
        if not src_meta or not dst_meta:
            return
        # A dragged squad targets a WING (move the whole squad between wings).
        if src_meta[0] == "squad":
            dst_wing = self._wing_path_of(dst_meta)
            if dst_wing is None:
                self._flash_reject(dst)
                return
            self._drop_squad_into_wing(src_meta[1], dst_wing)
            return
        # Slots / pilots target a SQUAD (squad node or a slot's parent squad).
        dst_squad = self._squad_path_of(dst_meta)
        if dst_squad is None:
            self._flash_reject(dst)
            return
        if src_meta[0] == "slot":
            self._drop_slot_into_squad(src_meta[1], dst_squad)
        elif src_meta[0] == "unassigned" and self.mode == "live":
            self._drop_pilot_into_squad(src_meta[1][0], dst_squad)

    def _squad_path_of(self, meta):
        kind, path = meta
        if kind == "squad":
            return path
        if kind == "slot":
            return (path[0], path[1])
        return None

    def _wing_path_of(self, meta):
        kind, path = meta
        if kind in ("wing", "squad", "slot"):
            return (path[0],)
        return None

    def _drop_squad_into_wing(self, squad_path, wing_path):
        wi, si = squad_path
        twi = wing_path[0]
        if twi == wi:
            return   # same wing → no-op
        t = self.current_template()
        squad = t.wings[wi].squads.pop(si)
        t.wings[twi].squads.append(squad)
        self._after_structure_change()

    def _drop_slot_into_squad(self, slot_path, squad_path):
        wi, si, li = slot_path
        t = self.current_template()
        slot = t.wings[wi].squads[si].slots.pop(li)
        t.wings[squad_path[0]].squads[squad_path[1]].slots.append(slot)
        self._after_structure_change()

    def _drop_pilot_into_squad(self, character_id, squad_path):
        # Live: queue a single ESI move for this pilot into the target squad.
        session = self._esi_session_provider()
        if session is None:
            return
        t = self.current_template()
        wing = t.wings[squad_path[0]]
        squad = wing.squads[squad_path[1]]
        mv = fleet_composer.Move(pilot_id=character_id, pilot_name="",
                                 target_wing_name=wing.name,
                                 target_squad_name=squad.name,
                                 target_role="squad_member")
        self._execute_moves(session, [mv])

    def _flash_reject(self, item):
        if not item:
            return
        self._status.config(text="Invalid drop target.", fg=FG_YELLOW)
```

- [ ] **Step 2: Re-run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS.

- [ ] **Step 3: Manual sanity (dev box)**

In template mode, drag a slot from one squad to another → tree updates, file saved. Drag a whole squad onto a different wing → the squad (and its slots) moves to that wing. In live mode (boss), drag an Unassigned pilot onto a squad → status shows the move.

- [ ] **Step 4: Commit**

```bash
git add fleet_template_window.py
git commit -m "feat(fleet-templates): drag-and-drop slots and unassigned pilots"
```

---

### Task D9: Template-mode undo (Ctrl+Z)

Spec §2/§6 lock a template-mode local undo stack. Snapshots are serialized
templates (via `template_to_dict`), so undo is a pure restore — no widget-state
bookkeeping.

**Files:**
- Modify: `fleet_template_window.py` (add `_push_undo`/`_undo`; bind Ctrl+Z; add `self._push_undo()` to each mutator)

- [ ] **Step 1: Add the undo methods**

```python
# fleet_template_window.py — add to FleetTemplateWindow
    def _push_undo(self):
        """Snapshot the current template before a structural/rule edit
        (template mode only). Capped at 50 levels."""
        if self.mode != "template":
            return
        from fleet_template_store import template_to_dict
        t = self.current_template()
        if t is not None:
            self._undo_stack.append(template_to_dict(t))
            del self._undo_stack[:-50]

    def _undo(self, _evt=None):
        if self.mode != "template" or not self._undo_stack:
            return
        from fleet_template_store import template_from_dict, validate_template
        restored = template_from_dict(self._undo_stack.pop())
        validate_template(restored)
        for i, t in enumerate(self.store.templates):
            if t.id == restored.id:
                self.store.templates[i] = restored
                break
        else:
            return
        self._current_template_id = restored.id
        self.store.save()
        self._reload_tree()
        self._reload_rules()
        self._reload_settings()
```

- [ ] **Step 2: Bind Ctrl+Z**

Add to the end of `__init__` (after `self.set_mode("template")`):

```python
        self.win.bind("<Control-z>", self._undo)
```

- [ ] **Step 3: Snapshot before each edit**

Add `self._push_undo()` as the **first statement** of each of these mutators:
`_add_wing`, `_add_squad`, `_add_slot`, `_rename_selected`, `_set_max_size`,
`_edit_slot` (before opening `SlotEditor`), `_delete_selected`,
`_drop_slot_into_squad`, `_drop_squad_into_wing`, `_add_rule`, `_update_rule`,
`_move_rule`, `_delete_rule`. Example:

```python
    def _add_wing(self):
        self._push_undo()
        t = self.current_template()
        if t is None:
            return
        t.wings.append(Wing(name=f"Wing {len(t.wings) + 1}", max_size=None, squads=[]))
        self._after_structure_change()
```

- [ ] **Step 4: Re-run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS.

- [ ] **Step 5: Manual sanity (dev box)**

In template mode: add a wing, add a squad, rename it, delete it — then press Ctrl+Z repeatedly; each press reverts one edit and the tree/rules refresh. Confirm Ctrl+Z is a no-op in Live mode.

- [ ] **Step 6: Commit**

```bash
git add fleet_template_window.py
git commit -m "feat(fleet-templates): template-mode undo stack (Ctrl+Z)"
```

---

### Task D10: Live-mode slot rendering (matched pilot + color) + single-compose reload

Spec §6/§10: in Live mode each slot shows its matched pilot and a status color —
**green** in position, **orange** present-but-needs-moving, **grey** empty.
This replaces the Task D2 `_reload_tree` with a live-aware version that runs one
`compose()` per reload and shares the result with the Unassigned renderer.

**Files:**
- Modify: `fleet_template_window.py` (add tag colors to `_build_tree`; replace `_reload_tree` and `_render_unassigned`; add `_role_suffix`/`_slot_match_map`)

- [ ] **Step 1: Add slot color tags to `_build_tree`**

Add immediately after `vsb.pack(side=tk.RIGHT, fill=tk.Y)` in `_build_tree`:

```python
        self._tree.tag_configure("inpos", foreground=FG_GREEN)   # in correct position
        self._tree.tag_configure("moveme", foreground=FG_YELLOW)  # present, needs moving
        self._tree.tag_configure("empty", foreground=FG_DIM)      # unfilled slot
```

- [ ] **Step 2: Replace `_reload_tree` (from Task D2) with the live-aware version**

```python
# fleet_template_window.py — replace _reload_tree
    def _reload_tree(self):
        self._tree.delete(*self._tree.get_children())
        self._node_meta.clear()
        t = self.current_template()
        if t is None:
            self._last_preview = None
            return
        # Exactly one compose() per reload (live only); template mode skips it.
        self._last_preview = self._compose_preview() if self.mode == "live" else None
        match_map = self._slot_match_map() if self.mode == "live" else {}
        for wi, wing in enumerate(t.wings):
            cap = f"  (max {wing.max_size})" if wing.max_size else ""
            wid = self._tree.insert("", "end", text=f"▼ {wing.name}{cap}", open=True)
            self._node_meta[wid] = ("wing", (wi,))
            for si, squad in enumerate(wing.squads):
                scap = f"  (max {squad.max_size})" if squad.max_size else ""
                sid = self._tree.insert(wid, "end",
                                        text=f"▼ {squad.name}{scap}", open=True)
                self._node_meta[sid] = ("squad", (wi, si))
                pending = list(match_map.get((wing.name, squad.name), []))
                for li, slot in enumerate(squad.slots):
                    if self.mode == "live" and pending:
                        p = pending.pop(0)
                        text = (f"✓ {p['name']} — {p['ship_type_name']}"
                                f"{self._role_suffix(slot.role)}")
                        nid = self._tree.insert(
                            sid, "end", text=text,
                            tags=("inpos" if p["in_position"] else "moveme",))
                    elif self.mode == "live":
                        nid = self._tree.insert(sid, "end",
                                                text=self._slot_label(slot),
                                                tags=("empty",))
                    else:
                        nid = self._tree.insert(sid, "end", text=self._slot_label(slot))
                    self._node_meta[nid] = ("slot", (wi, si, li))
        if self.mode == "live":
            self._render_unassigned()

    def _role_suffix(self, role):
        abbr = ROLE_ABBR.get(role, "")
        return f" [{abbr}]" if abbr else ""

    def _slot_match_map(self):
        """(wing_name, squad_name) → ordered [{name, ship_type_name, in_position}]
        for the slots in that squad, derived from the cached compose preview."""
        res = self._last_preview
        out: dict[tuple, list] = {}
        if res is None:
            return out
        type_by_id = {m["character_id"]: m.get("ship_type_name", "")
                      for m in self._live_members}
        for mv in res.moves:
            key = (mv.target_wing_name, mv.target_squad_name)
            out.setdefault(key, []).append({
                "name": mv.pilot_name,
                "ship_type_name": type_by_id.get(mv.pilot_id, ""),
                "in_position": mv.skip_reason == "already_correct",
            })
        return out
```

- [ ] **Step 3: Switch `_render_unassigned` (from Task D5) to the cached preview**

```python
# fleet_template_window.py — replace _render_unassigned
    def _render_unassigned(self):
        res = self._last_preview   # set by _reload_tree's single compose() call
        if res is None or not res.unassigned:
            return
        head = self._tree.insert("", "end", text="── Unassigned ──", open=True)
        self._node_meta[head] = ("unassigned_header", ())
        for m in res.unassigned:
            nid = self._tree.insert(head, "end",
                                    text=f"· {m['name']} — {m['ship_type_name']}")
            self._node_meta[nid] = ("unassigned", (m["character_id"],))
```

- [ ] **Step 4: Re-run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS.

- [ ] **Step 5: Manual sanity (dev box, real fleet)**

Switch to Live mode with a populated template: pilots already in their slot render **green**; pilots present but needing a move render **orange**; unfilled slots render grey `○`; unplaced pilots appear under **── Unassigned ──**.

- [ ] **Step 6: Commit**

```bash
git add fleet_template_window.py
git commit -m "feat(fleet-templates): live-mode slot pilot + color rendering"
```

---

# Phase E — `fc_gui.py` integration

### Task E1: "Fleet Templates" button + providers + open the window

**Files:**
- Modify: `fc_gui.py` (Fleet Management header near line 1396-1403; add an opener method; init the store)

- [ ] **Step 1: Initialize the template store at startup**

In `fc_gui.py`, right after the fittings store is created (fc_gui.py:580-582), add:

```python
# fc_gui.py — after self.fittings.load() block (~line 583)
        import fleet_template_store as _fleet_template_store
        self.fleet_templates = _fleet_template_store.FleetTemplateStore(
            os.path.join(app_dir(), "fleet_templates.json"))
        self.fleet_templates.load()
        self._fleet_template_window = None
```

- [ ] **Step 2: Add the opener + provider builders** (place beside the other Fleet Management methods, e.g. after `_open_active_doctrine` ~line 7252)

```python
# fc_gui.py — new methods
    def _active_doctrine_obj(self):
        """Resolve config['fleet']['active_doctrine'] (name or id) to a Doctrine."""
        key = self.config.get("fleet", {}).get("active_doctrine", "")
        if not key:
            return None
        for d in self.fittings.list_doctrines():
            if d.id == key or d.name == key:
                return d
        return None

    def _fleet_boss_session(self):
        """Current fleet-boss AuthEsiSession, or None if no authed boss."""
        import fleet_esi
        auth = self._motd_selected_fc_auth() or self.esi_auth
        if auth is None or not auth.is_authenticated:
            return None
        if not auth.has_scope("esi-fleets.write_fleet.v1"):
            return None
        info = auth.get_fleet_info()
        if not info or not auth.is_boss(info, auth.character_id):
            return None
        return fleet_esi.AuthEsiSession(auth)

    def _fleet_boss_info(self):
        auth = self._motd_selected_fc_auth() or self.esi_auth
        if auth is None or not auth.is_authenticated:
            return None
        info = auth.get_fleet_info()
        if not info:
            return None
        return {"fleet_id": info["fleet_id"],
                "is_boss": auth.is_boss(info, auth.character_id)}

    def _authed_character_names(self):
        names = []
        for acct in getattr(self, "esi_accounts", []) or []:
            nm = getattr(acct, "character_name", None)
            if nm:
                names.append(nm)
        names += list(self.fleet_templates.cached_characters)
        return sorted(set(names))

    def _open_fleet_templates(self):
        from fleet_template_window import FleetTemplateWindow
        existing = self._fleet_template_window
        if existing is not None:
            try:
                existing.win.lift()
                existing.win.focus_force()
                return
            except Exception:
                self._fleet_template_window = None
        self._fleet_template_window = FleetTemplateWindow(
            self.root,
            store=self.fleet_templates,
            fittings=self.fittings,
            config=self.config,
            esi_session_provider=self._fleet_boss_session,
            fleet_info_provider=self._fleet_boss_info,
            doctrine_provider=self._active_doctrine_obj,
            character_names_provider=self._authed_character_names,
        )
        # Clear the handle when the window closes so re-open works.
        win = self._fleet_template_window
        orig_destroy = win.destroy

        def _wrapped_destroy():
            orig_destroy()
            self._fleet_template_window = None

        win.destroy = _wrapped_destroy
        win.win.protocol("WM_DELETE_WINDOW", _wrapped_destroy)
```

- [ ] **Step 3: Add the button to the Fleet Management header**

In the Role Tracker header block (fc_gui.py:1396-1403), add next to the existing buttons:

```python
# fc_gui.py — in the role_header button row (~line 1403)
        ttk.Button(role_header, text="Fleet Templates", style="Dark.TButton",
                   command=self._open_fleet_templates).pack(side=tk.LEFT, padx=8)
```

- [ ] **Step 4: Verify the app imports and the button opens the window**

Run: `python -c "import fc_gui"`
Expected: no output, exit 0 (module imports clean).

Run: `python -m py_compile fc_gui.py fleet_template_window.py fleet_template_store.py fleet_composer.py fleet_esi.py`
Expected: no output, exit 0.

Manual (dev box): launch the app, open the Fleet Management tab, click **Fleet Templates** → the window opens in Template mode. Close and re-open → no duplicate window.

- [ ] **Step 5: Commit**

```bash
git add fc_gui.py
git commit -m "feat(fleet-templates): Fleet Management button + providers + window opener"
```

---

# Phase F — Verification

### Task F1: Full test + compile + import + headless smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the full pure test suite**

Run: `python -m pytest tests/test_fleet_template_store.py tests/test_fleet_composer.py tests/test_fleet_esi.py -v`
Expected: ALL PASS (no skips — these need no display).

- [ ] **Step 2: Run the window smoke test**

Run: `python -m pytest tests/test_fleet_template_window.py -v`
Expected: PASS on a machine with Tk; SKIP only on a truly headless box.

- [ ] **Step 3: Run the whole project test suite (no regressions)**

Run: `python -m pytest -q`
Expected: all previously-passing tests still pass; the new tests pass.

- [ ] **Step 4: Compile + import check**

Run: `python -m py_compile fleet_template_store.py fleet_composer.py fleet_esi.py fleet_template_window.py fc_gui.py`
Run: `python -c "import fleet_template_store, fleet_composer, fleet_esi, fc_gui; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 5: Manual end-to-end checklist (dev box, real fleet optional)**

Template mode (no ESI needed):
- [ ] Create a template; add 2 wings, each with 2 squads, each with slots (named + role + generic).
- [ ] Edit a slot through every type (named → role → generic); tree glyphs update (`●`/`◈`/`○`).
- [ ] Add 3 rules; reorder with ↑/↓; delete a referenced wing → rule shows `⚠`.
- [ ] Set a squad max size; confirm it shows in the tree and persists across re-open.
- [ ] Confirm `fleet_templates.json` in `app_dir()` round-trips (close + reopen app).

Template mode (more):
- [ ] Press Ctrl+Z after several edits → each press reverts one edit (undo).
- [ ] Drag a whole squad onto another wing → squad moves (template structural drag).

Live mode (requires being fleet boss in EVE):
- [ ] Switch to Live → tree fills from the real fleet; matched pilots show in slots, **green** (in position) / **orange** (needs moving); unfilled slots grey; Unassigned group lists unplaced pilots.
- [ ] Rules tab → "Test Rules" → dialog reports move/unfilled/unassigned counts + warnings.
- [ ] Apply with ≤ threshold moves → executes immediately; status shows progress.
- [ ] Apply with > threshold moves → confirm dialog appears with counts; cancel and re-apply.
- [ ] Set a squad cap below its live size, toggle Rebalance ON → exactly one move per cooldown; toggle OFF stops it.
- [ ] Drop from boss (lose boss role) mid-apply → apply aborts with the boss-lost alert.

- [ ] **Step 6: Final commit (docs/notes if any)**

```bash
git add -A
git commit -m "test(fleet-templates): verification pass — full suite green"
```

---

## Self-review notes (coverage map)

| Spec section | Implemented by |
|---|---|
| §3 four-file architecture | Phases A–D; `fc_gui` gains only providers + a button (E1) |
| §4 data model + advisory max_size + name refs | A1–A3 (`max_size` only read by `plan_rebalance` B4) |
| §5 window layout + mode rules | D1 (toggle, button states), D5 (mode switch loads/reverts) |
| §6 tree, slot display, context menu, keyboard | D2 (render, menu, F2/Delete), D8 (slot/pilot drag + squad→wing drag), D9 (Ctrl+Z undo), D10 (live slot pilot + green/orange/grey color) |
| §7 rules engine + matching + edge cases + Test Rules | B2 (every §7 row has a test in B2/B3, incl. unfilled-slot + ship_class), D3 (UI + "Test Rules" dry-run) |
| §8 hybrid apply | D6 (auto ≤ threshold, confirm > threshold, sequential 0.5 s, boss-lost abort) |
| §9 rebalancer + ESI budget + auto-disable | B4 (`plan_rebalance`; `overflow_strategy` reserved-in-v1, documented), D7 (loop, cooldown gate, 403 auto-off) |
| §10 multiboxing (named + generic, cache, live glow) | A4 (`cache_character`), D2 `SlotEditor`, D10 (green/orange/grey live state), E1 (`_authed_character_names`) |
| §11 doctrine integration + tag index | B1 (`build_tag_index`), B2 (doctrine_tag matching), D3 (greying), E1 (`_active_doctrine_obj`) |
| §12 testing | A1–A4, B1–B4, C1–C3 test files; D headless smoke; F manual checklist |
| §13 out-of-scope | Not implemented (no fleet invite, no fleet creation, no audit log, no per-pilot notes, no import/export, no MOTD auto-select) |

**Deliberate v1 deferrals (beyond spec §13):** the §6 context-menu item **"Kick from fleet"** is intentionally omitted — it is a destructive ESI action outside the tool's "manage structure only" remit; revisit if requested. The §6 **"Assign role"** action for Unassigned pilots is folded into drag-drop / Move-to-squad. `overflow_strategy` is reserved (see §9 row).

**Type-consistency check:** `Move`, `ComposeResult`, `RebalanceAction`, `FleetESIError`, `AuthEsiSession`, and the store dataclasses are named identically everywhere they appear (B1/B2/B4/C1/C3/D5/D6/D7/D10). Member dict keys (`character_id`, `name`, `ship_type_id`, `ship_type_name`, `ship_class`, `role`, `wing_id`, `squad_id`, `join_time`) match between `_enrich_members` (D5), `compose`/`_pilot_matches` (B2 — `ship_class` read directly, no callable), and `plan_rebalance` (B4). Live-structure shape (`{"wings":[{"id","name","squads":[{"id","name"}]}]}`) matches between `get_wings` (C2), `compose` (B2), `plan_rebalance` (B4), and the executors (D6/D7). The cached compose preview (`self._last_preview`) is written once per reload in `_reload_tree` (D10) and read by `_slot_match_map` + `_render_unassigned` (D10) — one `compose()` per reload.
