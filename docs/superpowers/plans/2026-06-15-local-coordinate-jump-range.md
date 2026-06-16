# Local-Coordinate Jump-Range Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make jump-range / staging-range checks instant and offline by loading all New Eden system coordinates + names from a bundled static table instead of fetching them one system at a time from ESI.

**Architecture:** A new `system_coords.py` module owns an in-memory table of K-space system coordinates, names, region IDs and true-security, loaded once from a committed/bundled `system_coords.json` (generated from the Fuzzwork SDE). The two existing network choke points in `jump_range.py` (`search_system` name→id, and the position lookup inside `calculate_ly_distance`) consult this local table first and fall back to ESI only for systems not present. This drops a cold range check from several seconds of serialized ESI round-trips to sub-millisecond pure math, with zero UI changes required. A correct jump-legality predicate (Pochven/Jove/Zarzakh/highsec exclusions) and an efficient "all systems within range" scan are added on top.

**Tech Stack:** Python 3.11+ (3.13 for tests via `py -3.13 -m pytest`), stdlib only (`csv`, `json`, `math`, `gzip`, `threading`) — no numpy/scipy. PyInstaller for packaging (`FCTool.spec`). Tests: `pytest` + `pytest-mock`.

---

## Context: verified facts this plan depends on

These were researched and adversarially verified (2026-06-15). Do not re-derive — use as given.

**Light-year constant (ALREADY FIXED — committed change in working tree):** `jump_range.py:23` is now `LY_IN_METERS = 9.46e15` (CCP's official 9,460,000,000,000,000.0 m). Task 1 only adds a regression test pinning it.

**Fuzzwork SDE CSV** — `https://www.fuzzwork.co.uk/dump/latest/csv/mapSolarSystems.csv` (the `/csv/` subdirectory is mandatory; the bare path 404s). Header (26 cols, UTF-8 BOM): `regionID, constellationID, solarSystemID, solarSystemName, x, y, z, xMin … , security (col 22), factionID, radius, sunTypeID, securityClass`. Values are quoted; `x/y/z` are meters in scientific notation; `security` is full-precision true-sec (do NOT round before comparing). ~8,030 total systems, ~5,215 in K-space.

**CCP first-party alternative** (if you prefer first-party over Fuzzwork): `mapSolarSystems.jsonl` inside `https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip`. Note the shape differs: key is `_key` (=solarSystemID), coords are `position.{x,y,z}`, name is a multilingual object (`name.en`), security is `securityStatus`. This plan uses Fuzzwork CSV (matches the repo's existing `_load_stargate_graph` pattern); the generator is the only thing that would change to switch sources.

**Jump-legality predicate (all numeric IDs CONFIRMED):**
```
EXCLUDED_REGION_IDS = {10000070, 10000004, 10000017, 10000019, 10001000}
#                      Pochven   UUA-F4    J7HZ-F    A821-A    Yasna Zakh(Zarzakh region)
ZARZAKH_SYSTEM_ID   = 30100000   # true-sec -1.00 → must exclude by ID, not by security
HIGHSEC_CUTOFF      = 0.45       # true_security >= 0.45 is highsec → cyno blocked
K-space ID gate     : 30_000_000 <= system_id <= 30_999_999
```
A system is a legal capital/JF/Black-Ops jump **destination** iff: in the K-space ID range AND not Zarzakh AND its region not in `EXCLUDED_REGION_IDS` AND true-sec `< 0.45`. (Cyno-jammers are dynamic per-system state, not in static data — out of scope.)

**Codebase integration hinge (verified):** every coordinate-dependent computation funnels through `jump_range.search_system` (name→id) and the `get_system_info` position lookup. Routing (`get_stargate_route`) is already fully local (BFS over `stargate_jumps.json`). `check_range` couples a BFS route into every call (`jump_range.py:387-390`). The GUI hot loop is `_compute_range_list` (`fc_gui.py:8358-8392`) on the `do_check` worker thread (`fc_gui.py:8407`); `find_systems_in_range` has no GUI callers. Prewarm `_prewarm_cache_async` (`fc_gui.py:641`, invoked `:444`) becomes redundant.

**Packaging (verified):** `FCTool.spec` `datas=[('fire_alert.mp3', '.'), ('stargate_jumps.json', '.')]` bundles into `_MEIPASS`. `app_dir()` = writable dir next to exe (frozen) / repo dir (dev); `bundle_dir()` = `_MEIPASS` (frozen) / repo dir (dev). The correct read pattern is "try `app_dir()`, else `bundle_dir()`" (as `fc_gui.py:6128-6131` does for `fire_alert.mp3`). `stargate_jumps.json` is intentionally committed (NOT gitignored — see `.gitignore:19-21`); runtime caches (`esi_cache.json`, etc.) are gitignored. Release landmine: `dist/` holds the user's real `config.json` + ESI tokens — never zip the whole `dist/`; the new data file will also appear there after a build, so keep enumerating release-zip contents.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `system_coords.py` | **Create** | In-memory K-space coordinate/name/region/security table; position & name lookup; legality predicate; within-range scan. The new core. |
| `system_coords.json` | **Create (generated artifact, committed)** | ~5,215 K-space systems: `{id: {name, x, y, z, region_id, security}}`. Bundled into the exe via `FCTool.spec`. |
| `tools/gen_system_coords.py` | **Create** | One-off generator: download Fuzzwork CSV → filter K-space → write `system_coords.json`. Run manually to refresh after CCP expansions. |
| `jump_range.py` | **Modify** | Route `calculate_ly_distance` + `search_system` through `system_coords` first; add `range_to_targets`; add legality + `include_route` to `check_range`; fix the Fuzzwork `/csv/` URL + `bundle_dir` fallback in `_load_stargate_graph`. |
| `fc_gui.py` | **Modify** | Drop redundant `get_system_info` prewarm from `_compute_range_list`; short-circuit `_prewarm_cache_async`. |
| `FCTool.spec` | **Modify** | Add `('system_coords.json', '.')` to `datas`. |
| `.gitignore` | **Modify** | Add a comment documenting `system_coords.json` is intentionally committed. |
| `README.md` | **Modify** | Document the bundled table + how to regenerate it. |
| `tests/test_system_coords.py` | **Create** | Unit tests for the new module. |
| `tests/test_jump_range.py` | **Modify** | LY-constant regression + local-first behavior + `range_to_targets` tests. |

**Test command for every task:** `py -3.13 -m pytest <path> -v` (run from repo root; the default `python`/3.12 has no pytest).

---

### Task 1: Regression test pinning the light-year constant

The fix is already in `jump_range.py:23`. Pin it so it can never silently regress, and verify the distance math reads it.

**Files:**
- Test: `tests/test_jump_range.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_jump_range.py`:

```python
def test_ly_constant_matches_ccp_official_value():
    # CCP's official in-game light year is exactly 9,460,000,000,000,000.0 m.
    # Not the physical 9.4607e15, not the old wrong 9.4605e15.
    assert jump_range.LY_IN_METERS == 9_460_000_000_000_000.0


def test_calculate_ly_distance_uses_ly_constant(mocker):
    # Two points exactly 9.46e15 m apart on the x-axis must read as 1.00 ly.
    mocker.patch(
        "jump_range.system_coords.get_position",
        side_effect=lambda sid: {
            1: {"x": 0.0, "y": 0.0, "z": 0.0},
            2: {"x": 9.46e15, "y": 0.0, "z": 0.0},
        }.get(sid),
    )
    dist = jump_range.calculate_ly_distance(1, 2)
    assert dist == pytest.approx(1.0, abs=1e-9)
```

- [ ] **Step 2: Run it to verify the second test fails (module seam not built yet)**

Run: `py -3.13 -m pytest tests/test_jump_range.py::test_calculate_ly_distance_uses_ly_constant -v`
Expected: FAIL — `module 'jump_range' has no attribute 'system_coords'` (the local-first seam is added in Task 5). `test_ly_constant_matches_ccp_official_value` PASSES already.

- [ ] **Step 3: No implementation in this task** — the constant is already correct; the second test is intentionally written ahead of Task 5's seam.

- [ ] **Step 4: Commit the passing pin now, leave the seam test for Task 5**

Temporarily mark the seam test so the suite is green until Task 5:

```python
@pytest.mark.skip(reason="enabled in Task 5 when system_coords seam lands")
def test_calculate_ly_distance_uses_ly_constant(mocker):
    ...
```

Run: `py -3.13 -m pytest tests/test_jump_range.py -q` → Expected: all pass (one skipped).

- [ ] **Step 5: Commit**

```bash
git add tests/test_jump_range.py
git commit -m "test: pin LY_IN_METERS to CCP's 9.46e15

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Coordinate-table generator script

**Files:**
- Create: `tools/gen_system_coords.py`

- [ ] **Step 1: Write the generator**

Create `tools/gen_system_coords.py`:

```python
"""
Generate system_coords.json — the bundled New Eden coordinate table.

Downloads the Fuzzwork SDE mapSolarSystems.csv, keeps only K-space systems
(30,000,000–30,999,999), and writes a compact id->record JSON to the repo root.

Run manually to refresh after a CCP expansion that adds/moves systems:
    py -3.13 tools/gen_system_coords.py
Coordinates are part of the static universe and change very rarely.
"""
import csv
import io
import json
import os

import requests

# The /csv/ subdirectory is REQUIRED — the bare .../latest/mapSolarSystems.csv 404s.
CSV_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/mapSolarSystems.csv"
OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "system_coords.json")

K_SPACE_MIN = 30_000_000
K_SPACE_MAX = 30_999_999


def main() -> None:
    print(f"Downloading {CSV_URL} ...")
    resp = requests.get(CSV_URL, timeout=60)
    resp.raise_for_status()
    # The CSV is UTF-8 with a BOM; utf-8-sig strips it so the first column key
    # is "regionID" and not "﻿regionID".
    text = resp.content.decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(text))
    table: dict[str, dict] = {}
    for row in reader:
        sid = int(row["solarSystemID"])
        if not (K_SPACE_MIN <= sid <= K_SPACE_MAX):
            continue
        table[str(sid)] = {
            "name": row["solarSystemName"],
            "x": float(row["x"]),
            "y": float(row["y"]),
            "z": float(row["z"]),
            "region_id": int(row["regionID"]),
            "security": float(row["security"]),  # full-precision true-sec
        }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(table, f, separators=(",", ":"))
    print(f"Wrote {len(table)} K-space systems to {OUT_PATH}")
    # Sanity check: Jita must be present and read as highsec.
    jita = table.get("30000142")
    assert jita and jita["security"] >= 0.45, "Jita missing or not highsec — bad data"
    print(f"Sanity OK: Jita security={jita['security']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it to produce the committed artifact**

Run: `py -3.13 tools/gen_system_coords.py`
Expected: `Wrote 5215 K-space systems to .../system_coords.json` (count may differ slightly across SDE updates) and `Sanity OK: Jita security=0.9459…`.

- [ ] **Step 3: Verify the artifact shape**

Run:
```bash
py -3.13 -c "import json; d=json.load(open('system_coords.json')); print(len(d)); print(d['30000142'])"
```
Expected: a count near 5215 and a record like `{'name': 'Jita', 'x': ..., 'y': ..., 'z': ..., 'region_id': 10000002, 'security': 0.9459...}`.

- [ ] **Step 4: Commit the generator and the generated table**

```bash
git add tools/gen_system_coords.py system_coords.json
git commit -m "feat: add bundled New Eden coordinate table + generator

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `system_coords.py` — loader

**Files:**
- Create: `system_coords.py`
- Test: `tests/test_system_coords.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_system_coords.py`:

```python
import json

import pytest

import system_coords


def _load_fixture(monkeypatch, tmp_path, table):
    """Point system_coords at a tiny tmp table and force a clean reload."""
    path = tmp_path / "system_coords.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    monkeypatch.setattr(system_coords, "_data_path", lambda: str(path))
    # Reset module globals so _load() re-reads our fixture.
    system_coords._loaded = False
    for d in (system_coords._coords, system_coords._region_of,
              system_coords._security_of, system_coords._name_of,
              system_coords._id_of_name):
        d.clear()


FIXTURE = {
    "30000142": {"name": "Jita", "x": 1.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000002, "security": 0.9459},
    "30002187": {"name": "Amarr", "x": 2.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000043, "security": 0.9281},
}


def test_load_populates_tables(monkeypatch, tmp_path):
    _load_fixture(monkeypatch, tmp_path, FIXTURE)
    system_coords._load()
    assert system_coords._coords[30000142] == (1.0, 0.0, 0.0)
    assert system_coords._region_of[30000142] == 10000002
    assert system_coords._security_of[30000142] == pytest.approx(0.9459)
    assert system_coords._id_of_name["jita"] == 30000142


def test_load_missing_file_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(system_coords, "_data_path", lambda: None)
    system_coords._loaded = False
    system_coords._coords.clear()
    system_coords._load()  # must not raise
    assert system_coords._coords == {}
    assert system_coords._loaded is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -3.13 -m pytest tests/test_system_coords.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'system_coords'`.

- [ ] **Step 3: Write the loader**

Create `system_coords.py`:

```python
"""
Local New Eden coordinate / name / legality table.

Loads system_coords.json (bundled with the app, generated by
tools/gen_system_coords.py) once into memory. Replaces per-system ESI
position/name lookups in the jump-range hot path with pure local reads.
"""
import json
import os
import threading

from app_path import app_dir, bundle_dir

_COORDS_FILENAME = "system_coords.json"

# In-memory tables, built once by _load().
_coords: dict[int, tuple[float, float, float]] = {}   # id -> (x, y, z) meters
_region_of: dict[int, int] = {}                        # id -> region_id
_security_of: dict[int, float] = {}                    # id -> true-sec (full precision)
_name_of: dict[int, str] = {}                          # id -> canonical name
_id_of_name: dict[str, int] = {}                       # lowercased name -> id
_loaded = False
_lock = threading.Lock()


def _data_path() -> str | None:
    """Prefer a writable copy next to the exe, else the bundled read-only copy."""
    p = os.path.join(app_dir(), _COORDS_FILENAME)
    if os.path.exists(p):
        return p
    p = os.path.join(bundle_dir(), _COORDS_FILENAME)
    if os.path.exists(p):
        return p
    return None


def _load() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        path = _data_path()
        if not path:
            # No table shipped/cached: degrade gracefully — callers fall back to ESI.
            _loaded = True
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for sid_str, rec in raw.items():
                sid = int(sid_str)
                _coords[sid] = (rec["x"], rec["y"], rec["z"])
                _region_of[sid] = rec["region_id"]
                _security_of[sid] = rec["security"]
                name = rec["name"]
                _name_of[sid] = name
                _id_of_name[name.lower()] = sid
            print(f"[Coords] Loaded {len(_coords)} systems from {os.path.basename(path)}")
        except Exception as e:  # corrupt/partial file: degrade, don't crash the app
            print(f"[Coords] Failed to load {path}: {e}")
        _loaded = True
```

- [ ] **Step 4: Run to verify it passes**

Run: `py -3.13 -m pytest tests/test_system_coords.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add system_coords.py tests/test_system_coords.py
git commit -m "feat: system_coords loader for local coordinate table

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `system_coords.py` — position + name lookup

**Files:**
- Modify: `system_coords.py`
- Test: `tests/test_system_coords.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_system_coords.py`:

```python
def test_get_position_returns_esi_compatible_dict(monkeypatch, tmp_path):
    _load_fixture(monkeypatch, tmp_path, FIXTURE)
    pos = system_coords.get_position(30000142)
    assert pos == {"x": 1.0, "y": 0.0, "z": 0.0}
    assert system_coords.get_position(39999999) is None  # unknown id


def test_resolve_name_is_case_insensitive(monkeypatch, tmp_path):
    _load_fixture(monkeypatch, tmp_path, FIXTURE)
    assert system_coords.resolve_name("Jita") == 30000142
    assert system_coords.resolve_name("jita") == 30000142
    assert system_coords.resolve_name("AMARR") == 30002187
    assert system_coords.resolve_name("Nowhere") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -3.13 -m pytest tests/test_system_coords.py -k "get_position or resolve_name" -v`
Expected: FAIL — `module 'system_coords' has no attribute 'get_position'`.

- [ ] **Step 3: Implement the lookups**

Append to `system_coords.py`:

```python
def get_position(system_id: int) -> dict | None:
    """Return {"x","y","z"} (ESI-compatible shape) or None if not in the table."""
    _load()
    xyz = _coords.get(system_id)
    if xyz is None:
        return None
    return {"x": xyz[0], "y": xyz[1], "z": xyz[2]}


def resolve_name(name: str) -> int | None:
    """Exact (case-insensitive) name -> system_id, or None. No fuzzy matching."""
    _load()
    return _id_of_name.get(name.lower())
```

- [ ] **Step 4: Run to verify it passes**

Run: `py -3.13 -m pytest tests/test_system_coords.py -k "get_position or resolve_name" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add system_coords.py tests/test_system_coords.py
git commit -m "feat: system_coords position + name lookup

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Route `jump_range` distance + name resolution through the local table

This is the task that delivers the speed win: positions and name→id come from the local table; ESI is only a fallback for systems not present.

**Files:**
- Modify: `jump_range.py` (add import; rewrite `calculate_ly_distance` body; prepend local lookup to `search_system`)
- Test: `tests/test_jump_range.py` (un-skip Task 1's seam test; add local-first tests)

- [ ] **Step 1: Write the failing tests**

In `tests/test_jump_range.py`, remove the `@pytest.mark.skip` decorator added in Task 1 from `test_calculate_ly_distance_uses_ly_constant`, and append:

```python
def test_search_system_prefers_local_table_no_network(mocker):
    mocker.patch("jump_range.system_coords.resolve_name", return_value=30000142)
    post = mocker.patch("jump_range.requests.post")
    assert jump_range.search_system("Jita") == 30000142
    post.assert_not_called()  # local hit must not touch ESI


def test_search_system_falls_back_to_esi_when_not_local(mocker):
    mocker.patch("jump_range.system_coords.resolve_name", return_value=None)
    fake = mocker.MagicMock()
    fake.ok = True
    fake.json.return_value = {"systems": [{"id": 30009999, "name": "Weirdspace"}]}
    mocker.patch("jump_range.requests.post", return_value=fake)
    mocker.patch("jump_range.rate_limit")
    jump_range._system_name_cache.pop("weirdspace", None)
    assert jump_range.search_system("Weirdspace") == 30009999


def test_calculate_ly_distance_falls_back_to_esi_position(mocker):
    # Origin known locally, destination only available via ESI get_system_info.
    mocker.patch(
        "jump_range.system_coords.get_position",
        side_effect=lambda sid: {"x": 0.0, "y": 0.0, "z": 0.0} if sid == 1 else None,
    )
    mocker.patch(
        "jump_range.get_system_info",
        return_value={"position": {"x": 9.46e15, "y": 0.0, "z": 0.0}},
    )
    assert jump_range.calculate_ly_distance(1, 2) == pytest.approx(1.0, abs=1e-9)
```

- [ ] **Step 2: Run to verify they fail**

Run: `py -3.13 -m pytest tests/test_jump_range.py -k "local_table or falls_back or uses_ly_constant" -v`
Expected: FAIL — `jump_range` has no `system_coords` attribute.

- [ ] **Step 3: Add the import**

In `jump_range.py`, add to the imports near the top (after `from app_path import app_dir`):

```python
import system_coords
```

- [ ] **Step 4: Rewrite `calculate_ly_distance` to be local-first**

Replace the body of `calculate_ly_distance` (currently `jump_range.py:298-313`) with:

```python
def calculate_ly_distance(system_a_id: int, system_b_id: int) -> float | None:
    """Light-year distance between two systems. Uses the local coordinate table
    when available; falls back to ESI get_system_info for systems not in it."""
    pos_a = system_coords.get_position(system_a_id)
    if pos_a is None:
        info_a = get_system_info(system_a_id)
        pos_a = info_a.get("position") if info_a else None
    pos_b = system_coords.get_position(system_b_id)
    if pos_b is None:
        info_b = get_system_info(system_b_id)
        pos_b = info_b.get("position") if info_b else None
    if not pos_a or not pos_b:
        return None

    dx = pos_a["x"] - pos_b["x"]
    dy = pos_a["y"] - pos_b["y"]
    dz = pos_a["z"] - pos_b["z"]
    distance_m = math.sqrt(dx * dx + dy * dy + dz * dz)
    return distance_m / LY_IN_METERS
```

- [ ] **Step 5: Prepend local resolution to `search_system`**

In `search_system` (currently `jump_range.py:219`), insert at the very top of the function body, before the existing `_system_name_cache` lookup:

```python
    local_id = system_coords.resolve_name(name)
    if local_id is not None:
        return local_id
```

- [ ] **Step 6: Run the new tests + the full jump_range suite**

Run: `py -3.13 -m pytest tests/test_jump_range.py -v`
Expected: all PASS (including the previously-skipped `test_calculate_ly_distance_uses_ly_constant`).

- [ ] **Step 7: Commit**

```bash
git add jump_range.py tests/test_jump_range.py
git commit -m "feat: local-first coords + name resolution in jump_range

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `system_coords.py` — jump-legality predicate

**Files:**
- Modify: `system_coords.py`
- Test: `tests/test_system_coords.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_system_coords.py`:

```python
LEGALITY_FIXTURE = {
    "30000142": {"name": "Jita", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000002, "security": 0.9459},     # highsec -> illegal
    "30000789": {"name": "Nullhole", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000060, "security": -0.21},      # nullsec -> legal
    "30001161": {"name": "Lowhole", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000040, "security": 0.31},       # lowsec -> legal
    "30000021": {"name": "Kuharah", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000070, "security": -0.05},      # Pochven -> illegal
    "30100000": {"name": "Zarzakh", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10001000, "security": -1.0},       # Zarzakh -> illegal
    "30000444": {"name": "Edgecase", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000050, "security": 0.45},       # 0.45 == highsec -> illegal
}


def test_is_legal_jump_destination(monkeypatch, tmp_path):
    _load_fixture(monkeypatch, tmp_path, LEGALITY_FIXTURE)
    assert system_coords.is_legal_jump_destination(30000789) is True   # nullsec
    assert system_coords.is_legal_jump_destination(30001161) is True   # lowsec
    assert system_coords.is_legal_jump_destination(30000142) is False  # highsec
    assert system_coords.is_legal_jump_destination(30000021) is False  # Pochven region
    assert system_coords.is_legal_jump_destination(30100000) is False  # Zarzakh id
    assert system_coords.is_legal_jump_destination(30000444) is False  # 0.45 is highsec
    assert system_coords.is_legal_jump_destination(31000001) is False  # WH id range
    assert system_coords.is_legal_jump_destination(99999999) is False  # unknown
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -3.13 -m pytest tests/test_system_coords.py -k legal -v`
Expected: FAIL — no attribute `is_legal_jump_destination`.

- [ ] **Step 3: Implement the predicate**

Append to `system_coords.py` (constants at the top of the module-level constant block, function below the lookups):

```python
# --- Jump-drive legality (verified IDs; see plan §Context) -------------------
EXCLUDED_REGION_IDS = {
    10000070,   # Pochven       — jump drives can exit but not enter
    10000004,   # UUA-F4 (Jove) — inaccessible
    10000017,   # J7HZ-F (Jove) — inaccessible
    10000019,   # A821-A (Jove) — inaccessible
    10001000,   # Yasna Zakh    — Zarzakh's region; cyno beacon blocked
}
ZARZAKH_SYSTEM_ID = 30100000   # true-sec -1.00, so exclude by ID not security
HIGHSEC_CUTOFF = 0.45          # true_security >= 0.45 is highsec (cyno blocked)
```

```python
def is_legal_jump_destination(system_id: int) -> bool:
    """True iff a capital/JF/Black-Ops jump can land here: K-space, not Zarzakh,
    not Pochven/Jove, and lowsec/nullsec (true-sec < 0.45). Cyno-jammers are
    dynamic state and not considered here."""
    _load()
    if not (30_000_000 <= system_id <= 30_999_999):
        return False
    if system_id == ZARZAKH_SYSTEM_ID:
        return False
    region = _region_of.get(system_id)
    if region is None or region in EXCLUDED_REGION_IDS:
        return False
    sec = _security_of.get(system_id)
    if sec is None or sec >= HIGHSEC_CUTOFF:
        return False
    return True
```

- [ ] **Step 4: Run to verify it passes**

Run: `py -3.13 -m pytest tests/test_system_coords.py -k legal -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add system_coords.py tests/test_system_coords.py
git commit -m "feat: jump-legality predicate (Pochven/Jove/Zarzakh/highsec)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `system_coords.py` — efficient "systems within range" scan

**Files:**
- Modify: `system_coords.py`
- Test: `tests/test_system_coords.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_system_coords.py`:

```python
def test_systems_within_range_filters_and_sorts(monkeypatch, tmp_path):
    LY = 9.46e15
    table = {
        "30000001": {"name": "Origin", "x": 0.0, "y": 0.0, "z": 0.0,
                     "region_id": 10000060, "security": -0.2},
        "30000002": {"name": "Near", "x": 1.0 * LY, "y": 0.0, "z": 0.0,
                     "region_id": 10000060, "security": -0.2},   # 1 ly, legal
        "30000003": {"name": "Far", "x": 8.0 * LY, "y": 0.0, "z": 0.0,
                     "region_id": 10000060, "security": -0.2},   # 8 ly, out of 5 ly
        "30000004": {"name": "NearHigh", "x": 2.0 * LY, "y": 0.0, "z": 0.0,
                     "region_id": 10000002, "security": 0.9},    # 2 ly but highsec
    }
    _load_fixture(monkeypatch, tmp_path, table)

    legal = system_coords.systems_within_range(30000001, 5.0, legal_only=True)
    assert [sid for sid, _ in legal] == [30000002]  # Far out of range, NearHigh illegal

    everything = system_coords.systems_within_range(30000001, 5.0, legal_only=False)
    assert [sid for sid, _ in everything] == [30000002, 30000004]  # sorted by distance
    assert everything[0][1] == pytest.approx(1.0, abs=1e-6)
    assert everything[1][1] == pytest.approx(2.0, abs=1e-6)
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -3.13 -m pytest tests/test_system_coords.py -k within_range -v`
Expected: FAIL — no attribute `systems_within_range`.

- [ ] **Step 3: Implement the scan**

Append to `system_coords.py`:

```python
def systems_within_range(origin_id: int, range_ly: float,
                         legal_only: bool = True) -> list[tuple[int, float]]:
    """All systems within range_ly of origin_id, as (system_id, distance_ly),
    sorted nearest-first. O(n) over the ~5,200 K-space systems — a few ms,
    pure stdlib. Compares squared distance to avoid sqrt on non-matches."""
    from jump_range import LY_IN_METERS  # lazy import avoids a circular import
    _load()
    origin = _coords.get(origin_id)
    if origin is None:
        return []
    ox, oy, oz = origin
    max_m = range_ly * LY_IN_METERS
    max_m_sq = max_m * max_m

    out: list[tuple[int, float]] = []
    for sid, (x, y, z) in _coords.items():
        if sid == origin_id:
            continue
        dx = ox - x
        dy = oy - y
        dz = oz - z
        d_sq = dx * dx + dy * dy + dz * dz
        if d_sq > max_m_sq:
            continue
        if legal_only and not is_legal_jump_destination(sid):
            continue
        out.append((sid, (d_sq ** 0.5) / LY_IN_METERS))
    out.sort(key=lambda t: t[1])
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `py -3.13 -m pytest tests/test_system_coords.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add system_coords.py tests/test_system_coords.py
git commit -m "feat: systems_within_range O(n) local scan

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `range_to_targets` + legality/decoupling on `JumpRangeChecker.check_range`

Adds a pure, testable multi-target helper (what the GUI hot loop should call), surfaces `legal_destination`/`reachable`, and lets callers skip the BFS route.

**Files:**
- Modify: `jump_range.py` (`JumpRangeChecker.check_range` signature + result; add `range_to_targets`)
- Test: `tests/test_jump_range.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_jump_range.py`:

```python
def test_check_range_can_skip_route(mocker):
    mocker.patch("jump_range.search_system", side_effect=lambda n: {"a": 1, "b": 2}[n.lower()])
    mocker.patch("jump_range.calculate_ly_distance", return_value=3.0)
    mocker.patch("jump_range.system_coords.is_legal_jump_destination", return_value=True)
    route = mocker.patch("jump_range.get_stargate_route")
    checker = jump_range.JumpRangeChecker(ship_type="Dreadnought", jdc_level=5)

    result = checker.check_range("A", "B", include_route=False)
    assert result["in_range"] is True          # 3.0 <= 7.0
    assert result["legal_destination"] is True
    assert result["reachable"] is True
    assert result["gate_jumps"] is None
    route.assert_not_called()                  # route skipped


def test_range_to_targets_marks_range_and_legality(mocker):
    mocker.patch("jump_range.calculate_ly_distance",
                 side_effect=lambda o, t: {10: 3.0, 11: 9.0, 12: None}[t])
    mocker.patch("jump_range.system_coords.is_legal_jump_destination",
                 side_effect=lambda sid: sid != 11)
    checker = jump_range.JumpRangeChecker(ship_type="Dreadnought", jdc_level=5)  # 7 ly

    rows = checker.range_to_targets(1, [10, 11, 12])
    by_id = {r["system_id"]: r for r in rows}
    assert by_id[10]["in_range"] is True and by_id[10]["legal_destination"] is True
    assert by_id[11]["in_range"] is False and by_id[11]["legal_destination"] is False
    assert by_id[12]["distance_ly"] is None and by_id[12]["in_range"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -3.13 -m pytest tests/test_jump_range.py -k "skip_route or range_to_targets" -v`
Expected: FAIL — `check_range` has no `include_route` kwarg / no `range_to_targets`.

- [ ] **Step 3: Update `check_range` and add `range_to_targets`**

In `JumpRangeChecker.check_range` (currently `jump_range.py:359`), change the signature to add `include_route: bool = True`:

```python
    def check_range(self, origin: str, destination: str,
                    connections: list[str] | None = None,
                    include_route: bool = True) -> dict:
```

Inside it, after `result` is built (the existing dict), add the legality/reachable fields, and guard the route block with `include_route`. Replace the existing trailing route block:

```python
        result["legal_destination"] = system_coords.is_legal_jump_destination(dest_id)
        result["reachable"] = bool(in_range and result["legal_destination"])

        if include_route:
            route = get_stargate_route(origin_id, dest_id, connections=connections)
            result["gate_jumps"] = len(route) - 1 if route else None
        else:
            result["gate_jumps"] = None

        return result
```

Add the new method to `JumpRangeChecker` (e.g. after `check_range`):

```python
    def range_to_targets(self, origin_id: int, target_ids: list[int]) -> list[dict]:
        """Distance + in-range + legality from origin to each target id.
        Pure local when the coordinate table covers the systems. No BFS routing."""
        rng = self.jump_range
        rows: list[dict] = []
        for tid in target_ids:
            dist = calculate_ly_distance(origin_id, tid)
            rows.append({
                "system_id": tid,
                "distance_ly": round(dist, 2) if dist is not None else None,
                "in_range": dist is not None and dist <= rng,
                "legal_destination": system_coords.is_legal_jump_destination(tid),
            })
        return rows
```

- [ ] **Step 4: Run to verify it passes (and nothing regressed)**

Run: `py -3.13 -m pytest tests/test_jump_range.py -v`
Expected: all PASS. (Existing `check_range` callers keep working: `include_route` defaults to `True`.)

- [ ] **Step 5: Commit**

```bash
git add jump_range.py tests/test_jump_range.py
git commit -m "feat: range_to_targets + legality/reachable + optional route on check_range

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Simplify the GUI hot loop and prewarm

Remove the now-redundant per-system `get_system_info` prewarming (positions come from the local table). This is a mechanical removal; behavior is preserved because `calculate_ly_distance`/`search_system` self-serve locally.

**Files:**
- Modify: `fc_gui.py` — `_compute_range_list` (`:8358-8392`) and `_prewarm_cache_async` (`:641`)

> Re-read the exact current lines before editing — line numbers drift. Match on the code, not the numbers.

- [ ] **Step 1: Remove the redundant prewarm pass in `_compute_range_list`**

In `_compute_range_list`, the first loop currently resolves ids AND prewarms positions:

```python
    for sys_name in systems_list:
        sid = search_system(sys_name)
        if sid:
            sec_ids[sys_name] = sid
            get_system_info(sid)          # <-- redundant now; positions are local
    get_system_info(dest_id)              # <-- redundant now
    save_route_cache()                    # <-- nothing fetched; safe to drop here
```

Change it to just resolve ids (no position prefetch, no mid-loop cache save):

```python
    for sys_name in systems_list:
        sid = search_system(sys_name)
        if sid:
            sec_ids[sys_name] = sid
```

Leave the second loop (the one calling `calculate_ly_distance` and `get_stargate_route`) and the final `save_route_cache()` unchanged — route results are still worth persisting.

- [ ] **Step 2: Short-circuit `_prewarm_cache_async`**

In `_prewarm_cache_async`'s worker `prewarm()` (`fc_gui.py:660-666`), the position prefetch is redundant once the table loads. Replace the loop body so it only warms the local table (one cheap load) and resolves names, dropping per-system ESI position calls:

```python
    def prewarm():
        try:
            import system_coords
            system_coords._load()          # load the local table once, off the UI thread
            for name in systems:
                search_system(name)        # local-first; only ESI for unknown systems
            save_route_cache()
        except Exception:
            pass
```

- [ ] **Step 3: Manual smoke test (no unit test — these are GUI-thread internals)**

Run the app and perform a range check with several staging systems:
Run: `py -3.13 fc_gui.py`
Expected: the friendly/hostile range tables populate effectively instantly (no multi-second delay), distances match the previous behavior, and reachability is correct. Confirm a highsec staging system shows as not-reachable if/when the UI surfaces `reachable`/`legal_destination` (display wiring is optional follow-up — the data is now present in `_compute_range_list` results if you choose to show it).

- [ ] **Step 4: Run the full suite to confirm no import/regression break**

Run: `py -3.13 -m pytest -q`
Expected: all pass (474+ existing plus the new tests).

- [ ] **Step 5: Commit**

```bash
git add fc_gui.py
git commit -m "perf: drop redundant ESI prewarm now that coords are local

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Fix the latent Fuzzwork URL + bundle fallback in `_load_stargate_graph`

The stargate-graph downloader uses a URL that 404s today (missing `/csv/`), and reads `stargate_jumps.json` only from `app_dir()` even though it's bundled into `bundle_dir()`. A fresh frozen install with no loose copy would fail. Mirror the `fire_alert.mp3` fallback and fix the URL.

**Files:**
- Modify: `jump_range.py` — `_JUMPS_CACHE_FILE` resolution + `_load_stargate_graph` URL

- [ ] **Step 1: Add a bundle-aware path for the stargate file**

In `jump_range.py`, add the `bundle_dir` import (update the existing `from app_path import app_dir`):

```python
from app_path import app_dir, bundle_dir
```

Where `_JUMPS_CACHE_FILE` is defined (`jump_range.py:84`), and where `_load_stargate_graph` checks `os.path.exists(_JUMPS_CACHE_FILE)` (`:101`), make it try `app_dir()` then `bundle_dir()`. Replace the existence check at the top of the disk-load branch:

```python
        jumps_file = _JUMPS_CACHE_FILE
        if not os.path.exists(jumps_file):
            bundled = os.path.join(bundle_dir(), "stargate_jumps.json")
            if os.path.exists(bundled):
                jumps_file = bundled
        if os.path.exists(jumps_file):
            try:
                with open(jumps_file, "r") as f:
                    data = json.load(f)
```

(Keep the rest of the disk-load branch identical, just reading from `jumps_file`.)

- [ ] **Step 2: Fix the download URL**

In the download branch (`jump_range.py:116-118`), correct the URL to include `/csv/`:

```python
            resp = requests.get(
                "https://www.fuzzwork.co.uk/dump/latest/csv/mapSolarSystemJumps.csv",
                timeout=30
            )
```

- [ ] **Step 3: Verify existing route tests still pass**

The route tests mock `_load_stargate_graph` entirely, so they don't exercise the URL; run them to confirm no syntax/import regression:
Run: `py -3.13 -m pytest tests/test_jump_range.py -q`
Expected: all pass.

- [ ] **Step 4: Manual: confirm a clean download path works**

Temporarily move the cached file and confirm the corrected URL downloads (only if you want to validate the network path):
```bash
py -3.13 -c "import jump_range; jump_range._stargate_graph.clear(); jump_range._stargate_graph_loaded=False; import os; os.path.exists('stargate_jumps.json') or print('no cache'); jump_range._load_stargate_graph(); print(len(jump_range._stargate_graph), 'systems')"
```
Expected: prints a system count near 5,215 (from cache or a successful `/csv/` download).

- [ ] **Step 5: Commit**

```bash
git add jump_range.py
git commit -m "fix: correct Fuzzwork /csv/ URL and bundle_dir fallback for stargate graph

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Packaging, gitignore, and docs

**Files:**
- Modify: `FCTool.spec`, `.gitignore`, `README.md`

- [ ] **Step 1: Bundle the coordinate table into the exe**

In `FCTool.spec`, add `system_coords.json` to `datas`:

```python
    datas=[('fire_alert.mp3', '.'), ('stargate_jumps.json', '.'), ('system_coords.json', '.')],
```

- [ ] **Step 2: Document the committed-data intent in `.gitignore`**

Under the existing `stargate_jumps.json` note (`.gitignore:19-21`), add:

```
# system_coords.json is intentionally committed: it is bundled by FCTool.spec
# (a static New Eden coordinate table generated by tools/gen_system_coords.py),
# so the source must build from a clean clone.
```

(Do not add `system_coords.json` to the ignore list.)

- [ ] **Step 3: README note**

In `README.md`, near the build/data notes, add a short subsection:

```markdown
### System coordinate table

`system_coords.json` is a committed, bundled snapshot of New Eden system
coordinates/names/regions/security used for instant, offline jump-range checks.
Regenerate it after a CCP expansion that changes the universe:

    py -3.13 tools/gen_system_coords.py

Coordinates are static and change very rarely, so refreshes are infrequent.
```

- [ ] **Step 4: Verify a frozen build finds the table (optional, if PyInstaller is available)**

Run: `pyinstaller --clean --noconfirm FCTool.spec` then launch `dist/FCTool.exe` and do a range check.
Expected: works with no `system_coords.json` next to the exe (it loads from `_MEIPASS` via the `bundle_dir()` fallback). **Release reminder:** the build leaves the real `config.json` + tokens in `dist/` — when cutting a release, enumerate the zip and include only `FCTool.exe`, `config.example.json`, `SETUP.txt`.

- [ ] **Step 5: Commit**

```bash
git add FCTool.spec .gitignore README.md
git commit -m "build: bundle system_coords.json; document regeneration

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Final full-suite verification

- [ ] **Step 1: Run everything**

Run: `py -3.13 -m pytest -q`
Expected: all pass (existing 474 + new `test_system_coords.py` + added `test_jump_range.py` cases).

- [ ] **Step 2: Cold-path timing sanity (optional)**

```bash
py -3.13 -c "import time, jump_range; c=jump_range.JumpRangeChecker(); t=time.perf_counter(); r=c.check_range('Jita','Amarr', include_route=False); print(round((time.perf_counter()-t)*1000,1),'ms', r['distance_ly'],'ly', 'reachable=',r['reachable'])"
```
Expected: a few milliseconds (vs. seconds before), a sensible LY distance, and `reachable=False` (Amarr is highsec).

- [ ] **Step 3: Commit any final touch-ups, then hand off for review** (requesting-code-review skill).

---

## Out of scope (flagged, not implemented here)

- **JDC scaling formula.** `JumpRangeChecker.jump_range` uses `base * (1 + 0.25*jdc) / (1 + 0.25*5)`. Whether 0.25/level matches current EVE Jump Drive Calibration is a separate mechanics-correctness question; not touched.
- **Unifying `system_cache.py`.** Its ESI-based name→id (for autocomplete) overlaps the new table's names. A future cleanup could source autocomplete from `system_coords`, but that's a separate refactor.
- **Cyno-jammers / sov state.** Dynamic per-system data; a static table cannot represent it. Reachability here means "legal by static geography," not "no active jammer."
- **CCP first-party SDE source.** The generator uses Fuzzwork; switching to CCP's JSONL zip only changes `tools/gen_system_coords.py` (different field names: `_key`, `position.{x,y,z}`, `name.en`, `securityStatus`).

---

## Self-Review

**1. Spec coverage** — Speed fix (Tasks 5, 9 — local positions/names, drop prewarm) ✓; efficient within-range scan (Task 7) ✓; jump-legality correctness (Tasks 6, 8) ✓; LY constant (done + pinned Task 1) ✓; data source/bundling (Tasks 2, 11) ✓; the two external links' contributions — Fuzzwork CSV (Task 2 generator) and the CCP map-data rules (legality predicate Task 6, LY constant) ✓; latent URL bug surfaced by research (Task 10) ✓.

**2. Placeholder scan** — No "TBD"/"add error handling"/"similar to Task N". Every code step shows complete code; every run step shows the exact command and expected result. GUI Task 9 uses a manual smoke test (justified: `_compute_range_list`/`prewarm` are nested GUI-thread closures with no unit seam) and routes correctness through the unit-tested primitives it calls.

**3. Type consistency** — `get_position` returns `{"x","y","z"}` (Task 4) consumed identically by `calculate_ly_distance` (Task 5). `resolve_name`/`is_legal_jump_destination`/`systems_within_range` signatures are defined once and reused verbatim in Tasks 5/8/9. `range_to_targets` row keys (`system_id`, `distance_ly`, `in_range`, `legal_destination`) match between definition (Task 8) and its test. `EXCLUDED_REGION_IDS`/`ZARZAKH_SYSTEM_ID`/`HIGHSEC_CUTOFF` are defined once (Task 6) and not duplicated. The lazy `from jump_range import LY_IN_METERS` inside `systems_within_range` avoids the `jump_range → system_coords` import cycle (jump_range imports system_coords at top; system_coords imports jump_range only lazily).
