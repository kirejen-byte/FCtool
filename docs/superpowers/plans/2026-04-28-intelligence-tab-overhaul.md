# Intelligence Tab Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the zKillboard Intel tab to Intelligence, add a collapsible Paste Intel drawer that auto-detects local/d-scan/fleet pastes, and use a cached standings list to drive friendly vs. hostile counts and same-system trends.

**Architecture:** Three new pure-Python modules (`intel_paste.py`, `standings_cache.py`, plus an `intel_analyzer.py` for orchestration) feed a single new UI drawer in `fc_gui.py`. Parsing and analysis are pure functions tested in isolation; the cache and ESI helpers are stateful but mockable; the GUI wiring is verified by a manual smoke test.

**Tech Stack:** Python 3.11+, Tkinter, pytest, pytest-mock, EVE ESI (REST + OAuth via `esi_auth.py`).

**Spec:** [docs/superpowers/specs/2026-04-28-intelligence-tab-overhaul-design.md](../specs/2026-04-28-intelligence-tab-overhaul-design.md)

---

## File Structure

**New files:**
- `intel_paste.py` — paste-format dataclasses, format auto-detection, four format parsers, and the `detect_and_parse` entry point.
- `standings_cache.py` — fetch standings from ESI, persist to disk, expose `is_friendly()`.
- `intel_analyzer.py` — pure-function analyzers that consume `ParsedScan` plus context (standings cache, fleet roster, session state) and return rendered result strings.
- `intel_session.py` — in-memory session container that holds recent local-scans, d-scans, and fleet pastes.
- `tests/test_intel_paste.py` — parser tests for all four formats plus detection edges.
- `tests/test_standings_cache.py` — load/save round-trip and `is_friendly` lookup.
- `tests/test_intel_analyzer.py` — local-scan, d-scan, and trend analyzer tests.
- `tests/test_intel_session.py` — session state add/clear/window tests.
- `tests/fixtures/intel/` — text fixture files (one per format).

**Modified files:**
- `esi_auth.py` — add bulk-name resolver, affiliations, contacts (personal/corp/alliance), and `is_fleet_boss`.
- `ship_classes.py` — add `is_ship_type(type_id)` predicate.
- `fc_gui.py` — rename `_build_zkill_tab` → `_build_intel_tab`, change tab labels, add Paste Intel drawer and its handlers.

Each module owns one responsibility. The parser does not know about ESI; the analyzer does not know about the GUI; the cache does not know about parsing. Tests can run any module in isolation with mocks.

---

## Task 1: Test fixtures and parser dataclasses

**Files:**
- Create: `intel_paste.py`
- Create: `tests/fixtures/intel/local_scan.txt`
- Create: `tests/fixtures/intel/dscan.txt`
- Create: `tests/fixtures/intel/fleet_composition.txt`
- Create: `tests/fixtures/intel/fleet_summary.txt`
- Create: `tests/test_intel_paste.py`

- [ ] **Step 1: Create the fixture files**

`tests/fixtures/intel/local_scan.txt`:
```
Securitas Protector
Tyreece Arkan
Nessa Volkov
RandomEnemy123
```

`tests/fixtures/intel/dscan.txt`:
```
12345	Ragnar's Vulture	Vulture	5.2 AU
12346	Wrecking Ball	Loki	3.1 AU
12347	Probe	Combat Scanner Probe I	14.3 AU
12348	A Citadel	Astrahus	9.8 AU
```

`tests/fixtures/intel/fleet_composition.txt` (use literal tab characters between fields):
```
Securitas Protector	O-BDXB (Docked)	Archon	Carrier	Fleet Commander (Boss)	5 - 5 - 5	
Tyreece Arkan	C-N4OD (Docked)	Flycatcher	Interdictor	Squad Member	0 - 4 - 5	Wing 1 / Squad 1
```

`tests/fixtures/intel/fleet_summary.txt`:
```
Flycatcher	Interdictor	1
Archon	Carrier	1
```

- [ ] **Step 2: Write the dataclasses skeleton in `intel_paste.py`**

```python
"""
Paste-format detection and parsing for the Intelligence tab.

Supports four EVE Online paste formats:
- Local scan (one character name per line)
- Directional scan (tab-separated: type_id, item_name, type_name, distance)
- Fleet composition (tab-separated, includes leadership-skill column)
- Fleet summary (tab-separated: ship_name, ship_class, count)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LocalScan:
    pilot_names: list[str]


@dataclass
class DScanRow:
    type_id: int
    item_name: str
    type_name: str
    distance_au: float | None  # None when distance is "-" or absent


@dataclass
class DScan:
    rows: list[DScanRow]


@dataclass
class FleetMember:
    pilot: str
    system: str
    ship_name: str
    ship_class: str
    role: str
    links: str
    wing_squad: str


@dataclass
class FleetComposition:
    members: list[FleetMember]


@dataclass
class FleetSummaryRow:
    ship_name: str
    ship_class: str
    count: int


@dataclass
class FleetSummary:
    rows: list[FleetSummaryRow]


ParsedScan = LocalScan | DScan | FleetComposition | FleetSummary


def detect_and_parse(text: str) -> ParsedScan | None:
    """Auto-detect format and parse. Returns None for unrecognized input."""
    raise NotImplementedError
```

- [ ] **Step 3: Write the test bootstrap**

`tests/test_intel_paste.py`:
```python
import os

import pytest

from intel_paste import (
    DScan,
    FleetComposition,
    FleetSummary,
    LocalScan,
    detect_and_parse,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "intel")


def _read(name: str) -> str:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_dataclasses_exist():
    assert LocalScan is not None
    assert DScan is not None
    assert FleetComposition is not None
    assert FleetSummary is not None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_intel_paste.py -v`
Expected: 1 PASS (the smoke test).

- [ ] **Step 5: Commit**

```bash
git add intel_paste.py tests/test_intel_paste.py tests/fixtures/intel/
git commit -m "Bootstrap intel_paste module with format dataclasses and fixtures"
```

---

## Task 2: Local-scan parser

**Files:**
- Modify: `intel_paste.py`
- Modify: `tests/test_intel_paste.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_intel_paste.py`:
```python
def test_parse_local_scan_basic():
    from intel_paste import parse_local_scan
    text = "Securitas Protector\nTyreece Arkan\nNessa Volkov\n"
    result = parse_local_scan(text)
    assert isinstance(result, LocalScan)
    assert result.pilot_names == ["Securitas Protector", "Tyreece Arkan", "Nessa Volkov"]


def test_parse_local_scan_skips_blank_lines():
    from intel_paste import parse_local_scan
    text = "Alice\n\n\nBob\n"
    result = parse_local_scan(text)
    assert result.pilot_names == ["Alice", "Bob"]


def test_parse_local_scan_rejects_lines_with_digits():
    from intel_paste import parse_local_scan
    text = "Alice\nRandomEnemy123\nBob\n"
    result = parse_local_scan(text)
    assert result.pilot_names == ["Alice", "Bob"]


def test_parse_local_scan_keeps_apostrophes_and_hyphens():
    from intel_paste import parse_local_scan
    text = "O'Reilly\nJean-Luc\n"
    result = parse_local_scan(text)
    assert result.pilot_names == ["O'Reilly", "Jean-Luc"]


def test_parse_local_scan_from_fixture():
    from intel_paste import parse_local_scan
    result = parse_local_scan(_read("local_scan.txt"))
    assert "Securitas Protector" in result.pilot_names
    assert "RandomEnemy123" not in result.pilot_names  # has digits
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_paste.py -v`
Expected: 4 FAIL (`parse_local_scan` not defined), 1 PASS.

- [ ] **Step 3: Implement `parse_local_scan`**

Add to `intel_paste.py`:
```python
import re

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z' \-]{0,36}[A-Za-z]$")


def parse_local_scan(text: str) -> LocalScan:
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _NAME_RE.match(line):
            names.append(line)
    return LocalScan(pilot_names=names)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_intel_paste.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_paste.py tests/test_intel_paste.py
git commit -m "Add local-scan parser to intel_paste"
```

---

## Task 3: D-scan parser

**Files:**
- Modify: `intel_paste.py`
- Modify: `tests/test_intel_paste.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_intel_paste.py`:
```python
def test_parse_dscan_basic():
    from intel_paste import parse_dscan
    text = "12345\tRagnar's Vulture\tVulture\t5.2 AU\n"
    result = parse_dscan(text)
    assert isinstance(result, DScan)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.type_id == 12345
    assert row.item_name == "Ragnar's Vulture"
    assert row.type_name == "Vulture"
    assert row.distance_au == pytest.approx(5.2)


def test_parse_dscan_handles_km():
    from intel_paste import parse_dscan
    text = "999\tThing\tHurricane\t48,231 km\n"
    result = parse_dscan(text)
    assert result.rows[0].distance_au == pytest.approx(48231 / 149_597_870.7, rel=1e-3)


def test_parse_dscan_handles_dash_distance():
    from intel_paste import parse_dscan
    text = "1\tA\tB\t-\n"
    result = parse_dscan(text)
    assert result.rows[0].distance_au is None


def test_parse_dscan_skips_malformed_lines():
    from intel_paste import parse_dscan
    text = "12345\tA\tHurricane\t1.0 AU\nbroken line\n67890\tB\tSabre\t2.0 AU\n"
    result = parse_dscan(text)
    assert len(result.rows) == 2
    assert result.rows[1].type_id == 67890


def test_parse_dscan_from_fixture():
    from intel_paste import parse_dscan
    result = parse_dscan(_read("dscan.txt"))
    assert len(result.rows) == 4
    assert result.rows[0].type_name == "Vulture"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_paste.py -v -k dscan`
Expected: 5 FAIL (`parse_dscan` not defined).

- [ ] **Step 3: Implement `parse_dscan`**

Add to `intel_paste.py`:
```python
_KM_PER_AU = 149_597_870.7


def _parse_distance(token: str) -> float | None:
    token = token.strip()
    if not token or token == "-":
        return None
    cleaned = token.replace(",", "").replace(" ", "")
    if cleaned.endswith("AU"):
        try:
            return float(cleaned[:-2])
        except ValueError:
            return None
    if cleaned.endswith("km"):
        try:
            return float(cleaned[:-2]) / _KM_PER_AU
        except ValueError:
            return None
    if cleaned.endswith("m"):
        try:
            return float(cleaned[:-1]) / (_KM_PER_AU * 1000)
        except ValueError:
            return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_dscan(text: str) -> DScan:
    rows: list[DScanRow] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n").rstrip("\r")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        try:
            type_id = int(parts[0].strip())
        except ValueError:
            continue
        rows.append(DScanRow(
            type_id=type_id,
            item_name=parts[1].strip(),
            type_name=parts[2].strip(),
            distance_au=_parse_distance(parts[3]),
        ))
    return DScan(rows=rows)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_intel_paste.py -v`
Expected: 10 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_paste.py tests/test_intel_paste.py
git commit -m "Add d-scan parser to intel_paste"
```

---

## Task 4: Fleet-composition parser

**Files:**
- Modify: `intel_paste.py`
- Modify: `tests/test_intel_paste.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_intel_paste.py`:
```python
def test_parse_fleet_composition_basic():
    from intel_paste import parse_fleet_composition
    text = (
        "Securitas Protector\tO-BDXB (Docked)\tArchon\tCarrier\t"
        "Fleet Commander (Boss)\t5 - 5 - 5\t\n"
        "Tyreece Arkan\tC-N4OD (Docked)\tFlycatcher\tInterdictor\t"
        "Squad Member\t0 - 4 - 5\tWing 1 / Squad 1\n"
    )
    result = parse_fleet_composition(text)
    assert isinstance(result, FleetComposition)
    assert len(result.members) == 2
    boss = result.members[0]
    assert boss.pilot == "Securitas Protector"
    assert boss.system == "O-BDXB (Docked)"
    assert boss.ship_name == "Archon"
    assert boss.ship_class == "Carrier"
    assert boss.role == "Fleet Commander (Boss)"
    assert boss.links == "5 - 5 - 5"
    assert boss.wing_squad == ""
    member = result.members[1]
    assert member.wing_squad == "Wing 1 / Squad 1"


def test_parse_fleet_composition_handles_trailing_tab():
    from intel_paste import parse_fleet_composition
    text = "Alice\tJita\tRifter\tFrigate\tFleet Commander (Boss)\t5 - 5 - 5\t\t\n"
    result = parse_fleet_composition(text)
    assert len(result.members) == 1
    assert result.members[0].wing_squad == ""


def test_parse_fleet_composition_from_fixture():
    from intel_paste import parse_fleet_composition
    result = parse_fleet_composition(_read("fleet_composition.txt"))
    assert len(result.members) == 2
    assert result.members[0].ship_name == "Archon"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_paste.py -v -k fleet_composition`
Expected: 3 FAIL.

- [ ] **Step 3: Implement `parse_fleet_composition`**

Add to `intel_paste.py`:
```python
def parse_fleet_composition(text: str) -> FleetComposition:
    members: list[FleetMember] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.rstrip("\n").rstrip("\r").split("\t")
        if len(parts) < 6:
            continue
        # Pad to at least 7 fields so wing_squad is always defined
        while len(parts) < 7:
            parts.append("")
        members.append(FleetMember(
            pilot=parts[0].strip(),
            system=parts[1].strip(),
            ship_name=parts[2].strip(),
            ship_class=parts[3].strip(),
            role=parts[4].strip(),
            links=parts[5].strip(),
            wing_squad=parts[6].strip(),
        ))
    return FleetComposition(members=members)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_intel_paste.py -v`
Expected: 13 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_paste.py tests/test_intel_paste.py
git commit -m "Add fleet-composition parser to intel_paste"
```

---

## Task 5: Fleet-summary parser

**Files:**
- Modify: `intel_paste.py`
- Modify: `tests/test_intel_paste.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_intel_paste.py`:
```python
def test_parse_fleet_summary_basic():
    from intel_paste import parse_fleet_summary
    text = "Flycatcher\tInterdictor\t1\nArchon\tCarrier\t1\n"
    result = parse_fleet_summary(text)
    assert isinstance(result, FleetSummary)
    assert len(result.rows) == 2
    assert result.rows[0].ship_name == "Flycatcher"
    assert result.rows[0].ship_class == "Interdictor"
    assert result.rows[0].count == 1


def test_parse_fleet_summary_skips_non_integer_count():
    from intel_paste import parse_fleet_summary
    text = "Flycatcher\tInterdictor\t1\nGarbage\tRow\tnot-a-number\n"
    result = parse_fleet_summary(text)
    assert len(result.rows) == 1


def test_parse_fleet_summary_from_fixture():
    from intel_paste import parse_fleet_summary
    result = parse_fleet_summary(_read("fleet_summary.txt"))
    assert len(result.rows) == 2
    assert result.rows[1].ship_name == "Archon"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_paste.py -v -k fleet_summary`
Expected: 3 FAIL.

- [ ] **Step 3: Implement `parse_fleet_summary`**

Add to `intel_paste.py`:
```python
def parse_fleet_summary(text: str) -> FleetSummary:
    rows: list[FleetSummaryRow] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.rstrip("\n").rstrip("\r").split("\t")
        if len(parts) != 3:
            continue
        try:
            count = int(parts[2].strip())
        except ValueError:
            continue
        rows.append(FleetSummaryRow(
            ship_name=parts[0].strip(),
            ship_class=parts[1].strip(),
            count=count,
        ))
    return FleetSummary(rows=rows)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_intel_paste.py -v`
Expected: 16 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_paste.py tests/test_intel_paste.py
git commit -m "Add fleet-summary parser to intel_paste"
```

---

## Task 6: Format auto-detection (`detect_and_parse`)

**Files:**
- Modify: `intel_paste.py`
- Modify: `tests/test_intel_paste.py`

- [ ] **Step 1: Write failing detection tests**

Append to `tests/test_intel_paste.py`:
```python
def test_detect_local_scan():
    result = detect_and_parse(_read("local_scan.txt"))
    assert isinstance(result, LocalScan)


def test_detect_dscan():
    result = detect_and_parse(_read("dscan.txt"))
    assert isinstance(result, DScan)
    assert len(result.rows) == 4


def test_detect_fleet_composition():
    result = detect_and_parse(_read("fleet_composition.txt"))
    assert isinstance(result, FleetComposition)
    assert len(result.members) == 2


def test_detect_fleet_summary():
    result = detect_and_parse(_read("fleet_summary.txt"))
    assert isinstance(result, FleetSummary)
    assert len(result.rows) == 2


def test_detect_unrecognized_returns_none():
    assert detect_and_parse("") is None
    assert detect_and_parse("   \n\n  ") is None


def test_detect_priority_fleet_summary_over_dscan_when_three_cols():
    # Three tab-separated cols with integer last field — fleet summary
    text = "Flycatcher\tInterdictor\t1\nArchon\tCarrier\t2\n"
    assert isinstance(detect_and_parse(text), FleetSummary)


def test_detect_priority_fleet_composition_over_others():
    text = (
        "Alice\tJita\tRifter\tFrigate\tFleet Commander (Boss)\t5 - 5 - 5\t\n"
        "Bob\tAmarr\tRifter\tFrigate\tSquad Member\t0 - 4 - 5\tWing 1 / Squad 1\n"
    )
    assert isinstance(detect_and_parse(text), FleetComposition)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_paste.py -v -k detect`
Expected: 7 FAIL (NotImplementedError or similar).

- [ ] **Step 3: Implement `detect_and_parse`**

Replace the stub `detect_and_parse` in `intel_paste.py`:
```python
import re as _re

_LEADERSHIP_RE = _re.compile(r"\d+\s*-\s*\d+\s*-\s*\d+")
_FLEET_KEYWORDS = ("Boss", "Wing ", "Squad ")


def _looks_like_fleet_composition(non_blank: list[str]) -> bool:
    if not non_blank:
        return False
    if not all(len(line.split("\t")) >= 5 for line in non_blank):
        return False
    return any(
        any(kw in line for kw in _FLEET_KEYWORDS) or _LEADERSHIP_RE.search(line)
        for line in non_blank
    )


def _looks_like_fleet_summary(non_blank: list[str]) -> bool:
    if not non_blank:
        return False
    for line in non_blank:
        parts = line.split("\t")
        if len(parts) != 3:
            return False
        try:
            int(parts[2].strip())
        except ValueError:
            return False
    return True


def _looks_like_dscan(non_blank: list[str]) -> bool:
    for line in non_blank:
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        last = parts[3].strip()
        if last.endswith("AU") or last.endswith("km") or last == "-":
            return True
    return False


def detect_and_parse(text: str) -> ParsedScan | None:
    non_blank = [
        ln.rstrip("\r") for ln in text.splitlines()
        if ln.strip()
    ]
    if not non_blank:
        return None
    if _looks_like_fleet_composition(non_blank):
        return parse_fleet_composition(text)
    if _looks_like_fleet_summary(non_blank):
        return parse_fleet_summary(text)
    if _looks_like_dscan(non_blank):
        return parse_dscan(text)
    parsed = parse_local_scan(text)
    if parsed.pilot_names:
        return parsed
    return None
```

- [ ] **Step 4: Run all parser tests**

Run: `pytest tests/test_intel_paste.py -v`
Expected: 23 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_paste.py tests/test_intel_paste.py
git commit -m "Add format auto-detection to intel_paste"
```

---

## Task 7: `ship_classes.is_ship_type` predicate

**Files:**
- Modify: `ship_classes.py`
- Create: `tests/test_ship_classes.py`

- [ ] **Step 1: Write failing tests**

`tests/test_ship_classes.py`:
```python
import pytest

import ship_classes
from ship_classes import is_ship_type


def test_known_capital_is_ship():
    # Archon (carrier) — present in CAPITAL_CLASSES
    assert is_ship_type(23757) is True


def test_known_command_ship_is_ship():
    # Vulture
    assert is_ship_type(22448) is True


def test_known_logistics_is_ship():
    # Guardian
    assert is_ship_type(11987) is True


def test_known_destroyer_via_classify(monkeypatch):
    # classify_ship returns "Destroyer" for type_id in DESTROYERS / T3D — those are ships
    monkeypatch.setattr(ship_classes, "classify_ship", lambda tid: "Destroyer" if tid == 16236 else None)
    assert is_ship_type(16236) is True


def test_non_ship_returns_false(monkeypatch):
    # Astrahus (citadel) — group 1657 is structure, not a ship
    fake_response = {"group_id": 1657}
    monkeypatch.setattr(ship_classes, "_fetch_group_id_for_type", lambda tid: 1657)
    assert is_ship_type(35832) is False


def test_unknown_falls_back_to_group_lookup(monkeypatch):
    monkeypatch.setattr(ship_classes, "_fetch_group_id_for_type", lambda tid: 25)  # Frigate
    assert is_ship_type(587) is True  # Rifter, not in any hardcoded set


def test_unknown_with_no_group_returns_false(monkeypatch):
    monkeypatch.setattr(ship_classes, "_fetch_group_id_for_type", lambda tid: None)
    assert is_ship_type(99999999) is False
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_ship_classes.py -v`
Expected: 7 FAIL (`is_ship_type` not defined).

- [ ] **Step 3: Implement `is_ship_type` in `ship_classes.py`**

Read the existing file first to see what's there. Find the bottom of the file (after the `classify_ship` function) and append:

```python
# ── Ship-type predicate ─────────────────────────────────────────────────────

# Ship category in the EVE SDE is category_id == 6.
# We reach the category via the type's group_id. Group IDs we already trust
# as ships are the ones used in `classify_ship` plus the tackle groups.
_SHIP_GROUP_IDS_KNOWN: set[int] = {
    GROUP_COMMAND_SHIPS,
    GROUP_COMMAND_DESTROYERS,
    GROUP_LOGISTICS_CRUISERS,
    GROUP_LOGISTICS_FRIGATES,
    GROUP_DESTROYERS,
    GROUP_TACTICAL_DESTROYERS,
    GROUP_INTERDICTORS,
    GROUP_FRIGATE,
    GROUP_ASSAULT_FRIGATE,
    GROUP_INTERCEPTOR,
    GROUP_ELECTRONIC_ATTACK_SHIP,
}

# Cache for ESI group-id lookups
_type_group_cache: dict[int, int | None] = {}
_type_group_lock = threading.Lock()


def _fetch_group_id_for_type(type_id: int) -> int | None:
    """Resolve a type_id to its group_id via ESI, with in-memory caching."""
    with _type_group_lock:
        if type_id in _type_group_cache:
            return _type_group_cache[type_id]
    try:
        resp = requests.get(
            f"https://esi.evetech.net/latest/universe/types/{type_id}/",
            timeout=5,
            headers={"User-Agent": "FCTool/1.0 (EVE FC Assistant)"},
        )
        if resp.ok:
            gid = resp.json().get("group_id")
            with _type_group_lock:
                _type_group_cache[type_id] = gid
            return gid
    except Exception:
        pass
    with _type_group_lock:
        _type_group_cache[type_id] = None
    return None


# Hardcoded ship type_ids we already classify
_KNOWN_SHIP_TYPE_IDS: set[int] = (
    COMMAND_SHIPS
    | COMMAND_DESTROYERS
    | LOGISTICS_CRUISERS
    | LOGISTICS_FRIGATES
)
for _cap_set in CAPITAL_CLASSES.values():
    _KNOWN_SHIP_TYPE_IDS |= _cap_set


def is_ship_type(type_id: int) -> bool:
    """Return True if type_id is a ship hull, False for structures/drones/etc."""
    if type_id in _KNOWN_SHIP_TYPE_IDS:
        return True
    # classify_ship returns a non-None label for known ship classes
    if classify_ship(type_id) is not None:
        return True
    gid = _fetch_group_id_for_type(type_id)
    if gid is None:
        return False
    return gid in _SHIP_GROUP_IDS_KNOWN
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ship_classes.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add ship_classes.py tests/test_ship_classes.py
git commit -m "Add is_ship_type predicate to ship_classes"
```

---

## Task 8: ESI bulk helpers (names, affiliations, contacts, fleet boss)

**Files:**
- Modify: `esi_auth.py`
- Modify: `tests/test_esi_auth.py`

- [ ] **Step 1: Read existing ESI patterns**

```bash
grep -nE "def get_|esi_get|esi_post" esi_auth.py | head
```

Confirm `esi_get` exists; identify whether `esi_post` exists. (If not, add it as part of this task.)

- [ ] **Step 2: Write failing tests**

Append to `tests/test_esi_auth.py`:
```python
class _FakePostResponse(_FakeResponse):
    pass


def test_resolve_names_to_ids_batches(monkeypatch):
    auth = ESIAuth(client_id="x", client_secret="y", callback_url="z", token_file="/tmp/t.json")
    captured: list[list[str]] = []

    def fake_post(path, body, **kw):
        captured.append(list(body))
        return {
            "characters": [{"name": n, "id": 1000 + i} for i, n in enumerate(body)]
        }

    monkeypatch.setattr(auth, "esi_post", fake_post, raising=False)
    out = auth.resolve_names_to_ids(["Alice", "Bob"])
    assert out == {"Alice": 1000, "Bob": 1001}
    assert captured == [["Alice", "Bob"]]


def test_resolve_names_to_ids_chunks_over_1000(monkeypatch):
    auth = ESIAuth(client_id="x", client_secret="y", callback_url="z", token_file="/tmp/t.json")
    seen_batches: list[int] = []

    def fake_post(path, body, **kw):
        seen_batches.append(len(body))
        return {"characters": [{"name": n, "id": i} for i, n in enumerate(body)]}

    monkeypatch.setattr(auth, "esi_post", fake_post, raising=False)
    names = [f"P{i}" for i in range(1500)]
    out = auth.resolve_names_to_ids(names)
    assert seen_batches == [1000, 500]
    assert len(out) == 1500


def test_get_affiliations_batches(monkeypatch):
    auth = ESIAuth(client_id="x", client_secret="y", callback_url="z", token_file="/tmp/t.json")

    def fake_post(path, body, **kw):
        return [{"character_id": cid, "corporation_id": 100, "alliance_id": 200} for cid in body]

    monkeypatch.setattr(auth, "esi_post", fake_post, raising=False)
    out = auth.get_affiliations([1, 2, 3])
    assert len(out) == 3
    assert out[0] == {"character_id": 1, "corporation_id": 100, "alliance_id": 200}


def test_is_fleet_boss_true(monkeypatch):
    auth = ESIAuth(client_id="x", client_secret="y", callback_url="z", token_file="/tmp/t.json")
    auth._character_id = 42

    def fake_get(path, **kw):
        return {"fleet_id": 999, "role": "fleet_commander", "wing_id": 0, "squad_id": 0}

    monkeypatch.setattr(auth, "esi_get", fake_get)
    assert auth.is_fleet_boss() is True


def test_is_fleet_boss_false_when_not_in_fleet(monkeypatch):
    auth = ESIAuth(client_id="x", client_secret="y", callback_url="z", token_file="/tmp/t.json")
    auth._character_id = 42
    monkeypatch.setattr(auth, "esi_get", lambda path, **kw: None)
    assert auth.is_fleet_boss() is False


def test_is_fleet_boss_false_when_member(monkeypatch):
    auth = ESIAuth(client_id="x", client_secret="y", callback_url="z", token_file="/tmp/t.json")
    auth._character_id = 42
    monkeypatch.setattr(auth, "esi_get", lambda path, **kw: {"fleet_id": 1, "role": "squad_member"})
    assert auth.is_fleet_boss() is False
```

- [ ] **Step 3: Run tests to verify failure**

Run: `pytest tests/test_esi_auth.py -v -k "resolve_names or get_affiliations or is_fleet_boss"`
Expected: 6 FAIL.

- [ ] **Step 4: Implement helpers in `esi_auth.py`**

First, ensure `esi_post` exists. If not, add this near `esi_get`:
```python
def esi_post(self, path: str, body, params: dict | None = None) -> dict | list | None:
    """POST to ESI with the auth token. `body` is JSON-serialized."""
    token = self._ensure_access_token()
    if not token:
        return None
    url = ESI_BASE + path
    headers = {**HEADERS, "Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=body, params=params or {},
                             headers=headers, timeout=10)
        if resp.ok:
            return resp.json()
    except Exception:
        pass
    return None
```

Then add the bulk helpers (group with the existing fleet section):
```python
def resolve_names_to_ids(self, names: list[str]) -> dict[str, int]:
    """Resolve a list of EVE names to character IDs. Batches of 1000."""
    out: dict[str, int] = {}
    if not names:
        return out
    for i in range(0, len(names), 1000):
        chunk = names[i:i + 1000]
        data = self.esi_post("/universe/ids/", chunk)
        if not isinstance(data, dict):
            continue
        for entry in data.get("characters", []) or []:
            n = entry.get("name")
            cid = entry.get("id")
            if n and cid:
                out[n] = cid
    return out


def get_affiliations(self, char_ids: list[int]) -> list[dict]:
    """Resolve characters to corp/alliance affiliations. Batches of 1000."""
    out: list[dict] = []
    if not char_ids:
        return out
    for i in range(0, len(char_ids), 1000):
        chunk = char_ids[i:i + 1000]
        data = self.esi_post("/characters/affiliation/", chunk)
        if isinstance(data, list):
            out.extend(data)
    return out


def get_personal_contacts(self) -> list[dict]:
    if not self._character_id:
        return []
    data = self.esi_get(f"/characters/{self._character_id}/contacts/")
    return data if isinstance(data, list) else []


def get_corp_contacts(self) -> list[dict]:
    if not self._character_id:
        return []
    info = self.esi_get(f"/characters/{self._character_id}/")
    if not isinstance(info, dict):
        return []
    corp_id = info.get("corporation_id")
    if not corp_id:
        return []
    data = self.esi_get(f"/corporations/{corp_id}/contacts/")
    return data if isinstance(data, list) else []


def get_alliance_contacts(self) -> list[dict]:
    if not self._character_id:
        return []
    info = self.esi_get(f"/characters/{self._character_id}/")
    if not isinstance(info, dict):
        return []
    alliance_id = info.get("alliance_id")
    if not alliance_id:
        return []
    data = self.esi_get(f"/alliances/{alliance_id}/contacts/")
    return data if isinstance(data, list) else []


def is_fleet_boss(self) -> bool:
    if not self._character_id:
        return False
    data = self.esi_get(f"/characters/{self._character_id}/fleet/")
    if not isinstance(data, dict):
        return False
    return data.get("role") == "fleet_commander"
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_esi_auth.py -v`
Expected: existing tests still pass + 6 new ones pass.

- [ ] **Step 6: Commit**

```bash
git add esi_auth.py tests/test_esi_auth.py
git commit -m "Add bulk-name, affiliation, contacts, and fleet-boss ESI helpers"
```

---

## Task 9: Standings cache module

**Files:**
- Create: `standings_cache.py`
- Create: `tests/test_standings_cache.py`

- [ ] **Step 1: Write failing tests**

`tests/test_standings_cache.py`:
```python
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from standings_cache import StandingsCache, is_friendly


def _write_cache(tmp_path, fetched_at, friendly, hostile, source=42):
    path = tmp_path / "standings_cache.json"
    path.write_text(json.dumps({
        "fetched_at": fetched_at.isoformat(),
        "source_character_id": source,
        "friendly_ids": friendly,
        "hostile_ids": hostile,
    }))
    return str(path)


def test_load_returns_empty_when_missing(tmp_path):
    cache = StandingsCache(path=str(tmp_path / "missing.json"))
    cache.load()
    assert cache.friendly_ids == set()
    assert cache.hostile_ids == set()


def test_load_round_trip(tmp_path):
    path = _write_cache(tmp_path, datetime.now(timezone.utc), [1, 2], [3, 4])
    cache = StandingsCache(path=path)
    cache.load()
    assert cache.friendly_ids == {1, 2}
    assert cache.hostile_ids == {3, 4}


def test_save_then_load(tmp_path):
    path = str(tmp_path / "out.json")
    cache = StandingsCache(path=path)
    cache.friendly_ids = {10, 20}
    cache.hostile_ids = {30}
    cache.fetched_at = datetime(2026, 4, 28, tzinfo=timezone.utc)
    cache.source_character_id = 99
    cache.save()
    cache2 = StandingsCache(path=path)
    cache2.load()
    assert cache2.friendly_ids == {10, 20}
    assert cache2.hostile_ids == {30}
    assert cache2.source_character_id == 99


def test_is_stale_when_old(tmp_path):
    path = _write_cache(tmp_path, datetime.now(timezone.utc) - timedelta(hours=25), [], [])
    cache = StandingsCache(path=path)
    cache.load()
    assert cache.is_stale(max_age_hours=24)


def test_is_stale_when_fresh(tmp_path):
    path = _write_cache(tmp_path, datetime.now(timezone.utc) - timedelta(hours=1), [], [])
    cache = StandingsCache(path=path)
    cache.load()
    assert not cache.is_stale(max_age_hours=24)


def test_refresh_pulls_from_esi(monkeypatch, tmp_path):
    cache = StandingsCache(path=str(tmp_path / "x.json"))

    class FakeAuth:
        _character_id = 42
        def get_personal_contacts(self): return [
            {"contact_id": 1, "contact_type": "character", "standing": 5.0},
            {"contact_id": 99, "contact_type": "character", "standing": -10.0},
        ]
        def get_corp_contacts(self): return [
            {"contact_id": 100, "contact_type": "corporation", "standing": 7.5},
        ]
        def get_alliance_contacts(self): return [
            {"contact_id": 200, "contact_type": "alliance", "standing": 0.0},  # neutral, dropped
            {"contact_id": 201, "contact_type": "alliance", "standing": -5.0},
        ]

    cache.refresh(FakeAuth())
    assert cache.friendly_ids == {1, 100}
    assert cache.hostile_ids == {99, 201}
    assert cache.source_character_id == 42


def test_is_friendly_uses_own_chars():
    assert is_friendly(123, None, None, friendly_ids=set(), own_character_ids={123}) is True


def test_is_friendly_alliance_match():
    assert is_friendly(7, 8, 9, friendly_ids={9}, own_character_ids=set()) is True


def test_is_friendly_corp_match():
    assert is_friendly(7, 8, 9, friendly_ids={8}, own_character_ids=set()) is True


def test_is_friendly_unknown_returns_false():
    assert is_friendly(7, 8, 9, friendly_ids={1}, own_character_ids=set()) is False


def test_is_friendly_handles_none_ids():
    assert is_friendly(None, None, None, friendly_ids={1}, own_character_ids=set()) is False
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_standings_cache.py -v`
Expected: ImportError or 11 FAIL.

- [ ] **Step 3: Implement `standings_cache.py`**

```python
"""
Standings cache for the Intelligence tab.

Builds a flat friendly/hostile entity-id set from the main character's
personal, corp, and alliance contact lists, persists it to disk, and
exposes is_friendly() for the analyzers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def is_friendly(
    char_id: int | None,
    corp_id: int | None,
    alliance_id: int | None,
    friendly_ids: set[int],
    own_character_ids: set[int],
) -> bool:
    if char_id is not None and char_id in own_character_ids:
        return True
    for entity in (char_id, corp_id, alliance_id):
        if entity is not None and entity in friendly_ids:
            return True
    return False


class StandingsCache:
    def __init__(self, path: str):
        self.path = path
        self.friendly_ids: set[int] = set()
        self.hostile_ids: set[int] = set()
        self.fetched_at: datetime | None = None
        self.source_character_id: int | None = None

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self.friendly_ids = set(data.get("friendly_ids") or [])
        self.hostile_ids = set(data.get("hostile_ids") or [])
        ts = data.get("fetched_at")
        if ts:
            try:
                self.fetched_at = datetime.fromisoformat(ts)
            except ValueError:
                self.fetched_at = None
        self.source_character_id = data.get("source_character_id")

    def save(self) -> None:
        payload = {
            "fetched_at": (self.fetched_at or datetime.now(timezone.utc)).isoformat(),
            "source_character_id": self.source_character_id,
            "friendly_ids": sorted(self.friendly_ids),
            "hostile_ids": sorted(self.hostile_ids),
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, self.path)

    def is_stale(self, max_age_hours: float = 24.0) -> bool:
        if self.fetched_at is None:
            return True
        age = datetime.now(timezone.utc) - self.fetched_at
        return age > timedelta(hours=max_age_hours)

    def refresh(self, auth) -> None:
        """Pull contacts from ESI and rebuild the cache."""
        friendly: set[int] = set()
        hostile: set[int] = set()
        for getter in (auth.get_personal_contacts,
                       auth.get_corp_contacts,
                       auth.get_alliance_contacts):
            try:
                rows = getter() or []
            except Exception:
                rows = []
            for row in rows:
                cid = row.get("contact_id")
                standing = row.get("standing", 0)
                if cid is None:
                    continue
                if standing > 0:
                    friendly.add(int(cid))
                elif standing < 0:
                    hostile.add(int(cid))
        self.friendly_ids = friendly
        self.hostile_ids = hostile
        self.fetched_at = datetime.now(timezone.utc)
        self.source_character_id = getattr(auth, "_character_id", None)
        self.save()

    def age_string(self) -> str:
        if self.fetched_at is None:
            return "never"
        delta = datetime.now(timezone.utc) - self.fetched_at
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m old"
        if hours < 24:
            return f"{int(hours)}h old"
        return f"{int(hours / 24)}d old"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_standings_cache.py -v`
Expected: 11 PASS.

- [ ] **Step 5: Commit**

```bash
git add standings_cache.py tests/test_standings_cache.py
git commit -m "Add standings_cache module with disk persistence and is_friendly check"
```

---

## Task 10: IntelSession state container

**Files:**
- Create: `intel_session.py`
- Create: `tests/test_intel_session.py`

- [ ] **Step 1: Write failing tests**

`tests/test_intel_session.py`:
```python
from datetime import datetime, timedelta, timezone

from intel_paste import DScan, DScanRow, FleetSummary, FleetSummaryRow, LocalScan
from intel_session import IntelSession


def _dscan(types: list[str]) -> DScan:
    return DScan(rows=[
        DScanRow(type_id=i, item_name=f"Ship {i}", type_name=t, distance_au=1.0)
        for i, t in enumerate(types)
    ])


def test_add_local_scan_records_timestamp():
    s = IntelSession()
    s.add_local_scan("Jita", LocalScan(pilot_names=["Alice"]))
    assert len(s.local_scans) == 1
    assert s.local_scans[0].system == "Jita"


def test_add_dscan_records_timestamp():
    s = IntelSession()
    s.add_dscan("O-BDXB", _dscan(["Vulture"]))
    assert len(s.dscan_scans) == 1


def test_latest_fleet_paste_returns_most_recent():
    s = IntelSession()
    s.add_fleet_paste(FleetSummary(rows=[FleetSummaryRow("A", "Frigate", 1)]))
    s.add_fleet_paste(FleetSummary(rows=[FleetSummaryRow("B", "Frigate", 2)]))
    latest = s.latest_fleet_paste()
    assert latest is not None
    assert latest.parsed.rows[0].ship_name == "B"


def test_latest_fleet_paste_when_empty():
    s = IntelSession()
    assert s.latest_fleet_paste() is None


def test_prior_dscan_in_window():
    s = IntelSession()
    older = datetime.now(timezone.utc) - timedelta(minutes=5)
    s.dscan_scans.append(_make_entry(older, "O-BDXB", _dscan(["Sabre"])))
    prior = s.prior_dscan("O-BDXB", window_minutes=15)
    assert prior is not None
    assert prior.parsed.rows[0].type_name == "Sabre"


def test_prior_dscan_outside_window():
    s = IntelSession()
    older = datetime.now(timezone.utc) - timedelta(minutes=20)
    s.dscan_scans.append(_make_entry(older, "O-BDXB", _dscan(["Sabre"])))
    assert s.prior_dscan("O-BDXB", window_minutes=15) is None


def test_prior_dscan_different_system():
    s = IntelSession()
    older = datetime.now(timezone.utc) - timedelta(minutes=2)
    s.dscan_scans.append(_make_entry(older, "Jita", _dscan(["Sabre"])))
    assert s.prior_dscan("O-BDXB", window_minutes=15) is None


def test_prior_dscan_returns_most_recent_within_window():
    s = IntelSession()
    now = datetime.now(timezone.utc)
    s.dscan_scans.append(_make_entry(now - timedelta(minutes=10), "O-BDXB", _dscan(["A"])))
    s.dscan_scans.append(_make_entry(now - timedelta(minutes=2), "O-BDXB", _dscan(["B"])))
    # When called *after* a third paste, the helper should return the latest of the two.
    prior = s.prior_dscan("O-BDXB", window_minutes=15)
    assert prior.parsed.rows[0].type_name == "B"


def test_clear_wipes_all():
    s = IntelSession()
    s.add_local_scan("Jita", LocalScan(pilot_names=["X"]))
    s.add_dscan("Jita", _dscan(["Y"]))
    s.add_fleet_paste(FleetSummary(rows=[]))
    s.clear()
    assert s.local_scans == []
    assert s.dscan_scans == []
    assert s.fleet_pastes == []


def _make_entry(ts, system, parsed):
    from intel_session import ScanEntry
    return ScanEntry(timestamp=ts, system=system, parsed=parsed)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_session.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `intel_session.py`**

```python
"""In-memory session state for the Intelligence tab."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Generic, TypeVar

from intel_paste import (
    DScan,
    FleetComposition,
    FleetSummary,
    LocalScan,
    ParsedScan,
)

T = TypeVar("T")


@dataclass
class ScanEntry(Generic[T]):
    timestamp: datetime
    system: str
    parsed: T


@dataclass
class FleetPasteEntry:
    timestamp: datetime
    parsed: FleetComposition | FleetSummary


@dataclass
class IntelSession:
    local_scans: list[ScanEntry[LocalScan]] = field(default_factory=list)
    dscan_scans: list[ScanEntry[DScan]] = field(default_factory=list)
    fleet_pastes: list[FleetPasteEntry] = field(default_factory=list)

    def add_local_scan(self, system: str, parsed: LocalScan) -> None:
        self.local_scans.append(ScanEntry(
            timestamp=datetime.now(timezone.utc),
            system=system,
            parsed=parsed,
        ))

    def add_dscan(self, system: str, parsed: DScan) -> None:
        self.dscan_scans.append(ScanEntry(
            timestamp=datetime.now(timezone.utc),
            system=system,
            parsed=parsed,
        ))

    def add_fleet_paste(self, parsed: FleetComposition | FleetSummary) -> None:
        self.fleet_pastes.append(FleetPasteEntry(
            timestamp=datetime.now(timezone.utc),
            parsed=parsed,
        ))

    def latest_fleet_paste(self) -> FleetPasteEntry | None:
        return self.fleet_pastes[-1] if self.fleet_pastes else None

    def prior_dscan(self, system: str, window_minutes: int = 15) -> ScanEntry[DScan] | None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        candidates = [
            e for e in self.dscan_scans
            if e.system == system and e.timestamp >= cutoff
        ]
        return candidates[-1] if candidates else None

    def prior_local_scan(self, system: str, window_minutes: int = 15) -> ScanEntry[LocalScan] | None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        candidates = [
            e for e in self.local_scans
            if e.system == system and e.timestamp >= cutoff
        ]
        return candidates[-1] if candidates else None

    def clear(self) -> None:
        self.local_scans.clear()
        self.dscan_scans.clear()
        self.fleet_pastes.clear()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_intel_session.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_session.py tests/test_intel_session.py
git commit -m "Add IntelSession in-memory state container"
```

---

## Task 11: Local-scan analyzer

**Files:**
- Create: `intel_analyzer.py`
- Create: `tests/test_intel_analyzer.py`

- [ ] **Step 1: Write failing tests**

`tests/test_intel_analyzer.py`:
```python
from intel_paste import LocalScan
from intel_analyzer import LocalScanResult, analyze_local_scan


class _FakeAuth:
    """Stand-in for ESIAuth — overrides batch helpers only."""

    def __init__(self, name_to_id, affiliations):
        self._name_to_id = name_to_id
        self._affiliations = affiliations

    def resolve_names_to_ids(self, names):
        return {n: self._name_to_id[n] for n in names if n in self._name_to_id}

    def get_affiliations(self, ids):
        return [self._affiliations[i] for i in ids if i in self._affiliations]


def test_analyze_local_scan_classifies_pilots():
    scan = LocalScan(pilot_names=["Alice", "Bob", "Carol"])
    auth = _FakeAuth(
        name_to_id={"Alice": 1, "Bob": 2, "Carol": 3},
        affiliations={
            1: {"character_id": 1, "corporation_id": 100, "alliance_id": 200},
            2: {"character_id": 2, "corporation_id": 101, "alliance_id": 201},
            3: {"character_id": 3, "corporation_id": 102, "alliance_id": None},
        },
    )
    result = analyze_local_scan(
        scan, auth=auth,
        friendly_ids={200, 101},  # Alice's alliance, Bob's corp
        own_character_ids=set(),
    )
    assert isinstance(result, LocalScanResult)
    assert result.friendly_count == 2
    assert result.hostile_count == 1
    assert result.unresolved_names == []
    assert result.total == 3


def test_analyze_local_scan_buckets_unresolved():
    scan = LocalScan(pilot_names=["Alice", "GhostName"])
    auth = _FakeAuth(
        name_to_id={"Alice": 1},
        affiliations={1: {"character_id": 1, "corporation_id": 100, "alliance_id": 200}},
    )
    result = analyze_local_scan(scan, auth=auth, friendly_ids=set(), own_character_ids=set())
    assert result.unresolved_names == ["GhostName"]
    assert result.hostile_count == 1
    assert result.total == 2


def test_analyze_local_scan_own_chars_count_friendly():
    scan = LocalScan(pilot_names=["Me"])
    auth = _FakeAuth(
        name_to_id={"Me": 42},
        affiliations={42: {"character_id": 42, "corporation_id": 1, "alliance_id": 2}},
    )
    result = analyze_local_scan(
        scan, auth=auth,
        friendly_ids=set(),
        own_character_ids={42},
    )
    assert result.friendly_count == 1
    assert result.hostile_count == 0


def test_analyze_local_scan_top_hostile_affiliations():
    scan = LocalScan(pilot_names=["A", "B", "C", "D"])
    auth = _FakeAuth(
        name_to_id={"A": 1, "B": 2, "C": 3, "D": 4},
        affiliations={
            1: {"character_id": 1, "corporation_id": 10, "alliance_id": 100},
            2: {"character_id": 2, "corporation_id": 11, "alliance_id": 100},
            3: {"character_id": 3, "corporation_id": 12, "alliance_id": 101},
            4: {"character_id": 4, "corporation_id": 13, "alliance_id": None},
        },
    )
    result = analyze_local_scan(scan, auth=auth, friendly_ids=set(), own_character_ids=set())
    # Two pilots in alliance 100, one in alliance 101, one with no alliance
    counts = dict(result.top_hostile_alliances)
    assert counts[100] == 2
    assert counts[101] == 1
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_analyzer.py -v`
Expected: ImportError or FAIL.

- [ ] **Step 3: Implement local-scan analyzer in `intel_analyzer.py`**

```python
"""
Pure-function analyzers for parsed intel.

Each analyzer takes a ParsedScan plus context (ESI auth, standings sets,
session state) and returns a structured result that the GUI renders.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from intel_paste import (
    DScan,
    FleetComposition,
    FleetSummary,
    LocalScan,
)
from standings_cache import is_friendly


@dataclass
class LocalScanResult:
    total: int
    friendly_count: int
    hostile_count: int
    unresolved_names: list[str]
    hostile_pilots: list[tuple[str, int | None, int | None]]  # (name, corp_id, alliance_id)
    top_hostile_alliances: list[tuple[int, int]]  # [(alliance_id, count), ...]
    top_hostile_corps: list[tuple[int, int]]


def analyze_local_scan(
    scan: LocalScan,
    auth,
    friendly_ids: set[int],
    own_character_ids: set[int],
) -> LocalScanResult:
    name_to_id = auth.resolve_names_to_ids(scan.pilot_names)
    unresolved = [n for n in scan.pilot_names if n not in name_to_id]

    affiliations = auth.get_affiliations(list(name_to_id.values()))
    aff_by_char = {a["character_id"]: a for a in affiliations if a.get("character_id")}

    friendly = 0
    hostile = 0
    hostile_pilots: list[tuple[str, int | None, int | None]] = []
    hostile_corp_counter: Counter[int] = Counter()
    hostile_alliance_counter: Counter[int] = Counter()

    for name, cid in name_to_id.items():
        aff = aff_by_char.get(cid, {})
        corp = aff.get("corporation_id")
        alliance = aff.get("alliance_id")
        if is_friendly(cid, corp, alliance, friendly_ids, own_character_ids):
            friendly += 1
        else:
            hostile += 1
            hostile_pilots.append((name, corp, alliance))
            if corp is not None:
                hostile_corp_counter[corp] += 1
            if alliance is not None:
                hostile_alliance_counter[alliance] += 1

    # NOTE: unresolved names are reported separately (see formatter). They are
    # NOT folded into hostile_count, so total = friendly + hostile + unresolved.

    return LocalScanResult(
        total=len(scan.pilot_names),
        friendly_count=friendly,
        hostile_count=hostile,
        unresolved_names=unresolved,
        hostile_pilots=hostile_pilots,
        top_hostile_alliances=hostile_alliance_counter.most_common(5),
        top_hostile_corps=hostile_corp_counter.most_common(5),
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_intel_analyzer.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_analyzer.py tests/test_intel_analyzer.py
git commit -m "Add local-scan analyzer to intel_analyzer"
```

---

## Task 12: D-scan analyzer with friendly source priority

**Files:**
- Modify: `intel_analyzer.py`
- Modify: `tests/test_intel_analyzer.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_intel_analyzer.py`:
```python
from collections import Counter

from intel_paste import (
    DScan,
    DScanRow,
    FleetComposition,
    FleetMember,
    FleetSummary,
    FleetSummaryRow,
)
from intel_analyzer import (
    DScanResult,
    DScanSource,
    analyze_dscan,
)


def _ship_dscan(types: list[str]) -> DScan:
    return DScan(rows=[
        DScanRow(type_id=1000 + i, item_name=f"Ship {i}", type_name=t, distance_au=1.0)
        for i, t in enumerate(types)
    ])


def test_analyze_dscan_no_source_ships_only(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    scan = _ship_dscan(["Vulture", "Sabre", "Sabre"])
    result = analyze_dscan(scan, friendly_source=None, fleet_roster=None)
    assert isinstance(result, DScanResult)
    assert result.total_ships == 3
    assert result.source == DScanSource.NONE
    assert result.friendly_count is None
    assert result.hostile_count is None
    assert "No fleet roster" in result.note


def test_analyze_dscan_filters_non_ships(monkeypatch):
    def fake_is_ship(tid):
        return tid != 1002  # 1002 is non-ship
    monkeypatch.setattr("intel_analyzer.is_ship_type", fake_is_ship)
    rows = [
        DScanRow(1000, "A", "Vulture", 1.0),
        DScanRow(1001, "B", "Sabre", 1.0),
        DScanRow(1002, "Citadel", "Astrahus", 1.0),  # filtered out
    ]
    result = analyze_dscan(DScan(rows=rows), friendly_source=None, fleet_roster=None)
    assert result.total_ships == 2


def test_analyze_dscan_with_pasted_summary(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    # D-scan: 1 Vulture, 3 Sabres
    scan = _ship_dscan(["Vulture", "Sabre", "Sabre", "Sabre"])
    # Fleet roster: 1 Vulture, 1 Sabre
    roster = FleetSummary(rows=[
        FleetSummaryRow("Vulture", "Command Ship", 1),
        FleetSummaryRow("Sabre", "Interdictor", 1),
    ])
    result = analyze_dscan(scan, friendly_source=DScanSource.PASTED, fleet_roster=roster)
    assert result.friendly_count == 2
    # 4 ships - 2 friendly = 2 hostile
    assert result.hostile_count == 2
    # Hostile breakdown: 0 Vulture, 2 Sabre
    breakdown = dict(result.hostile_by_type)
    assert breakdown.get("Sabre") == 2
    assert breakdown.get("Vulture", 0) == 0


def test_analyze_dscan_with_pasted_composition(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    scan = _ship_dscan(["Archon", "Flycatcher"])
    roster = FleetComposition(members=[
        FleetMember("Securitas Protector", "O-BDXB", "Archon", "Carrier",
                    "Fleet Commander (Boss)", "5 - 5 - 5", ""),
        FleetMember("Tyreece Arkan", "O-BDXB", "Flycatcher", "Interdictor",
                    "Squad Member", "0 - 4 - 5", "Wing 1 / Squad 1"),
    ])
    result = analyze_dscan(scan, friendly_source=DScanSource.PASTED, fleet_roster=roster)
    assert result.friendly_count == 2
    assert result.hostile_count == 0


def test_analyze_dscan_does_not_underflow(monkeypatch):
    """If pasted fleet has more ships of a type than dscan shows (e.g., docked),
    the hostile count for that type clamps at 0."""
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    scan = _ship_dscan(["Vulture"])  # 1 ship
    roster = FleetSummary(rows=[FleetSummaryRow("Vulture", "Command Ship", 5)])  # 5 docked
    result = analyze_dscan(scan, friendly_source=DScanSource.PASTED, fleet_roster=roster)
    # min(dscan, friendly) for friendly_count
    assert result.friendly_count == 1
    assert result.hostile_count == 0
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_analyzer.py -v -k dscan`
Expected: 5 FAIL.

- [ ] **Step 3: Implement d-scan analyzer**

Append to `intel_analyzer.py`:
```python
from enum import Enum

from ship_classes import is_ship_type


class DScanSource(Enum):
    PASTED = "pasted"
    ESI = "esi"
    NONE = "none"


@dataclass
class DScanResult:
    total_ships: int
    source: DScanSource
    friendly_count: int | None
    hostile_count: int | None
    hostile_by_type: list[tuple[str, int]]  # sorted desc by count
    friendly_by_type: list[tuple[str, int]]
    dscan_by_type: list[tuple[str, int]]
    note: str = ""


def _roster_type_counts(roster) -> Counter[str]:
    counts: Counter[str] = Counter()
    if isinstance(roster, FleetSummary):
        for row in roster.rows:
            counts[row.ship_name] += row.count
    elif isinstance(roster, FleetComposition):
        for member in roster.members:
            counts[member.ship_name] += 1
    return counts


def analyze_dscan(
    scan: DScan,
    friendly_source: DScanSource | None,
    fleet_roster,  # FleetSummary | FleetComposition | None
) -> DScanResult:
    ship_rows = [r for r in scan.rows if is_ship_type(r.type_id)]
    dscan_counts: Counter[str] = Counter(r.type_name for r in ship_rows)
    total = sum(dscan_counts.values())

    if friendly_source is None or friendly_source == DScanSource.NONE or fleet_roster is None:
        return DScanResult(
            total_ships=total,
            source=DScanSource.NONE,
            friendly_count=None,
            hostile_count=None,
            hostile_by_type=[],
            friendly_by_type=[],
            dscan_by_type=dscan_counts.most_common(),
            note="No fleet roster: paste a fleet composition, or be fleet boss to use ESI.",
        )

    roster_counts = _roster_type_counts(fleet_roster)
    friendly_counts: Counter[str] = Counter()
    hostile_counts: Counter[str] = Counter()
    for type_name, count in dscan_counts.items():
        f = min(count, roster_counts.get(type_name, 0))
        friendly_counts[type_name] = f
        hostile_counts[type_name] = count - f

    return DScanResult(
        total_ships=total,
        source=friendly_source,
        friendly_count=sum(friendly_counts.values()),
        hostile_count=sum(hostile_counts.values()),
        hostile_by_type=[(t, c) for t, c in hostile_counts.most_common() if c > 0],
        friendly_by_type=[(t, c) for t, c in friendly_counts.most_common() if c > 0],
        dscan_by_type=dscan_counts.most_common(),
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_intel_analyzer.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_analyzer.py tests/test_intel_analyzer.py
git commit -m "Add d-scan analyzer with friendly-source priority"
```

---

## Task 13: Trend computation

**Files:**
- Modify: `intel_analyzer.py`
- Modify: `tests/test_intel_analyzer.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_intel_analyzer.py`:
```python
from intel_analyzer import DScanTrend, compute_dscan_trend


def test_trend_no_prior_returns_none(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    current = analyze_dscan(_ship_dscan(["Sabre"]),
                            friendly_source=None, fleet_roster=None)
    assert compute_dscan_trend(current_result=current, prior_result=None,
                               minutes_ago=0) is None


def test_trend_basic_delta(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    prior = analyze_dscan(
        _ship_dscan(["Hurricane", "Hurricane", "Stiletto"]),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[]),  # everyone hostile
    )
    current = analyze_dscan(
        _ship_dscan(["Hurricane", "Hurricane", "Hurricane",
                     "Hurricane", "Hurricane",
                     "Sabre", "Sabre", "Damnation"]),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[]),
    )
    trend = compute_dscan_trend(current_result=current, prior_result=prior, minutes_ago=2)
    assert isinstance(trend, DScanTrend)
    # Hostile prior=3, current=8, delta=+5
    assert trend.hostile_prior == 3
    assert trend.hostile_current == 8
    assert trend.hostile_delta == 5
    diff = dict(trend.type_delta)
    assert diff["Hurricane"] == 3
    assert diff["Sabre"] == 2
    assert diff["Damnation"] == 1
    assert diff["Stiletto"] == -1
    assert "Damnation" in trend.new_types
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_analyzer.py -v -k trend`
Expected: 2 FAIL.

- [ ] **Step 3: Implement trend computation**

Append to `intel_analyzer.py`:
```python
@dataclass
class DScanTrend:
    minutes_ago: int
    hostile_prior: int
    hostile_current: int
    hostile_delta: int
    type_delta: list[tuple[str, int]]  # sorted by abs(delta) desc
    new_types: list[str]


def compute_dscan_trend(
    current_result: DScanResult,
    prior_result: DScanResult | None,
    minutes_ago: int,
) -> DScanTrend | None:
    if prior_result is None:
        return None

    prior_hostile = dict(prior_result.hostile_by_type)
    current_hostile = dict(current_result.hostile_by_type)

    type_deltas: dict[str, int] = {}
    for t in set(prior_hostile) | set(current_hostile):
        delta = current_hostile.get(t, 0) - prior_hostile.get(t, 0)
        if delta != 0:
            type_deltas[t] = delta

    new_types = [t for t in current_hostile if t not in prior_hostile]
    sorted_deltas = sorted(type_deltas.items(), key=lambda kv: abs(kv[1]), reverse=True)

    prior_total = prior_result.hostile_count or 0
    current_total = current_result.hostile_count or 0
    return DScanTrend(
        minutes_ago=minutes_ago,
        hostile_prior=prior_total,
        hostile_current=current_total,
        hostile_delta=current_total - prior_total,
        type_delta=sorted_deltas,
        new_types=new_types,
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_intel_analyzer.py -v`
Expected: 11 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_analyzer.py tests/test_intel_analyzer.py
git commit -m "Add d-scan trend computation"
```

---

## Task 14: Result rendering helpers

**Files:**
- Modify: `intel_analyzer.py`
- Modify: `tests/test_intel_analyzer.py`

The GUI needs deterministic strings to display. Centralize formatting in pure functions so they can be unit-tested.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_intel_analyzer.py`:
```python
from intel_analyzer import format_dscan_result, format_local_scan_result


def test_format_local_scan_basic():
    r = LocalScanResult(
        total=10, friendly_count=3, hostile_count=7,
        unresolved_names=[], hostile_pilots=[],
        top_hostile_alliances=[], top_hostile_corps=[],
    )
    text = format_local_scan_result(r)
    assert "Local — 10 pilots" in text
    assert "Friendly: 3" in text
    assert "Hostile:  7" in text


def test_format_dscan_no_source(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    r = analyze_dscan(_ship_dscan(["Vulture"]), friendly_source=None, fleet_roster=None)
    text = format_dscan_result(r, trend=None, roster_age_minutes=None)
    assert "D-Scan — 1 ships in range" in text
    assert "No fleet roster" in text


def test_format_dscan_with_breakdown(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    r = analyze_dscan(
        _ship_dscan(["Vulture", "Sabre", "Sabre"]),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[FleetSummaryRow("Vulture", "Command Ship", 1)]),
    )
    text = format_dscan_result(r, trend=None, roster_age_minutes=2)
    assert "Friendly" in text and "1" in text
    assert "Hostile" in text and "2" in text
    assert "Sabre × 2" in text


def test_format_dscan_with_stale_roster_warning(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    r = analyze_dscan(
        _ship_dscan(["Vulture"]),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[FleetSummaryRow("Vulture", "Command Ship", 1)]),
    )
    text = format_dscan_result(r, trend=None, roster_age_minutes=8)
    assert "8m old" in text


def test_format_dscan_with_trend(monkeypatch):
    monkeypatch.setattr("intel_analyzer.is_ship_type", lambda tid: True)
    r = analyze_dscan(
        _ship_dscan(["Hurricane"] * 5),
        friendly_source=DScanSource.PASTED,
        fleet_roster=FleetSummary(rows=[]),
    )
    trend = DScanTrend(
        minutes_ago=2,
        hostile_prior=3,
        hostile_current=5,
        hostile_delta=2,
        type_delta=[("Hurricane", 2)],
        new_types=[],
    )
    text = format_dscan_result(r, trend=trend, roster_age_minutes=1)
    assert "Trend" in text
    assert "3 → 5" in text
    assert "+2 Hurricane" in text
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_intel_analyzer.py -v -k format`
Expected: 5 FAIL.

- [ ] **Step 3: Implement formatters**

Append to `intel_analyzer.py`:
```python
def format_local_scan_result(r: LocalScanResult) -> str:
    lines = [f"Local — {r.total} pilots"]
    lines.append(f"  Friendly: {r.friendly_count}")
    lines.append(f"  Hostile:  {r.hostile_count}")
    if r.unresolved_names:
        lines.append(f"  Unresolved: {len(r.unresolved_names)}")
    if r.top_hostile_alliances:
        lines.append("  Top hostile alliances:")
        for aid, count in r.top_hostile_alliances:
            lines.append(f"    Alliance {aid} × {count}")
    return "\n".join(lines)


def format_dscan_result(
    r: DScanResult,
    trend: "DScanTrend | None",
    roster_age_minutes: int | None,
) -> str:
    lines = [f"D-Scan — {r.total_ships} ships in range"]
    if r.source == DScanSource.NONE:
        lines.append(f"  {r.note}")
        if r.dscan_by_type:
            lines.append("  Ships seen:")
            for type_name, count in r.dscan_by_type:
                lines.append(f"    {type_name} × {count}")
        return "\n".join(lines)

    label = "pasted fleet" if r.source == DScanSource.PASTED else "ESI fleet"
    if roster_age_minutes is not None:
        lines.append(f"  Friendly (from {label}, {roster_age_minutes}m old): {r.friendly_count}")
    else:
        lines.append(f"  Friendly (from {label}): {r.friendly_count}")
    lines.append(f"  Hostile (estimate):                   {r.hostile_count}")
    for type_name, count in r.hostile_by_type:
        lines.append(f"    {type_name} × {count}")

    lines.append("⚠ Other friendly fleets in system would inflate the hostile count.")
    if roster_age_minutes is not None and roster_age_minutes >= 5:
        lines.append(f"⚠ Pasted roster is {roster_age_minutes}m old. Refresh if it has changed.")

    if trend is not None:
        sign = "+" if trend.hostile_delta >= 0 else ""
        lines.append("")
        lines.append(f"Trend (vs scan {trend.minutes_ago}m ago):")
        lines.append(f"  Hostile: {trend.hostile_prior} → {trend.hostile_current} ({sign}{trend.hostile_delta})")
        if trend.type_delta:
            parts = []
            for type_name, delta in trend.type_delta:
                marker = " (new)" if type_name in trend.new_types else ""
                parts.append(f"{'+' if delta > 0 else ''}{delta} {type_name}{marker}")
            lines.append("  " + ", ".join(parts))

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_intel_analyzer.py -v`
Expected: 16 PASS.

- [ ] **Step 5: Commit**

```bash
git add intel_analyzer.py tests/test_intel_analyzer.py
git commit -m "Add result formatters for local-scan and d-scan analyses"
```

---

## Task 15: GUI rename — Intelligence tab and method names

**Files:**
- Modify: `fc_gui.py`

This task is mechanical: rename the tab text, the method, and the alert label. No new behavior.

- [ ] **Step 1: Identify all references**

```bash
grep -n "_build_zkill_tab\|zKillboard Intel\|** zKill ALERT \\*\\*" fc_gui.py
```

- [ ] **Step 2: Rename method and tab text**

In `fc_gui.py`:

Replace `def _build_zkill_tab(self)` with `def _build_intel_tab(self)`.

Replace the call site `self._build_zkill_tab()` (around line 465) with `self._build_intel_tab()`.

Replace `self.notebook.add(tab, text="  zKillboard Intel  ")` with `self.notebook.add(tab, text="  Intelligence  ")`.

Replace `self.notebook.tab(1, text="  zKillboard Intel  ")` (line 1945) with `self.notebook.tab(1, text="  Intelligence  ")`.

Replace `self.notebook.tab(1, text="  ** zKill ALERT **  ")` (line 1956) with `self.notebook.tab(1, text="  ** Intel ALERT **  ")`.

**Do NOT** rename the inner-pane label `"zKillboard Intel"` (line 981) — it identifies the live feed as distinct from the new paste flow.

- [ ] **Step 3: Smoke-launch the app**

Run: `python fc_gui.py`

Verify:
- Tab title reads `Intelligence`
- Tab still loads without errors
- Live zKill feed still appears in the left pane

Close the app.

- [ ] **Step 4: Commit**

```bash
git add fc_gui.py
git commit -m "Rename zKillboard Intel tab to Intelligence"
```

---

## Task 16: GUI — Paste Intel collapsible drawer skeleton

**Files:**
- Modify: `fc_gui.py`

This task adds the drawer UI scaffolding only. Parse-button wiring comes in the next task.

- [ ] **Step 1: Locate the insertion point**

In `_build_intel_tab` (formerly `_build_zkill_tab`), find the line that says:
```python
# ── Intelligence Fusion Panel ─────────────────────────────────────
intel_frame = tk.Frame(tab, bg=BG_PANEL, ...
```
The new drawer goes immediately above this section, after the affiliation row 3 ends.

- [ ] **Step 2: Add the drawer code**

Insert this block before the `# ── Intelligence Fusion Panel` comment:

```python
# ── Paste Intel drawer (collapsible) ──────────────────────────────
self._paste_drawer_expanded = False
self._paste_drawer_frame = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                     highlightbackground=BORDER_COLOR,
                                     highlightthickness=1)
self._paste_drawer_frame.pack(fill=tk.X, padx=10, pady=(2, 5))

self._paste_header = tk.Frame(self._paste_drawer_frame, bg=BG_PANEL)
self._paste_header.pack(fill=tk.X, padx=10, pady=4)

self._paste_toggle_btn = tk.Label(
    self._paste_header, text="▶ Paste Intel",
    font=("Consolas", 10, "bold"), fg=FG_ACCENT, bg=BG_PANEL,
    cursor="hand2",
)
self._paste_toggle_btn.pack(side=tk.LEFT)
self._paste_toggle_btn.bind("<Button-1>", lambda e: self._toggle_paste_drawer())

self._paste_format_chip = tk.Label(
    self._paste_header, text="", font=("Consolas", 9),
    fg=FG_DIM, bg=BG_PANEL,
)
self._paste_format_chip.pack(side=tk.LEFT, padx=15)

self._paste_standings_age = tk.Label(
    self._paste_header, text="Standings: never",
    font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL,
)
self._paste_standings_age.pack(side=tk.RIGHT, padx=10)

ttk.Button(
    self._paste_header, text="Refresh Standings", style="Dark.TButton",
    command=self._refresh_standings,
).pack(side=tk.RIGHT)

# Body (hidden by default)
self._paste_body = tk.Frame(self._paste_drawer_frame, bg=BG_PANEL)

self._paste_text = tk.Text(
    self._paste_body, height=6, font=("Consolas", 10),
    bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
    borderwidth=1, relief=tk.RIDGE, wrap=tk.WORD,
)
self._paste_text.pack(fill=tk.X, padx=10, pady=(2, 4))
self._paste_text.bind("<<Modified>>", self._on_paste_text_modified)

paste_btn_row = tk.Frame(self._paste_body, bg=BG_PANEL)
paste_btn_row.pack(fill=tk.X, padx=10, pady=(0, 4))
ttk.Button(paste_btn_row, text="Parse", style="Dark.TButton",
           command=self._parse_pasted_intel).pack(side=tk.LEFT, padx=(0, 4))
ttk.Button(paste_btn_row, text="Clear", style="Dark.TButton",
           command=self._clear_paste).pack(side=tk.LEFT, padx=(0, 4))
ttk.Button(paste_btn_row, text="Collapse", style="Dark.TButton",
           command=self._toggle_paste_drawer).pack(side=tk.LEFT)

self._paste_result = tk.Text(
    self._paste_body, height=8, font=("Consolas", 10),
    bg=BG_ENTRY, fg=FG_TEXT, state=tk.DISABLED,
    borderwidth=1, relief=tk.RIDGE, wrap=tk.WORD,
)
self._paste_result.pack(fill=tk.X, padx=10, pady=(0, 6))
```

- [ ] **Step 3: Add the toggle handler and stub handlers**

Add these methods to `MainWindow` (anywhere with the other intel-tab methods):

```python
def _toggle_paste_drawer(self):
    self._paste_drawer_expanded = not self._paste_drawer_expanded
    if self._paste_drawer_expanded:
        self._paste_toggle_btn.config(text="▼ Paste Intel")
        self._paste_body.pack(fill=tk.X, padx=0, pady=0)
    else:
        self._paste_toggle_btn.config(text="▶ Paste Intel")
        self._paste_body.pack_forget()

def _on_paste_text_modified(self, event=None):
    # Reset the modified flag so the event fires again next change
    self._paste_text.edit_modified(False)
    text = self._paste_text.get("1.0", tk.END)
    from intel_paste import detect_and_parse
    parsed = detect_and_parse(text)
    if parsed is None:
        self._paste_format_chip.config(text="Detected: --", fg=FG_DIM)
    else:
        label = type(parsed).__name__
        self._paste_format_chip.config(text=f"Detected: {label}", fg=FG_ACCENT)

def _parse_pasted_intel(self):
    """Stub — wired in the next task."""
    pass

def _clear_paste(self):
    self._paste_text.delete("1.0", tk.END)
    self._paste_result.config(state=tk.NORMAL)
    self._paste_result.delete("1.0", tk.END)
    self._paste_result.config(state=tk.DISABLED)
    self._paste_format_chip.config(text="", fg=FG_DIM)

def _refresh_standings(self):
    """Stub — wired in the next task."""
    pass
```

- [ ] **Step 4: Smoke-test**

Run: `python fc_gui.py`

- Open the Intelligence tab.
- Verify the `▶ Paste Intel` strip is visible above the Intelligence Fusion panel.
- Click the strip; the body expands.
- Paste a sample d-scan; the chip says `Detected: DScan`.
- Click `Clear`; the body and chip clear.
- Click the strip header again; body collapses.

Close the app.

- [ ] **Step 5: Commit**

```bash
git add fc_gui.py
git commit -m "Add Paste Intel drawer skeleton to Intelligence tab"
```

---

## Task 17: GUI — wire Parse button, Refresh Standings, and summary log line

**Files:**
- Modify: `fc_gui.py`

- [ ] **Step 1: Add module-level imports near the existing intel imports**

Find the imports near line 40 (`from intel_monitor import ...`). Add:
```python
from intel_paste import (
    DScan, FleetComposition, FleetSummary, LocalScan, detect_and_parse,
)
from intel_session import IntelSession
from standings_cache import StandingsCache
import intel_analyzer
import os as _os
from app_path import app_dir as _app_dir
```

- [ ] **Step 2: Initialize session and standings on `MainWindow.__init__`**

Find the `__init__` of `MainWindow` (look for `self.zkill_monitor: ZKillMonitor | None = None` near line 131). Add right below:
```python
self._intel_session = IntelSession()
self._standings_cache = StandingsCache(
    path=_os.path.join(_app_dir(), "standings_cache.json")
)
self._standings_cache.load()
```

- [ ] **Step 3: Replace the `_refresh_standings` stub**

```python
def _refresh_standings(self):
    auth = self._active_auth_for_intel()
    if auth is None:
        self._set_paste_result("No active SSO character; can't refresh standings.")
        return
    try:
        self._standings_cache.refresh(auth)
    except Exception as exc:
        self._set_paste_result(f"Standings refresh failed: {exc}")
        return
    self._update_standings_label()
    self._set_paste_result(
        f"Standings refreshed. {len(self._standings_cache.friendly_ids)} "
        f"friendly, {len(self._standings_cache.hostile_ids)} hostile."
    )

def _update_standings_label(self):
    self._paste_standings_age.config(
        text=f"Standings: {self._standings_cache.age_string()}"
    )

def _active_auth_for_intel(self):
    """Return the ESIAuth instance for the user's main character."""
    # Reuse the existing pattern used elsewhere in fc_gui — adapt to whatever
    # method/property already exposes the active ESI auth on MainWindow.
    return getattr(self, "_active_esi_auth", None)
```

Note: if `MainWindow` already exposes the active auth under a different name (look for `_esi_auth_for(...)`, `self._main_auth`, or similar — `grep -n "ESIAuth" fc_gui.py`), adapt `_active_auth_for_intel` to call that. If no clean accessor exists, add a TODO comment and have it return the first authenticated character from the multi-char manager.

Call `self._update_standings_label()` once at the end of `_build_intel_tab` so the label is correct on startup.

- [ ] **Step 4: Replace the `_parse_pasted_intel` stub**

```python
def _parse_pasted_intel(self):
    text = self._paste_text.get("1.0", tk.END)
    parsed = detect_and_parse(text)
    if parsed is None:
        self._set_paste_result("Unrecognized paste format.")
        return

    auth = self._active_auth_for_intel()
    own_chars = self._own_character_ids()
    system = self._infer_intel_system()

    if isinstance(parsed, FleetComposition) or isinstance(parsed, FleetSummary):
        self._intel_session.add_fleet_paste(parsed)
        rows = (parsed.members if isinstance(parsed, FleetComposition)
                else parsed.rows)
        kind = "composition" if isinstance(parsed, FleetComposition) else "summary"
        self._set_paste_result(f"Stored fleet {kind}: {len(rows)} entries.")
        self._append_intel_summary_line(f"Fleet {kind} stored ({len(rows)} entries)")
        return

    if isinstance(parsed, LocalScan):
        if auth is None:
            self._set_paste_result("No active SSO character; cannot resolve names.")
            return
        result = intel_analyzer.analyze_local_scan(
            parsed, auth=auth,
            friendly_ids=self._standings_cache.friendly_ids,
            own_character_ids=own_chars,
        )
        self._intel_session.add_local_scan(system, parsed)
        text_out = intel_analyzer.format_local_scan_result(result)
        self._set_paste_result(text_out)
        self._append_intel_summary_line(
            f"Local {system} — {result.friendly_count} friendly, "
            f"{result.hostile_count} hostile"
        )
        return

    if isinstance(parsed, DScan):
        # Choose friendly source
        latest_fleet = self._intel_session.latest_fleet_paste()
        if latest_fleet is not None:
            friendly_source = intel_analyzer.DScanSource.PASTED
            roster = latest_fleet.parsed
            roster_age_min = int(
                (datetime.now(timezone.utc) - latest_fleet.timestamp).total_seconds() / 60
            )
        elif auth is not None and auth.is_fleet_boss():
            # ESI fallback: build a minimal FleetSummary from /fleets/{id}/members/
            members = auth.get_fleet_members() or []
            from collections import Counter
            counts = Counter(m.get("ship_type_id") for m in members if m.get("ship_type_id"))
            from intel_paste import FleetSummary, FleetSummaryRow
            from zkill_monitor import resolve_name
            roster = FleetSummary(rows=[
                FleetSummaryRow(resolve_name(tid, "type"), "", count)
                for tid, count in counts.items()
            ])
            friendly_source = intel_analyzer.DScanSource.ESI
            roster_age_min = 0
        else:
            friendly_source = None
            roster = None
            roster_age_min = None

        prior = self._intel_session.prior_dscan(system, window_minutes=15)
        result = intel_analyzer.analyze_dscan(parsed, friendly_source=friendly_source,
                                              fleet_roster=roster)
        trend = None
        if prior is not None:
            prior_result = intel_analyzer.analyze_dscan(
                prior.parsed, friendly_source=friendly_source, fleet_roster=roster,
            )
            minutes_ago = int(
                (datetime.now(timezone.utc) - prior.timestamp).total_seconds() / 60
            )
            trend = intel_analyzer.compute_dscan_trend(
                current_result=result, prior_result=prior_result,
                minutes_ago=minutes_ago,
            )
        self._intel_session.add_dscan(system, parsed)
        text_out = intel_analyzer.format_dscan_result(
            result, trend=trend, roster_age_minutes=roster_age_min,
        )
        self._set_paste_result(text_out)
        delta_str = ""
        if trend and trend.hostile_delta:
            sign = "+" if trend.hostile_delta > 0 else ""
            delta_str = f" ({sign}{trend.hostile_delta} hostile vs scan {trend.minutes_ago}m ago)"
        h_str = (str(result.hostile_count)
                 if result.hostile_count is not None else "?")
        f_str = (str(result.friendly_count)
                 if result.friendly_count is not None else "?")
        self._append_intel_summary_line(
            f"D-Scan {system} — {h_str} hostile, {f_str} friendly{delta_str}"
        )
```

- [ ] **Step 5: Add helper methods**

```python
def _set_paste_result(self, text: str):
    self._paste_result.config(state=tk.NORMAL)
    self._paste_result.delete("1.0", tk.END)
    self._paste_result.insert(tk.END, text)
    self._paste_result.config(state=tk.DISABLED)

def _append_intel_summary_line(self, message: str):
    """Append a one-line summary to the right-pane intel log."""
    if not hasattr(self, "_intel_log"):
        return
    from datetime import datetime as _dt
    stamp = _dt.now().strftime("%H:%M")
    self._intel_log.config(state=tk.NORMAL)
    self._intel_log.insert(tk.END, f"[{stamp}] {message}\n")
    self._intel_log.see(tk.END)
    self._intel_log.config(state=tk.DISABLED)

def _own_character_ids(self) -> set[int]:
    """Return character IDs of all logged-in SSO characters."""
    # Adapt to however the multi-character manager exposes its list.
    # If MainWindow has self._esi_auths: dict[int, ESIAuth], return set(self._esi_auths.keys()).
    # Otherwise return a single-element set from the active char.
    auths = getattr(self, "_esi_auths", None)
    if isinstance(auths, dict):
        return {cid for cid in auths.keys() if cid}
    auth = self._active_auth_for_intel()
    return {auth._character_id} if auth and auth._character_id else set()

def _infer_intel_system(self) -> str:
    """Best-effort guess of the system the active character is currently in."""
    auth = self._active_auth_for_intel()
    if auth is None:
        return "unknown"
    try:
        loc = auth.get_location()
    except Exception:
        return "unknown"
    if not loc:
        return "unknown"
    sid = loc.get("solar_system_id")
    if sid:
        from zkill_monitor import resolve_name
        return resolve_name(sid, "solar_system")
    return "unknown"
```

- [ ] **Step 6: Add `from datetime import datetime, timezone` near the top of `fc_gui.py` if not already present**

```bash
grep -n "from datetime import" fc_gui.py
```

Make sure both `datetime` and `timezone` are imported.

- [ ] **Step 7: Smoke-test the full flow**

Run: `python fc_gui.py` and walk through this sequence:

1. Open the Intelligence tab → expand the drawer.
2. Click `Refresh Standings`. Verify the age label updates.
3. Paste local scan from `tests/fixtures/intel/local_scan.txt`. Click `Parse`. Verify the result panel shows pilot counts. Verify a `[HH:MM] Local …` line appears in the right-pane intel log.
4. Paste fleet composition from `tests/fixtures/intel/fleet_composition.txt`. Click `Parse`. Verify the result says `Stored fleet composition: 2 entries.`
5. Paste d-scan from `tests/fixtures/intel/dscan.txt`. Click `Parse`. Verify friendly/hostile breakdown using the pasted fleet composition.
6. Paste a slightly different d-scan (add a Sabre or two). Click `Parse`. Verify the trend block appears with the delta.
7. Click `Clear`. Verify the result panel and text box empty.

Close the app. If any step misbehaves, fix and rerun before committing.

- [ ] **Step 8: Commit**

```bash
git add fc_gui.py
git commit -m "Wire Paste Intel drawer to parser, analyzer, and standings cache"
```

---

## Task 18: Final regression sweep

**Files:** none

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: all existing tests still pass; all new tests pass.

- [ ] **Step 2: Visual launch**

Run: `python fc_gui.py`. Walk the manual smoke checklist from Task 17 once more, plus:
- Verify the live zKill feed still appears in the left pane.
- Verify the unread alert still flashes (paste fixture not needed; just trigger any zkill log entry).
- Verify the existing Intelligence Fusion panel and Intel Channels still function.

Close the app.

- [ ] **Step 3: Commit nothing (this is verification only)**

If any test failed or any UI element broke, fix it before declaring complete.

---

## Self-review checklist

- [x] Spec §1 (rename) → Task 15
- [x] Spec §2 (non-goals) — respected; no Discord/audio/web changes in any task
- [x] Spec §3.1 (tab rename) → Task 15
- [x] Spec §3.2 (drawer UI) → Task 16
- [x] Spec §4 (parser + auto-detection) → Tasks 1–6
- [x] Spec §5 (standings cache) → Task 9
- [x] Spec §6 (local-scan analysis) → Task 11
- [x] Spec §7 (d-scan analysis with friendly source priority) → Task 12
- [x] Spec §8 (trend tracking) → Task 13
- [x] Spec §9 (ESI helpers) → Task 8
- [x] Spec §9 (`is_ship_type`) → Task 7
- [x] Spec §10 (file map) → File Structure section above; Tasks create the listed modules
- [x] Spec §11 (testing) → unit tests in Tasks 2–14; manual plan in Tasks 16–18
- [x] Spec §13 (acceptance criteria) → covered by Tasks 15–18 manual plan
- [x] No placeholders ("TBD", "implement later") in any task
- [x] Type and method names consistent across tasks (`detect_and_parse`, `is_friendly`, `analyze_dscan`, `DScanSource`, `IntelSession.prior_dscan`, etc.)
