# Intelligence Tab Overhaul — Design

**Date:** 2026-04-28
**Branch:** `code-review-improvements` (or follow-up branch)
**Status:** Approved for implementation planning

## 1. Goals

Broaden the existing zKillboard Intel tab into a general-purpose Intelligence tab that ingests pasted EVE scans, classifies pilots and ships against cached standings, and surfaces actionable counts and trends to the fleet commander.

### Specifically

- Rename the tab to **Intelligence**.
- Add an unobtrusive **Paste Intel** drawer that auto-detects four scan formats: local, directional (d-scan), fleet composition, and fleet summary.
- For local scans, output friendly vs. hostile pilot counts using cached standings.
- For d-scan paste, output friendly vs. hostile ship counts. Use a pasted fleet roster as the friendly source when available; fall back to ESI fleet data when the active character is fleet boss.
- Track session-scoped trends across serial scans of the same system, scoped to a 15-minute window.

## 2. Non-goals

- No persistence of scan history across app restarts.
- No Discord, audio, or system-tray notifications for paste analysis.
- No editing of pasted scans after parse.
- No equivalent feature in the web UI (`fc_web.py`).
- No structures, deployables, drones, probes, or wrecks in the d-scan analysis. Ships only.

## 3. UI changes

### 3.1 Tab rename

- `_build_zkill_tab` becomes `_build_intel_tab`.
- Tab label changes from `"  zKillboard Intel  "` to `"  Intelligence  "`.
- Unread-alert label changes from `"  ** zKill ALERT **  "` to `"  ** Intel ALERT **  "`.
- The left-pane header `"zKillboard Intel"` stays as-is so users can still tell the live feed apart from the new paste flow.

### 3.2 Paste Intel drawer

A new collapsible strip sits between the existing filter panel and the Intelligence Fusion panel.

**Collapsed state:**
- One clickable row: `▶ Paste Intel`. No other widgets visible.

**Expanded state:**
- Header row: `▼ Paste Intel`, an auto-detected format chip (e.g., `Detected: D-Scan`), and a `Refresh Standings` button.
- A 6-row `Text` widget with placeholder hint: `Paste local, d-scan, fleet composition, or fleet summary…`
- Buttons: `Parse`, `Clear`, `Collapse`.
- A read-only result panel directly below shows the most recent analysis output.

**Persistent summary line:**
- Each successful parse also appends a one-line summary to the right-pane Intel Channels log so the result remains visible after the drawer collapses. Example:
  `[14:32] D-Scan O-BDXB — 18 hostile, 7 friendly (+4 hostile vs scan 2m ago)`

## 4. Parser

A new module `intel_paste.py` exposes one entry point:

```python
def detect_and_parse(text: str) -> ParsedScan | None
```

`ParsedScan` is a tagged union (one of `LocalScan`, `DScan`, `FleetComposition`, `FleetSummary`) carrying parsed rows plus a `raw` field for debugging.

### 4.1 Detection rules

Detection runs heuristics in this priority order. The first match wins.

1. **Fleet composition** — every non-blank line has at least 5 tab-separated fields **and** any line contains `Boss`, `Wing N`, `Squad N`, or the leadership-skill pattern `\d+\s*-\s*\d+\s*-\s*\d+`.
2. **Fleet summary** — every non-blank line has exactly 3 tab-separated fields, and the third is an integer.
3. **D-scan** — at least one line has 4 tab-separated fields and the last field ends in `AU` or `km`.
4. **Local** — fallback. Each non-blank line is a candidate character name. Lines containing digits, tabs, or non-name punctuation (other than apostrophe, hyphen, space) are dropped.

If no rule matches, `detect_and_parse` returns `None` and the chip shows `Unrecognized`. The Parse button is a no-op.

### 4.2 Parsed shapes

```python
@dataclass
class FleetMember:
    pilot: str
    system: str           # may include "(Docked)" suffix; preserved
    ship_name: str        # ship hull, e.g. "Archon"
    ship_class: str       # e.g. "Carrier"
    role: str             # "Fleet Commander (Boss)", "Squad Member", etc.
    links: str            # leadership skills, e.g. "5 - 5 - 5"
    wing_squad: str       # e.g. "Wing 1 / Squad 1" (empty for FC row)

@dataclass
class FleetSummaryRow:
    ship_name: str
    ship_class: str
    count: int

@dataclass
class DScanRow:
    type_id: int
    item_name: str        # player-set ship name
    type_name: str        # ship hull, e.g. "Vulture"
    distance_au: float | None  # None when "-" or absent

@dataclass
class LocalScan:
    pilot_names: list[str]
```

## 5. Standings cache

A new module `standings_cache.py` owns the friendly/hostile decision.

### 5.1 Sources

The cache builds from the **main selected character's** three contact lists:

- `GET /characters/{char_id}/contacts/`
- `GET /corporations/{corp_id}/contacts/`
- `GET /alliances/{alliance_id}/contacts/`

### 5.2 Storage

Disk file: `standings_cache.json` next to other caches in `app_dir()`.

```json
{
  "fetched_at": "2026-04-28T10:30:00Z",
  "source_character_id": 90143494,
  "friendly_ids": [123, 456, 789],
  "hostile_ids": [101, 202]
}
```

`friendly_ids` holds every entity ID with standing greater than zero. `hostile_ids` holds every entity ID with standing less than zero. Zero standings are dropped (neutral counts as hostile per the friendly check below).

### 5.3 Refresh policy

- On app start, load the cache from disk.
- If the cache is older than 24 hours, schedule a background refresh.
- The drawer's `Refresh Standings` button forces an immediate refresh.
- A status label inside the drawer shows the cache age (e.g., `Standings: 3h old`).

### 5.4 Friendly check

```python
def is_friendly(
    char_id: int | None,
    corp_id: int | None,
    alliance_id: int | None,
    own_character_ids: set[int],
) -> bool
```

Returns `True` when any of:

1. `char_id` is in `own_character_ids` (the user's managed SSO characters).
2. Any of `char_id`, `corp_id`, `alliance_id` is in `friendly_ids`.

Returns `False` otherwise. Neutral and unknown count as hostile per Q1 of the brainstorm.

## 6. Local-scan analysis

1. Resolve all pasted names to character IDs in one batch via `POST /universe/ids/` (up to 1000 names per call). Names that fail to resolve go into an `unresolved` bucket.
2. Resolve each character's corp and alliance via `POST /characters/affiliation/` (up to 1000 ids per call). Cache results by character ID for the session.
3. For each pilot, call `is_friendly(char_id, corp_id, alliance_id, own_character_ids)`.
4. Produce a result block:

```
Local — 47 pilots
  Friendly: 12
  Hostile:  33
  Unresolved: 2
  Top hostile affiliations:
    Pandemic Horde × 18
    Goonswarm Federation × 9
    Black Legion × 4
```

The result panel shows the first ~5 hostile pilot names with their affiliation; a "Show all" link expands to the full list.

## 7. D-scan analysis

### 7.1 Filter to ships

After parse, drop every row whose `type_id` is not a ship type. Use `ship_classes.is_ship_type(type_id)` (new helper); fall back to `/universe/types/{id}/` group lookup, cached on disk.

### 7.2 Friendly source priority

Pick the first available source:

1. **Pasted fleet roster** — the most recent fleet-composition or fleet-summary paste in the session.
2. **ESI fleet data** — only if the active SSO character is fleet boss. Verify via `/characters/{id}/fleet/` and check `role == "fleet_commander"`.
3. **Neither** — output ship counts only, with a note explaining how to enable the breakdown.

### 7.3 Comparison

- Count d-scan ships by type.
- Count friendly ships by type from the chosen source.
- Subtract: `hostile_count_by_type[t] = max(0, dscan[t] - friendly[t])`.
- Sum into totals.

### 7.4 Output

```
D-Scan — 25 ships in range
  Friendly (from pasted fleet, 4m old): 7
  Hostile (estimate):                   18
    Hurricane × 2
    Sabre × 3
    Damnation × 1
    …
⚠ Other friendly fleets in system would inflate the hostile count.
⚠ Pasted roster is 4m old. Refresh if it has changed.
```

Three banners can appear, each independently:

1. **Multi-fleet caveat** — always shown when a friendly source exists. Reminds the FC that the type-subtraction approach can't distinguish other friendly groups in system.
2. **Stale-roster warning** — shown when the chosen friendly source is older than 5 minutes.
3. **No-source note** — shown when neither a pasted roster nor fleet-boss ESI access is available. Replaces the friendly/hostile breakdown with a ships-only count and a one-line hint: `No fleet roster: paste a fleet composition, or be fleet boss to use ESI.`

## 8. Trend tracking

### 8.1 State

```python
@dataclass
class IntelSession:
    local_scans: list[tuple[datetime, str, LocalScan]]       # (when, system, parsed)
    dscan_scans: list[tuple[datetime, str, DScan]]
    fleet_pastes: list[tuple[datetime, FleetComposition | FleetSummary]]
```

`MainWindow` owns one `IntelSession` instance. Cleared by the drawer's `Clear` button or by closing the app.

### 8.2 System inference for trends

D-scans don't carry an explicit system. The system is inferred by, in order:

1. The active SSO character's location at parse time (`get_location()`).
2. The most recent local-scan system (if any) within the last 60 seconds.
3. Otherwise, treated as `"unknown"` and excluded from trend matching.

### 8.3 Trend output

When a new d-scan arrives in system `S`, find the most recent prior d-scan in `S` within 15 minutes. If found, append a trend block:

```
Trend (vs scan 2m ago):
  Hostile: 12 → 18 (+6)
  +3 Hurricane, +2 Sabre, +1 Damnation (new), -1 Stiletto
```

For local scans, the trend block shows pilot-count delta only (no type breakdown applies).

Beyond 15 minutes, the new scan becomes a fresh baseline with no trend.

## 9. ESI helpers to add

In `esi_auth.py`:

- `resolve_names_to_ids(names: list[str]) -> dict[str, int]` — wraps `POST /universe/ids/`. Batches up to 1000.
- `get_affiliations(char_ids: list[int]) -> list[dict]` — wraps `POST /characters/affiliation/`. Batches up to 1000.
- `get_personal_contacts() -> list[dict]` — wraps the personal contacts endpoint.
- `get_corp_contacts() -> list[dict]` — wraps the corp contacts endpoint. Resolves the corp ID from `/characters/{id}/`.
- `get_alliance_contacts() -> list[dict]` — wraps the alliance contacts endpoint. Resolves the alliance ID from `/characters/{id}/`.
- `is_fleet_boss() -> bool` — checks whether the active character's `/fleet/` response carries `role == "fleet_commander"`.

In `ship_classes.py`:

- `is_ship_type(type_id: int) -> bool` — predicate using the existing classification data plus an ESI group lookup with disk-backed cache.

## 10. Files

### New

- `intel_paste.py` — parser, format detection, dataclasses.
- `standings_cache.py` — fetch, persist, look up standings.
- `tests/test_intel_paste.py` — fixture-driven parser tests for all four formats and edge cases.
- `tests/test_standings_cache.py` — load/save round-trip and `is_friendly` tests.

### Modified

- `fc_gui.py` — rename tab and method, add drawer UI, wire paste → parse → analyze → display.
- `esi_auth.py` — add the helpers above; add the contacts and standings scopes if missing.
- `ship_classes.py` — add `is_ship_type()`.

## 11. Testing approach

- **Unit tests for the parser** — fixture files in `tests/fixtures/intel/` covering each format, plus malformed inputs.
- **Unit tests for the standings cache** — round-trip save/load, friendly/hostile lookup against synthetic standings, refresh-on-stale logic.
- **Manual test plan** for the GUI integration:
  1. Paste a local scan; verify counts and unresolved bucket.
  2. Paste a d-scan with no fleet roster → ships-only output.
  3. Paste a fleet-composition, then a d-scan → friendly/hostile breakdown.
  4. Paste a second d-scan in the same system within 15 minutes → trend block appears.
  5. Paste a second d-scan after 15 minutes → no trend, fresh baseline.
  6. Click `Refresh Standings` → cache age label updates.
  7. Click `Clear` → result panel and session state empty.

## 12. Open assumptions

- The user's "main selected character" for standings is `config["tracked_character"]`, falling back to the first SSO character in the session if blank.
- Standings comparisons use the value as ESI returns it (any value greater than zero is friendly, any value less than zero is hostile). No threshold tuning in this pass.
- The fleet-composition paste format matches the sample provided in the brainstorm. Other dialects (e.g., from third-party tools) are out of scope until users report them.

## 13. Acceptance criteria

The feature is complete when:

1. The tab title reads `Intelligence` and unread alerts read `** Intel ALERT **`.
2. The Paste Intel drawer collapses and expands without disrupting the live zKill feed.
3. All four scan formats parse correctly from the brainstorm samples.
4. Local-scan output shows friendly vs. hostile counts using cached standings.
5. D-scan output uses the pasted fleet roster when available, falls back to ESI when fleet boss, and degrades gracefully otherwise.
6. Trend deltas appear for serial same-system scans within 15 minutes.
7. All new unit tests pass.
8. Existing tests still pass.
