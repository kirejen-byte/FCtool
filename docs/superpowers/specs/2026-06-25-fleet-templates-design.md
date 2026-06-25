# Fleet Templates & Live Fleet Manager — Design Spec

**Date:** 2026-06-25  
**Status:** Approved design  
**Area:** Fleet Management tab → Fleet Templates window

---

## 1. Goal

Add a Fleet Templates system to FCTool that lets an FC:

- Build reusable wing/squad/slot structures offline (template mode)
- Apply a template to a live fleet with one click (live mode)
- Automatically assign pilots to positions and roles using a configurable rules engine (ship type, ship class, character name, or doctrine tag)
- Support multiboxing via named character slots alongside generic role slots
- Enforce optional squad/wing size caps via a periodic background rebalancer that is easy to pause and conserves ESI calls

---

## 2. Locked Decisions

| # | Decision |
|---|---|
| Window | Single `Toplevel` with a `[Template / Live]` mode toggle at top-right |
| Entry point | `Fleet Templates` button added to Fleet Management tab header |
| Apply flow | Hybrid: ≤ threshold moves execute immediately; > threshold shows confirm dialog |
| Default bulk threshold | 5 moves (configurable per template) |
| Rebalancer writes | Maximum 1 move per `move_cooldown` seconds (default 45 s); only 1 move per rebalance tick |
| Rule matching order | Named slots first → rules in priority order → generic slots → Unassigned |
| Rule conflict | Highest-priority (lowest index) rule wins per pilot |
| Overflow pilots | Appear in virtual "Unassigned" group; FC drags manually |
| Undo | Template mode only (local undo stack); Live mode has no undo (ESI calls irreversible) |
| Persistence | New `fleet_templates.json` alongside `fittings_library.json` |
| Doctrine integration | Doctrine tag conditions are inactive (greyed) when no doctrine is loaded |
| Session timers | Surfaced in tooltip on Rebalance toggle; cooldown default 45 s exceeds the ~30 s timer |

---

## 3. Architecture

Four new files; `fc_gui.py` gains only a button:

```
fc_gui.py
  └─ opens ──► fleet_template_window.py   (Tk only)
                  ├─ fleet_template_store.py   (pure data + persistence)
                  ├─ fleet_composer.py          (pure matching logic)
                  └─ fleet_esi.py               (ESI writes + rate limiting)
                       └─ rate_limiter.py       (existing)
```

### `fleet_template_store.py`
- Dataclasses: `FleetTemplate`, `Wing`, `Squad`, `Slot`, `AssignmentRule`, `RebalanceSettings`
- `load() / save()` for `fleet_templates.json`
- CRUD: `add_template`, `delete_template`, `rename_template`, `set_wing`, `set_squad`, `set_slot`, `set_rule`
- On load: validates rule `wing_name`/`squad_name` references; marks broken rules with `broken=True`
- No Tk, no ESI, no network

### `fleet_composer.py`
- `compose(template, live_members, doctrine=None) → MoveList`
- `MoveList`: ordered list of `Move(pilot_id, target_wing_id, target_squad_id, target_role, skip_reason=None)`
- `skip_reason` set (not None) for already-correct positions, broken rules, missing squads — these generate no ESI calls
- Edge cases all handled here (see §7)
- No Tk, no ESI — fully unit-testable

### `fleet_esi.py`
- `create_wing(fleet_id, name) → wing_id`
- `create_squad(fleet_id, wing_id, name) → squad_id`
- `move_member(fleet_id, member_id, wing_id, squad_id, role)`
- `rename_wing / rename_squad / delete_wing / delete_squad`
- All calls go through `rate_limiter("esi")`
- Retries once on 5xx; raises `FleetESIError` on 403 (boss lost), 404 (pilot left), or second failure
- Caller (`fleet_template_window.py`) handles `FleetESIError`

### `fleet_template_window.py`
- `FleetTemplateWindow(root, fittings_store, esi_session, config)`
- Owns the `root.after` polling loops (UI sync + rebalancer)
- Imports `fleet_template_store`, `fleet_composer`, `fleet_esi`
- All Tk widget construction here; no logic

---

## 4. Data Model

### `fleet_templates.json` schema

```json
{
  "version": 1,
  "templates": [
    {
      "id": "uuid4",
      "name": "Standard Armor Fleet",
      "doctrine_id": "uuid4-or-null",
      "wings": [
        {
          "name": "Alpha Wing",
          "max_size": null,
          "squads": [
            {
              "name": "Logi Squad",
              "max_size": 10,
              "slots": [
                {"character": "Kyra Dawnfall", "tag": null,        "role": "squad_commander"},
                {"character": null,             "tag": "Logistics", "role": "squad_member"},
                {"character": null,             "tag": null,        "role": "squad_member"}
              ]
            }
          ]
        }
      ],
      "rules": [
        {
          "priority": 0,
          "condition": {"type": "doctrine_tag", "value": "Links"},
          "action": {"role": "squad_commander", "wing_name": "Alpha Wing", "squad_name": "Logi Squad"}
        },
        {
          "priority": 1,
          "condition": {"type": "ship_type", "value": "Damnation"},
          "action": {"role": "squad_commander", "wing_name": "Alpha Wing", "squad_name": null}
        }
      ],
      "settings": {
        "rebalance_interval_s": 60,
        "move_cooldown_s": 45,
        "bulk_apply_threshold": 5,
        "overflow_strategy": "least_populated"
      }
    }
  ],
  "cached_characters": ["Kyra Dawnfall", "Alt Name"]
}
```

**Slot fields:**
- `character`: string → named slot (exact match); null → role/generic slot
- `tag`: doctrine tag string → role slot; null → generic slot
- `role`: ESI role string (`squad_commander` / `wing_commander` / `fleet_commander` / `squad_member`)

**`max_size` is advisory** — it is not enforced at template edit time (you may add more slots than the cap). It is enforced only by the live rebalancer, which moves overflow pilots out when the cap is exceeded in the live fleet.

**Rule condition types:** `doctrine_tag` | `ship_type` | `ship_class` | `character`  
**Rule action:** `role` (required) + `wing_name` + `squad_name` (both optional; null = "anywhere")  
**Rules reference wings/squads by name** — survive structural reordering; broken refs flagged at load.

---

## 5. Window Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  Fleet Templates                              [ Template | Live ] │
│  Template: [ My Standard Fleet ▼ ]  [New]  [Rename]  [Delete]   │
├────────────────────────────┬─────────────────────────────────────┤
│  Wing / Squad Tree         │  [ Members │ Rules │ Settings ]     │
│                            │                                      │
│  ▼ Wing 1 "Alpha"          │  (context panel — see §6)           │
│    ▼ Squad 1 "Logi"        │                                      │
│      · Links (role slot)   │                                      │
│      · Kyra Alt (named)    │                                      │
│    ▼ Squad 2 "DPS"         │                                      │
│      · DPS  (role slot)    │                                      │
│  ▼ Wing 2 "Bravo"  ...     │                                      │
│  ── Unassigned ──          │  ← Live mode only                   │
│      · Pilot X — Rifter    │                                      │
│                            │                                      │
│  [+ Add Wing]              │                                      │
├────────────────────────────┴─────────────────────────────────────┤
│  [Save Template]    Rebalance: [OFF ▶]    ● Synced 12s ago  [Apply]│
└──────────────────────────────────────────────────────────────────┘
```

- **Template mode**: Apply button disabled; Save button active; Rebalance toggle disabled
- **Live mode**: Apply button active; Save replaced by sync status; Rebalance toggle active
- **Mode switch Template→Live**: loads ESI fleet into tree; any unsaved template changes prompted
- **Mode switch Live→Template**: no ESI calls; tree reverts to stored template

---

## 6. Fleet Tree

### Slot display

| Type | Template display | Live display |
|---|---|---|
| Named | `● Kyra Dawnfall [SC]` | `✓ Kyra Dawnfall — Archon [SC]` (green if in position) |
| Role slot | `◈ Links [SC]` | `✓ Pilot X — Claymore [SC]` once matched |
| Generic | `○ (empty)` | `○ (empty)` if unmatched |
| Unassigned | — | `· Pilot Y — Rifter` in Unassigned group |

### Drag-and-drop

Implemented via `ttk.Treeview` with `<Button-1>` / `<B1-Motion>` / `<ButtonRelease-1>` bindings:
- Slot → different squad: reorder (template) or queue ESI move (live)
- Slot → different wing: same
- Unassigned pilot → slot: assign + queue ESI move
- Squad → wing: moves whole squad; queues all pilots in live mode
- Drop on invalid target (wing-onto-slot, etc.): rejected with brief highlight

### Right-click context menu

| Target | Options |
|---|---|
| Wing | Rename / Add Squad / Set max size / Delete Wing |
| Squad | Rename / Add Slot / Set max size / Delete Squad |
| Slot | Edit (type/tag/role) / Delete |
| Unassigned pilot (Live) | Move to squad… / Assign role / Kick from fleet (confirm) |
| Blank tree area | Add Wing |

### Keyboard shortcuts

- `F2` — rename selected item
- `Delete` — delete selected (confirm if non-empty)
- `Ctrl+Z` — undo last edit (template mode only)

---

## 7. Rules Engine

### Right-panel Rules tab layout

```
[↕] IF [ Ship Type    ▼ ] [ Damnation         ] → [ squad_commander ▼ ] [ Wing 1 ▼ ][ Squad 1 ▼ ]  [✕]
[↕] IF [ Doctrine Tag ▼ ] [ Links             ] → [ squad_commander ▼ ] [ Wing 1 ▼ ][ Squad 2 ▼ ]  [✕]
[↕] IF [ Ship Class   ▼ ] [ Logistics Cruiser ] → [ squad_member    ▼ ] [ any      ▼][ any      ▼]  [✕]
                                                                                       [+ Add Rule]
[Test Rules]
```

- `↕` handle: drag to reorder priority
- Condition types: `Ship Type` | `Ship Class` | `Character` | `Doctrine Tag`
- Action: role dropdown + optional wing/squad dropdowns (null = "anywhere in fleet")
- Rules with broken wing/squad refs show ⚠ and are skipped at apply time
- Doctrine Tag rules greyed with tooltip "No doctrine active" when no doctrine loaded

### Matching algorithm (in `fleet_composer.py`)

```
Input:  template, live_members (ESI format), doctrine (optional)
Output: MoveList

1. Build pilot index: pilot_id → {name, ship_type_id, ship_type_name, ship_group, wing_id, squad_id, role}
2. Build doctrine tag index: ship_type_id → [tags] (requires active doctrine + fittings catalog)
3. Resolve named slots: for each slot where character != null, find pilot by name (case-insensitive)
   → exact match: assign to slot; pilot removed from pool
   → no match: slot stays empty (no ESI call)
4. Resolve rule slots: for each role-slot (tag != null), evaluate rules in priority order against remaining pool
   → first rule that matches a pilot claims that pilot for that slot
5. Fill generic slots: round-robin from remaining pool, left-to-right tree order
6. Remaining pool → Unassigned (no ESI calls)
7. Diff each assignment against current ESI state → skip if already correct
8. Return ordered MoveList
```

**Pilot ordering within pool**: by fleet join_time ascending (longest-serving pilot matched first).

### Edge cases

| Situation | Behaviour |
|---|---|
| 6 Damnations, 5 SC slots | First 5 by join time fill SC slots. 6th matched to next generic slot as squad_member. Apply preview warns: "1 Damnation unplaced by SC rule." |
| 0 pilots match a rule slot | Slot stays empty. Apply preview: "1 slot unfilled (no match)." No ESI call. |
| Pilot matches two rules | Higher-priority (lower index) rule wins. |
| Doctrine tag condition, no doctrine | Rule inactive (greyed). Never fires. |
| Pilot already in correct position and role | Diffed out — 0 ESI calls for that pilot. |
| Pilot leaves fleet mid-apply | `fleet_esi.py` catches 404 → skip, log. Status bar: "N moves skipped (pilot left fleet)." |
| Boss lost mid-apply | `fleet_esi.py` catches 403 → raises `FleetESIError(reason="boss_lost")` → window aborts apply, shows alert. |
| Wing/squad in rule deleted | Rule flagged `broken=True` at load; skipped at apply; shown as ⚠ in Rules tab. |
| ESI role already correct | Read role from `/members/` before writing; skip role call if already matches. |
| Wing/squad doesn't exist in live fleet | `fleet_esi.py` creates it first (POST wing/squad), then moves pilots in. |

---

## 8. Apply Flow (Hybrid)

```
FC clicks [Apply Template]
    │
    ├─ compose() → MoveList
    ├─ count executable moves (skip_reason == None)
    │
    ├─ if count ≤ bulk_apply_threshold
    │     └─ execute immediately → progress in status bar
    │
    └─ if count > bulk_apply_threshold
          └─ show confirm dialog:
               "N moves required: X repositioned, Y role changes, Z unfilled ⚠"
               "ESI calls: ~N  |  Estimated time: ~T seconds"
               [Cancel]  [Apply N moves]
                   └─ on confirm → execute
```

**Execution loop** (sequential, 0.5 s between calls):
- For each `Move` in MoveList (skip_reason == None):
  - If wing/squad doesn't exist in live fleet: create via `fleet_esi.py` first
  - Call `fleet_esi.move_member(...)` 
  - Update status bar: "Moving pilots… K / N"
- On `FleetESIError(boss_lost)`: abort, alert FC
- On other `FleetESIError`: log, skip, continue
- On completion: "Applied N moves. K skipped."

---

## 9. Size-Cap Rebalancer

**Toggle**: `Rebalance: [OFF ▶]` in bottom bar. Single click. Tooltip: *"Each pilot move triggers a ~30 s session timer. Default cooldown: 45 s."*

**When ON** — `root.after` loop at `rebalance_interval_s`:

```
1. GET /fleets/{id}/members/ (1 ESI read)
2. For each squad with len(members) > max_size:
   a. Identify overflow pilots (last-joined first)
   b. Find target squad:
      i.  Under-cap squad in same wing → prefer
      ii. Least-populated squad across all wings
      iii.No under-cap squad exists → create overflow squad in same wing
   c. Queue 1 move (only 1 per tick regardless of how many squads are over cap)
3. If move_cooldown not elapsed since last write → skip this tick
4. Execute the 1 queued move
5. Status bar: "Rebalancer: moved Pilot X → Wing 2 / Squad 3"
```

**ESI call budget at defaults:**

| Source | Rate |
|---|---|
| UI sync read | 1 / 30 s |
| Rebalancer read | 1 / 60 s (shares with UI sync when aligned) |
| Rebalancer write | ≤ 1 / 45 s |
| Apply (on demand) | ~1 per pilot, one-shot |

Steady-state: ~2 reads/min + ≤ 1 write/45 s — well within ESI limits.

**Auto-disable conditions:**
- ESI returns 403 (not boss) → toggle turns OFF, alert shown
- Window closed → polling stops

---

## 10. Multiboxing Support

Named slots hold a `character` string. Characters sourced from:

1. **Characters tab** (ESI-authenticated accounts in FCTool) — shown in an autocomplete dropdown
2. **Freetext entry** + optional "Save to roster" checkbox — cached in `fleet_templates.json` under `cached_characters`

In Live mode, named slots glow green if the character is present and already in the correct position. Orange if present but in wrong position. Grey if not in fleet.

A single character name may appear in multiple templates (common for alt accounts) but only once per template (enforced by the store).

---

## 11. Doctrine Integration

When a doctrine is selected (via the existing doctrine dropdown in Fleet Management):

- The Rules tab shows doctrine tags in the condition dropdown (e.g. `Links`, `Logistics`, `DPS`)
- The `fleet_composer.py` receives the active `Doctrine` object and its `FittingsStore` reference
- Tag lookup: for each live pilot, scan the active doctrine's `members` list for any `DoctrineMember` whose fit's `ship_type_id` matches the pilot's hull; collect the union of those members' `tags`. This produces a `dict[ship_type_id → set[str]]` index built once at compose-time.
- If a pilot's hull has no matching doctrine fits: tag set is empty → no doctrine-tag rules fire for that pilot → falls through to ship-type/class rules

Doctrine tag rules are **display-greyed** with tooltip `"No doctrine active"` when `active_doctrine is None`. They are stored normally and activate automatically when a doctrine is loaded.

---

## 12. Testing

`tests/test_fleet_composer.py` (pure, no Tk, no ESI):
- Named slot exact match
- Named slot missing from fleet → slot empty, no move
- Rule priority: two rules match same pilot → first wins
- 6 Damnations / 5 SC slots → 5 placed + 1 warning
- Pilot already in correct position → skip (no move in list)
- Doctrine tag rule with no doctrine → rule inactive
- Broken rule (wing deleted) → skipped, does not crash
- Generic slot fill order
- Pilot in Unassigned when no slot/rule matches

`tests/test_fleet_template_store.py`:
- Round-trip save/load preserves all fields
- Broken rule detection at load
- `cached_characters` deduplication

---

## 13. Out of Scope (v1)

- Fleet invite (inviting pilots into the fleet via ESI) — out of scope; tool manages structure only
- Automatic fleet creation (create a new fleet from scratch via ESI)
- History / audit log of moves made
- Per-pilot notes or annotations
- Import/export templates between users
- Doctrine auto-select based on MOTD content (future integration point)
