"""
FCTool GUI - Fleet Commander Assistant Frontend
Tkinter-based GUI that wraps all FCTool modules.
"""

import collections
import json
import os
import queue
import re
import sys
import shutil
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
import requests
from tkinter import ttk, scrolledtext, messagebox, filedialog, simpledialog
from tkinter import font as tkfont
from datetime import datetime

# Platform-specific sound support
if sys.platform == "win32":
    try:
        import winsound
        HAS_WINSOUND = True
    except ImportError:
        HAS_WINSOUND = False
else:
    HAS_WINSOUND = False

# Fix Windows console encoding (stdout/stderr are None in windowed PyInstaller builds)
if sys.platform == "win32":
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    else:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import intel_filter
import intel_monitor
import intel_stream
from chat_monitor import ChatMonitor, ChatMessage
from intel_monitor import (
    IntelReport, parse_intel_message, scan_available_channels,
    discover_channels, INTEL_CHANNELS, parse_dscan_text, make_dscan_summary,
    resolve_characters, coalesce_report, load_standings_whitelist,
    load_standings,
)
from intel_resolver import IntelResolver
from intel_paste import (
    DScan, FleetComposition, FleetSummary, FleetSummaryRow, LocalScan,
    detect_and_parse,
)
from intel_session import IntelSession
from standings_cache import StandingsCache
import intel_analyzer
import os as _os
from app_path import app_dir as _app_dir
from datetime import timezone
from xup_counter import XUpCounter, XUpState
from zkill_monitor import ZKillMonitor, KillAlert
from jump_range import JumpRangeChecker, search_system, get_stargate_route, get_system_info
from wh_route import find_wh_route, fetch_connections, WHRoute
from autocomplete import AutocompleteEntry
import system_cache
from system_cache import get_sorted_names, get_system_names, get_region_map
from esi_auth import ESIAuth, load_all_tokens
from loss_tracker import FleetLossTracker
from cyno_check import analyze_character as cyno_analyze_character
from eve_paths import resolve_eve_logs_path, gamelogs_dir_for
from default_config import DEFAULT_CONFIG
import tts_helper
from app_path import app_dir
import ship_classes
import charge_tracker
import command_bursts
# Fitting / doctrine / MOTD service layer (Fittings tab). Tk-free pure modules;
# type_catalog and fittings_store are instantiated per-app in __init__.
import fit_models
import fleet_guidance
import fit_parser
import fit_dna
import pyfa_import
import motd_builder
import motd_markup
from markup_editor import MarkupEditor
from eveo_tracker import find_thumbs, preview_running
from eveo_overlay import OverlayWindow
import overlay_rules
import fleet_composer
import eve_client_tracker
import window_activator
import preview_layout
import hotkey_service
import damage_flash
from gamelog_monitor import GamelogMonitor
import preview_tile
from preview_tile import TileWindow, STRIP_H as _TILE_STRIP_H
from app_io import atomic_write_json
from app_log import get_logger
from rate_limiter import rate_limit
from esi_constants import ESI_BASE, ESI_HEADERS

log = get_logger(__name__)


CONFIG_PATH = os.path.join(app_dir(), "config.json")


# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 is the pseudo-handle value -4.
_DPI_PER_MONITOR_AWARE_V2 = -4


def _read_overlay_dpi_pref() -> str:
    """Best-effort read of config['overlay']['dpi_awareness'] BEFORE the Tk root
    (and self.config) exist. Returns 'auto' when the file/key is absent."""
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        return (cfg.get("overlay", {}) or {}).get("dpi_awareness", "auto")
    except Exception:
        return "auto"


def _apply_dpi_awareness(pref: str, user32=None) -> None:
    """Attempt Per-Monitor-V2 DPI awareness before Tk root creation. Gated by
    pref != 'off'; wrapped so older Windows / any failure is non-fatal. The
    user32 arg is injectable for tests; in production it's ctypes.windll.user32."""
    if pref == "off":
        return
    try:
        if user32 is None:
            if sys.platform != "win32":
                return
            import ctypes
            user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext(_DPI_PER_MONITOR_AWARE_V2)
    except Exception:
        # Older Windows lacks this API, or the process is already aware — the
        # overlay still works on uniform-DPI setups. One quiet line, no crash.
        log.debug("[overlay] Per-Monitor-V2 DPI awareness not applied")


# ── Color Scheme ──────────────────────────────────────────────────────────────
BG_DARK = "#1a1a2e"
BG_PANEL = "#16213e"
BG_ENTRY = "#0f3460"
FG_TEXT = "#e0e0e0"
FG_DIM = "#888899"
FG_ACCENT = "#00d4ff"
FG_GREEN = "#00ff88"
FG_RED = "#ff4444"
FG_ORANGE = "#ff8c00"
FG_YELLOW = "#ffdd00"
FG_WHITE = "#ffffff"
FG_MAGENTA = "#ff66ff"
FG_UNDER = "#ff6666"
BORDER_COLOR = "#2a2a4a"

# ── Notebook tab indices ────────────────────────────────────────────────────────
# Order of self.notebook.add() calls in _build_ui:
#   0 Fleet Management, 1 Intelligence, 2 Jump Range, 3 Navigation,
#   4 Characters, 5 Fittings, 6 Settings.
# Inserting Fittings at index 5 leaves the earlier tabs (Intel=1, Characters=4)
# unchanged; only the Settings tab shifts 5 -> 6.
FITTINGS_TAB_INDEX = 5
DOCTRINES_SUBTAB_INDEX = 1

# ESI fittings scopes (added after some characters were already authed; SSO
# grants scopes only at login, so older tokens lack these and need re-auth).
SCOPE_FITTINGS_READ = "esi-fittings.read_fittings.v1"
SCOPE_FITTINGS_WRITE = "esi-fittings.write_fittings.v1"

# Verdict -> color map for command-burst rendering. Hoisted to module level so
# both the per-pilot Links rows and the off-hull rows share one source of truth
# (command_bursts is imported above; the FG_* constants are in scope here).
VERDICT_COLOR = {
    command_bursts.Verdict.BONUSED: FG_GREEN,
    command_bursts.Verdict.BONUSED_CONDITIONAL: FG_GREEN,
    command_bursts.Verdict.FITS_NO_BONUS: FG_YELLOW,
    command_bursts.Verdict.CANT_FIT: FG_RED,
    command_bursts.Verdict.UNKNOWN: FG_DIM,
}

# ── Intel-filter pure helpers (Tk-free; unit-testable) ──────────────────────


def high_priority(report, min_reported: int) -> bool:
    """Priority predicate: True if the report's pilot count meets the threshold
    OR the report has a computed route from staging (within range / reachable).
    NOT a drop filter -- only marks inline emphasis."""
    if report is None:
        return False
    count = (report.pilot_count or 0)
    if min_reported > 0 and count >= min_reported:
        return True
    if getattr(report, "route_from_staging", ""):
        return True
    return False


def add_filter_item(items: list, item: dict) -> tuple[list, bool]:
    """Append ``item`` ({"id", "name"}) to a copy of ``items``, de-duped by id.

    Returns a NEW list and a bool indicating whether the item was added (False
    if an entry with the same id already existed). Inputs are never mutated, so
    this is safe to unit-test and to call from the UI. ``item`` must carry an
    integer-ish ``id``; items with no usable id are rejected (added=False).
    """
    out = list(items or [])
    try:
        new_id = int(item["id"])
    except (KeyError, TypeError, ValueError):
        return out, False
    for existing in out:
        try:
            if int(existing.get("id")) == new_id:
                return out, False  # already present
        except (TypeError, ValueError, AttributeError):
            continue
    out.append({"id": new_id, "name": str(item.get("name", "")).strip()})
    return out, True


def add_coalition_item(names: list, name: str) -> tuple[list, bool]:
    """Append a coalition ``name`` to a copy of ``names``, de-duped by value.

    Returns a NEW list and a bool indicating whether it was added (False if the
    name was blank or already present). Inputs are never mutated.
    """
    out = list(names or [])
    clean = (name or "").strip()
    if not clean or clean in out:
        return out, False
    out.append(clean)
    return out, True


def remove_filter_item(items: list, index: int) -> list:
    """Return a NEW list with the element at ``index`` removed.

    Out-of-range indices return a copy unchanged. Works for both {"id","name"}
    dicts and bare coalition-name strings. Inputs are never mutated.
    """
    out = list(items or [])
    if 0 <= index < len(out):
        del out[index]
    return out


def rename_coalition_in_selection(selected: list, old: str, new: str) -> list:
    """Return a NEW selected-coalitions list with ``old`` replaced by ``new``.

    Used to keep ``config["intel_filter"]["parties"]["coalitions"]`` consistent
    when a coalition is renamed in the manager. Every occurrence of ``old`` is
    swapped for ``new`` in place (order preserved). If ``new`` already appears in
    the list the rename collapses onto it without creating a duplicate. Inputs
    are never mutated. ``old``/``new`` are compared exactly (coalition keys are
    case-sensitive dict keys).
    """
    out: list = []
    for name in list(selected or []):
        repl = new if name == old else name
        if repl not in out:
            out.append(repl)
    return out


def remove_coalition_from_selection(selected: list, name: str) -> list:
    """Return a NEW selected-coalitions list with every ``name`` removed.

    Used to keep ``config["intel_filter"]["parties"]["coalitions"]`` consistent
    when a coalition is deleted in the manager. Inputs are never mutated.
    """
    return [n for n in list(selected or []) if n != name]


def mutate_staging_lists(friendly: list[str], hostile: list[str],
                         action: str, name: str,
                         target: str = "friendly") -> tuple[list[str], list[str]]:
    """Pure, Tk-free mutator for the two staging-system lists.

    Returns NEW (friendly, hostile) lists; inputs are never mutated in place so
    this is safe to unit-test and to call from the UI.

    action:
      - "add":    add ``name`` to ``target`` ("friendly"/"hostile"). De-dupes
                  case-insensitively within the target list. If ``name`` already
                  exists (case-insensitively) in the OTHER list it is MOVED:
                  removed from the other list and appended to the target.
      - "remove": remove ``name`` (case-insensitive) from ``target`` only.

    ``name`` is normalized by stripping surrounding whitespace; the stripped
    display text is what gets stored (canonicalization beyond this is the
    caller's job and must not block list mutation). Blank names are ignored.
    """
    # Work on copies so callers' lists are never mutated.
    friendly = list(friendly)
    hostile = list(hostile)

    clean = (name or "").strip()
    if not clean:
        return friendly, hostile

    lower = clean.lower()

    def _without(lst: list[str]) -> list[str]:
        return [s for s in lst if s.strip().lower() != lower]

    if action == "remove":
        if target == "hostile":
            hostile = _without(hostile)
        else:
            friendly = _without(friendly)
        return friendly, hostile

    if action == "add":
        # Remove any existing copy from BOTH lists (handles de-dupe within the
        # target list and a move from the other list), then append to target.
        friendly = _without(friendly)
        hostile = _without(hostile)
        if target == "hostile":
            hostile.append(clean)
        else:
            friendly.append(clean)
        return friendly, hostile

    return friendly, hostile


# Channels EVE creates by default that are not intel channels. The picker's
# *suggestion* list hides these so discovery noise doesn't bury real intel
# channels; the user can still type any of them in by hand to track them.
_NON_INTEL_CHANNEL_NAMES = {
    "local", "corp", "alliance", "fleet", "rookie help",
}
_NON_INTEL_CHANNEL_PREFIXES = ("private chat",)


def normalize_tracked_channels(names, seed=None) -> list[str]:
    """Pure, Tk-free normalizer for the tracked intel-channel list.

    Strips surrounding whitespace from each name, drops blanks, and de-dupes
    case-insensitively while preserving first-seen order and the original
    casing of the first occurrence. Returns a NEW list; ``names`` is never
    mutated in place, so this is safe to unit-test and to call from the UI.

    If the cleaned result is empty and ``seed`` is provided, the seed is
    normalized the same way and returned instead — this implements the
    "missing/empty tracked list falls back to the seed (sorted INTEL_CHANNELS)"
    safety behavior in one place.
    """
    def _dedupe(raw) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in (raw or ()):
            clean = (item or "").strip()
            if not clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(clean)
        return out

    result = _dedupe(names)
    if not result and seed is not None:
        result = _dedupe(seed)
    return result


def filter_suggestion_channels(names) -> list[str]:
    """Pure, Tk-free noise filter for the channel-picker *suggestion* pool.

    Given discovered channel names, returns the subset suitable as default
    autocomplete suggestions: hides obvious non-intel system channels
    (case-insensitive exact "Local"/"Corp"/"Alliance"/"Fleet"/"Rookie Help"
    and anything starting with "Private Chat"). Order is preserved and the
    list is de-duped case-insensitively. This only affects what is *suggested*;
    callers may still let the user manually add any hidden name. Discovery
    itself is never filtered by this function.
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in (names or ()):
        clean = (item or "").strip()
        if not clean:
            continue
        lower = clean.lower()
        if lower in _NON_INTEL_CHANNEL_NAMES:
            continue
        if any(lower.startswith(p) for p in _NON_INTEL_CHANNEL_PREFIXES):
            continue
        if lower in seen:
            continue
        seen.add(lower)
        out.append(clean)
    return out


def compute_intel_channel_suggestions(discovered, tracked) -> list[str]:
    """Pure, Tk-free predicate for the Suggested intel-channels panel.

    Returns the discovered channel names whose name contains "intel" or
    "intelligence" (case-insensitive) and that are NOT already tracked
    (compared case-insensitively). Order follows ``discovered``; the result is
    de-duped case-insensitively, preserving first-seen casing. Returns a NEW
    list; inputs are never mutated, so this is safe to unit-test.
    """
    tracked_keys = {
        (t or "").strip().lower()
        for t in (tracked or ())
        if (t or "").strip()
    }
    out: list[str] = []
    seen: set[str] = set()
    for item in (discovered or ()):
        name = (item or "").strip()
        if not name:
            continue
        lower = name.lower()
        if "intel" not in lower and "intelligence" not in lower:
            continue
        if lower in tracked_keys or lower in seen:
            continue
        seen.add(lower)
        out.append(name)
    return out


def extract_staging_system_names(staging_system, *lists) -> list[str]:
    """Pure, Tk-free extractor for the systems worth pre-warming.

    Collects system names the user actually cares about: the single configured
    ``staging_system`` plus any number of staging *lists*. Each list item may be
    a plain name string or a ``{"id": ..., "name": ...}`` dict (the jump-range
    staging lists are persisted as either form, depending on when they were
    saved), so this normalizes both shapes to a bare name.

    Blanks are skipped and names are de-duped case-insensitively while
    preserving first-seen order and the original casing of the first
    occurrence. Returns a NEW list; inputs are never mutated, so this is safe to
    unit-test and to call from the prewarm worker.
    """
    def _name_of(item) -> str:
        if isinstance(item, dict):
            return str(item.get("name", "") or "").strip()
        return str(item or "").strip()

    out: list[str] = []
    seen: set[str] = set()

    def _add(raw):
        name = _name_of(raw)
        if not name:
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(name)

    _add(staging_system)
    for lst in lists:
        for item in (lst or ()):
            _add(item)
    return out


def build_linux_screenshot_cmds(wayland, available, x, y, w, h, out_path):
    """Choose Linux screenshot capture + clipboard commands.

    wayland: bool (True for a Wayland session). available: a set of tool names
    found on PATH. Returns (capture_cmd, clipboard_cmd, error):
      - capture_cmd: argv list that writes the region PNG to out_path, or None.
      - clipboard_cmd: argv list that reads a PNG from STDIN onto the clipboard,
        or None when no clipboard tool is available (caller saves a file).
      - error: a user-facing string when no capture tool exists, else None.
    """
    capture_cmd = None
    if wayland:
        if "grim" in available:
            capture_cmd = ["grim", "-g", f"{x},{y} {w}x{h}", out_path]
    else:
        if "maim" in available:
            capture_cmd = ["maim", "-g", f"{w}x{h}+{x}+{y}", out_path]
        elif "scrot" in available:
            capture_cmd = ["scrot", "-a", f"{x},{y},{w},{h}", out_path]
        elif "import" in available:
            capture_cmd = ["import", "-window", "root", "-crop", f"{w}x{h}+{x}+{y}", out_path]
    if capture_cmd is None:
        if wayland:
            return None, None, "No screenshot tool found (install grim for Wayland)"
        return None, None, "No screenshot tool found (install maim, scrot, or imagemagick)"
    clipboard_cmd = None
    if wayland:
        if "wl-copy" in available:
            clipboard_cmd = ["wl-copy", "--type", "image/png"]
    else:
        if "xclip" in available:
            clipboard_cmd = ["xclip", "-selection", "clipboard", "-t", "image/png"]
    return capture_cmd, clipboard_cmd, None


def _motd_link_initial_state(config) -> bool:
    """Initial state of the MOTD auto-update link at tab build.

    Always False, by design. The link is session-scoped: it must be turned on
    deliberately each session. Persisting it and restoring it on startup caused
    the app to push a freshly-opened (default) MOTD over the fleet's real one
    before the FC had set anything up, so any persisted ``motd_link`` value is
    intentionally ignored. ``config`` is accepted (and unused) to document that
    the decision does not depend on it and to pin this behavior in tests.
    """
    return False


def _filter_cap_entries(entries, only_region: str) -> list:
    """Filter capability asset entries down to a single region.

    Entries are dicts shaped ``{"ship", "location", "region"}``. When
    ``only_region`` is falsy the list is returned unchanged (no filtering).
    When set, only dict entries whose ``region`` matches survive; non-dict
    entries are dropped. Pure function so the role+region item predicate is
    unit-testable without Tk.
    """
    if not only_region:
        return list(entries)
    return [
        e for e in entries
        if isinstance(e, dict) and e.get("region") == only_region
    ]


def _exemption_entry_key(entry: dict):
    """Canonical de-dupe key for a doctrine ideal-% exemption entry.

    A "capital" meta entry de-dupes on kind alone (there is only ever one);
    "group"/"type" entries de-dupe on (kind, id). Pure so the editor's add
    helper is unit-testable without Tk."""
    kind = entry.get("kind")
    if kind == "capital":
        return ("capital",)
    return (kind, entry.get("id"))


def _add_exemption_entry(entries: list[dict], new: dict) -> list[dict]:
    """Return a NEW exemption list with ``new`` appended unless an entry with the
    same canonical key is already present (a no-op then). Copies entries so the
    caller's list is never mutated in place."""
    out = [dict(e) for e in (entries or [])]
    key = _exemption_entry_key(new)
    if any(_exemption_entry_key(e) == key for e in out):
        return out
    out.append(dict(new))
    return out


def _remove_exemption_entry(entries: list[dict], index: int) -> list[dict]:
    """Return a NEW exemption list with the entry at ``index`` removed. An
    out-of-range index is a safe no-op (returns a copy)."""
    out = [dict(e) for e in (entries or [])]
    if 0 <= index < len(out):
        del out[index]
    return out


def _read_text_file_for_import(path: str) -> str:
    """Read a text file for one-time import (EVE-O config). Split out so tests
    can stub it without touching the real filesystem. EVE-O writes UTF-8."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


def build_default_fleet_template(primary_name, primary_id, *, new_id=None):
    """Construct the seeded 'Default' fleet template (Subcaps/Caps wings).

    Pure: no self, no Tk, no ESI, no I/O. ``primary_name``/``primary_id`` pin the
    Subcaps wing commander (a wing_commander-role slot physically inside the
    Subcaps->DPS squad, per the established idiom); when ``primary_name`` is
    falsy the WC slot is left empty. ``doctrine_id`` is None so the doctrine_tag
    rules stay dormant until a doctrine is active, while the capital + default
    rules always route. Returns a validated FleetTemplate (validate_template has
    been run, so ``.broken`` flags are set).
    """
    import fleet_template_store as fts
    from uuid import uuid4
    wc_slot = fts.Slot(character=(primary_name or None), tag=None,
                       role="wing_commander",
                       character_id=(primary_id if primary_name else None))
    subcaps = fts.Wing(name="Subcaps", max_size=None, squads=[
        fts.Squad(name="DPS", max_size=None, slots=[wc_slot]),
        fts.Squad(name="Logi", max_size=None, slots=[]),
        fts.Squad(name="Special", max_size=None, slots=[]),
    ])
    caps = fts.Wing(name="Caps", max_size=None, squads=[
        fts.Squad(name="Caps", max_size=None, slots=[]),
    ])
    rules = [
        fts.AssignmentRule(priority=0,
            condition=fts.RuleCondition("capital", ""),
            action=fts.RuleAction("squad_member", "Caps", "Caps")),
        fts.AssignmentRule(priority=1,
            condition=fts.RuleCondition("doctrine_tag", "Logistics"),
            action=fts.RuleAction("squad_member", "Subcaps", "Logi")),
        fts.AssignmentRule(priority=2,
            condition=fts.RuleCondition("doctrine_tag", "DPS"),
            action=fts.RuleAction("squad_member", "Subcaps", "DPS")),
        fts.AssignmentRule(priority=3,
            condition=fts.RuleCondition("default", ""),
            action=fts.RuleAction("squad_member", "Subcaps", "DPS")),
    ]
    t = fts.FleetTemplate(id=new_id or uuid4().hex, name="Default",
                          doctrine_id=None, wings=[subcaps, caps],
                          rules=rules, settings=fts.RebalanceSettings())
    fts.validate_template(t)
    return t


class FCToolGUI:
    def __init__(self):
        _apply_dpi_awareness(_read_overlay_dpi_pref())
        self.root = tk.Tk()
        self.root.title("FCTool - Fleet Commander Assistant")
        self.root.geometry("1200x900")
        self.root.configure(bg=BG_DARK)
        self.root.minsize(1000, 700)

        # Try to set icon (non-critical)
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        # Serializes config.json writes across the Tk thread and background
        # workers (e.g. _refresh_ansiblex_from_esi) so a read-modify-write on
        # one thread cannot interleave with a write on another and corrupt or
        # clobber settings. Created before any save path can run.
        self._config_lock = threading.Lock()
        # Short TTL cache for get_fleet_info() shared by _fleet_boss_session and
        # _fleet_boss_info. The Fleet Templates window calls both back-to-back on
        # entering live mode; without this each does its own ESI round-trip.
        # (ts_monotonic, info_dict_or_None); see _cached_fleet_info.
        self._fleet_info_cache = None
        self.config = self._load_config()
        # One-time migration of the config-driven intel filter + coalition seed
        # (synchronous part; the Triumvirate. id is resolved off-thread later,
        # once esi_auth is set up). Must run before _build_ui() because the
        # Intelligence-tab filter panel reads config["intel_filter"].
        self._migrate_intel_filter_config()
        # User-editable, persisted jump-range staging systems (start empty —
        # no hard-coded seeds). Loaded from config["jump_range"], managed inline
        # on the Jump Range tab.
        _jr_cfg = self.config.get("jump_range", {})
        self._friendly_staging: list[str] = list(
            _jr_cfg.get("friendly_staging_systems", []) or [])
        self._hostile_staging: list[str] = list(
            _jr_cfg.get("hostile_staging_systems", []) or [])
        # User-editable, persisted intel channels (shared with the web UI via
        # config["intel_channels"]["tracked"]). On first run the key is seeded
        # from the hard-coded INTEL_CHANNELS so existing users keep today's
        # channels, then persisted. A missing/empty list falls back to the seed
        # for safety. cached_discovered backs the Settings picker's suggestions.
        self._tracked_intel_channels: list[str] = self._load_tracked_intel_channels()
        self.chat_monitor: ChatMonitor | None = None
        self.xup_counter: XUpCounter | None = None
        # Re-entry guard for the "Import from EVE" (ESI in-game fittings) flow.
        # Set True for the duration of a single import so a second click while
        # the (slow) ESI fetch is in flight is ignored instead of spawning a
        # second worker + picker window. Reset on every terminating path. The
        # button reference is stored in _build_fittings_subtab so it can be
        # disabled while busy. Touched only on the Tk thread.
        self._esi_import_busy = False
        self._esi_import_btn = None
        # Command-burst charge tracking. The tracker records "charge up" calls
        # parsed from fleet chat; the roster maps lowercased pilot name ->
        # ship_type_id (rebuilt each fleet poll) so build_pilot_rows can match
        # charge senders to their booster ships off-thread.
        self.charge_tracker = charge_tracker.ChargeTracker()
        self._booster_roster: dict[str, int] = {}   # lowercased name -> ship_type_id
        self._burst_icons: dict[str, object] = {}    # discipline -> tk.PhotoImage
        self._burst_icons_small: dict[str, object] = {}   # half-size copies for the inline top strip
        # Best-effort coalescing flag for booster-UI refreshes. It is read/written
        # from multiple threads (chat-monitor thread, fleet-fetch thread, Tk thread),
        # but is deliberately lock-free: the worst case of a race is one redundant or
        # briefly-skipped root.after(250, ...), which self-corrects on the next event.
        # The actual cross-thread hand-off is root.after, used the same way throughout
        # this file. (Set False again in _run_booster_refresh on the Tk thread.)
        self._booster_refresh_pending = False
        # Tk-thread-only booster render state (written/read only on the Tk thread,
        # so no lock is needed). Populated by _apply_booster_compute /
        # _update_specialized_roles and consumed by _render_links_section.
        self._booster_rows_by_name: dict = {}    # lowercased pilot name -> command_bursts.PilotRow
        self._booster_ship_names: dict = {}       # ship_type_id -> resolved hull name (or None)
        self._booster_is_boss: bool = False
        # Per-section ship-type expand state, keyed by id(content_frame) -> set of
        # open type_ids. Lets _populate_role_section preserve user expansions
        # across its frequent destroy/recreate rebuilds (every fleet poll and
        # every debounced charge-post).
        self._role_expand_state: dict = {}
        self._links_categories: dict = {}         # {ship_type_id: [(name, char_id), ...]} cached each poll
        self._links_threshold = 5
        self.zkill_monitor: ZKillMonitor | None = None
        self._intel_session = IntelSession()
        self._standings_cache = StandingsCache(
            path=_os.path.join(_app_dir(), "standings_cache.json")
        )
        self._standings_cache.load()
        # If the standings cache is older than 24h, refresh in the background.
        # This can't happen synchronously here because there may not be an
        # authenticated character yet, and we don't want to block app startup.
        if self._standings_cache.is_stale(max_age_hours=24):
            self._schedule_background_standings_refresh()
        self.jump_checker: JumpRangeChecker | None = None
        self._running = False
        self._chat_thread: threading.Thread | None = None
        self._sound_enabled = self.config.get("sound_on_ready", False)
        # Fleet loss tracker
        self._loss_tracker = FleetLossTracker()
        self._loss_audio_enabled = self.config.get("loss_audio_enabled", True)
        self._ansiblex_connections: list[str] = []  # "id1|id2" strings for ESI route
        # Maps (id1, id2) -> (name1, name2) for identifying Ansiblex jumps in routes
        self._ansiblex_id_pairs: dict[tuple[int, int], tuple[str, str]] = {}

        # ESI SSO auth — multi-character support
        esi_cfg = self.config.get("esi", {})
        self.esi_accounts: list[ESIAuth] = []
        if esi_cfg.get("client_id"):
            self.esi_accounts = load_all_tokens(
                client_id=esi_cfg["client_id"],
                client_secret=esi_cfg.get("client_secret", ""),
                callback_url=esi_cfg.get("callback_url", "http://localhost:8834/callback"),
            )
        # Primary character: saved primary_character_id first,
        # then tracked_character name, then first account available.
        primary_id = self.config.get("primary_character_id")
        tracked = self.config.get("tracked_character", "")
        self.esi_auth = None
        if primary_id:
            for acct in self.esi_accounts:
                if acct.character_id == primary_id:
                    self.esi_auth = acct
                    break
        if not self.esi_auth and tracked:
            for acct in self.esi_accounts:
                if acct.character_name == tracked:
                    self.esi_auth = acct
                    break
        if not self.esi_auth and self.esi_accounts:
            self.esi_auth = self.esi_accounts[0]

        # ── Eve-O Preview overlay state ─────────────────────────────────────
        self._overlay = None
        self._overlay_states: dict = {}        # name_lower -> CharState (poller)
        self._overlay_state_ts: dict = {}      # name_lower -> monotonic fetch ts
        self._overlay_after_id = None
        self._overlay_poller = None            # Phase 2 daemon thread
        self._overlay_poller_stop = None       # threading.Event while running
        self._overlay_status_label = None
        self._overlay_thumbs_fn = find_thumbs  # injectable for tests
        # Process-based Eve-O detection for the "thumbnails hidden" status state
        # (Eve-O can be running with every thumbnail window hidden). Injectable.
        self._overlay_preview_running_fn = preview_running

        # ── Native preview controller state ─────────────────────────────────
        self._preview_tiles = {}               # hwnd -> TileWindow
        self._preview_video_labels = {}        # hwnd -> composed on-video label text
        self._preview_tile_rects = {}          # hwnd -> (x, y, w, body_h) screen px
        self._preview_clients = {}             # hwnd -> ClientWindow
        self._preview_hotkeys = None           # HotkeyService (lazy, native only)
        self._preview_hotkey_map = {}          # hk_id -> action tuple
        self._preview_after_id = None
        self._preview_intel = {}               # system_id -> (ts, kind)
        self._preview_disabled_session = False
        self._preview_tick_count = 0           # drives the 8-tick re-letterbox check
        self._preview_tick_fails = 0           # consecutive failed ticks (BUG A guard)
        self._preview_last_key = ""            # last-activated char key (cycle anchor)
        self._preview_find_clients = eve_client_tracker.find_clients  # injectable
        self._preview_hotkey_factory = hotkey_service.HotkeyService  # injectable
        # ── Damage flash (Task B6) ──────────────────────────────────────────
        self._preview_damage = damage_flash.DamageFlashTracker()  # rolling-window tracker
        self._preview_gamelog = None           # GamelogMonitor (lazy, native only)
        self._preview_layer_hp = {}            # char key -> {shield,armor,hull} | None (poller-written)
        self._preview_damage_until = {}        # char key -> monotonic ts the red hold ends
        self._preview_damage_since = {}        # char key -> monotonic ts the pulse started
        self._preview_gamelog_factory = GamelogMonitor  # injectable
        # ── Hide rules / per-char selection (Task C2) ───────────────────────
        self._preview_lost_focus_since = None  # tick_count focus was first lost (or None)
        self._preview_win32 = None             # foreground backend; None → lazy real singleton
        # ── Active highlight / cycle exclusion / switch-external (Task C4) ────
        self._preview_excluded = set()         # session-only cycle-exclusion char keys
        self._preview_last_external_hwnd = None  # last non-EVE, non-ours foreground hwnd
        # Seed rules created once, the first time the feature is enabled.
        if self._overlay_cfg().get("enabled", False):
            self._overlay_seed_rules_if_empty()
        # Auto-start the controller if the user left it enabled last session.
        self.root.after(1200, self._overlay_boot_if_enabled)

        # Fitting / doctrine services (Fittings tab). Built AFTER esi_auth so the
        # type catalog's id->name fallback can reach the public ESI endpoint via
        # _catalog_esi_adapter. The catalog resolves names/slots from the bundled
        # fit_types.json (and caches ESI fallbacks to fit_types_cache.json); the
        # store persists the fittings library to app_dir()/fittings_library.json.
        import type_catalog as _type_catalog
        import fittings_store as _fittings_store
        self._migrate_fittings_config()
        self.type_catalog = _type_catalog.TypeCatalog(esi=self._catalog_esi_adapter())
        self.fittings = _fittings_store.FittingsStore(
            os.path.join(app_dir(), "fittings_library.json"))
        self.fittings.load()
        self.fittings.catalog = self.type_catalog   # enables Defenders auto-tag on add
        self._heal_fit_dna()   # one-time: rewrite legacy bare-id T3 DNA to canonical
        import fleet_template_store as _fleet_template_store
        self.fleet_templates = _fleet_template_store.FleetTemplateStore(
            os.path.join(app_dir(), "fleet_templates.json"))
        self.fleet_templates.load()
        # Seed the editable "Default" template once (survives user deletion via
        # the fleet_default_seeded config flag; never auto-applied).
        self._seed_default_fleet_template()
        self._fleet_template_window = None
        # Per-dialog/sub-tab state placeholders (populated as the tab is built).
        self._fit_selected_id: str | None = None
        self._doctrine_selected_id: str | None = None
        # Fittings-library column sort state (click-to-sort headers).
        self._fit_sort_column: str = "name"
        self._fit_sort_reverse: bool = False

        # Discover ansiblex from ESI if authenticated, else fall back to config
        self._refresh_ansiblex_from_esi()
        self._prewarm_cache_async()

        # If the coalition seed was freshly created this run, resolve
        # Triumvirate.'s alliance id in the background and fold it into the
        # "The Initiative." coalition. Never blocks startup.
        if getattr(self, "_coalitions_need_triumvirate", False):
            self._resolve_triumvirate_async()

        # Start fleet location refresh loop (updates role tracker locations)
        self.root.after(5000, self._refresh_fleet_locations)

        # Start current system refresh loop (ESI character location)
        self.root.after(3000, self._refresh_current_system)

        # Load system names for autocomplete (runs in background if cache miss)
        self._system_names: list[str] = []
        self._system_labels: dict[str, str] = {}
        self._load_system_names_async()

        self._build_ui()
        self._setup_modules()
        self._start_monitoring()

        # Auto-refresh character data in the background shortly after startup
        if self.esi_accounts and hasattr(self, '_char_tab_content'):
            self.root.after(3000, lambda: self._refresh_character_tab(force=True))

        # Start periodic character tab refresh (location + ship every 5 min)
        self.root.after(300_000, self._auto_refresh_character_tab)

        # Refresh the logi/cap channel cache shortly after startup so the MOTD
        # channel autocomplete reflects the latest logs (cached names are
        # already seeded synchronously above). No-ops if the logs path is unset.
        self.root.after(2500, self._motd_scan_channels)

        # Pre-generate loss threshold TTS audio in the background
        tts_helper.pregenerate([
            "Ten percent of fleet lost",
            "Twenty five percent of fleet lost",
            "Fifty percent of fleet lost",
        ])

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Global mouse-wheel router: scroll whatever scrollable area is hovered.
        self.root.bind_all("<MouseWheel>", self._on_global_mousewheel)
        self.root.bind_all("<Button-4>", self._on_global_mousewheel)
        self.root.bind_all("<Button-5>", self._on_global_mousewheel)

    # ── System Names for Autocomplete ─────────────────────────────────────────

    def _load_system_names_async(self):
        """Load system names and region map in background threads."""
        def load():
            try:
                names = get_sorted_names()
                self._system_names = names
                self.root.after(0, self._update_autocomplete_lists)
            except Exception as e:
                print(f"[FCTool] Error loading system names: {e}")

        def load_regions():
            try:
                systems = get_system_names()
                region_map = get_region_map()
                # Build name -> "Name (Region)" label map
                labels = {}
                for name, sid in systems.items():
                    region = region_map.get(str(sid), "")
                    if region:
                        labels[name] = f"{name} ({region})"
                self._system_labels = labels
                self.root.after(0, self._update_autocomplete_lists)
            except Exception as e:
                print(f"[FCTool] Error loading region map: {e}")

        threading.Thread(target=load, daemon=True).start()
        threading.Thread(target=load_regions, daemon=True).start()

    def _update_autocomplete_lists(self):
        """Push loaded system names + region labels into all autocomplete widgets."""
        all_names = list(self._system_names)
        labels = dict(self._system_labels)
        print(f"[Autocomplete] Updating: {len(all_names)} names, {len(labels)} labels")

        # Merge EVE Scout WH system names
        try:
            conns = fetch_connections()
            for c in conns:
                if c.dest_system_name and c.dest_system_name not in all_names:
                    all_names.append(c.dest_system_name)
                # Add region label for WH-connected systems
                if c.dest_system_name and c.dest_region_name:
                    labels[c.dest_system_name] = f"{c.dest_system_name} ({c.dest_region_name})"
            all_names.sort()
        except Exception:
            pass

        for attr in ['_range_origin', '_range_dest', '_wh_origin', '_wh_dest', '_staging_entry', '_range_add_entry', '_motd_staging_entry']:
            widget = getattr(self, attr, None)
            if widget and hasattr(widget, 'update_completions'):
                widget.update_completions(all_names, labels)

        # Refresh the intel-filter location autocomplete if it is in System
        # mode (system names may have just finished loading).
        if (getattr(self, '_loc_add_entry', None) is not None
                and getattr(self, '_loc_type_var', None) is not None
                and self._loc_type_var.get() == "System"):
            self._loc_add_entry.update_completions(list(self._system_names))

    # ── Ansiblex Connection Resolver ─────────────────────────────────────────

    def _refresh_ansiblex_from_esi(self):
        """Pull ansiblex gates from ESI if authenticated, else use config.
        Runs in background. Updates config and re-resolves connections."""
        def do_refresh():
            if self.esi_auth and self.esi_auth.is_authenticated:
                try:
                    gates = self.esi_auth.discover_ansiblex_gates()
                    if gates:
                        # Hold the config lock across the mutate-then-write so
                        # this background thread cannot interleave its
                        # read-modify-write with a save on the Tk thread. The
                        # write is done inline (not via _save_config) because
                        # the lock is non-reentrant. A failed save must not
                        # crash the worker.
                        with self._config_lock:
                            self.config["ansiblex_connections"] = gates
                            try:
                                atomic_write_json(
                                    CONFIG_PATH, self.config, indent=4)
                            except Exception:
                                log.exception(
                                    "Failed to save config.json (ansiblex refresh)")
                        print(f"[Ansiblex] ESI refresh: {len(gates)} gate(s)")
                    else:
                        print("[Ansiblex] ESI returned no gates, keeping config")
                except Exception as e:
                    print(f"[Ansiblex] ESI refresh failed: {e}")
            else:
                print("[Ansiblex] Not authenticated, using config file")
            # Resolve whatever is in config (ESI-refreshed or static)
            self._resolve_ansiblex_sync()

        threading.Thread(target=do_refresh, daemon=True).start()

    def _resolve_ansiblex_sync(self):
        """Resolve all ansiblex pairs from config into ID strings."""
        pairs = self.config.get("ansiblex_connections", [])
        if not pairs:
            return
        resolved = []
        id_pairs = {}
        for pair in pairs:
            if len(pair) == 2:
                id1 = search_system(pair[0])
                id2 = search_system(pair[1])
                if id1 and id2:
                    resolved.append(f"{id1}|{id2}")
                    id_pairs[(id1, id2)] = (pair[0], pair[1])
                    id_pairs[(id2, id1)] = (pair[1], pair[0])
        self._ansiblex_connections = resolved
        self._ansiblex_id_pairs = id_pairs
        # Clear route cache so new ansiblex connections take effect
        from jump_range import _route_disk_cache, _cache_lock
        with _cache_lock:
            # Only clear routes that used ansiblex (have ':' suffix in key)
            stale = [k for k in _route_disk_cache if ':' in k and '|' in k]
            for k in stale:
                del _route_disk_cache[k]
        print(f"[Ansiblex] Resolved {len(resolved)} gate(s), cleared {len(stale)} cached routes")

    def _resolve_ansiblex_async(self):
        """Resolve Ansiblex system name pairs to ID pairs in background."""
        pairs = self.config.get("ansiblex_connections", [])
        if not pairs:
            return

        def resolve():
            resolved = []
            id_pairs = {}
            for pair in pairs:
                if len(pair) == 2:
                    id1 = search_system(pair[0])
                    id2 = search_system(pair[1])
                    if id1 and id2:
                        resolved.append(f"{id1}|{id2}")
                        id_pairs[(id1, id2)] = (pair[0], pair[1])
                        id_pairs[(id2, id1)] = (pair[1], pair[0])
                        print(f"[Ansiblex] Resolved: {pair[0]} <-> {pair[1]} ({id1}|{id2})")
            self._ansiblex_connections = resolved
            self._ansiblex_id_pairs = id_pairs
            print(f"[Ansiblex] {len(resolved)} gate(s) configured")

        threading.Thread(target=resolve, daemon=True).start()

    def _get_ansiblex_connections(self) -> list[str] | None:
        """Return Ansiblex connection strings, resolving synchronously if needed."""
        if self._ansiblex_connections:
            return self._ansiblex_connections
        pairs = self.config.get("ansiblex_connections", [])
        if not pairs:
            return None
        resolved = []
        id_pairs = {}
        for pair in pairs:
            if len(pair) == 2:
                id1 = search_system(pair[0])
                id2 = search_system(pair[1])
                if id1 and id2:
                    resolved.append(f"{id1}|{id2}")
                    id_pairs[(id1, id2)] = (pair[0], pair[1])
                    id_pairs[(id2, id1)] = (pair[1], pair[0])
        if resolved:
            self._ansiblex_connections = resolved
            self._ansiblex_id_pairs.update(id_pairs)
            print(f"[Ansiblex] Sync-resolved {len(resolved)} gate(s)")
        return resolved or None

    def _prewarm_cache_async(self):
        """Pre-resolve the user's own staging systems in background so the first
        range check is fast.

        Warms ONLY systems the user actually cares about — no hard-coded group
        geography: the configured ``zkillboard.staging_system`` plus the
        friendly/hostile jump-range staging lists
        (``jump_range.friendly_staging_systems`` / ``hostile_staging_systems``,
        each a list of name strings or ``{id,name}`` dicts). Names are deduped
        case-insensitively and blanks are skipped by
        ``extract_staging_system_names``."""
        staging = self.config.get("zkillboard", {}).get("staging_system", "")
        jr_cfg = self.config.get("jump_range", {}) or {}
        friendly = jr_cfg.get("friendly_staging_systems", []) or []
        hostile = jr_cfg.get("hostile_staging_systems", []) or []
        systems = extract_staging_system_names(staging, friendly, hostile)
        if not systems:
            return

        def prewarm():
            from jump_range import save_route_cache
            import system_coords
            system_coords._load()          # load the local table once, off the UI thread
            for name in systems:
                search_system(name)        # local-first; only ESI for unknown systems
            save_route_cache()
            print(f"[Cache] Pre-warmed {len(systems)} staging system(s)")
            try:
                import system_cache
                system_cache.get_region_map()  # build/load region label map off the UI thread
            except Exception:
                pass

        threading.Thread(target=prewarm, daemon=True).start()

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    cfg = json.load(f)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
                # An existing config.json that is corrupt/unreadable would
                # otherwise crash startup — and in the frozen windowed exe the
                # traceback is invisible, so the app just fails to open.
                # Preserve the bad file for recovery and fall back to defaults
                # so the app ALWAYS starts.
                backup = f"{CONFIG_PATH}.corrupt"
                try:
                    shutil.copy2(CONFIG_PATH, backup)
                    log.warning(
                        "config.json is corrupt/unreadable (%s); backed up to "
                        "%s and reverting to defaults", e, backup)
                except OSError as copy_err:
                    log.warning(
                        "config.json is corrupt/unreadable (%s) and the backup "
                        "to %s failed (%s); reverting to defaults",
                        e, backup, copy_err)
                cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        else:
            # No config.json (e.g. a fresh source clone or first run): run on
            # the built-in defaults so the app works out of the box — the public
            # Client ID is baked in (PKCE, no secret). Deep-copied so the
            # module-level default is never mutated.
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        # Auto-detect the EVE chat-logs folder when it's blank or a placeholder
        # (handles OneDrive-redirected Documents). Explicit user paths are kept.
        cfg["eve_logs_path"] = resolve_eve_logs_path(cfg.get("eve_logs_path", ""))
        return cfg

    def _save_config(self):
        # Atomic write (temp file + os.replace) so a crash/full-disk/lock
        # mid-write cannot corrupt every setting at once. Locked so the Tk
        # thread and background workers cannot interleave writes. A failed
        # settings save must never crash the UI.
        try:
            with self._config_lock:
                atomic_write_json(CONFIG_PATH, self.config, indent=4)
        except Exception:
            log.exception("Failed to save config.json")

    def _seed_default_fleet_template(self):
        """Seed the editable 'Default' fleet template once, on first run.

        Idempotent via the ``fleet_default_seeded`` config flag (NOT
        store-emptiness) so a user who deletes 'Default' is not re-seeded.
        Data-only: appends to the store, saves, and sets the flag; it never
        auto-applies, never opens the Fleet Templates window, and never touches
        ESI. Any failure is logged and swallowed so a bad seed cannot crash
        startup.
        """
        try:
            if self.config.get("fleet_default_seeded", False):
                return
            name = getattr(self.esi_auth, "character_name", None) if self.esi_auth else None
            cid = getattr(self.esi_auth, "character_id", None) if self.esi_auth else None
            t = build_default_fleet_template(name, cid)
            self.fleet_templates.templates.append(t)
            self.fleet_templates.save()
            if name:
                self.fleet_templates.cache_character(name, cid)
            self.config["fleet_default_seeded"] = True
            self._save_config()
        except Exception:
            log.exception("[fleet-templates] failed to seed the Default template")

    def _save_staging_systems(self):
        """Persist the friendly/hostile staging lists into config["jump_range"]
        and write config.json immediately.

        Deliberately does NOT call _save_settings() and does NOT restart any
        modules — this is a lightweight config write only.
        """
        jr = self.config.setdefault("jump_range", {})
        jr["friendly_staging_systems"] = list(self._friendly_staging)
        jr["hostile_staging_systems"] = list(self._hostile_staging)
        self._save_config()

    def _load_tracked_intel_channels(self) -> list[str]:
        """Return the tracked intel channels from config, seeding on first run.

        Reads ``config["intel_channels"]["tracked"]``. If that key is missing or
        empty, it is SEEDED from ``sorted(INTEL_CHANNELS)`` (empty by default)
        and persisted to config.json. The returned list is normalized
        (whitespace-stripped, case-insensitively de-duped); a normalized list
        that round-trips to a different value is written back so config stays
        clean.
        """
        seed = sorted(INTEL_CHANNELS)
        ic = self.config.setdefault("intel_channels", {})
        raw = ic.get("tracked")
        tracked = normalize_tracked_channels(raw, seed=seed)
        # Persist when seeding (key absent/empty) or when normalization changed
        # the stored value, so the on-disk config matches what we use in memory.
        if not raw or list(raw) != tracked:
            ic["tracked"] = list(tracked)
            self._save_config()
        return tracked

    def _save_tracked_intel_channels(self):
        """Persist the tracked intel channels into config["intel_channels"]
        and write config.json immediately. Lightweight config write only — does
        not restart modules (mirrors _save_staging_systems)."""
        ic = self.config.setdefault("intel_channels", {})
        ic["tracked"] = list(self._tracked_intel_channels)
        self._save_config()

    # ── Intel filter config (migration + persistence) ─────────────────────────

    def _migrate_intel_filter_config(self):
        """One-time migration to the config-driven intel filter schema.

        Idempotent — only seeds keys that are absent, so it is safe to call on
        every startup. Two independent migrations:

        * ``config["coalitions"]`` — seeded from
          ``intel_filter.build_default_coalitions()`` (The Initiative. starts
          with just The Initiative.; Triumvirate. is folded in later by
          :meth:`_resolve_triumvirate_async` off the main thread). When freshly
          seeded, ``self._coalitions_need_triumvirate`` is set so __init__ kicks
          off that background resolution.
        * ``config["intel_filter"]`` — seeded to "Anywhere + Anyone" (matches
          all K-space) with ``min_pilots`` carried over from the existing
          ``zkillboard.min_pilots_involved`` (default 25) and ``max_jumps`` 0.

        After migration the in-memory ``self._intel_filter`` is bound to the
        live ``config["intel_filter"]`` dict so the panel and the display filter
        always see the current state; ``_save_config()`` persists it.
        """
        self._coalitions_need_triumvirate = False
        changed = False

        if "coalitions" not in self.config or not isinstance(
            self.config.get("coalitions"), dict
        ):
            self.config["coalitions"] = intel_filter.build_default_coalitions()
            self._coalitions_need_triumvirate = True
            changed = True

        if "intel_filter" not in self.config or not isinstance(
            self.config.get("intel_filter"), dict
        ):
            zk_cfg = self.config.get("zkillboard", {})
            try:
                seed_min = int(zk_cfg.get("min_pilots_involved", 25))
            except (TypeError, ValueError):
                seed_min = 25
            self.config["intel_filter"] = {
                "combine": "AND",
                "location": {"anywhere": True, "systems": [], "regions": []},
                "parties": {
                    "anyone": True,
                    "alliances": [],
                    "corporations": [],
                    "coalitions": [],
                },
                "min_pilots": seed_min,
                "max_jumps": 0,
                "capitals": {"alert": True, "bypass_filter": False},
            }
            changed = True

        # Bind in-memory criteria to the live config dict.
        self._intel_filter = self.config["intel_filter"]

        # Backfill the capitals settings for EXISTING configs that have an
        # intel_filter but predate the capital-alert feature. Never clobber an
        # already-present capitals dict — only fill in missing keys.
        caps = self._intel_filter.get("capitals")
        if not isinstance(caps, dict):
            caps = {"alert": True, "bypass_filter": False}
            self._intel_filter["capitals"] = caps
            changed = True
        if "alert" not in caps:
            caps["alert"] = True
            changed = True
        if "bypass_filter" not in caps:
            caps["bypass_filter"] = False
            changed = True

        if changed:
            self._save_config()

    def _migrate_fittings_config(self):
        """Seed ``config['fittings']`` on first run (idempotent).

        Mirrors :meth:`_migrate_intel_filter_config`: only fills in keys that are
        absent, so it is safe to call on every startup. The block holds the
        Fittings-tab preferences kept outside the fittings library file (the tag
        vocabulary lives in the library; everything UI/session-scoped lives here):

        * ``pyfa_path``    — last-used pyfa savepath/dir for the pyfa importer.
        * ``motd_budget``  — conservative raw-markup MOTD length ceiling (~3000).
        * ``motd_template``— persisted MOTD-writer field selections (Phase 7).
        * ``logi_channel`` — remembered logi/cap channel for the MOTD writer.
        """
        changed = False
        fit_cfg = self.config.get("fittings")
        if not isinstance(fit_cfg, dict):
            fit_cfg = {}
            self.config["fittings"] = fit_cfg
            changed = True
        defaults = {
            "pyfa_path": "",
            "motd_budget": 3000,
            "motd_template": {},
            "logi_channel": "",
            "saved_motds": [],
        }
        for key, val in defaults.items():
            if key not in fit_cfg:
                fit_cfg[key] = val
                changed = True
        if changed:
            self._save_config()

    def _catalog_esi_adapter(self):
        """Return an id->name resolver for TypeCatalog's unknown-ID fallback.

        The app's ESIAuth has no id->name method (its resolve_ids/resolve_names
        are name->id). TypeCatalog needs the inverse, served by the public
        ``POST /universe/names/`` endpoint (no auth, batched up to 1000 ids),
        which returns ``[{id, name, category}]``. We reshape that into the
        ``{id: {"name", "category"}}`` map the catalog expects. TypeCatalog
        caches results to fit_types_cache.json, so this is hit only for IDs
        absent from the bundled fit_types.json.
        """
        gui = self

        class _Adapter:
            def resolve_names(self, type_ids):
                try:
                    rows = gui.esi_auth.esi_post_public(
                        "/universe/names/", list(type_ids)) or []
                except Exception:
                    return {}
                out = {}
                for r in rows:
                    if isinstance(r, dict) and "id" in r:
                        out[r["id"]] = {
                            "name": r.get("name"),
                            "category": r.get("category"),
                        }
                return out

        return _Adapter()

    def _resolve_triumvirate_async(self):
        """Resolve Triumvirate.'s alliance id off-thread and fold it into the
        "The Initiative." coalition. Best-effort; never blocks or raises.

        On success appends ``{"id","name"}`` to
        ``config["coalitions"]["The Initiative."]["alliances"]`` (de-duped),
        persists, and refreshes any open coalition pickers on the main thread.
        """
        def worker():
            try:
                if not self.esi_auth:
                    return
                res = self.esi_auth.resolve_alliance("Triumvirate.")
            except Exception:
                return
            if not res or "id" not in res:
                return
            self.root.after(0, self._apply_triumvirate_resolution, res)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_triumvirate_resolution(self, res: dict):
        """Main-thread: add resolved Triumvirate. to The Initiative. + persist."""
        try:
            coalitions = self.config.setdefault("coalitions", {})
            init = coalitions.setdefault(
                "The Initiative.", {"alliances": [], "corporations": []}
            )
            alliances = init.setdefault("alliances", [])
            new_list, added = add_filter_item(alliances, res)
            if added:
                init["alliances"] = new_list
                self._save_config()
                self._refresh_coalition_pickers()
        except Exception as e:
            print(f"[IntelFilter] Triumvirate fold-in failed: {e}")

    def _refresh_coalition_pickers(self):
        """Refresh the coalition autocomplete candidates if the parties picker
        is currently in Coalition mode. Safe to call before the panel exists.

        The parties add-entry is shared across Alliance/Corporation/Coalition,
        so we only push coalition names when Coalition is the active type — that
        avoids clobbering live alliance/corp type-ahead suggestions."""
        type_var = getattr(self, "_par_type_var", None)
        entry = getattr(self, "_parties_coalition_entry", None)
        if (type_var is not None and entry is not None
                and type_var.get() == "Coalition"
                and hasattr(entry, "update_completions")):
            entry.update_completions(
                sorted(self.config.get("coalitions", {}).keys()))

    def _save_intel_filter(self):
        """Persist the intel filter to config (lightweight write, no restart).

        ``self._intel_filter`` is the same dict object as
        ``config["intel_filter"]`` so it is already current; this just flushes
        config.json. The display filter reads config live, so no module reload
        is needed (mirrors _save_staging_systems)."""
        self.config["intel_filter"] = self._intel_filter
        self._save_config()

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background=BG_DARK)
        style.configure("Panel.TFrame", background=BG_PANEL)
        style.configure("Dark.TLabel", background=BG_DARK, foreground=FG_TEXT,
                         font=("Consolas", 10))
        style.configure("Header.TLabel", background=BG_DARK, foreground=FG_ACCENT,
                         font=("Consolas", 14, "bold"))
        style.configure("Status.TLabel", background=BG_PANEL, foreground=FG_TEXT,
                         font=("Consolas", 10))
        # Resolve a guaranteed-available monospace family ONCE and build concrete
        # Font objects for the shared button styles. A bare ("Consolas", 10) tuple
        # can fail to resolve on some machines (style.lookup -> ''), which under
        # the clam theme collapses ttk.Buttons to ~6x6 px with invisible text.
        # Consolas is first so Windows is visually unchanged; TkFixedFont is a Tk
        # built-in alias that always exists, as the final fallback.
        _families = set(tkfont.families(self.root))
        _btn_family = next(
            (f for f in ("Consolas", "Courier New", "DejaVu Sans Mono",
                         "Liberation Mono", "Lucida Console", "Monaco",
                         "TkFixedFont")
             if f in _families),
            "TkFixedFont")
        # Kept on self so they are not garbage-collected.
        self._btn_font = tkfont.Font(family=_btn_family, size=10)
        self._btn_font_bold = tkfont.Font(family=_btn_family, size=10, weight="bold")

        style.configure("Dark.TButton", background=BG_ENTRY, foreground=FG_TEXT,
                         font=self._btn_font, borderwidth=1, padding=(8, 4))
        style.map("Dark.TButton",
                  background=[("active", "#1a5a90")],
                  foreground=[("active", FG_WHITE)])
        style.configure("Green.TButton", background="#006644", foreground=FG_WHITE,
                         font=self._btn_font_bold, padding=(8, 4))
        style.map("Green.TButton",
                  background=[("active", "#008855")])
        style.configure("Red.TButton", background="#660022", foreground=FG_WHITE,
                         font=self._btn_font_bold, padding=(8, 4))
        style.map("Red.TButton",
                  background=[("active", "#882233")])
        style.configure("Dark.TNotebook", background=BG_DARK, borderwidth=0)
        style.configure("Dark.TNotebook.Tab", background=BG_PANEL,
                         foreground=FG_TEXT, font=("Consolas", 10),
                         padding=[12, 4])
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", BG_ENTRY)],
                  foreground=[("selected", FG_ACCENT)])

        # Dark-theme ALL ttk.Comboboxes app-wide (filter type selectors, ship
        # menu, WH size, character filters, coalition manager). The base
        # "TCombobox" style is restyled so every combobox — almost all of which
        # are state="readonly" here — shows a dark field with light text instead
        # of the clam default (white field / black text).
        style.configure(
            "TCombobox",
            fieldbackground=BG_ENTRY, background=BG_ENTRY, foreground=FG_TEXT,
            arrowcolor=FG_TEXT, bordercolor=BORDER_COLOR,
            lightcolor=BG_ENTRY, darkcolor=BG_ENTRY,
            selectbackground="#1a5a90", selectforeground=FG_WHITE,
            font=("Consolas", 9),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", BG_ENTRY), ("disabled", BG_PANEL)],
            foreground=[("readonly", FG_TEXT), ("disabled", FG_DIM)],
            selectbackground=[("readonly", BG_ENTRY)],
            selectforeground=[("readonly", FG_TEXT)],
            background=[("active", BG_ENTRY)],
            arrowcolor=[("disabled", FG_DIM), ("active", FG_WHITE)],
        )

        # Dark-theme ALL ttk scrollbars app-wide (Roles, Specialized Roles,
        # Character tab, Settings tab — all default Vertical.TScrollbar). The
        # base "Vertical.TScrollbar"/"Horizontal.TScrollbar" styles are restyled
        # so every ttk scrollbar inherits a dark trough + accent thumb instead of
        # the clam default (white/grey). Classic tk.Scrollbars (e.g. inside
        # scrolledtext.ScrolledText) are NOT reached by this — see
        # _theme_scrolledtext_bar for those.
        for _sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(
                _sb,
                troughcolor=BG_DARK, background=BG_ENTRY, arrowcolor=FG_TEXT,
                bordercolor=BORDER_COLOR, darkcolor=BG_ENTRY, lightcolor=BG_ENTRY,
                troughrelief="flat", relief="flat", borderwidth=0,
            )
            style.map(
                _sb,
                background=[("active", "#1a5a90"), ("disabled", BG_PANEL)],
                arrowcolor=[("active", FG_WHITE), ("disabled", FG_DIM)],
            )

        # Dark-theme the Treeview used by the Fittings library list (the only
        # ttk.Treeview in the app). Heading + rows get the dark palette; the
        # selected row uses the same accent-blue as listboxes/comboboxes.
        style.configure(
            "Dark.Treeview",
            background=BG_ENTRY, fieldbackground=BG_ENTRY, foreground=FG_TEXT,
            bordercolor=BORDER_COLOR, borderwidth=0, font=("Consolas", 9),
            rowheight=20,
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", "#1a5a90")],
            foreground=[("selected", FG_WHITE)],
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=BG_PANEL, foreground=FG_ACCENT, font=("Consolas", 9, "bold"),
            relief="flat",
        )
        style.map(
            "Dark.Treeview.Heading",
            background=[("active", BG_ENTRY)],
        )

        # The drop-down POPUP is a plain Tk Listbox inside the combobox popdown
        # that ttk styles do NOT reach — theme it via the option database on the
        # root. This runs before any combobox is created (panels build later),
        # so the options apply to every popup app-wide.
        self.root.option_add("*TCombobox*Listbox.background", BG_ENTRY)
        self.root.option_add("*TCombobox*Listbox.foreground", FG_WHITE)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#1a5a90")
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG_WHITE)
        self.root.option_add("*TCombobox*Listbox.font", "Consolas 9")

        # ── Title Bar ─────────────────────────────────────────────────────────
        title_frame = tk.Frame(self.root, bg=BG_DARK, pady=8)
        title_frame.pack(fill=tk.X)
        tk.Label(title_frame, text="FCTool", font=("Consolas", 18, "bold"),
                 fg=FG_ACCENT, bg=BG_DARK).pack(side=tk.LEFT, padx=15)
        tk.Label(title_frame, text="Fleet Commander Assistant",
                 font=("Consolas", 11), fg=FG_DIM, bg=BG_DARK
                 ).pack(side=tk.LEFT, padx=5)

        # Staging system indicator (always visible)
        staging_name = self.config.get("zkillboard", {}).get("staging_system", "")
        self._staging_display = tk.Label(
            title_frame, text=f"Staging: {staging_name}" if staging_name else "Staging: --",
            font=("Consolas", 10, "bold"), fg=FG_YELLOW, bg=BG_DARK,
        )
        self._staging_display.pack(side=tk.LEFT, padx=20)

        # Current system indicator (pulled from ESI location)
        self._current_system_display = tk.Label(
            title_frame, text="System: --",
            font=("Consolas", 10, "bold"), fg=FG_GREEN, bg=BG_DARK,
        )
        self._current_system_display.pack(side=tk.LEFT, padx=(0, 20))
        self._current_system_name = ""
        self._current_system_region = ""

        # EVE Time (UTC) — always-visible clock (EVE servers run on UTC).
        self._eve_clock = tk.Label(
            title_frame, text="EVE --:--:--",
            font=("Consolas", 12, "bold"), fg=FG_ACCENT, bg=BG_DARK)
        self._eve_clock.pack(side=tk.RIGHT, padx=15)
        self._update_eve_clock()

        # Status indicators on right
        self._status_frame = tk.Frame(title_frame, bg=BG_DARK)
        self._status_frame.pack(side=tk.RIGHT, padx=15)
        self._chat_status = tk.Label(self._status_frame, text="CHAT: --",
                                      font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK)
        self._chat_status.pack(side=tk.LEFT, padx=8)
        self._zkill_status = tk.Label(self._status_frame, text="ZKILL: --",
                                       font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK)
        self._zkill_status.pack(side=tk.LEFT, padx=8)

        # ── Notebook (Tabs) ──────────────────────────────────────────────────
        self.notebook = ttk.Notebook(self.root, style="Dark.TNotebook")
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self._build_xup_tab()
        self._build_intel_tab()
        self._build_range_tab()
        self._build_wh_route_tab()
        self._build_character_tab()
        self._build_fitting_tab()
        self._build_settings_tab()

        # Track zkill alert notifications
        self._zkill_has_unread = False
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _update_eve_clock(self):
        """Refresh the always-visible EVE-time (UTC) clock once per second."""
        from datetime import datetime, timezone
        try:
            now = datetime.now(timezone.utc)
            self._eve_clock.config(text=now.strftime("EVE  %Y.%m.%d  %H:%M:%S"))
        except tk.TclError:
            return  # widget gone (app closing)
        try:
            self.root.after(1000, self._update_eve_clock)
        except (tk.TclError, RuntimeError):
            pass

    # ── X-Up Tab ──────────────────────────────────────────────────────────────

    def _build_xup_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Fleet Management  ")

        # ── Combined Fleet Status Bar (X-Up + Losses) ────────────────────────
        status_frame = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                 highlightbackground=BORDER_COLOR, highlightthickness=1)
        status_frame.pack(fill=tk.X, padx=10, pady=(8, 4))

        threshold = self.config.get("xup", {}).get("threshold", 50)

        # Left: X-UP section
        xup_section = tk.Frame(status_frame, bg=BG_PANEL)
        xup_section.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, pady=6)

        xup_label_row = tk.Frame(xup_section, bg=BG_PANEL)
        xup_label_row.pack(fill=tk.X)

        tk.Label(xup_label_row, text="X-UP:", font=("Consolas", 10, "bold"),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(0, 5))

        self._xup_count_label = tk.Label(xup_label_row, text="0",
                                          font=("Consolas", 22, "bold"),
                                          fg=FG_ACCENT, bg=BG_PANEL)
        self._xup_count_label.pack(side=tk.LEFT)

        tk.Label(xup_label_row, text="/",
                 font=("Consolas", 16), fg=FG_DIM, bg=BG_PANEL
                 ).pack(side=tk.LEFT, padx=(2, 2))

        # Editable threshold spinbox inline with the counter
        self._threshold_var = tk.StringVar(value=str(threshold))
        self._threshold_spin = tk.Spinbox(
            xup_label_row, from_=1, to=500,
            textvariable=self._threshold_var,
            font=("Consolas", 14, "bold"), width=4,
            bg=BG_ENTRY, fg=FG_YELLOW, insertbackground=FG_WHITE,
            buttonbackground=BG_PANEL, borderwidth=1, relief=tk.FLAT,
            command=self._on_threshold_change,
        )
        self._threshold_spin.pack(side=tk.LEFT)
        self._threshold_spin.bind("<Return>", lambda e: self._on_threshold_change())
        self._threshold_spin.bind("<FocusOut>", lambda e: self._on_threshold_change())

        ttk.Button(xup_label_row, text="Reset", style="Red.TButton",
                   command=self._reset_xup).pack(side=tk.LEFT, padx=(8, 10))
        ttk.Button(xup_label_row, text="Remove…", style="Dark.TButton",
                   command=self._open_remove_xup_dialog).pack(side=tk.LEFT, padx=(0, 8))

        self._xup_status = tk.Label(xup_label_row, text="Waiting for fleet chat...",
                                     font=("Consolas", 10, "bold"),
                                     fg=FG_DIM, bg=BG_PANEL)
        self._xup_status.pack(side=tk.LEFT, padx=(5, 0))

        self._xup_canvas = tk.Canvas(xup_section, height=12,
                                      bg=BG_DARK, highlightthickness=0)
        self._xup_canvas.pack(fill=tk.X, pady=(3, 0))

        # Vertical divider
        tk.Frame(status_frame, bg=BORDER_COLOR, width=1
                 ).pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)

        # Right: LOSSES section (filled in by _build_loss_bar)
        self._loss_section = tk.Frame(status_frame, bg=BG_PANEL)
        self._loss_section.pack(side=tk.LEFT, padx=10, pady=6)

        # Settings gear (overflow menu for Test Audio / Reset Losses)
        self._build_status_bar_menu(status_frame)

        # ── Role Tracker Section ──────────────────────────────────────────────
        role_header = tk.Frame(tab, bg=BG_DARK)
        role_header.pack(fill=tk.X, padx=10, pady=(4, 2))
        tk.Label(role_header, text="Role Tracker", font=("Consolas", 10, "bold"),
                 fg=FG_ACCENT, bg=BG_DARK).pack(side=tk.LEFT)
        tk.Label(role_header, text="(not case sensitive)", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_DARK).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(role_header, text="+ Add Role", style="Dark.TButton",
                   command=self._add_role_slot).pack(side=tk.LEFT, padx=8)
        ttk.Button(role_header, text="Reset All", style="Red.TButton",
                   command=self._reset_all_roles).pack(side=tk.LEFT, padx=3)
        ttk.Button(role_header, text="Collapse All", style="Dark.TButton",
                   command=lambda: self._set_all_roles_collapsed(True)).pack(side=tk.LEFT, padx=3)
        ttk.Button(role_header, text="Expand All", style="Dark.TButton",
                   command=lambda: self._set_all_roles_collapsed(False)).pack(side=tk.LEFT, padx=3)

        # Preset role buttons — row 1: defaults, row 2: custom
        preset_container = tk.Frame(tab, bg=BG_DARK)
        preset_container.pack(fill=tk.X, padx=10, pady=(0, 2))

        _PRESET_LABEL_W = 8  # Fixed width so rows align
        preset_row1 = tk.Frame(preset_container, bg=BG_DARK)
        preset_row1.pack(fill=tk.X)
        tk.Label(preset_row1, text="Presets:", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_DARK, width=_PRESET_LABEL_W, anchor=tk.W
                 ).pack(side=tk.LEFT, padx=(0, 4))

        self._preset_row2 = tk.Frame(preset_container, bg=BG_DARK)
        # Row 2 only packed when custom presets exist

        self._default_presets = [
            ("C-Cyno", "c", "Cyno", None),
            ("D-Dictors", "d", "Dictors", None),
            ("F-Fax 3", "f", "FAX", 3),
            ("Z-Defenders-8", "z", "Defenders", 8),
            ("1-Dreads-10", "1", "Dreads", 10),
        ]
        self._preset_frame = preset_row1
        self._MAX_CUSTOM_PRESETS = 8
        self._rebuild_preset_buttons()
        ttk.Button(role_header, text="Screenshot", style="Dark.TButton",
                   command=self._take_screenshot).pack(side=tk.RIGHT, padx=3)
        ttk.Button(role_header, text="Fleet Templates", style="Dark.TButton",
                   command=self._open_fleet_templates).pack(side=tk.RIGHT, padx=3)
        # Kick Pods lives with the right-hand group. Right-packing lays out
        # right-to-left, so packing it last places it left of Fleet Templates:
        #   … [Kick Pods] [Fleet Templates] [Screenshot]
        ttk.Button(role_header, text="Kick Pods", style="Red.TButton",
                   command=self._kick_pods_from_fleet).pack(side=tk.RIGHT, padx=3)
        self._screenshot_link = tk.Label(role_header, text="", font=("Consolas", 9),
                                          fg=FG_GREEN, bg=BG_DARK)
        self._screenshot_link.pack(side=tk.RIGHT, padx=5)

        # Role tracker container — bounded height so it never displaces
        # Fleet Composition / Specialized Roles below. Scrolls if roles overflow.
        # Uses a 2-column grid when role count crosses the threshold.
        self._ROLE_2COL_THRESHOLD = 3
        self._ROLE_AREA_MAX_HEIGHT = 300  # pixels

        role_outer = tk.Frame(tab, bg=BG_DARK, height=self._ROLE_AREA_MAX_HEIGHT)
        role_outer.pack(fill=tk.X, padx=10, pady=2)
        role_outer.pack_propagate(False)  # Respect height even if empty

        self._role_canvas = tk.Canvas(role_outer, bg=BG_DARK, highlightthickness=0)
        role_scrollbar = ttk.Scrollbar(
            role_outer, orient=tk.VERTICAL, command=self._role_canvas.yview,
        )
        self._role_container = tk.Frame(self._role_canvas, bg=BG_DARK)
        self._role_container.bind(
            "<Configure>",
            lambda e: self._role_canvas.configure(
                scrollregion=self._role_canvas.bbox("all")
            ),
        )
        self._role_canvas_window = self._role_canvas.create_window(
            (0, 0), window=self._role_container, anchor=tk.NW,
        )
        # Keep inner frame width equal to canvas width so grid columns expand
        self._role_canvas.bind(
            "<Configure>",
            lambda e: self._role_canvas.itemconfig(
                self._role_canvas_window, width=e.width
            ),
        )
        self._role_canvas.configure(yscrollcommand=role_scrollbar.set)
        self._role_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        role_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Configure 2 columns with equal weight for grid layout
        self._role_container.grid_columnconfigure(0, weight=1, uniform="role")
        self._role_container.grid_columnconfigure(1, weight=1, uniform="role")

        # Mouse-wheel scrolling handled by the global router (_on_global_mousewheel).
        self._register_scroll_canvas(self._role_canvas)

        self._role_slots: list[dict] = []

        # ── Fleet Loss Tracker (inline in status bar) ────────────────────────
        tk.Label(self._loss_section, text="LOSSES:",
                 font=("Consolas", 10, "bold"),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(0, 5))
        self._loss_status_label = tk.Label(
            self._loss_section, text="(waiting for fleet)",
            font=("Consolas", 10), fg=FG_DIM, bg=BG_PANEL, cursor="question_arrow",
        )
        self._loss_status_label.pack(side=tk.LEFT, padx=(0, 10))
        _loss_tip = (
            "Mainline Fleet: tackle losses (frigs, dessies, ceptors, AFs, EAFs, T3Ds)\n"
            "are ignored — only major ship losses count toward alerts.\n"
            "Support Fleet: all losses count. Mode is auto-detected from fleet comp."
        )
        self._loss_status_label.bind(
            "<Enter>", lambda e, t=_loss_tip: self._show_tooltip(e, t)
        )
        self._loss_status_label.bind(
            "<Leave>", lambda e: self._hide_tooltip()
        )

        self._loss_audio_var = tk.BooleanVar(value=self._loss_audio_enabled)

        def _on_loss_audio_toggle():
            self._loss_audio_enabled = self._loss_audio_var.get()
            self.config["loss_audio_enabled"] = self._loss_audio_enabled
            self._save_config()

        tk.Checkbutton(self._loss_section, text="Audio",
                       variable=self._loss_audio_var,
                       font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                       selectcolor=BG_ENTRY, activebackground=BG_PANEL,
                       activeforeground=FG_YELLOW,
                       command=_on_loss_audio_toggle,
                       ).pack(side=tk.LEFT)

        # ── Fleet Composition & Specialized Roles ────────────────────────────
        comp_outer = tk.Frame(tab, bg=BG_DARK)
        comp_outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 2))

        # Left panel: Fleet Composition (Top 10 ships)
        comp_left = tk.Frame(comp_outer, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                              highlightbackground=BORDER_COLOR, highlightthickness=1)
        comp_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

        tk.Label(comp_left, text="Fleet Composition", font=("Consolas", 10, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).pack(anchor=tk.W, padx=8, pady=(4, 2))

        self._fleet_size_label = tk.Label(comp_left, text="Fleet Size: --",
                                           font=("Consolas", 10, "bold"),
                                           fg=FG_YELLOW, bg=BG_PANEL)
        self._fleet_size_label.pack(anchor=tk.W, padx=8, pady=(0, 4))

        # Doctrine selector + clickable link → Fittings ▸ Doctrines (Phase C).
        doc_row = tk.Frame(comp_left, bg=BG_PANEL)
        doc_row.pack(anchor=tk.W, fill=tk.X, padx=8, pady=(0, 2))
        tk.Label(doc_row, text="Doctrine:", font=("Consolas", 9),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT)
        self._fleet_doctrine_var = tk.StringVar(
            value=self.config.get("fleet", {}).get("active_doctrine", ""))
        self._fleet_doctrine_combo = ttk.Combobox(
            doc_row, textvariable=self._fleet_doctrine_var, state="readonly",
            font=("Consolas", 9), width=22)
        self._fleet_doctrine_combo.pack(side=tk.LEFT, padx=4)
        self._fleet_doctrine_combo.bind(
            "<<ComboboxSelected>>", lambda e: self._on_fleet_doctrine_change())
        self._fleet_doctrine_link = tk.Label(
            doc_row, text="↗ open", font=("Consolas", 9, "underline"),
            fg=FG_ACCENT, bg=BG_PANEL, cursor="hand2")
        self._fleet_doctrine_link.pack(side=tk.LEFT, padx=6)
        self._fleet_doctrine_link.bind(
            "<Button-1>", lambda e: self._open_active_doctrine())
        self._refresh_fleet_doctrine_combo()

        comp_header = tk.Frame(comp_left, bg=BG_PANEL)
        comp_header.pack(fill=tk.X, padx=8)
        tk.Label(comp_header, text="Ship Type", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT)

        self._fleet_comp_frame = tk.Frame(comp_left, bg=BG_PANEL)
        self._fleet_comp_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        self._fleet_comp_labels: list[tk.Label] = []
        self._fleet_comp_prev: list[tuple[str, int]] = []  # for flicker prevention

        # Right panel: Specialized Roles (collapsible sections)
        comp_right_outer = tk.Frame(comp_outer, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                     highlightbackground=BORDER_COLOR, highlightthickness=1)
        comp_right_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))

        tk.Label(comp_right_outer, text="Specialized Roles", font=("Consolas", 10, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).pack(anchor=tk.W, padx=8, pady=(4, 2))

        # Scrollable container for specialized roles
        spec_canvas = tk.Canvas(comp_right_outer, bg=BG_PANEL, highlightthickness=0)
        spec_scrollbar = ttk.Scrollbar(comp_right_outer, orient=tk.VERTICAL,
                                        command=spec_canvas.yview)
        self._spec_roles_frame = tk.Frame(spec_canvas, bg=BG_PANEL)
        self._spec_roles_frame.bind(
            "<Configure>",
            lambda e: spec_canvas.configure(scrollregion=spec_canvas.bbox("all"))
        )
        spec_canvas.create_window((0, 0), window=self._spec_roles_frame, anchor=tk.NW)
        spec_canvas.configure(yscrollcommand=spec_scrollbar.set)
        spec_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        spec_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse-wheel scrolling handled by the global router.
        self._register_scroll_canvas(spec_canvas)

        # Top row: the red-color note on the left, and the command-burst coverage
        # strip (fleet-aggregate \u2713/\u2717 per discipline) inline on the right, so the
        # icons sit high and compact instead of taking their own full-height row.
        self._load_burst_icons()
        spec_top_row = tk.Frame(self._spec_roles_frame, bg=BG_PANEL)
        spec_top_row.pack(fill=tk.X, padx=4, pady=(0, 2))
        tk.Label(spec_top_row, text="\u26a0 Red = insufficient numbers",
                 font=("Consolas", 8), fg="#ff6666", bg=BG_PANEL
                 ).pack(side=tk.LEFT, anchor=tk.W)
        # Persistent container created once; only its children are rebuilt by
        # _render_coverage_strip on each poll.
        self._booster_strip = tk.Frame(spec_top_row, bg=BG_PANEL)
        self._booster_strip.pack(side=tk.RIGHT, anchor=tk.E)
        ttk.Button(spec_top_row, text="Remove link…", style="Dark.TButton",
                   command=self._open_remove_charge_dialog
                   ).pack(side=tk.RIGHT, padx=(0, 6))

        # Create collapsible sections (order matters for display)
        # Links / Command Ships sits ABOVE DPS and is expanded by default.
        self._links_container, self._links_content, self._links_count = \
            self._create_collapsible_section(
                self._spec_roles_frame, "Links / Command Ships", collapsed=False)
        self._dps_container, self._dps_content, self._dps_count = \
            self._create_collapsible_section(self._spec_roles_frame, "DPS")
        # Non-boss banner. Created on _spec_roles_frame (NOT _links_container) so
        # it stays visible even when the Links section is collapsed. Packed /
        # forgotten dynamically by _render_boss_banner (just before the Links
        # container). Per-pilot booster charges now render INSIDE _links_content
        # via _render_links_section, so they collapse with the section.
        self._booster_banner = tk.Label(
            self._spec_roles_frame, bg=BG_PANEL, fg=FG_YELLOW,
            font=("Consolas", 8), anchor=tk.W, justify=tk.LEFT, wraplength=320)
        self._logi_container, self._logi_content, self._logi_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Logistics")
        self._defenders_container, self._defenders_content, self._defenders_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Defenders")
        self._cyno_container, self._cyno_content, self._cyno_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Cyno")
        self._webs_container, self._webs_content, self._webs_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Webs")
        self._hics_container, self._hics_content, self._hics_count = \
            self._create_collapsible_section(self._spec_roles_frame, "HICs")
        self._fax_container, self._fax_content, self._fax_count = \
            self._create_collapsible_section(self._spec_roles_frame, "FAX")
        self._dreads_container, self._dreads_content, self._dreads_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Dreadnoughts")
        self._bridge_container, self._bridge_content, self._bridge_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Bridge")

        # Store cached fleet locations for specialized role pilot info
        self._fleet_locations_cache: dict[str, tuple[str, str, str]] = {}

        # ── X-Up Log ─────────────────────────────────────────────────────────
        # Collapsible drawer (mirrors the Cyno Check drawer). Starts CLOSED;
        # the log widget is built into an unpacked body so writers keep working
        # while it is hidden. New x-up events do NOT auto-expand the drawer.
        self._xup_log_expanded = False

        self._xup_log_drawer_frame = tk.Frame(
            tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
            highlightbackground=BORDER_COLOR, highlightthickness=1)
        self._xup_log_drawer_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        xup_log_header = tk.Frame(self._xup_log_drawer_frame, bg=BG_PANEL)
        xup_log_header.pack(fill=tk.X, padx=10, pady=4)

        self._xup_log_toggle_btn = tk.Label(
            xup_log_header, text="▶ X-Up Log",
            font=("Consolas", 10, "bold"), fg=FG_ACCENT, bg=BG_PANEL,
            cursor="hand2",
        )
        self._xup_log_toggle_btn.pack(side=tk.LEFT)
        self._xup_log_toggle_btn.bind(
            "<Button-1>", lambda e: self._toggle_xup_log())

        # Body (hidden by default)
        self._xup_log_body = tk.Frame(self._xup_log_drawer_frame, bg=BG_PANEL)

        self._xup_log = scrolledtext.ScrolledText(
            self._xup_log_body, height=4, font=("Consolas", 9),
            bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
            selectbackground="#1a5a90", wrap=tk.WORD, state=tk.DISABLED,
            borderwidth=1, relief=tk.RIDGE
        )
        self._xup_log.pack(fill=tk.X, padx=10, pady=(0, 6))
        self._theme_scrolledtext_bar(self._xup_log)
        self._xup_log.tag_config("xup", foreground=FG_GREEN)
        self._xup_log.tag_config("fire", foreground=FG_RED, font=("Consolas", 10, "bold"))
        self._xup_log.tag_config("ready", foreground=FG_YELLOW, font=("Consolas", 10, "bold"))
        self._xup_log.tag_config("dim", foreground=FG_DIM)
        self._xup_log.tag_config("role", foreground=FG_MAGENTA)

        # Paint the empty-state coverage strip (all ✗) on startup.
        self._schedule_booster_refresh()

    # ── Config-driven intel filter panel ──────────────────────────────────────

    def _build_intel_filter_panel(self, parent):
        """Build the criteria-based fight-alert filter panel.

        Layout:
          * Top row — Min Pilots spinbox, Max Jumps spinbox, Combine toggle
            ("Location AND/OR Parties").
          * Two side-by-side group panels — LOCATION and INVOLVED PARTIES —
            each with an Anywhere/Anyone checkbox, an add-row (type selector +
            AutocompleteEntry + Add), and a listbox (Remove + double-click).

        Every change persists immediately to ``config["intel_filter"]`` via
        :meth:`_save_intel_filter` and the display filter reads it live.
        """
        flt = self._intel_filter

        filter_frame = tk.Frame(parent, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                highlightbackground=BORDER_COLOR,
                                highlightthickness=1)
        filter_frame.pack(fill=tk.X, padx=10, pady=(2, 5))

        # ── Collapsible header (always visible) ────────────────────────────
        # Mirrors the "Paste Intel" drawer on this tab: a clickable label with
        # a ▼/▶ arrow that pack/forgets the body frame so the user can reclaim
        # vertical space for the live feed. Default = expanded.
        self._intel_filter_expanded = True
        self._intel_filter_header = tk.Label(
            filter_frame, text="▼ Filters",
            font=("Consolas", 10, "bold"), fg=FG_ACCENT, bg=BG_PANEL,
            cursor="hand2",
        )
        self._intel_filter_header.pack(anchor="w", padx=10, pady=4)
        self._intel_filter_header.bind(
            "<Button-1>", lambda e: self._toggle_intel_filter_panel())

        # Body holds all filter content; collapsing it pack_forgets this frame.
        self._intel_filter_body = tk.Frame(filter_frame, bg=BG_PANEL)
        self._intel_filter_body.pack(fill=tk.X)

        # ── Top row: Min Pilots, Max Jumps, Combine ────────────────────────
        top = tk.Frame(self._intel_filter_body, bg=BG_PANEL)
        top.pack(fill=tk.X, padx=10, pady=(5, 4))

        tk.Label(top, text="Min Pilots:", font=("Consolas", 9),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        try:
            _min0 = int(flt.get("min_pilots", 25))
        except (TypeError, ValueError):
            _min0 = 25
        self._zkill_min_pilots_var = tk.StringVar(value=str(_min0))
        self._zkill_min_pilots_spin = tk.Spinbox(
            top, from_=1, to=500, textvariable=self._zkill_min_pilots_var,
            font=("Consolas", 10), width=4, bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, buttonbackground=BG_PANEL,
            borderwidth=1, relief=tk.RIDGE,
            command=self._on_min_pilots_change,
        )
        self._zkill_min_pilots_spin.pack(side=tk.LEFT, padx=(4, 15))
        self._zkill_min_pilots_spin.bind(
            "<Return>", lambda e: self._on_min_pilots_change())
        self._zkill_min_pilots_spin.bind(
            "<FocusOut>", lambda e: self._on_min_pilots_change())

        tk.Label(top, text="Max Jumps:", font=("Consolas", 9),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        try:
            _max0 = int(flt.get("max_jumps", 0))
        except (TypeError, ValueError):
            _max0 = 0
        self._zkill_max_jumps_var = tk.StringVar(value=str(_max0))
        self._zkill_max_jumps_spin = tk.Spinbox(
            top, from_=0, to=200, textvariable=self._zkill_max_jumps_var,
            font=("Consolas", 10), width=4, bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, buttonbackground=BG_PANEL,
            borderwidth=1, relief=tk.RIDGE,
            command=self._on_max_jumps_change,
        )
        self._zkill_max_jumps_spin.pack(side=tk.LEFT, padx=(4, 5))
        self._zkill_max_jumps_spin.bind(
            "<Return>", lambda e: self._on_max_jumps_change())
        self._zkill_max_jumps_spin.bind(
            "<FocusOut>", lambda e: self._on_max_jumps_change())
        tk.Label(top, text="(0=no limit)", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(0, 20))

        # Combine toggle
        tk.Label(top, text="Match:", font=("Consolas", 9, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).pack(side=tk.LEFT, padx=(0, 4))
        combine0 = "OR" if str(flt.get("combine", "AND")).upper() == "OR" else "AND"
        self._intel_combine_var = tk.StringVar(value=combine0)
        for label, val in (("Location AND Parties", "AND"),
                           ("Location OR Parties", "OR")):
            tk.Radiobutton(
                top, text=label, value=val, variable=self._intel_combine_var,
                font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                selectcolor=BG_ENTRY, activebackground=BG_PANEL,
                activeforeground=FG_ACCENT, command=self._on_combine_change,
            ).pack(side=tk.LEFT, padx=(0, 4))

        # ── Capitals row: hostile-capital alert toggles ────────────────────
        # A "hostile capital" is a cap whose corp AND alliance are both outside
        # the standings-based friendly set (your own + blues). These two boxes
        # gate the in-app intel feed only.
        caps_cfg = flt.setdefault(
            "capitals", {"alert": True, "bypass_filter": False})
        cap_row = tk.Frame(self._intel_filter_body, bg=BG_PANEL)
        cap_row.pack(fill=tk.X, padx=10, pady=(0, 4))

        self._cap_alert_var = tk.BooleanVar(
            value=bool(caps_cfg.get("alert", True)))
        tk.Checkbutton(
            cap_row, text="Alert on hostile capitals",
            variable=self._cap_alert_var,
            font=("Consolas", 9), fg=FG_ORANGE, bg=BG_PANEL,
            selectcolor=BG_ENTRY, activebackground=BG_PANEL,
            activeforeground=FG_ORANGE, command=self._on_capital_alert_toggle,
        ).pack(side=tk.LEFT)

        self._cap_bypass_var = tk.BooleanVar(
            value=bool(caps_cfg.get("bypass_filter", False)))
        self._cap_bypass_chk = tk.Checkbutton(
            cap_row, text="…even outside my location/parties filter",
            variable=self._cap_bypass_var,
            font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
            selectcolor=BG_ENTRY, activebackground=BG_PANEL,
            activeforeground=FG_TEXT, command=self._on_capital_bypass_toggle,
            disabledforeground=FG_DIM,
        )
        self._cap_bypass_chk.pack(side=tk.LEFT, padx=(12, 0))
        # Checkbox 2 is only meaningful when checkbox 1 is on.
        self._cap_bypass_chk.config(
            state=tk.NORMAL if self._cap_alert_var.get() else tk.DISABLED)

        # Dim hint: bypass + no Max-Jumps lets hostile-cap alerts in from all of
        # K-space; pair with Max Jumps to bound the firehose.
        tk.Label(
            self._intel_filter_body,
            text="(bypass + Max Jumps=0 → hostile-cap alerts from all of "
                 "K-space; set Max Jumps to bound it)",
            font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL,
        ).pack(anchor="w", padx=12, pady=(0, 2))

        # ── Two side-by-side group panels ──────────────────────────────────
        groups = tk.Frame(self._intel_filter_body, bg=BG_PANEL)
        groups.pack(fill=tk.X, padx=10, pady=(0, 6))
        groups.columnconfigure(0, weight=1, uniform="grp")
        groups.columnconfigure(1, weight=1, uniform="grp")

        # ---- LOCATION group ----
        loc = tk.Frame(groups, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                       highlightbackground=BORDER_COLOR, highlightthickness=1)
        loc.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        tk.Label(loc, text="LOCATION", font=("Consolas", 9, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).pack(anchor="w", padx=6, pady=(4, 0))

        self._loc_anywhere_var = tk.BooleanVar(
            value=bool(flt.get("location", {}).get("anywhere", True)))
        tk.Checkbutton(
            loc, text="Anywhere (all K-space)", variable=self._loc_anywhere_var,
            font=("Consolas", 9), fg=FG_ORANGE, bg=BG_PANEL,
            selectcolor=BG_ENTRY, activebackground=BG_PANEL,
            activeforeground=FG_ORANGE, command=self._on_anywhere_toggle,
        ).pack(anchor="w", padx=6, pady=(0, 2))

        loc_add = tk.Frame(loc, bg=BG_PANEL)
        loc_add.pack(fill=tk.X, padx=6, pady=2)
        self._loc_type_var = tk.StringVar(value="System")
        loc_type = ttk.Combobox(
            loc_add, textvariable=self._loc_type_var, state="readonly",
            values=["System", "Region"], width=8, font=("Consolas", 9),
        )
        loc_type.pack(side=tk.LEFT, padx=(0, 4))
        self._loc_add_entry = AutocompleteEntry(
            loc_add, list(self._system_names), width=18,
            font=("Consolas", 10), bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
            # Disabled/readonly (when "Anywhere" is checked) shows a dark muted
            # red tint instead of Tk's default white, signalling "can't type
            # here right now" against the dark theme.
            disabledbackground="#3a1620", readonlybackground="#3a1620",
            disabledforeground=FG_DIM,
        )
        self._loc_add_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                 padx=(0, 4))
        self._loc_add_entry.bind("<Return>", lambda e: self._on_location_add())
        loc_type.bind("<<ComboboxSelected>>",
                      lambda e: self._on_location_type_change())
        ttk.Button(loc_add, text="Add", style="Dark.TButton",
                   command=self._on_location_add).pack(side=tk.LEFT)

        # Wrapping chip area: a Text widget into which compact removable chip
        # frames are embedded via window_create. The Text wraps embedded
        # windows like words, giving automatic horizontal flow + line wrap so
        # many selections fit without scrolling. Kept non-editable; embedded
        # chips stay clickable even while the Text is DISABLED.
        self._loc_chips = tk.Text(
            loc, height=4, wrap=tk.CHAR, bg=BG_ENTRY, fg=FG_TEXT,
            borderwidth=1, relief=tk.RIDGE, cursor="arrow", takefocus=0,
            highlightthickness=0, padx=4, pady=3, font=("Consolas", 9),
        )
        self._loc_chips.pack(fill=tk.X, padx=6, pady=(2, 2))
        self._make_chip_text_readonly(self._loc_chips)

        loc_btns = tk.Frame(loc, bg=BG_PANEL)
        loc_btns.pack(fill=tk.X, padx=6, pady=(0, 4))
        self._loc_status = tk.Label(loc_btns, text="", font=("Consolas", 8),
                                    fg=FG_DIM, bg=BG_PANEL)
        self._loc_status.pack(side=tk.LEFT, padx=8)

        # ---- INVOLVED PARTIES group ----
        par = tk.Frame(groups, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                       highlightbackground=BORDER_COLOR, highlightthickness=1)
        par.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        tk.Label(par, text="INVOLVED PARTIES", font=("Consolas", 9, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).pack(anchor="w", padx=6, pady=(4, 0))

        self._par_anyone_var = tk.BooleanVar(
            value=bool(flt.get("parties", {}).get("anyone", True)))
        tk.Checkbutton(
            par, text="Anyone", variable=self._par_anyone_var,
            font=("Consolas", 9), fg=FG_ORANGE, bg=BG_PANEL,
            selectcolor=BG_ENTRY, activebackground=BG_PANEL,
            activeforeground=FG_ORANGE, command=self._on_anyone_toggle,
        ).pack(anchor="w", padx=6, pady=(0, 2))

        par_add = tk.Frame(par, bg=BG_PANEL)
        par_add.pack(fill=tk.X, padx=6, pady=2)
        self._par_type_var = tk.StringVar(value="Alliance")
        par_type = ttk.Combobox(
            par_add, textvariable=self._par_type_var, state="readonly",
            values=["Alliance", "Corporation", "Coalition"], width=11,
            font=("Consolas", 9),
        )
        par_type.pack(side=tk.LEFT, padx=(0, 4))
        self._par_add_entry = AutocompleteEntry(
            par_add, [], width=16, font=("Consolas", 10), bg=BG_ENTRY,
            fg=FG_WHITE, insertbackground=FG_WHITE, borderwidth=1,
            relief=tk.RIDGE,
            # Disabled/readonly (when "Anyone" is checked) shows a dark muted
            # red tint instead of Tk's default white, signalling "can't type
            # here right now" against the dark theme.
            disabledbackground="#3a1620", readonlybackground="#3a1620",
            disabledforeground=FG_DIM,
        )
        self._par_add_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                 padx=(0, 4))
        self._par_add_entry.bind("<Return>", lambda e: self._on_parties_add())
        # add="+" so we DON'T clobber AutocompleteEntry's own <KeyRelease>
        # dropdown handler — both fire (local dropdown + live ESI type-ahead).
        self._par_add_entry.bind(
            "<KeyRelease>", self._on_parties_typeahead, add="+")
        par_type.bind("<<ComboboxSelected>>",
                      lambda e: self._on_parties_type_change())
        ttk.Button(par_add, text="Add", style="Dark.TButton",
                   command=self._on_parties_add).pack(side=tk.LEFT)
        # Coalition picker reference (refreshed when coalitions change).
        self._parties_coalition_entry = self._par_add_entry

        # Wrapping chip area (see LOCATION group above for technique).
        self._par_chips = tk.Text(
            par, height=4, wrap=tk.CHAR, bg=BG_ENTRY, fg=FG_TEXT,
            borderwidth=1, relief=tk.RIDGE, cursor="arrow", takefocus=0,
            highlightthickness=0, padx=4, pady=3, font=("Consolas", 9),
        )
        self._par_chips.pack(fill=tk.X, padx=6, pady=(2, 2))
        self._make_chip_text_readonly(self._par_chips)

        par_btns = tk.Frame(par, bg=BG_PANEL)
        par_btns.pack(fill=tk.X, padx=6, pady=(0, 4))
        ttk.Button(par_btns, text="Manage coalitions…",
                   style="Dark.TButton",
                   command=self._open_coalition_manager).pack(side=tk.LEFT)
        self._par_status = tk.Label(par_btns, text="", font=("Consolas", 8),
                                    fg=FG_DIM, bg=BG_PANEL)
        self._par_status.pack(side=tk.LEFT, padx=8)

        # Debounce handle for parties type-ahead.
        self._par_typeahead_after = None

        # Populate listboxes + enabled-state from current config.
        self._on_location_type_change()
        self._on_parties_type_change()
        self._refresh_intel_filter_lists()
        self._sync_location_enabled()
        self._sync_parties_enabled()

    # ---- Top-row handlers ----

    def _on_min_pilots_change(self):
        try:
            val = int(self._zkill_min_pilots_var.get())
        except ValueError:
            return
        val = max(1, min(500, val))
        self._intel_filter["min_pilots"] = val
        self._save_intel_filter()

    def _on_max_jumps_change(self):
        try:
            val = int(self._zkill_max_jumps_var.get())
        except ValueError:
            return
        val = max(0, min(200, val))
        self._intel_filter["max_jumps"] = val
        self._save_intel_filter()

    def _on_combine_change(self):
        val = "OR" if self._intel_combine_var.get() == "OR" else "AND"
        self._intel_filter["combine"] = val
        self._save_intel_filter()

    # ---- Capital-alert toggles ----

    def _on_capital_alert_toggle(self):
        caps = self._intel_filter.setdefault(
            "capitals", {"alert": True, "bypass_filter": False})
        on = bool(self._cap_alert_var.get())
        caps["alert"] = on
        # Checkbox 2 (bypass) is only meaningful when alerting is on.
        try:
            self._cap_bypass_chk.config(
                state=tk.NORMAL if on else tk.DISABLED)
        except Exception:
            pass
        self._save_intel_filter()

    def _on_capital_bypass_toggle(self):
        caps = self._intel_filter.setdefault(
            "capitals", {"alert": True, "bypass_filter": False})
        caps["bypass_filter"] = bool(self._cap_bypass_var.get())
        self._save_intel_filter()

    # ---- Anywhere / Anyone toggles ----

    def _on_anywhere_toggle(self):
        loc = self._intel_filter.setdefault(
            "location", {"anywhere": True, "systems": [], "regions": []})
        loc["anywhere"] = bool(self._loc_anywhere_var.get())
        self._save_intel_filter()
        self._sync_location_enabled()

    def _on_anyone_toggle(self):
        par = self._intel_filter.setdefault(
            "parties",
            {"anyone": True, "alliances": [], "corporations": [],
             "coalitions": []})
        par["anyone"] = bool(self._par_anyone_var.get())
        self._save_intel_filter()
        self._sync_parties_enabled()

    def _sync_location_enabled(self):
        """Dim the location chip area / add-row when Anywhere is checked.

        When Anywhere is on the chip area is ignored by the filter, so it is
        greyed (darker bg, dim chips) and the add-entry is disabled. The chip
        ``✕`` buttons remain wired but the muted styling signals they have no
        effect on matching until Anywhere is unchecked.
        """
        anywhere = bool(self._loc_anywhere_var.get())
        try:
            self._loc_add_entry.config(
                state=tk.DISABLED if anywhere else tk.NORMAL)
        except Exception:
            pass
        # Rebuild chips so their dim/normal styling and the ignored hint match.
        self._refresh_intel_filter_lists()

    def _sync_parties_enabled(self):
        anyone = bool(self._par_anyone_var.get())
        try:
            self._par_add_entry.config(
                state=tk.DISABLED if anyone else tk.NORMAL)
        except Exception:
            pass
        self._refresh_intel_filter_lists()

    # ---- Type-selector handlers (swap autocomplete candidates) ----

    def _on_location_type_change(self):
        kind = self._loc_type_var.get()
        if kind == "Region":
            try:
                names = sorted(set(get_region_map().values()))
            except Exception:
                names = []
        else:
            names = list(self._system_names)
        self._loc_add_entry.update_completions(names)

    def _on_parties_type_change(self):
        kind = self._par_type_var.get()
        if kind == "Coalition":
            names = sorted(self.config.get("coalitions", {}).keys())
        else:
            # Alliance / Corporation: live type-ahead only (no local list).
            names = []
        self._par_add_entry.update_completions(names)

    # ---- Location add / remove ----

    def _on_location_add(self):
        if self._loc_anywhere_var.get():
            return
        kind = self._loc_type_var.get()
        name = self._loc_add_entry.get().strip()
        if not name:
            return
        if kind == "System":
            self._loc_status.config(text=f"resolving {name}…", fg=FG_DIM)

            def worker():
                try:
                    sid = search_system(name)
                except Exception:
                    sid = None
                self.root.after(
                    0, self._apply_location_add, "systems", sid, name)

            threading.Thread(target=worker, daemon=True).start()
        else:  # Region
            self._loc_status.config(text=f"resolving {name}…", fg=FG_DIM)

            def worker():
                res = None
                try:
                    if self.esi_auth:
                        res = self.esi_auth.resolve_region(name)
                except Exception:
                    res = None
                rid = res.get("id") if res else None
                rname = res.get("name") if res else name
                self.root.after(
                    0, self._apply_location_add, "regions", rid, rname)

            threading.Thread(target=worker, daemon=True).start()

    def _apply_location_add(self, kind: str, item_id, name: str):
        """Main-thread: add a resolved {id,name} to location[kind]."""
        if item_id is None:
            self._loc_status.config(text=f"couldn't resolve {name}", fg=FG_RED)
            return
        loc = self._intel_filter.setdefault(
            "location", {"anywhere": True, "systems": [], "regions": []})
        new_list, added = add_filter_item(
            loc.get(kind, []), {"id": item_id, "name": name})
        loc[kind] = new_list
        if added:
            # Adding the first concrete item auto-unchecks Anywhere.
            if loc.get("anywhere"):
                loc["anywhere"] = False
                self._loc_anywhere_var.set(False)
                self._sync_location_enabled()
            self._loc_add_entry.delete(0, tk.END)
            self._loc_status.config(text=f"added {name}", fg=FG_GREEN)
        else:
            self._loc_status.config(text=f"{name} already listed", fg=FG_DIM)
        self._save_intel_filter()
        self._refresh_intel_filter_lists()

    def _remove_location_item(self, kind: str, within: int):
        """Per-chip removal: drop location[kind][within] (systems/regions)."""
        loc = self._intel_filter.setdefault(
            "location", {"anywhere": True, "systems": [], "regions": []})
        loc[kind] = remove_filter_item(loc.get(kind, []), within)
        # Removing the last concrete item re-checks Anywhere.
        if not loc.get("systems") and not loc.get("regions"):
            loc["anywhere"] = True
            self._loc_anywhere_var.set(True)
            self._save_intel_filter()
            self._sync_location_enabled()  # also refreshes chips
            return
        self._save_intel_filter()
        self._refresh_intel_filter_lists()

    # ---- Parties add / remove ----

    def _on_parties_add(self):
        if self._par_anyone_var.get():
            return
        kind = self._par_type_var.get()
        name = self._par_add_entry.get().strip()
        if not name:
            return
        if kind == "Coalition":
            # Local: store the coalition name string.
            self._apply_parties_coalition_add(name)
            return
        category = "alliance" if kind == "Alliance" else "corporation"
        self._par_status.config(text=f"resolving {name}…", fg=FG_DIM)

        def worker():
            res = None
            try:
                if self.esi_auth:
                    if category == "alliance":
                        res = self.esi_auth.resolve_alliance(name)
                    else:
                        res = self.esi_auth.resolve_corporation(name)
            except Exception:
                res = None
            dest = "alliances" if category == "alliance" else "corporations"
            self.root.after(0, self._apply_parties_add, dest, res, name)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_parties_add(self, dest: str, res, name: str):
        """Main-thread: add a resolved alliance/corp {id,name} to parties."""
        if not res or "id" not in res:
            self._par_status.config(text=f"couldn't resolve {name}", fg=FG_RED)
            return
        par = self._intel_filter.setdefault(
            "parties",
            {"anyone": True, "alliances": [], "corporations": [],
             "coalitions": []})
        new_list, added = add_filter_item(
            par.get(dest, []),
            {"id": res["id"], "name": res.get("name", name)})
        par[dest] = new_list
        if added:
            self._uncheck_anyone_if_set(par)
            self._par_add_entry.delete(0, tk.END)
            self._par_status.config(text=f"added {res.get('name', name)}",
                                    fg=FG_GREEN)
        else:
            self._par_status.config(text=f"{name} already listed", fg=FG_DIM)
        self._save_intel_filter()
        self._refresh_intel_filter_lists()

    def _apply_parties_coalition_add(self, name: str):
        par = self._intel_filter.setdefault(
            "parties",
            {"anyone": True, "alliances": [], "corporations": [],
             "coalitions": []})
        # Only accept known coalitions.
        if name not in self.config.get("coalitions", {}):
            self._par_status.config(text=f"unknown coalition {name}", fg=FG_RED)
            return
        new_list, added = add_coalition_item(par.get("coalitions", []), name)
        par["coalitions"] = new_list
        if added:
            self._uncheck_anyone_if_set(par)
            self._par_add_entry.delete(0, tk.END)
            self._par_status.config(text=f"added {name}", fg=FG_GREEN)
        else:
            self._par_status.config(text=f"{name} already listed", fg=FG_DIM)
        self._save_intel_filter()
        self._refresh_intel_filter_lists()

    def _uncheck_anyone_if_set(self, par: dict):
        """Adding the first concrete party auto-unchecks Anyone."""
        if par.get("anyone"):
            par["anyone"] = False
            self._par_anyone_var.set(False)
            self._sync_parties_enabled()

    def _remove_parties_item(self, kind: str, within: int):
        """Per-chip removal: drop parties[kind][within].

        ``kind`` is "alliances"/"corporations" (id-dict lists) or "coalitions"
        (name-string list); ``remove_filter_item`` handles both.
        """
        par = self._intel_filter.setdefault(
            "parties",
            {"anyone": True, "alliances": [], "corporations": [],
             "coalitions": []})
        par[kind] = remove_filter_item(par.get(kind, []), within)
        if (not par.get("alliances") and not par.get("corporations")
                and not par.get("coalitions")):
            par["anyone"] = True
            self._par_anyone_var.set(True)
            self._save_intel_filter()
            self._sync_parties_enabled()  # also refreshes chips
            return
        self._save_intel_filter()
        self._refresh_intel_filter_lists()

    def _on_parties_typeahead(self, event=None):
        """Best-effort live ESI type-ahead for Alliance/Corporation (debounced).

        Never blocks the UI thread. Silently no-ops for Coalition (local) or
        when the query is short. If search_entities returns [] (e.g. no search
        scope) the user simply relies on resolve-on-Add."""
        if event is not None and event.keysym in (
                "Return", "Up", "Down", "Tab", "Escape",
                "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return
        kind = self._par_type_var.get()
        if kind == "Coalition":
            return
        query = self._par_add_entry.get().strip()
        if len(query) < 3:
            return
        category = "alliance" if kind == "Alliance" else "corporation"
        if self._par_typeahead_after is not None:
            try:
                self.root.after_cancel(self._par_typeahead_after)
            except Exception:
                pass
        self._par_typeahead_after = self.root.after(
            300, lambda: self._do_parties_typeahead(query, category))

    def _do_parties_typeahead(self, query: str, category: str):
        self._par_typeahead_after = None

        def worker():
            results = []
            try:
                if self.esi_auth:
                    results = self.esi_auth.search_entities(query, [category])
            except Exception:
                results = []
            names = [r["name"] for r in results
                     if isinstance(r, dict) and r.get("name")]
            if names:
                self.root.after(
                    0, self._par_add_entry.update_completions, names)

        threading.Thread(target=worker, daemon=True).start()

    # ---- Chip rendering ----

    @staticmethod
    def _make_chip_text_readonly(text: "tk.Text"):
        """Make a chip ``tk.Text`` non-editable to typing while leaving its
        embedded chip widgets clickable.

        We bind keystrokes to "break" rather than ``state=DISABLED`` because the
        Text must accept ``window_create``/``delete`` during rebuilds without
        the caller toggling state each time. Mouse drag-selection is also
        suppressed so the area reads as a static display, not an edit field.
        """
        def _swallow(_event):
            return "break"
        # Block typed input and the common edit/paste/cut accelerators.
        text.bind("<Key>", _swallow)
        text.bind("<<Paste>>", _swallow)
        text.bind("<<Cut>>", _swallow)
        text.bind("<Button-2>", _swallow)  # X11 middle-click paste
        text.bind("<B1-Motion>", _swallow)

    def _build_chip(self, parent, label_text, tag_text, on_remove,
                    ignored=False):
        """Build one compact removable chip frame.

        ``label_text`` is the entity name, ``tag_text`` the dim type tag
        (region/system/alliance/corp/coalition), ``on_remove`` a 0-arg callback
        run when the ✕ is clicked. When ``ignored`` the chip is muted to signal
        the group is currently bypassed (Anywhere/Anyone on).
        """
        chip_bg = BG_PANEL if ignored else "#15406f"
        name_fg = FG_DIM if ignored else FG_TEXT
        tag_fg = FG_DIM
        x_fg = FG_DIM
        # Abbreviate the dim type tag to save horizontal width so more chips
        # fit per row. Unknown tags fall through unchanged (lowercased).
        tag_abbr = {
            "system": "sys", "region": "reg", "alliance": "alli",
            "corp": "corp", "corporation": "corp", "coalition": "coal",
        }
        short_tag = tag_abbr.get(str(tag_text).lower(), str(tag_text).lower())
        chip = tk.Frame(parent, bg=chip_bg, bd=0, relief=tk.FLAT,
                        highlightbackground=BORDER_COLOR, highlightthickness=1)
        tk.Label(chip, text=label_text, font=("Consolas", 8), fg=name_fg,
                 bg=chip_bg).pack(side=tk.LEFT, padx=(3, 1), pady=0)
        tk.Label(chip, text=short_tag, font=("Consolas", 7), fg=tag_fg,
                 bg=chip_bg).pack(side=tk.LEFT, padx=(0, 1), pady=0)
        x = tk.Label(chip, text="✕", font=("Consolas", 8, "bold"),
                     fg=x_fg, bg=chip_bg, cursor="hand2")
        x.pack(side=tk.LEFT, padx=(0, 2), pady=0)
        x.bind("<Button-1>", lambda e: on_remove())
        # Subtle hover affordance on the ✕.
        x.bind("<Enter>", lambda e: x.config(fg=FG_WHITE))
        x.bind("<Leave>", lambda e: x.config(fg=x_fg))
        return chip

    def _render_chips(self, text_widget, specs, ignored, empty_hint):
        """Clear ``text_widget`` and flow chips for ``specs`` into it.

        ``specs`` is a list of ``(label, tag, on_remove)`` tuples. Chips are
        embedded via ``window_create`` so the Text wraps them like words. When
        ``ignored`` the whole area is dimmed; when there are no specs a short dim
        hint is shown instead.
        """
        # Reflect the ignored state in the Text background so empty/dimmed
        # areas read as bypassed.
        text_widget.config(bg=BG_PANEL if ignored else BG_ENTRY)
        # Allow programmatic edits regardless of the readonly key bindings.
        text_widget.delete("1.0", tk.END)
        if not specs:
            hint = empty_hint if ignored else "(none — add above)"
            text_widget.insert(tk.END, hint)
            text_widget.tag_add("hint", "1.0", tk.END)
            text_widget.tag_config("hint", foreground=FG_DIM)
            return
        for label, tag, on_remove in specs:
            chip = self._build_chip(text_widget, label, tag, on_remove,
                                    ignored=ignored)
            text_widget.window_create(tk.END, window=chip, padx=1, pady=1)
            # A space between chips lets the Text wrap at chip boundaries.
            text_widget.insert(tk.END, " ")

    def _refresh_intel_filter_lists(self):
        """Rebuild both chip areas from the live config."""
        flt = self._intel_filter
        loc = flt.get("location", {})
        par = flt.get("parties", {})

        # ---- Location chips ----
        loc_ignored = bool(self._loc_anywhere_var.get())
        loc_specs = []
        for i, sysitem in enumerate(loc.get("systems", []) or []):
            loc_specs.append((
                sysitem.get("name", "?"), "system",
                lambda k="systems", idx=i: self._remove_location_item(k, idx),
            ))
        for i, regitem in enumerate(loc.get("regions", []) or []):
            loc_specs.append((
                regitem.get("name", "?"), "region",
                lambda k="regions", idx=i: self._remove_location_item(k, idx),
            ))
        self._render_chips(self._loc_chips, loc_specs, loc_ignored,
                           "(ignored — Anywhere on)")

        # ---- Parties chips ----
        par_ignored = bool(self._par_anyone_var.get())
        par_specs = []
        for i, al in enumerate(par.get("alliances", []) or []):
            par_specs.append((
                al.get("name", "?"), "alliance",
                lambda k="alliances", idx=i: self._remove_parties_item(k, idx),
            ))
        for i, co in enumerate(par.get("corporations", []) or []):
            par_specs.append((
                co.get("name", "?"), "corp",
                lambda k="corporations", idx=i:
                    self._remove_parties_item(k, idx),
            ))
        for i, cn in enumerate(par.get("coalitions", []) or []):
            par_specs.append((
                str(cn), "coalition",
                lambda k="coalitions", idx=i:
                    self._remove_parties_item(k, idx),
            ))
        self._render_chips(self._par_chips, par_specs, par_ignored,
                           "(ignored — Anyone on)")

    # ── Coalition manager dialog ──────────────────────────────────────────────

    def _open_coalition_manager(self):
        """Open the modal-ish coalition manager (create/rename/delete coalitions
        and edit their member alliances/corporations).

        All mutations write ``config["coalitions"]`` and persist via
        :meth:`_save_config`; rename/delete also propagate into
        ``config["intel_filter"]["parties"]["coalitions"]`` (the filter's
        selected-coalitions list) and refresh the filter panel listbox.
        After any create/rename/delete/member change the parties Coalition
        autocomplete is refreshed via :meth:`_refresh_coalition_pickers`.
        ESI resolution for member-add runs off the Tk main thread.
        """
        # Re-focus an already-open dialog instead of stacking duplicates.
        existing = getattr(self, "_coalition_mgr", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except tk.TclError:
                pass

        win = tk.Toplevel(self.root)
        self._coalition_mgr = win
        win.title("Manage Coalitions")
        win.configure(bg=BG_DARK)
        win.geometry("680x420")
        win.minsize(560, 360)
        try:
            win.transient(self.root)
        except tk.TclError:
            pass

        # Per-dialog state.
        self._cm_selected_name: str | None = None
        self._cm_member_index_map: list[tuple[str, int]] = []
        self._cm_typeahead_after = None

        def _on_close():
            self._coalition_mgr = None
            self._cm_member_index_map = []
            self._cm_typeahead_after = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        win.bind("<Escape>", lambda e: _on_close())

        body = tk.Frame(win, bg=BG_DARK)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        body.columnconfigure(0, weight=1, uniform="cm")
        body.columnconfigure(1, weight=1, uniform="cm")
        body.rowconfigure(0, weight=1)

        # ---- LEFT: coalition list + New/Rename/Delete ----
        left = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                        highlightbackground=BORDER_COLOR, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        tk.Label(left, text="COALITIONS", font=("Consolas", 9, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).pack(anchor="w", padx=6, pady=(6, 2))

        self._cm_listbox = tk.Listbox(
            left, height=12, font=("Consolas", 10), bg=BG_ENTRY, fg=FG_TEXT,
            selectbackground="#1a5a90", selectforeground=FG_WHITE,
            borderwidth=1, relief=tk.RIDGE, activestyle="none",
            exportselection=False,
        )
        self._cm_listbox.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))
        self._cm_listbox.bind(
            "<<ListboxSelect>>", lambda e: self._cm_on_select())

        left_btns = tk.Frame(left, bg=BG_PANEL)
        left_btns.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Button(left_btns, text="New", style="Dark.TButton",
                   command=self._cm_new_coalition).pack(side=tk.LEFT)
        ttk.Button(left_btns, text="Rename", style="Dark.TButton",
                   command=self._cm_rename_coalition).pack(side=tk.LEFT,
                                                           padx=(4, 0))
        ttk.Button(left_btns, text="Delete", style="Red.TButton",
                   command=self._cm_delete_coalition).pack(side=tk.LEFT,
                                                           padx=(4, 0))
        self._cm_left_status = tk.Label(left, text="", font=("Consolas", 8),
                                        fg=FG_DIM, bg=BG_PANEL, anchor="w",
                                        justify=tk.LEFT, wraplength=300)
        self._cm_left_status.pack(fill=tk.X, padx=6, pady=(0, 6))

        # ---- RIGHT: members of the selected coalition ----
        right = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                         highlightbackground=BORDER_COLOR, highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        self._cm_members_label = tk.Label(
            right, text="MEMBERS", font=("Consolas", 9, "bold"),
            fg=FG_ACCENT, bg=BG_PANEL)
        self._cm_members_label.pack(anchor="w", padx=6, pady=(6, 2))

        self._cm_members_listbox = tk.Listbox(
            right, height=10, font=("Consolas", 10), bg=BG_ENTRY, fg=FG_TEXT,
            selectbackground="#1a5a90", selectforeground=FG_WHITE,
            borderwidth=1, relief=tk.RIDGE, activestyle="none",
            exportselection=False,
        )
        self._cm_members_listbox.pack(fill=tk.BOTH, expand=True, padx=6,
                                      pady=(0, 2))
        self._cm_members_listbox.bind(
            "<Double-Button-1>", lambda e: self._cm_remove_member())

        mem_btns = tk.Frame(right, bg=BG_PANEL)
        mem_btns.pack(fill=tk.X, padx=6, pady=(0, 4))
        ttk.Button(mem_btns, text="Remove Selected", style="Dark.TButton",
                   command=self._cm_remove_member).pack(side=tk.LEFT)

        # Add-row: type selector + AutocompleteEntry + Add.
        add_row = tk.Frame(right, bg=BG_PANEL)
        add_row.pack(fill=tk.X, padx=6, pady=(0, 2))
        self._cm_type_var = tk.StringVar(value="Alliance")
        cm_type = ttk.Combobox(
            add_row, textvariable=self._cm_type_var, state="readonly",
            values=["Alliance", "Corporation"], width=11,
            font=("Consolas", 9),
        )
        cm_type.pack(side=tk.LEFT, padx=(0, 4))
        self._cm_add_entry = AutocompleteEntry(
            add_row, [], width=16, font=("Consolas", 10), bg=BG_ENTRY,
            fg=FG_WHITE, insertbackground=FG_WHITE, borderwidth=1,
            relief=tk.RIDGE,
        )
        self._cm_add_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                padx=(0, 4))
        self._cm_add_entry.bind("<Return>", lambda e: self._cm_add_member())
        # add="+" so we don't clobber AutocompleteEntry's own <KeyRelease>.
        self._cm_add_entry.bind(
            "<KeyRelease>", self._cm_on_typeahead, add="+")
        cm_type.bind("<<ComboboxSelected>>",
                     lambda e: self._cm_add_entry.update_completions([]))
        ttk.Button(add_row, text="Add", style="Dark.TButton",
                   command=self._cm_add_member).pack(side=tk.LEFT)

        self._cm_member_status = tk.Label(
            right, text="", font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL,
            anchor="w", justify=tk.LEFT, wraplength=300)
        self._cm_member_status.pack(fill=tk.X, padx=6, pady=(0, 6))

        # Initial population.
        self._cm_refresh_list()
        self._cm_refresh_members()

    # ---- Coalition manager: list (left) ----

    def _cm_refresh_list(self, select: str | None = None):
        """Rebuild the left coalition listbox from config; optionally select
        ``select`` (falls back to the current selection if still present)."""
        box = getattr(self, "_cm_listbox", None)
        if box is None:
            return
        target = select if select is not None else self._cm_selected_name
        names = sorted(self.config.get("coalitions", {}).keys())
        box.delete(0, tk.END)
        for name in names:
            box.insert(tk.END, name)
        # Restore selection.
        if target in names:
            idx = names.index(target)
            box.selection_clear(0, tk.END)
            box.selection_set(idx)
            box.see(idx)
            self._cm_selected_name = target
        else:
            self._cm_selected_name = None

    def _cm_on_select(self):
        box = getattr(self, "_cm_listbox", None)
        if box is None:
            return
        sel = box.curselection()
        if not sel:
            return
        self._cm_selected_name = box.get(sel[0])
        self._cm_member_status.config(text="")
        self._cm_refresh_members()

    def _cm_new_coalition(self):
        name = self._cm_prompt_name("New Coalition", "Coalition name:")
        if name is None:
            return  # cancelled
        clean = name.strip()
        coalitions = self.config.setdefault("coalitions", {})
        if not clean:
            self._cm_left_status.config(text="Name can't be empty.", fg=FG_RED)
            return
        if self._cm_name_exists(clean):
            self._cm_left_status.config(
                text=f"'{clean}' already exists.", fg=FG_RED)
            return
        coalitions[clean] = {"alliances": [], "corporations": []}
        self._save_config()
        self._refresh_coalition_pickers()
        self._cm_refresh_list(select=clean)
        self._cm_refresh_members()
        self._cm_left_status.config(text=f"Created '{clean}'.", fg=FG_GREEN)

    def _cm_rename_coalition(self):
        old = self._cm_selected_name
        if not old or old not in self.config.get("coalitions", {}):
            self._cm_left_status.config(
                text="Select a coalition to rename.", fg=FG_DIM)
            return
        name = self._cm_prompt_name("Rename Coalition",
                                    f"New name for '{old}':", initial=old)
        if name is None:
            return  # cancelled
        new = name.strip()
        if not new:
            self._cm_left_status.config(text="Name can't be empty.", fg=FG_RED)
            return
        if new == old:
            return  # no-op
        # A case-only rename of the SAME coalition (e.g. "Imperium" ->
        # "imperium") is allowed; reject only a genuine collision with a
        # DIFFERENT existing coalition.
        if new.lower() != old.lower() and self._cm_name_exists(new):
            self._cm_left_status.config(
                text=f"'{new}' already exists.", fg=FG_RED)
            return
        coalitions = self.config["coalitions"]
        # Preserve ordering by rebuilding the dict with the key renamed.
        coalitions[new] = coalitions.pop(old)
        # Propagate into the filter's selected-coalitions list.
        par = self._intel_filter.setdefault(
            "parties",
            {"anyone": True, "alliances": [], "corporations": [],
             "coalitions": []})
        par["coalitions"] = rename_coalition_in_selection(
            par.get("coalitions", []), old, new)
        self._save_config()
        self._refresh_coalition_pickers()
        self._refresh_intel_filter_lists()
        self._cm_refresh_list(select=new)
        self._cm_refresh_members()
        self._cm_left_status.config(
            text=f"Renamed to '{new}'.", fg=FG_GREEN)

    def _cm_delete_coalition(self):
        name = self._cm_selected_name
        coalitions = self.config.get("coalitions", {})
        if not name or name not in coalitions:
            self._cm_left_status.config(
                text="Select a coalition to delete.", fg=FG_DIM)
            return
        del coalitions[name]
        # Drop any filter reference to the deleted coalition.
        par = self._intel_filter.setdefault(
            "parties",
            {"anyone": True, "alliances": [], "corporations": [],
             "coalitions": []})
        par["coalitions"] = remove_coalition_from_selection(
            par.get("coalitions", []), name)
        # If that emptied all party criteria, fall back to "Anyone".
        if (not par.get("alliances") and not par.get("corporations")
                and not par.get("coalitions")):
            par["anyone"] = True
            anyone_var = getattr(self, "_par_anyone_var", None)
            if anyone_var is not None:
                anyone_var.set(True)
                self._sync_parties_enabled()
        self._save_config()
        self._refresh_coalition_pickers()
        self._refresh_intel_filter_lists()
        self._cm_selected_name = None
        self._cm_refresh_list()
        self._cm_refresh_members()
        self._cm_left_status.config(text=f"Deleted '{name}'.", fg=FG_ORANGE)

    def _cm_name_exists(self, name: str) -> bool:
        """Case-insensitive duplicate check against existing coalition keys."""
        lower = name.strip().lower()
        return any(k.lower() == lower
                   for k in self.config.get("coalitions", {}).keys())

    def _cm_prompt_name(self, title: str, prompt: str,
                        initial: str = "") -> str | None:
        """Dark-themed modal text prompt. Returns the entered string (possibly
        blank — caller validates) or None if cancelled/closed."""
        dlg = tk.Toplevel(self._coalition_mgr or self.root)
        dlg.title(title)
        dlg.configure(bg=BG_DARK)
        dlg.resizable(False, False)
        try:
            dlg.transient(self._coalition_mgr or self.root)
        except tk.TclError:
            pass

        tk.Label(dlg, text=prompt, font=("Consolas", 10),
                 fg=FG_TEXT, bg=BG_DARK).pack(anchor="w", padx=12, pady=(12, 4))
        var = tk.StringVar(value=initial)
        entry = tk.Entry(dlg, textvariable=var, font=("Consolas", 11),
                         bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                         width=32, borderwidth=1, relief=tk.RIDGE)
        entry.pack(fill=tk.X, padx=12, pady=(0, 8))

        result: dict[str, str | None] = {"value": None}

        def _ok():
            result["value"] = var.get()
            dlg.destroy()

        def _cancel():
            result["value"] = None
            dlg.destroy()

        btns = tk.Frame(dlg, bg=BG_DARK)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))
        ttk.Button(btns, text="OK", style="Green.TButton",
                   command=_ok).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=_cancel).pack(side=tk.RIGHT, padx=(0, 6))

        entry.bind("<Return>", lambda e: _ok())
        entry.bind("<Escape>", lambda e: _cancel())
        dlg.protocol("WM_DELETE_WINDOW", _cancel)

        # Center over the manager dialog and make modal.
        dlg.update_idletasks()
        parent = self._coalition_mgr or self.root
        try:
            px = parent.winfo_rootx() + max(
                0, (parent.winfo_width() - dlg.winfo_width()) // 2)
            py = parent.winfo_rooty() + 60
            dlg.geometry(f"+{px}+{py}")
        except tk.TclError:
            pass
        entry.focus_set()
        entry.select_range(0, tk.END)
        try:
            dlg.grab_set()
        except tk.TclError:
            pass
        dlg.wait_window()
        return result["value"]

    # ---- Coalition manager: members (right) ----

    def _cm_current_coalition(self) -> dict | None:
        """Return the selected coalition's dict (creating member lists if the
        stored value is malformed) or None when nothing is selected."""
        name = self._cm_selected_name
        if not name:
            return None
        coalitions = self.config.get("coalitions", {})
        entry = coalitions.get(name)
        if not isinstance(entry, dict):
            return None
        entry.setdefault("alliances", [])
        entry.setdefault("corporations", [])
        return entry

    def _cm_refresh_members(self):
        """Rebuild the right member listbox from the selected coalition."""
        box = getattr(self, "_cm_members_listbox", None)
        if box is None:
            return
        self._cm_member_index_map = []
        box.delete(0, tk.END)
        label = getattr(self, "_cm_members_label", None)
        coalition = self._cm_current_coalition()
        if coalition is None:
            if label is not None:
                label.config(text="MEMBERS")
            return
        if label is not None:
            label.config(text=f"MEMBERS — {self._cm_selected_name}")
        for i, al in enumerate(coalition.get("alliances", []) or []):
            box.insert(tk.END, f"{al.get('name', '?')}  [alliance]")
            self._cm_member_index_map.append(("alliances", i))
        for i, co in enumerate(coalition.get("corporations", []) or []):
            box.insert(tk.END, f"{co.get('name', '?')}  [corp]")
            self._cm_member_index_map.append(("corporations", i))

    def _cm_remove_member(self):
        box = getattr(self, "_cm_members_listbox", None)
        coalition = self._cm_current_coalition()
        if box is None or coalition is None:
            return
        sel = box.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self._cm_member_index_map):
            return
        kind, within = self._cm_member_index_map[idx]
        coalition[kind] = remove_filter_item(coalition.get(kind, []), within)
        self._save_config()
        self._refresh_coalition_pickers()
        self._cm_refresh_members()
        self._cm_member_status.config(text="Removed.", fg=FG_DIM)

    def _cm_add_member(self):
        coalition = self._cm_current_coalition()
        if coalition is None:
            self._cm_member_status.config(
                text="Select a coalition first.", fg=FG_DIM)
            return
        name = self._cm_add_entry.get().strip()
        if not name:
            return
        kind = self._cm_type_var.get()
        category = "alliance" if kind == "Alliance" else "corporation"
        target_name = self._cm_selected_name  # snapshot for the callback
        self._cm_member_status.config(text=f"resolving {name}…", fg=FG_DIM)

        def worker():
            res = None
            try:
                if self.esi_auth:
                    if category == "alliance":
                        res = self.esi_auth.resolve_alliance(name)
                    else:
                        res = self.esi_auth.resolve_corporation(name)
            except Exception:
                res = None
            dest = "alliances" if category == "alliance" else "corporations"
            self.root.after(
                0, self._cm_apply_member_add, target_name, dest, res, name)

        threading.Thread(target=worker, daemon=True).start()

    def _cm_apply_member_add(self, target_name: str, dest: str, res, name: str):
        """Main-thread: add a resolved {id,name} into the coalition's member
        list. Guards against the selection/coalition having changed while the
        ESI call was in flight."""
        # Dialog closed while resolving?
        if getattr(self, "_coalition_mgr", None) is None:
            return
        coalitions = self.config.get("coalitions", {})
        coalition = coalitions.get(target_name)
        if not isinstance(coalition, dict):
            return  # coalition was renamed/deleted mid-flight
        if not res or "id" not in res:
            if self._cm_selected_name == target_name:
                self._cm_member_status.config(
                    text=f"couldn't resolve {name}", fg=FG_RED)
            return
        coalition.setdefault(dest, [])
        new_list, added = add_filter_item(
            coalition.get(dest, []),
            {"id": res["id"], "name": res.get("name", name)})
        coalition[dest] = new_list
        self._save_config()
        self._refresh_coalition_pickers()
        # Only touch the UI if the user is still viewing this coalition.
        if self._cm_selected_name == target_name:
            if added:
                self._cm_add_entry.delete(0, tk.END)
                self._cm_member_status.config(
                    text=f"added {res.get('name', name)}", fg=FG_GREEN)
            else:
                self._cm_member_status.config(
                    text=f"{res.get('name', name)} already a member",
                    fg=FG_DIM)
            self._cm_refresh_members()

    def _cm_on_typeahead(self, event=None):
        """Best-effort debounced ESI type-ahead for the member add-entry.

        Never blocks the UI thread; if search_entities returns [] the user just
        relies on resolve-on-Add."""
        if event is not None and event.keysym in (
                "Return", "Up", "Down", "Tab", "Escape",
                "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return
        query = self._cm_add_entry.get().strip()
        if len(query) < 3:
            return
        kind = self._cm_type_var.get()
        category = "alliance" if kind == "Alliance" else "corporation"
        if self._cm_typeahead_after is not None:
            try:
                self.root.after_cancel(self._cm_typeahead_after)
            except Exception:
                pass
        self._cm_typeahead_after = self.root.after(
            300, lambda: self._cm_do_typeahead(query, category))

    def _cm_do_typeahead(self, query: str, category: str):
        self._cm_typeahead_after = None

        def worker():
            results = []
            try:
                if self.esi_auth:
                    results = self.esi_auth.search_entities(query, [category])
            except Exception:
                results = []
            names = [r["name"] for r in results
                     if isinstance(r, dict) and r.get("name")]
            if names and getattr(self, "_coalition_mgr", None) is not None:
                self.root.after(
                    0, self._cm_add_entry.update_completions, names)

        threading.Thread(target=worker, daemon=True).start()

    # ── zKillboard Tab ────────────────────────────────────────────────────────

    def _build_intel_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Intelligence  ")

        header = tk.Frame(tab, bg=BG_DARK)
        header.pack(fill=tk.X, padx=10, pady=(10, 2))
        tk.Label(header, text="Live Engagement Feed",
                 font=("Consolas", 13, "bold"), fg=FG_ACCENT, bg=BG_DARK
                 ).pack(side=tk.LEFT)

        tk.Label(header, text="  LIVE",
                 font=("Consolas", 10, "bold"),
                 fg=FG_GREEN, bg=BG_DARK).pack(side=tk.LEFT, padx=10)

        # Mute all alert sounds on this tab
        self._intel_mute_var = tk.BooleanVar(value=False)
        tk.Checkbutton(header, text="\U0001F50A Mute Alerts",
                       variable=self._intel_mute_var,
                       font=("Consolas", 11, "bold"), fg=FG_YELLOW, bg=BG_DARK,
                       selectcolor=BG_ENTRY, activebackground=BG_DARK,
                       activeforeground=FG_RED,
                       ).pack(side=tk.LEFT, padx=15)

        # ── Config-driven intel filter panel ───────────────────────────────
        self._build_intel_filter_panel(tab)

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

        # ── Intelligence Fusion Panel ─────────────────────────────────────
        intel_frame = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                               highlightbackground=BORDER_COLOR, highlightthickness=1)
        intel_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

        intel_row = tk.Frame(intel_frame, bg=BG_PANEL)
        intel_row.pack(fill=tk.X, padx=10, pady=5)

        self._intel_fusion_var = tk.BooleanVar(value=False)
        self._intel_fusion_btn = tk.Checkbutton(
            intel_row, text="Intelligence Fusion",
            variable=self._intel_fusion_var,
            font=("Consolas", 10, "bold"), fg=FG_MAGENTA, bg=BG_PANEL,
            selectcolor=BG_ENTRY, activebackground=BG_PANEL,
            activeforeground=FG_MAGENTA,
            command=self._toggle_intel_fusion,
        )
        self._intel_fusion_btn.pack(side=tk.LEFT)
        _fusion_tip = (
            "Tails your tracked in-game intel channels (from EVE's chat logs) "
            "and parses each report — system, pilot count, d-scan link, "
            "cyno/camp flags — surfacing it in the live feed below, "
            "cross-referenced with zKillboard activity. "
            "Pick which channels to watch in Settings → Intel Channels."
        )
        self._intel_fusion_btn.bind(
            "<Enter>", lambda e, t=_fusion_tip: self._show_tooltip(e, t))
        self._intel_fusion_btn.bind(
            "<Leave>", lambda e: self._hide_tooltip())

        # Min reported filter
        tk.Label(intel_row, text="  Min Reported:", font=("Consolas", 9),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT, padx=(10, 0))
        self._intel_min_reported_var = tk.StringVar(value="0")
        tk.Spinbox(
            intel_row, from_=0, to=500, textvariable=self._intel_min_reported_var,
            font=("Consolas", 10), width=4, bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, buttonbackground=BG_PANEL,
            borderwidth=1, relief=tk.RIDGE,
        ).pack(side=tk.LEFT, padx=(4, 10))

        # Opt-in audio ping for high-priority intel lines (default off; this is
        # the intel stream's own toggle, distinct from the global zkill mute).
        self._intel_sound_var = tk.BooleanVar(
            value=bool(self.config.get("intel_sound_enabled", False)))
        tk.Checkbutton(
            intel_row, text="Sound", variable=self._intel_sound_var,
            font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
            selectcolor=BG_ENTRY, activebackground=BG_PANEL,
            command=self._on_intel_sound_toggle,
        ).pack(side=tk.LEFT, padx=(10, 0))

        self._intel_channels_frame = tk.Frame(intel_row, bg=BG_PANEL)
        self._intel_channels_frame.pack(side=tk.LEFT, padx=(15, 0))

        # Intel-monitor state must exist before building the checkboxes, since
        # the (re)build inspects _intel_monitor / _intel_channels_enabled.
        self._intel_monitor: ChatMonitor | None = None
        self._intel_thread: threading.Thread | None = None
        self._intel_channels_enabled: set[str] = set()
        self._intel_buffer: "collections.deque" = collections.deque(maxlen=2000)
        self._intel_channel_colors: dict[str, str] = {}
        self._intel_autoscroll_paused = False
        self._intel_new_count = 0
        self._intel_new_btn = None
        self._intel_resolver = None
        self._intel_find_var = tk.StringVar(value="")

        # One checkbox per tracked intel channel (user-configurable, sourced
        # from config["intel_channels"]["tracked"] via self._tracked_intel_channels).
        self._intel_channel_vars: dict[str, tk.BooleanVar] = {}
        self._rebuild_intel_channel_checkboxes()

        # Fusion detection state
        self._recent_zkill_systems: dict[str, datetime] = {}
        self._recent_intel_systems: dict[str, datetime] = {}
        self._current_log = None  # Tracks active log widget for append helpers

        # ── Cyno Check drawer (collapsible) ───────────────────────────────
        self._build_cyno_check_drawer(tab)

        # ── Split pane: zKill (left) | Intel (right) ─────────────────────
        self._paned = tk.PanedWindow(tab, orient=tk.HORIZONTAL, bg=BG_DARK,
                                      sashwidth=4, sashrelief=tk.RIDGE)
        self._paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Left pane — zKillboard
        left_frame = tk.Frame(self._paned, bg=BG_DARK)
        left_header = tk.Frame(left_frame, bg=BG_DARK)
        left_header.pack(fill=tk.X, pady=(0, 2))
        tk.Label(left_header, text="zKillboard Intel", font=("Consolas", 10, "bold"),
                 fg=FG_ACCENT, bg=BG_DARK).pack(side=tk.LEFT)
        ttk.Button(left_header, text="Clear", style="Dark.TButton",
                   command=self._clear_zkill_log).pack(side=tk.RIGHT, padx=2)
        self._zkill_log = scrolledtext.ScrolledText(
            left_frame, height=30, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
            selectbackground="#1a5a90", wrap=tk.WORD, state=tk.DISABLED,
            borderwidth=1, relief=tk.RIDGE
        )
        self._zkill_log.pack(fill=tk.BOTH, expand=True)
        self._theme_scrolledtext_bar(self._zkill_log)
        self._paned.add(left_frame, stretch="always")

        # Right pane — Intel Channels (initially hidden)
        self._intel_right_frame = tk.Frame(self._paned, bg=BG_DARK)
        right_header = tk.Frame(self._intel_right_frame, bg=BG_DARK)
        right_header.pack(fill=tk.X, pady=(0, 2))
        tk.Label(right_header, text="Intel Channels",
                 font=("Consolas", 10, "bold"),
                 fg=FG_MAGENTA, bg=BG_DARK).pack(side=tk.LEFT)
        ttk.Button(right_header, text="Clear", style="Dark.TButton",
                   command=self._clear_intel_log).pack(side=tk.RIGHT, padx=2)
        # Controls sub-row: find box, pause/resume, and the "▼ N new" jump button.
        controls = tk.Frame(self._intel_right_frame, bg=BG_DARK)
        controls.pack(fill=tk.X, pady=(0, 2))
        tk.Label(controls, text="Find:", font=("Consolas", 9),
                 fg=FG_TEXT, bg=BG_DARK).pack(side=tk.LEFT)
        find_entry = tk.Entry(controls, textvariable=self._intel_find_var,
                              font=("Consolas", 9), width=18, bg=BG_ENTRY,
                              fg=FG_WHITE, insertbackground=FG_WHITE)
        find_entry.pack(side=tk.LEFT, padx=(4, 8))
        self._intel_find_var.trace_add(
            "write", lambda *a: self._intel_rerender_from_buffer())
        self._intel_pause_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            controls, text="Pause", variable=self._intel_pause_var,
            font=("Consolas", 9), fg=FG_TEXT, bg=BG_DARK,
            selectcolor=BG_ENTRY, activebackground=BG_DARK,
            command=lambda: setattr(self, "_intel_autoscroll_paused",
                                    self._intel_pause_var.get()),
        ).pack(side=tk.LEFT)
        self._intel_new_btn = tk.Button(
            controls, text="▼ 0 new", font=("Consolas", 8, "bold"),
            fg=FG_YELLOW, bg=BG_ENTRY, borderwidth=1, relief=tk.RIDGE,
            cursor="hand2", command=self._intel_jump_to_bottom)
        # packed on demand by _intel_update_new_button
        self._intel_log = scrolledtext.ScrolledText(
            self._intel_right_frame, height=30, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
            selectbackground="#1a5a90", wrap=tk.WORD, state=tk.DISABLED,
            borderwidth=1, relief=tk.RIDGE
        )
        self._intel_log.pack(fill=tk.BOTH, expand=True)
        self._theme_scrolledtext_bar(self._intel_log)
        # Don't add to paned yet — added when fusion is toggled on

        # Configure text tags for zkill log
        self._zkill_log.tag_config("fight", foreground=FG_RED,
                                    font=("Consolas", 11, "bold"))
        self._zkill_log.tag_config("info", foreground=FG_ACCENT)
        self._zkill_log.tag_config("value", foreground=FG_ORANGE)
        self._zkill_log.tag_config("dim", foreground=FG_DIM)
        self._zkill_log.tag_config("fused", foreground=FG_YELLOW,
                                    font=("Consolas", 11, "bold"))

        # Configure text tags for intel log
        for log in (self._intel_log,):
            # ── retained legacy tags (still referenced; do NOT drop) ──
            log.tag_config("intel", foreground=FG_MAGENTA,
                           font=("Consolas", 11, "bold"))
            log.tag_config("intel_meta", foreground=FG_DIM)
            log.tag_config("info", foreground=FG_ACCENT)
            log.tag_config("value", foreground=FG_ORANGE)
            log.tag_config("dim", foreground=FG_DIM)
            log.tag_config("fused", foreground=FG_YELLOW,
                           font=("Consolas", 11, "bold"))
            log.tag_config("hostile_char", foreground=FG_RED,
                           font=("Consolas", 10, "bold"))
            # ── new firehose stream tags ──
            log.tag_config("intel_system", foreground=FG_ACCENT,
                           underline=True)
            log.tag_config("intel_count", foreground=FG_ORANGE,
                           font=("Consolas", 10, "bold"))
            log.tag_config("intel_clear", foreground=FG_GREEN)
            log.tag_config("intel_camp", foreground=FG_RED,
                           font=("Consolas", 10, "bold"))
            log.tag_config("intel_spike", foreground=FG_YELLOW,
                           font=("Consolas", 10, "bold"))
            log.tag_config("intel_cyno", foreground=FG_RED,
                           font=("Consolas", 10, "bold"))
            log.tag_config("intel_dscan", foreground=FG_ACCENT,
                           underline=True)
            log.tag_config("intel_priority", background="#3a1a1a")
            log.tag_config("channel", foreground=FG_MAGENTA)
            log.tag_config("name_friendly", foreground=FG_GREEN)
            log.tag_config("name_hostile", foreground=FG_RED,
                           font=("Consolas", 10, "bold"))
            log.tag_config("name_neutral", foreground=FG_YELLOW)
            log.tag_config("name_unknown", foreground=FG_DIM)

        # Initialize the standings age label now that the cache has been loaded.
        self._update_standings_label()

    # ── Jump Range Tab ────────────────────────────────────────────────────────

    def _build_range_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Jump Range  ")

        # Input section
        input_frame = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                highlightbackground=BORDER_COLOR, highlightthickness=1)
        input_frame.pack(fill=tk.X, padx=10, pady=10)

        row1 = tk.Frame(input_frame, bg=BG_PANEL)
        row1.pack(fill=tk.X, padx=15, pady=(15, 5))

        tk.Label(row1, text="Origin:", font=("Consolas", 11),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        self._range_origin = AutocompleteEntry(row1, self._system_names,
                                                font=("Consolas", 12),
                                                bg=BG_ENTRY, fg=FG_WHITE,
                                                insertbackground=FG_WHITE, width=20,
                                                borderwidth=1, relief=tk.RIDGE)
        self._range_origin.pack(side=tk.LEFT, padx=(10, 4))
        ttk.Button(row1, text="Current", style="Dark.TButton",
                   command=lambda: self._set_origin_to_current_system(self._range_origin)
                   ).pack(side=tk.LEFT, padx=(0, 20))

        tk.Label(row1, text="Destination:", font=("Consolas", 11),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        self._range_dest = AutocompleteEntry(row1, self._system_names,
                                              font=("Consolas", 12),
                                              bg=BG_ENTRY, fg=FG_WHITE,
                                              insertbackground=FG_WHITE, width=20,
                                              borderwidth=1, relief=tk.RIDGE)
        self._range_dest.pack(side=tk.LEFT, padx=(10, 20))

        # Pre-fill origin with staging system
        staging = self.config.get("zkillboard", {}).get("staging_system", "")
        if staging:
            self._range_origin.insert(0, staging)

        row2 = tk.Frame(input_frame, bg=BG_PANEL)
        row2.pack(fill=tk.X, padx=15, pady=(5, 5))

        tk.Label(row2, text="Ship Type:", font=("Consolas", 11),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        self._ship_type_var = tk.StringVar(
            value=self.config.get("jump_range", {}).get("ship_type", "Dreadnought"))
        ship_types = ["Dreadnought", "Carrier", "Command Carrier", "Force Auxiliary",
                      "Supercarrier", "Titan", "Black Ops", "Jump Freighter", "Rorqual"]
        self._ship_menu = ttk.Combobox(row2, textvariable=self._ship_type_var,
                                        values=ship_types, state="readonly",
                                        font=("Consolas", 10), width=18)
        self._ship_menu.pack(side=tk.LEFT, padx=(10, 20))

        row3 = tk.Frame(input_frame, bg=BG_PANEL)
        row3.pack(fill=tk.X, padx=15, pady=(5, 15))

        ttk.Button(row3, text="Check Jump Range", style="Green.TButton",
                   command=self._do_range_check).pack(side=tk.LEFT, padx=5)

        # Bind Enter key
        self._range_origin.bind("<Return>", lambda e: self._do_range_check())
        self._range_dest.bind("<Return>", lambda e: self._do_range_check())

        # Results
        self._range_result_frame = tk.Frame(tab, bg=BG_DARK)
        self._range_result_frame.pack(fill=tk.X, padx=10, pady=5)

        self._range_result_label = tk.Label(
            self._range_result_frame, text="",
            font=("Consolas", 18, "bold"), fg=FG_DIM, bg=BG_DARK
        )
        self._range_result_label.pack()

        self._range_detail_label = tk.Label(
            self._range_result_frame, text="Enter two systems and click Check",
            font=("Consolas", 11), fg=FG_DIM, bg=BG_DARK, justify=tk.LEFT
        )
        self._range_detail_label.pack(pady=5)

        # Secondary range table
        self._range_secondary_frame = tk.Frame(tab, bg=BG_DARK)
        self._range_secondary_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

        # ── Staging-system manager (persisted friendly/hostile lists) ────────
        staging_frame = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                 highlightbackground=BORDER_COLOR, highlightthickness=1)
        staging_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        add_row = tk.Frame(staging_frame, bg=BG_PANEL)
        add_row.pack(fill=tk.X, padx=10, pady=(8, 4))

        tk.Label(add_row, text="Staging system:",
                 font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL
                 ).pack(side=tk.LEFT, padx=(0, 5))

        self._range_add_entry = AutocompleteEntry(
            add_row, self._system_names,
            labels=self._system_labels,
            font=("Consolas", 10), bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, width=20,
            borderwidth=1, relief=tk.RIDGE,
        )
        self._range_add_entry.pack(side=tk.LEFT, padx=5)
        # Enter defaults to adding as friendly.
        self._range_add_entry.bind("<Return>",
                                   lambda e: self._add_staging_system("friendly"))

        ttk.Button(add_row, text="Add Friendly", style="Green.TButton",
                   command=lambda: self._add_staging_system("friendly")
                   ).pack(side=tk.LEFT, padx=5)
        ttk.Button(add_row, text="Add Hostile", style="Red.TButton",
                   command=lambda: self._add_staging_system("hostile")
                   ).pack(side=tk.LEFT, padx=5)

        self._range_custom_label = tk.Label(
            add_row, text="", font=("Consolas", 9), fg=FG_ACCENT, bg=BG_PANEL,
        )
        self._range_custom_label.pack(side=tk.LEFT, padx=10)

        # Two side-by-side lists: friendly (green-tinted) / hostile (red-tinted)
        lists_row = tk.Frame(staging_frame, bg=BG_PANEL)
        lists_row.pack(fill=tk.X, padx=10, pady=(0, 8))

        friendly_col = tk.Frame(lists_row, bg=BG_PANEL)
        friendly_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        tk.Label(friendly_col, text="Friendly staging",
                 font=("Consolas", 9, "bold"), fg=FG_GREEN, bg=BG_PANEL
                 ).pack(anchor=tk.W)
        self._friendly_listbox = tk.Listbox(
            friendly_col, height=5, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_GREEN, selectbackground="#1a5a90",
            selectforeground=FG_WHITE, highlightthickness=1,
            highlightbackground=BORDER_COLOR, borderwidth=1, relief=tk.RIDGE,
            activestyle="none", exportselection=False,
        )
        self._friendly_listbox.pack(fill=tk.BOTH, expand=True, pady=(2, 2))
        self._friendly_listbox.bind(
            "<Double-Button-1>", lambda e: self._remove_staging_system("friendly"))
        ttk.Button(friendly_col, text="Remove Selected", style="Dark.TButton",
                   command=lambda: self._remove_staging_system("friendly")
                   ).pack(anchor=tk.W, pady=(2, 0))

        hostile_col = tk.Frame(lists_row, bg=BG_PANEL)
        hostile_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))
        tk.Label(hostile_col, text="Hostile staging",
                 font=("Consolas", 9, "bold"), fg=FG_RED, bg=BG_PANEL
                 ).pack(anchor=tk.W)
        self._hostile_listbox = tk.Listbox(
            hostile_col, height=5, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_RED, selectbackground="#5a1a1a",
            selectforeground=FG_WHITE, highlightthickness=1,
            highlightbackground=BORDER_COLOR, borderwidth=1, relief=tk.RIDGE,
            activestyle="none", exportselection=False,
        )
        self._hostile_listbox.pack(fill=tk.BOTH, expand=True, pady=(2, 2))
        self._hostile_listbox.bind(
            "<Double-Button-1>", lambda e: self._remove_staging_system("hostile"))
        ttk.Button(hostile_col, text="Remove Selected", style="Dark.TButton",
                   command=lambda: self._remove_staging_system("hostile")
                   ).pack(anchor=tk.W, pady=(2, 0))

        # Populate the listboxes from the persisted lists loaded at startup.
        self._refresh_staging_listboxes()

    def _refresh_staging_listboxes(self):
        """Redraw both staging listboxes from self._friendly/_hostile_staging."""
        for box, items in (
            (getattr(self, "_friendly_listbox", None), self._friendly_staging),
            (getattr(self, "_hostile_listbox", None), self._hostile_staging),
        ):
            if box is None:
                continue
            box.delete(0, tk.END)
            for name in items:
                box.insert(tk.END, name)

    def _rerun_range_check_if_ready(self):
        """Re-run the range check live if both origin and destination are set."""
        try:
            origin = self._range_origin.get().strip()
            dest = self._range_dest.get().strip()
        except Exception:
            return
        if origin and dest:
            self._do_range_check()

    def _add_staging_system(self, target: str):
        """Validate, persist, and add the entry's system to a staging list.

        target: "friendly" or "hostile". Validation is a non-blocking,
        case-insensitive membership check against the in-memory system-name
        list (``self._system_names``); it never touches the network on the Tk
        main thread. On a match we store the canonical-cased name; on a miss we
        show an inline error and do not mutate the lists. The numeric system ID
        is resolved later on the range-check background thread, so no lookup is
        needed here. If the autocomplete list has not finished loading yet we
        accept the name as-entered (unverified) rather than block. If the
        system already lives in the other list it is moved here.
        """
        name = self._range_add_entry.get().strip()
        if not name:
            return
        # Match against the in-memory autocomplete list (case-insensitive),
        # preferring the canonical-cased entry ("nol-m9" -> "NOL-M9"). This is
        # purely a dict/list lookup — no network call on the main thread.
        canonical = None
        for known in (self._system_names or ()):
            if known.lower() == name.lower():
                canonical = known
                break

        unverified = False
        if canonical is None:
            if self._system_names:
                # List is loaded and the name isn't in it -> reject inline,
                # matching the existing "Unknown system" error style.
                self._range_custom_label.config(
                    text=f"Unknown system: {name}", fg=FG_RED)
                return
            # List hasn't finished loading yet: don't block and don't hit the
            # network. Accept as-entered; it is validated/resolved later on the
            # range-check background thread.
            canonical = name
            unverified = True

        self._friendly_staging, self._hostile_staging = mutate_staging_lists(
            self._friendly_staging, self._hostile_staging,
            "add", canonical, target,
        )
        self._range_add_entry.delete(0, tk.END)
        self._refresh_staging_listboxes()
        self._save_staging_systems()
        word = "friendly" if target == "friendly" else "hostile"
        if unverified:
            # System-name list still loading — flag that we couldn't verify.
            self._range_custom_label.config(
                text=f"Added {canonical} ({word}) - unverified, list loading",
                fg=FG_YELLOW,
            )
        else:
            self._range_custom_label.config(
                text=f"Added {canonical} ({word})",
                fg=FG_GREEN if target == "friendly" else FG_RED,
            )
        self._rerun_range_check_if_ready()

    def _remove_staging_system(self, target: str):
        """Remove the selected system from a staging list, persist, and re-check."""
        box = (self._friendly_listbox if target == "friendly"
               else self._hostile_listbox)
        sel = box.curselection()
        if not sel:
            return
        name = box.get(sel[0])
        self._friendly_staging, self._hostile_staging = mutate_staging_lists(
            self._friendly_staging, self._hostile_staging,
            "remove", name, target,
        )
        self._refresh_staging_listboxes()
        self._save_staging_systems()
        self._range_custom_label.config(text=f"Removed {name}", fg=FG_DIM)
        self._rerun_range_check_if_ready()

    # ── Wormhole Route Tab ────────────────────────────────────────────────────

    def _build_wh_route_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Navigation  ")

        # Input section
        input_frame = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                highlightbackground=BORDER_COLOR, highlightthickness=1)
        input_frame.pack(fill=tk.X, padx=10, pady=10)

        title_row = tk.Frame(input_frame, bg=BG_PANEL)
        title_row.pack(fill=tk.X, padx=15, pady=(10, 5))
        tk.Label(title_row, text="Route Finder",
                 font=("Consolas", 13, "bold"), fg=FG_ACCENT, bg=BG_PANEL
                 ).pack(side=tk.LEFT)
        tk.Label(title_row, text="Ansiblex + WH shortcuts (Thera/Turnur)",
                 font=("Consolas", 10), fg=FG_DIM, bg=BG_PANEL
                 ).pack(side=tk.LEFT, padx=10)

        row1 = tk.Frame(input_frame, bg=BG_PANEL)
        row1.pack(fill=tk.X, padx=15, pady=5)

        tk.Label(row1, text="Origin:", font=("Consolas", 11),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        self._wh_origin = AutocompleteEntry(row1, self._system_names,
                                             font=("Consolas", 12),
                                             bg=BG_ENTRY, fg=FG_WHITE,
                                             insertbackground=FG_WHITE, width=20,
                                             borderwidth=1, relief=tk.RIDGE)
        self._wh_origin.pack(side=tk.LEFT, padx=(10, 4))
        ttk.Button(row1, text="Current", style="Dark.TButton",
                   command=lambda: self._set_origin_to_current_system(self._wh_origin)
                   ).pack(side=tk.LEFT, padx=(0, 20))

        tk.Label(row1, text="Destination:", font=("Consolas", 11),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        self._wh_dest = AutocompleteEntry(row1, self._system_names,
                                           font=("Consolas", 12),
                                           bg=BG_ENTRY, fg=FG_WHITE,
                                           insertbackground=FG_WHITE, width=20,
                                           borderwidth=1, relief=tk.RIDGE)
        self._wh_dest.pack(side=tk.LEFT, padx=(10, 20))

        # Pre-fill origin with staging system
        staging = self.config.get("zkillboard", {}).get("staging_system", "")
        if staging:
            self._wh_origin.insert(0, staging)

        row2 = tk.Frame(input_frame, bg=BG_PANEL)
        row2.pack(fill=tk.X, padx=15, pady=5)

        tk.Label(row2, text="Ship Size:", font=("Consolas", 11),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        self._wh_ship_size_var = tk.StringVar(value="any")
        sizes = ["any", "medium", "large", "xlarge", "capital"]
        self._wh_size_menu = ttk.Combobox(row2, textvariable=self._wh_ship_size_var,
                                           values=sizes, state="readonly",
                                           font=("Consolas", 10), width=12)
        self._wh_size_menu.pack(side=tk.LEFT, padx=(10, 20))

        row3 = tk.Frame(input_frame, bg=BG_PANEL)
        row3.pack(fill=tk.X, padx=15, pady=(5, 15))

        ttk.Button(row3, text="Find Route", style="Green.TButton",
                   command=self._do_wh_route).pack(side=tk.LEFT, padx=5)
        ttk.Button(row3, text="Refresh Connections", style="Dark.TButton",
                   command=self._refresh_wh_connections).pack(side=tk.LEFT, padx=5)

        self._wh_status_label = tk.Label(row3, text="",
                                          font=("Consolas", 10), fg=FG_DIM, bg=BG_PANEL)
        self._wh_status_label.pack(side=tk.LEFT, padx=15)

        # Bind Enter key
        self._wh_origin.bind("<Return>", lambda e: self._do_wh_route())
        self._wh_dest.bind("<Return>", lambda e: self._do_wh_route())

        # Result summary
        self._wh_result_frame = tk.Frame(tab, bg=BG_DARK)
        self._wh_result_frame.pack(fill=tk.X, padx=10, pady=5)

        self._wh_result_label = tk.Label(
            self._wh_result_frame, text="",
            font=("Consolas", 16, "bold"), fg=FG_DIM, bg=BG_DARK
        )
        self._wh_result_label.pack()

        self._wh_detail_label = tk.Label(
            self._wh_result_frame, text="Enter origin and destination, then click Find WH Shortcut",
            font=("Consolas", 10), fg=FG_DIM, bg=BG_DARK, justify=tk.LEFT
        )
        self._wh_detail_label.pack(pady=3)

        # Add Ansiblex tag for route log
        self._wh_log_ansiblex_tag_added = False

        # Waypoint buttons frame (populated after search)
        self._wh_waypoint_frame = tk.Frame(tab, bg=BG_DARK)
        self._wh_waypoint_frame.pack(fill=tk.X, padx=10, pady=(0, 2))

        # Route breakdown log
        self._wh_log = scrolledtext.ScrolledText(
            tab, height=20, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
            selectbackground="#1a5a90", wrap=tk.WORD, state=tk.DISABLED,
            borderwidth=1, relief=tk.RIDGE
        )
        self._wh_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self._theme_scrolledtext_bar(self._wh_log)
        self._wh_log.tag_config("header", foreground=FG_ACCENT,
                                 font=("Consolas", 11, "bold"))
        self._wh_log.tag_config("gate", foreground=FG_TEXT)
        self._wh_log.tag_config("wormhole", foreground=FG_YELLOW,
                                 font=("Consolas", 10, "bold"))
        self._wh_log.tag_config("saved", foreground=FG_GREEN,
                                 font=("Consolas", 11, "bold"))
        self._wh_log.tag_config("nosave", foreground=FG_ORANGE)
        self._wh_log.tag_config("dim", foreground=FG_DIM)
        self._wh_log.tag_config("info", foreground=FG_ACCENT)
        self._wh_log.tag_config("warn", foreground=FG_RED)
        self._wh_log.tag_config("ansiblex", foreground=FG_MAGENTA,
                                 font=("Consolas", 10, "bold"))

    def _do_wh_route(self):
        origin = self._wh_origin.get().strip()
        dest = self._wh_dest.get().strip()
        if not origin or not dest:
            self._wh_result_label.config(text="Enter both systems", fg=FG_ORANGE)
            return

        ship_size = self._wh_ship_size_var.get()
        self._wh_result_label.config(text="Searching...", fg=FG_DIM)
        self._wh_detail_label.config(text="Fetching EVE Scout data and calculating routes...")
        self._clear_wh_log()
        conns = self._get_ansiblex_connections()
        ansiblex_count = len(conns) if conns else 0
        self._append_wh_log(f"Searching route: {origin} -> {dest}\n", "header")
        self._append_wh_log(f"Ship size: {ship_size}  |  Ansiblex gates: {ansiblex_count}\n\n", "dim")
        self.root.update_idletasks()

        ansiblex_pairs = self._ansiblex_id_pairs.copy()
        # If ID pairs haven't been resolved yet, build them now
        if not ansiblex_pairs and conns:
            for conn_str in conns:
                parts = conn_str.split("|")
                if len(parts) == 2:
                    id1, id2 = int(parts[0]), int(parts[1])
                    from zkill_monitor import resolve_name
                    n1 = resolve_name(id1, "solar_system")
                    n2 = resolve_name(id2, "solar_system")
                    ansiblex_pairs[(id1, id2)] = (n1, n2)
                    ansiblex_pairs[(id2, id1)] = (n2, n1)

        def _find_ansiblex_in_route(from_name, to_name):
            """Find which Ansiblex gates are used in a gate route segment."""
            gates_used = []
            from_id = search_system(from_name)
            to_id = search_system(to_name)
            if from_id and to_id and conns:
                raw_route = get_stargate_route(from_id, to_id, connections=conns)
                if raw_route:
                    for i in range(len(raw_route) - 1):
                        pair = (raw_route[i], raw_route[i + 1])
                        if pair in ansiblex_pairs:
                            gates_used.append(ansiblex_pairs[pair])
            return gates_used

        def do_search():
            result = find_wh_route(origin, dest, ship_size, connections=conns)
            # Build per-leg Ansiblex usage for WH route display
            leg_ansiblex: dict[int, list[tuple[str, str]]] = {}
            wh_ansiblex = []
            if result and result.legs:
                for idx, leg in enumerate(result.legs):
                    if leg["type"] == "gate":
                        gates = _find_ansiblex_in_route(leg["from"], leg["to"])
                        if gates:
                            leg_ansiblex[idx] = gates
                            wh_ansiblex.extend(gates)
            # Always compute direct route ansiblex separately
            direct_ansiblex = []
            if result and result.gate_jumps_direct is not None and conns:
                direct_ansiblex = _find_ansiblex_in_route(origin, dest)
            # If WH route wins, show WH ansiblex; if direct wins, show direct ansiblex
            via_wh = result.total_jumps_via_wh if result else None
            direct = result.gate_jumps_direct if result else None
            if via_wh is not None and direct is not None and via_wh < direct:
                all_ansiblex = wh_ansiblex
            else:
                all_ansiblex = direct_ansiblex
            self.root.after(0, self._show_wh_result, result, all_ansiblex, leg_ansiblex)

        threading.Thread(target=do_search, daemon=True).start()

    def _set_destination_or_copy(self, system_name: str):
        """Set in-game destination via ESI and copy to clipboard."""
        # Always copy to clipboard
        self.root.clipboard_clear()
        self.root.clipboard_append(system_name)
        self.root.update()
        # Also set destination via ESI if authenticated
        try:
            if self.esi_auth and self.esi_auth.is_authenticated:
                sys_id = search_system(system_name)
                if sys_id and self.esi_auth.set_waypoint(sys_id, clear_other=True):
                    print(f"[Nav] Set destination + copied: {system_name}")
                    return
        except Exception as e:
            print(f"[Nav] ESI waypoint failed: {e}")
        print(f"[Nav] Copied to clipboard: {system_name}")

    # ── Fleet Composition & Specialized Roles ────────────────────────────

    def _create_collapsible_section(self, parent, title, collapsed=True):
        """Create a collapsible section with toggle arrow. Returns (container, content_frame, count_label)."""
        container = tk.Frame(parent, bg=BG_PANEL)
        container.pack(fill=tk.X, pady=2)

        is_open = tk.BooleanVar(value=not collapsed)
        header = tk.Frame(container, bg=BG_PANEL, cursor="hand2")
        header.pack(fill=tk.X)

        arrow_label = tk.Label(header, text="\u25B6" if collapsed else "\u25BC",
                                font=("Consolas", 9), fg=FG_ACCENT, bg=BG_PANEL)
        arrow_label.pack(side=tk.LEFT, padx=(0, 4))

        title_label = tk.Label(header, text=title, font=("Consolas", 10, "bold"),
                                fg=FG_TEXT, bg=BG_PANEL)
        title_label.pack(side=tk.LEFT)

        count_label = tk.Label(header, text="(0)", font=("Consolas", 9),
                                fg=FG_DIM, bg=BG_PANEL)
        count_label.pack(side=tk.LEFT, padx=(4, 0))

        content = tk.Frame(container, bg=BG_PANEL)
        if not collapsed:
            content.pack(fill=tk.X, padx=(16, 0))

        def toggle(event=None):
            if is_open.get():
                content.pack_forget()
                arrow_label.config(text="\u25B6")
                is_open.set(False)
            else:
                content.pack(fill=tk.X, padx=(16, 0))
                arrow_label.config(text="\u25BC")
                is_open.set(True)

        for widget in (header, arrow_label, title_label, count_label):
            widget.bind("<Button-1>", toggle)

        return container, content, count_label

    def _update_fleet_composition(self, ship_counts: dict[int, int], total: int):
        """Update the Top 10 fleet composition display."""
        from zkill_monitor import resolve_name

        self._fleet_size_label.config(text=f"Fleet Size: {total}")

        # Build top 10 list
        sorted_ships = sorted(ship_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        new_data = [(resolve_name(tid, "type"), count) for tid, count in sorted_ships]

        # Flicker prevention: only rebuild if data changed
        if new_data == self._fleet_comp_prev:
            return
        self._fleet_comp_prev = new_data

        # Clear and rebuild
        for widget in self._fleet_comp_frame.winfo_children():
            widget.destroy()
        self._fleet_comp_labels = []

        for ship_name, count in new_data:
            row = tk.Frame(self._fleet_comp_frame, bg=BG_PANEL)
            row.pack(fill=tk.X)
            lbl = tk.Label(row, text=f"{ship_name}: {count}",
                           font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W)
            lbl.pack(side=tk.LEFT)
            self._fleet_comp_labels.append(lbl)

        if not new_data:
            tk.Label(self._fleet_comp_frame, text="  No fleet data",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL, anchor=tk.W
                     ).pack(anchor=tk.W)

    def _update_specialized_roles(self, members: list[dict], ship_counts: dict[int, int], total: int):
        """Update all collapsible specialized role sections."""
        from ship_classes import (
            ALL_LINKS_COMMAND, ALL_LOGISTICS, ALL_CYNO, ALL_WEBS,
            ALL_HICS, ALL_BRIDGE, ALL_FAX, ALL_DREADS,
            TITANS, BLACK_OPS, TACTICAL_DESTROYERS, is_defender
        )
        from zkill_monitor import resolve_name

        # Cache this snapshot so a doctrine change can re-render without waiting
        # for the next fleet poll (consumed by _refresh_specialized_roles_from_cache).
        self._last_specialized_args = (members, ship_counts, total)

        # Compute doctrine-driven guidance when a doctrine is active (Phase C).
        self._fleet_guidance = None
        doc = self._active_fleet_doctrine()
        if doc is not None:
            cmd = sum(c for tid, c in ship_counts.items()
                      if tid in ALL_LINKS_COMMAND)
            frac = (cmd / total) if total else 0.0
            try:
                exempt_ids, hull_ids = self._resolve_exemptions_for_counts(
                    doc, ship_counts)
                self._fleet_guidance = fleet_guidance.compute_fleet_guidance(
                    doc, self.fittings.get_fit, self.type_catalog,
                    ship_counts, total, command_ship_fraction=frac,
                    exempt_type_ids=exempt_ids, doctrine_hull_ids=hull_ids)
            except Exception as exc:
                print(f"[Fleet] guidance compute failed: {exc}")
                self._fleet_guidance = None

        # Rebuild the command-burst roster (lowercased pilot name -> ship_type_id)
        # so charge-up calls in fleet chat can be matched to booster ships. The
        # raw ESI member dicts carry character_id (not character_name), so resolve
        # names the same way the role categorisation below does. Runs on the UI
        # thread (this method is always invoked via root.after); the heavy
        # build_pilot_rows work happens off-thread in _run_booster_refresh.
        roster: dict[str, int] = {}
        for m in (members or []):
            char_id = m.get("character_id")
            tid = m.get("ship_type_id")
            if char_id and tid is not None:
                name = resolve_name(char_id, "character") or ""
                if name:
                    roster[name.lower()] = tid
        self._booster_roster = roster
        self._schedule_booster_refresh()

        # Check >50% command ship rule
        command_count = sum(ship_counts.get(tid, 0) for tid in ALL_LINKS_COMMAND)
        # Only suppress the links listing in a real fleet (>=10) that is majority
        # command ships; in small/solo fleets always list them so a lone command
        # ship (e.g. the FC's own links Claymore) isn't hidden behind "(0)".
        skip_links = total >= 10 and (command_count / total) > 0.5

        # Categorize members
        categories: dict[str, dict[int, list[tuple[str, str]]]] = {
            "links": {}, "defenders": {}, "logi": {},
            "cyno": {}, "webs": {}, "hics": {},
            "fax": {}, "dreads": {}, "bridge": {},
        }

        for m in members:
            stid = m.get("ship_type_id", 0)
            char_id = m.get("character_id", 0)
            if not stid or not char_id:
                continue

            char_name = resolve_name(char_id, "character")
            entry = (char_name, str(char_id))

            if not skip_links and stid in ALL_LINKS_COMMAND:
                categories["links"].setdefault(stid, []).append(entry)
            if stid in ALL_LOGISTICS:
                categories["logi"].setdefault(stid, []).append(entry)
            if stid in TACTICAL_DESTROYERS or is_defender(stid):
                categories["defenders"].setdefault(stid, []).append(entry)
            if stid in ALL_CYNO:
                categories["cyno"].setdefault(stid, []).append(entry)
            if stid in ALL_WEBS:
                categories["webs"].setdefault(stid, []).append(entry)
            if stid in ALL_HICS:
                categories["hics"].setdefault(stid, []).append(entry)
            if stid in ALL_FAX:
                categories["fax"].setdefault(stid, []).append(entry)
            if stid in ALL_DREADS:
                categories["dreads"].setdefault(stid, []).append(entry)
            if stid in ALL_BRIDGE:
                categories["bridge"].setdefault(stid, []).append(entry)

        # Threshold checks: (count, min_threshold) -> red if below, green if at or above
        logi_threshold = max(5, int(total * 0.2)) if total > 0 else 5
        thresholds = {
            "links": 5,
            "defenders": 6,
            "logi": logi_threshold,
            "webs": 3,
            "cyno": None,   # No threshold
            "hics": None,
            "fax": None,
            "dreads": None,
            "bridge": None,
        }

        # Cache the Links categorization + threshold for _render_links_section,
        # which owns _links_content and re-renders it whenever booster compute
        # results arrive (independent of this poll). Tk-thread-only state.
        self._links_categories = categories["links"]
        self._links_threshold = thresholds["links"]

        # For bridge category, sort titans first
        bridge_sort_key = {}
        for tid in TITANS:
            bridge_sort_key[tid] = (0, tid)  # Titans first
        for tid in BLACK_OPS:
            bridge_sort_key[tid] = (1, tid)  # Black Ops second

        # Update each section
        section_map = {
            "links": (self._links_content, self._links_count),
            "defenders": (self._defenders_content, self._defenders_count),
            "logi": (self._logi_content, self._logi_count),
            "cyno": (self._cyno_content, self._cyno_count),
            "webs": (self._webs_content, self._webs_count),
            "hics": (self._hics_content, self._hics_count),
            "fax": (self._fax_content, self._fax_count),
            "dreads": (self._dreads_content, self._dreads_count),
            "bridge": (self._bridge_content, self._bridge_count),
        }

        # Display names for role categories
        role_display_names = {
            "links": "LINKS", "defenders": "DEFENDERS", "logi": "LOGI",
            "webs": "WEBS", "cyno": "CYNO", "hics": "HICS",
            "fax": "FAX", "dreads": "DREADS", "bridge": "BRIDGE",
        }

        # Track which roles are newly filled
        if not hasattr(self, '_roles_filled'):
            self._roles_filled: set[str] = set()

        for cat_key, (content, count_lbl) in section_map.items():
            members_dict = categories[cat_key]
            threshold = thresholds.get(cat_key)
            sort_override = bridge_sort_key if cat_key == "bridge" else None
            status_override = None if cat_key == "links" else self._role_guidance_badge(cat_key)
            if cat_key == "links":
                # Links is owned by _render_links_section so per-pilot booster
                # charges + off-hull posters render (and collapse) inside it. It
                # reads the _links_categories/_links_threshold cached just above.
                # The guided badge is applied INSIDE _render_links_section (it
                # also re-renders asynchronously from the booster refresh, which
                # would clobber a badge we set here), so nothing to do here.
                self._render_links_section()
            else:
                self._populate_role_section(content, count_lbl, members_dict,
                                            threshold=threshold,
                                            sort_override=sort_override,
                                            status_override=status_override)

            # Log when a capped role reaches its threshold for the first time
            if threshold is not None:
                count = sum(len(pilots) for pilots in members_dict.values())
                if count >= threshold and cat_key not in self._roles_filled:
                    self._roles_filled.add(cat_key)
                    role_name = role_display_names.get(cat_key, cat_key.upper())
                    self._append_xup_log(
                        f"[{role_name}] FILLED ({count}/{threshold})\n", "ready"
                    )
                elif count < threshold and cat_key in self._roles_filled:
                    self._roles_filled.discard(cat_key)

        # DPS is doctrine-driven: counted as the active doctrine's DPS-tagged
        # hulls in the live fleet (read from the guidance rollup). Unlike the
        # other roles it has no hull-class fallback, so the section is empty
        # when no doctrine is active. Drive the section's count badge from the
        # rollup; the content frame just carries a short explanatory note.
        dps_badge = self._role_guidance_badge("dps")
        for w in self._dps_content.winfo_children():
            w.destroy()
        if dps_badge is not None:
            text, colour = dps_badge
            self._dps_count.config(text=text, fg=colour)
            tk.Label(self._dps_content, text="  Doctrine DPS hulls in fleet",
                     font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL, anchor=tk.W
                     ).pack(anchor=tk.W)
            # Adjusted-denominator note: some present pilots (caps/recon) were
            # dropped from the ideal-% denominator so the targets track the
            # composition, not the raw fleet size.
            note = self._exclusion_note_text(getattr(self, "_fleet_guidance", None))
            if note:
                tk.Label(self._dps_content, text=f"  {note}",
                         font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL, anchor=tk.W
                         ).pack(anchor=tk.W)
        else:
            self._dps_count.config(text="—", fg=FG_DIM)
            tk.Label(self._dps_content, text="  No doctrine",
                     font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL, anchor=tk.W
                     ).pack(anchor=tk.W)

        # Keep the MOTD +/- deltas current as the live fleet changes.
        if hasattr(self, "_schedule_motd_preview"):
            self._schedule_motd_preview()

    # Panel role-section key -> doctrine rollup tag (Phase C guidance).
    _ROLE_KEY_TO_TAG = {"dps": "DPS", "links": "Links", "logi": "Logistics",
                        "defenders": "Defenders", "webs": "Support - Webs"}

    def _format_rollup(self, rr) -> tuple[str, str]:
        """Format a role rollup's live current/target into (text, colour).
        Shared by the DPS guidance line and _role_guidance_badge so the badge
        text and status colours stay identical. Assumes rr.current is not None
        (the 'live current value' path); callers handle the no-current case."""
        hi = "∞" if rr.target_max is None else str(rr.target_max)
        if rr.delta == 0:
            badge = ""
        elif rr.delta > 0:
            badge = f"+{rr.delta}"
        else:
            badge = str(rr.delta)  # already negative
        if rr.status == "in":
            colour = FG_GREEN
        elif rr.status == "under":
            colour = FG_UNDER
        else:  # over
            colour = FG_ORANGE
        text = f"{rr.current} / {rr.target_min}-{hi} {badge}".strip()
        return (text, colour)

    def _role_guidance_badge(self, role_key):
        """Return (text, colour) for a guided role's count badge, or None to fall
        back to the existing fixed-threshold rendering. Uses the active doctrine's
        role rollup (current / target range + under/in/over status). For 'links',
        returns None when the guidance suppressed links (high command-ship fraction)
        so the fixed-threshold behaviour is kept."""
        rep = getattr(self, "_fleet_guidance", None)
        if rep is None:
            return None
        tag = self._ROLE_KEY_TO_TAG.get(role_key)
        if not tag or tag not in rep.roles:
            return None
        if role_key == "links" and getattr(rep, "links_suppressed", False):
            return None
        rr = rep.roles[tag]
        if not rep.has_live_fleet or rr.current is None:
            # No live fleet: show the target range with no current/status.
            hi = "∞" if rr.target_max is None else str(rr.target_max)
            return (f"— / {rr.target_min}-{hi}", FG_DIM)
        return self._format_rollup(rr)

    def _populate_role_section(self, content_frame, count_label, ship_members,
                                threshold: int | None = None,
                                sort_override: dict | None = None,
                                rows_by_name: dict | None = None,
                                status_override: tuple | None = None):
        """Populate a collapsible section with ship type counts and pilot details.

        ``rows_by_name`` (lowercased pilot name -> command_bursts.PilotRow) is
        only passed for the Links section when we are fleet boss; when provided,
        each pilot row is rendered decorated with inline booster-charge cells via
        _build_decorated_pilot_row. The default None keeps all other sections (and
        the non-boss Links section) rendering the plain single-label pilot rows.
        """
        from zkill_monitor import resolve_name

        # Drop any tooltip still bound to a child about to be destroyed (its
        # <Leave> never fires once destroyed, which would orphan the tooltip).
        self._hide_tooltip()

        # Per-section expand state (keyed by this content frame so sections don't
        # bleed into each other). Lets expanded ship-type rows survive rebuilds.
        expanded = self._role_expand_state.setdefault(id(content_frame), set())

        for widget in content_frame.winfo_children():
            widget.destroy()

        total = sum(len(pilots) for pilots in ship_members.values())
        if status_override is not None:
            # Doctrine guidance owns the badge (current / target range + status).
            text, count_color = status_override
            count_label.config(text=text, fg=count_color)
        else:
            # Color the count based on threshold
            if threshold is not None:
                count_color = "#ff6666" if total < threshold else FG_GREEN
            else:
                count_color = FG_DIM
            count_label.config(text=f"({total})", fg=count_color)

        if not ship_members:
            tk.Label(content_frame, text="  None detected",
                     font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL, anchor=tk.W
                     ).pack(anchor=tk.W)
            return

        # Sort by count descending, or by sort_override if provided
        if sort_override:
            sorted_items = sorted(ship_members.items(),
                                   key=lambda x: sort_override.get(x[0], (2, x[0])))
        else:
            sorted_items = sorted(ship_members.items(),
                                   key=lambda x: len(x[1]), reverse=True)

        for type_id, pilots in sorted_items:
            ship_name = resolve_name(type_id, "type")

            # Ship type row (collapsed by default, click to expand pilot list)
            ship_row = tk.Frame(content_frame, bg=BG_PANEL)
            ship_row.pack(fill=tk.X, pady=(2, 0))

            is_open = tk.BooleanVar(value=(type_id in expanded))
            arrow = tk.Label(ship_row, text="\u25B6", font=("Consolas", 8),
                              fg=FG_DIM, bg=BG_PANEL)
            arrow.pack(side=tk.LEFT)
            ship_label = tk.Label(ship_row, text=f"{ship_name} - {len(pilots)}",
                                   font=("Consolas", 9, "bold"), fg=FG_TEXT,
                                   bg=BG_PANEL, cursor="hand2")
            ship_label.pack(side=tk.LEFT)

            pilot_frame = tk.Frame(content_frame, bg=BG_PANEL)
            # Populate pilot details (hidden by default)
            for char_name, _char_id in pilots:
                loc = self._fleet_locations_cache.get(char_name)
                if loc:
                    sys_name, region_name, _ship = loc
                    loc_text = f"({sys_name} - {region_name})" if region_name else f"({sys_name})"
                else:
                    loc_text = ""
                if rows_by_name is None:
                    tk.Label(pilot_frame,
                             text=f"    {char_name} {loc_text}",
                             font=("Consolas", 8), fg=FG_GREEN, bg=BG_PANEL, anchor=tk.W
                             ).pack(anchor=tk.W)
                else:
                    prow = rows_by_name.get(char_name.lower())
                    self._build_decorated_pilot_row(pilot_frame, char_name, loc_text, prow)

            # If this ship type was expanded before the rebuild, restore the
            # open state immediately (mirrors the toggle's "open" branch).
            if is_open.get():
                pilot_frame.pack(fill=tk.X, after=ship_row)
                arrow.config(text="\u25BC")

            def toggle(event=None, _open=is_open, _arrow=arrow,
                       _pf=pilot_frame, _sr=ship_row, _tid=type_id):
                if _open.get():
                    _pf.pack_forget()
                    _arrow.config(text="\u25B6")
                    _open.set(False)
                    expanded.discard(_tid)
                else:
                    _pf.pack(fill=tk.X, after=_sr)
                    _arrow.config(text="\u25BC")
                    _open.set(True)
                    expanded.add(_tid)

            for w in (ship_row, arrow, ship_label):
                w.bind("<Button-1>", toggle)

    def _build_decorated_pilot_row(self, parent, char_name, loc_text, prow):
        """Render one pilot row, optionally decorated with inline booster cells.

        The name label matches the plain (undecorated) pilot label exactly so a
        pilot with no charges looks identical to the rows_by_name=None path. When
        ``prow`` (a command_bursts.PilotRow) has .cells, an over-limit warning and
        one icon+glyph cell per discipline are appended inline, each with a
        verdict tooltip. Pure Tk work (no network) — hull names are pre-resolved
        off-thread into self._booster_ship_names."""
        rf = tk.Frame(parent, bg=BG_PANEL)
        rf.pack(anchor=tk.W, fill=tk.X)
        tk.Label(rf, text=f"    {char_name} {loc_text}",
                 font=("Consolas", 8), fg=FG_GREEN, bg=BG_PANEL, anchor=tk.W
                 ).pack(side=tk.LEFT)
        if prow is None or not prow.cells:
            return
        if prow.over_limit:
            warn = tk.Label(rf, text=" ⚠", bg=BG_PANEL, fg=FG_YELLOW,
                            font=("Consolas", 8, "bold"))
            warn.pack(side=tk.LEFT)
            wt = f"{prow.charge_count} charges linked — fit may be unusual/bad"
            warn.bind("<Enter>", lambda e, t=wt: self._show_tooltip(e, t))
            warn.bind("<Leave>", lambda e: self._hide_tooltip())
        ship_name = (self._booster_ship_names.get(prow.ship_type_id)
                     if prow.ship_type_id is not None else None)
        for cell in prow.cells:
            cf = tk.Frame(rf, bg=BG_PANEL)
            cf.pack(side=tk.LEFT, padx=(8, 0))
            icon = self._burst_icons_small.get(cell.discipline)
            if icon is not None:
                di = tk.Label(cf, image=icon, bg=BG_PANEL)
            else:
                di = tk.Label(cf, text=command_bursts.DISCIPLINE_LABEL[cell.discipline][:2],
                              bg=BG_PANEL, fg=FG_TEXT, font=("Consolas", 8))
            di.pack(side=tk.LEFT)
            gl = tk.Label(cf, text=command_bursts.VERDICT_GLYPH[cell.verdict],
                          bg=BG_PANEL, fg=VERDICT_COLOR[cell.verdict],
                          font=("Consolas", 9, "bold"))
            gl.pack(side=tk.LEFT)
            tip = command_bursts.verdict_text(
                cell.verdict, command_bursts.DISCIPLINE_LABEL[cell.discipline],
                cell.charges, ship_name)
            for wdg in (di, gl):
                wdg.bind("<Enter>", lambda e, t=tip: self._show_tooltip(e, t))
                wdg.bind("<Leave>", lambda e: self._hide_tooltip())

    def _clear_waypoint_frame(self):
        """Remove all waypoint buttons."""
        for w in self._wh_waypoint_frame.winfo_children():
            w.destroy()

    def _build_waypoint_buttons(self, result: WHRoute):
        """Build a row of waypoint copy buttons from the WH route legs."""
        self._clear_waypoint_frame()

        tk.Label(self._wh_waypoint_frame, text="Waypoints:",
                 font=("Consolas", 10, "bold"), fg=FG_ACCENT, bg=BG_DARK
                 ).pack(side=tk.LEFT, padx=(0, 8))

        # Build waypoints as (system, arrival_type) where arrival_type
        # indicates HOW you arrive at this system (gate, wh, or start)
        waypoints = []
        for i, leg in enumerate(result.legs):
            if i == 0:
                waypoints.append(("START", leg["from"]))
            waypoints.append((leg["type"], leg["to"]))

        for i, (arrival_type, system) in enumerate(waypoints):
            if i > 0:
                is_wh = arrival_type == "wormhole"
                arrow_color = FG_YELLOW if is_wh else FG_DIM
                arrow_text = " ~WH~ " if is_wh else " >> "
                tk.Label(self._wh_waypoint_frame, text=arrow_text,
                         font=("Consolas", 9, "bold"), fg=arrow_color, bg=BG_DARK
                         ).pack(side=tk.LEFT)

            # Color: WH destinations in yellow, final dest in green, rest white
            is_last = (i == len(waypoints) - 1)
            if arrival_type == "wormhole":
                fg = FG_YELLOW
            elif is_last:
                fg = FG_GREEN
            else:
                fg = FG_WHITE

            btn = tk.Button(
                self._wh_waypoint_frame, text=system,
                font=("Consolas", 9, "bold"), fg=fg, bg=BG_ENTRY,
                activebackground="#1a5a90", activeforeground=FG_WHITE,
                borderwidth=1, relief=tk.RIDGE, cursor="hand2",
                command=lambda s=system: self._set_destination_or_copy(s),
            )
            btn.pack(side=tk.LEFT, padx=1)

        hint = "  (click to set destination)" if self.esi_auth and self.esi_auth.is_authenticated else "  (click to copy)"
        tk.Label(self._wh_waypoint_frame, text=hint,
                 font=("Consolas", 8), fg=FG_DIM, bg=BG_DARK
                 ).pack(side=tk.LEFT, padx=5)

    def _show_wh_result(self, result: WHRoute | None,
                        ansiblex_in_route: list[tuple[str, str]] | None = None,
                        leg_ansiblex: dict[int, list[tuple[str, str]]] | None = None):
        self._clear_waypoint_frame()
        if ansiblex_in_route is None:
            ansiblex_in_route = []
        if leg_ansiblex is None:
            leg_ansiblex = {}

        if result is None:
            self._wh_result_label.config(text="System not found", fg=FG_RED)
            self._wh_detail_label.config(text="")
            return

        direct = result.gate_jumps_direct
        via_wh = result.total_jumps_via_wh

        # Show direct route info (includes Ansiblex if configured)
        has_ansiblex = len(self._ansiblex_connections) > 0
        route_label = "Direct route (with Ansiblex)" if has_ansiblex else "Direct gate route"
        if direct is not None:
            self._append_wh_log(f"{route_label}: {direct} jumps\n", "info")
        else:
            self._append_wh_log(f"{route_label}: no route found\n", "warn")

        if via_wh is None or (direct is not None and via_wh >= direct):
            if direct is not None:
                self._wh_result_label.config(text=f"BEST ROUTE: {direct} jumps (direct)", fg=FG_GREEN)
            else:
                self._wh_result_label.config(text="No route found", fg=FG_RED)
            detail = f"Direct: {direct} jumps" if direct else "No route"
            detail += "  |  No WH shortcut saves jumps"
            self._wh_detail_label.config(text=detail, fg=FG_DIM)
            self._append_wh_log(
                f"\nNo wormhole route is shorter than the direct route.\n", "nosave"
            )
            # Show Ansiblex gates used in the direct route
            if ansiblex_in_route:
                self._append_wh_log(
                    f"\nAnsiblex gates in route ({len(ansiblex_in_route)}):\n", "ansiblex"
                )
                for sys_a, sys_b in ansiblex_in_route:
                    self._append_wh_log(
                        f"  JB  {sys_a}  >>  {sys_b}\n", "ansiblex"
                    )
            return

        saved = result.jumps_saved
        self._wh_result_label.config(
            text=f"BEST ROUTE: {via_wh} jumps via {result.hub_name} (saves {saved}!)", fg=FG_GREEN
        )
        self._wh_detail_label.config(
            text=f"Direct: {direct} jumps  |  Via {result.hub_name}: {via_wh} jumps  |  Saves {saved} jumps",
            fg=FG_GREEN,
        )

        # Build clickable waypoint buttons
        self._build_waypoint_buttons(result)

        self._append_wh_log(f"\nWH Route via {result.hub_name}: {via_wh} jumps ", "saved")
        self._append_wh_log(f"(saves {saved} jumps!)\n\n", "saved")

        # Show route breakdown with clear leg demarcation
        self._append_wh_log("Route Breakdown:\n", "header")
        self._append_wh_log("=" * 60 + "\n", "dim")

        for i, leg in enumerate(result.legs, 1):
            idx = i - 1  # 0-based index for leg_ansiblex lookup
            if leg["type"] == "gate":
                jb_count = len(leg_ansiblex.get(idx, []))
                jb_note = f"  (incl. {jb_count} JB)" if jb_count else ""
                self._append_wh_log(
                    f"  Leg {i} [GATE]:  {leg['from']}  -->  {leg['to']}  "
                    f"({leg['jumps']} jumps){jb_note}\n", "gate"
                )
                # Show Ansiblex gates used in this leg
                for sys_a, sys_b in leg_ansiblex.get(idx, []):
                    self._append_wh_log(
                        f"         JB  {sys_a}  >>  {sys_b}\n", "ansiblex"
                    )
            elif leg["type"] == "wormhole":
                self._append_wh_log(
                    f"  Leg {i} [WORMHOLE]:  {leg['from']}  ~~>  {leg['to']}\n", "wormhole"
                )
                self._append_wh_log(
                    f"         Sig: {leg.get('signature', '?')}  |  "
                    f"Size: {leg.get('max_ship_size', '?')}  |  "
                    f"~{leg.get('remaining_hours', '?')}h remaining\n", "dim"
                )
            if i < len(result.legs):
                self._append_wh_log("  " + "-" * 40 + "\n", "dim")

        self._append_wh_log("=" * 60 + "\n", "dim")
        self._append_wh_log(f"  TOTAL: {via_wh} jumps\n\n", "saved")

        # Show connection details
        if result.entry_connection:
            ec = result.entry_connection
            self._append_wh_log("Entry WH Details:\n", "info")
            self._append_wh_log(
                f"  {ec.dest_system_name} ({ec.dest_region_name}, {ec.dest_security_class}) "
                f"-> {ec.hub_name}\n", "dim"
            )
            self._append_wh_log(
                f"  Signature: {ec.dest_signature}  |  Type: {ec.wh_type}  |  "
                f"Max size: {ec.max_ship_size}\n", "dim"
            )
        if result.exit_connection:
            xc = result.exit_connection
            self._append_wh_log("Exit WH Details:\n", "info")
            self._append_wh_log(
                f"  {xc.hub_name} -> {xc.dest_system_name} ({xc.dest_region_name}, "
                f"{xc.dest_security_class})\n", "dim"
            )
            self._append_wh_log(
                f"  Signature: {xc.hub_signature} ({xc.hub_name} side) / "
                f"{xc.dest_signature} (K-space side)\n", "dim"
            )
            self._append_wh_log(
                f"  Type: {xc.wh_type}  |  Max size: {xc.max_ship_size}\n", "dim"
            )

    def _refresh_wh_connections(self):
        self._wh_status_label.config(text="Refreshing...", fg=FG_DIM)
        self.root.update_idletasks()

        def do_refresh():
            conns = fetch_connections()
            thera = sum(1 for c in conns if c.hub_name == "Thera")
            turnur = sum(1 for c in conns if c.hub_name == "Turnur")
            self.root.after(0, self._wh_status_label.config,
                            {"text": f"Thera: {thera} | Turnur: {turnur} connections",
                             "fg": FG_GREEN})
            # Show in log
            self.root.after(0, self._show_connections_summary, conns)
            # Update autocomplete with WH system names
            self.root.after(0, self._update_autocomplete_lists)

        threading.Thread(target=do_refresh, daemon=True).start()

    def _show_connections_summary(self, conns):
        self._clear_wh_log()
        self._append_wh_log(f"Current EVE Scout Connections ({len(conns)} total)\n\n", "header")

        for hub in ["Thera", "Turnur"]:
            hub_conns = [c for c in conns if c.hub_name == hub]
            if not hub_conns:
                continue
            self._append_wh_log(f"{hub} ({len(hub_conns)} connections):\n", "info")
            for c in sorted(hub_conns, key=lambda x: x.dest_system_name):
                sec_tag = "gate"
                if c.dest_security_class == "hs":
                    sec_tag = "saved"  # green for highsec
                elif c.dest_security_class == "ls":
                    sec_tag = "nosave"  # orange for lowsec
                elif c.dest_security_class == "ns":
                    sec_tag = "warn"   # red for nullsec

                self._append_wh_log(
                    f"  {c.dest_system_name:20s} {c.dest_region_name:20s} "
                    f"{c.dest_security_class:4s} {c.max_ship_size:8s} "
                    f"~{c.remaining_hours}h  {c.dest_signature}\n", sec_tag
                )
            self._append_wh_log("\n", "dim")

    def _on_tab_changed(self, event=None):
        """Handle tab switches — clear zkill alerts, refresh character tab."""
        current = self.notebook.index(self.notebook.select())
        if current == 1 and self._zkill_has_unread:
            # Switched to zKill tab — clear notification
            self._zkill_has_unread = False
            self.notebook.tab(1, text="  Intelligence  ")
            self._zkill_status.config(bg=BG_DARK)
        elif current == 4 and hasattr(self, '_char_tab_content'):
            # Switched to Characters tab — auto-refresh
            self._refresh_character_tab()
        elif current == 0:
            # Switched to Fleet Management — keep the doctrine dropdown fresh.
            try:
                self._refresh_fleet_doctrine_combo()
            except Exception:
                pass

    def _notify_zkill_tab(self):
        """Flash the zKill tab to indicate a new alert if not currently viewing it."""
        current = self.notebook.index(self.notebook.select())
        if current != 1:
            self._zkill_has_unread = True
            self.notebook.tab(1, text="  ** Intel ALERT **  ")
            # Flash the ZKILL status label red
            self._flash_zkill_status(0)

    def _flash_zkill_status(self, count):
        """Flash the ZKILL status indicator between red and normal."""
        if not self._zkill_has_unread or count >= 20:
            if not self._zkill_has_unread:
                self._zkill_status.config(bg=BG_DARK)
            return
        if count % 2 == 0:
            self._zkill_status.config(fg=FG_RED)
        else:
            self._zkill_status.config(fg=FG_GREEN)
        self.root.after(500, self._flash_zkill_status, count + 1)

    def _on_sound_toggle(self):
        """Immediately save the sound preference when toggled."""
        self._sound_enabled = self._sound_var.get()
        self.config["sound_on_ready"] = self._sound_enabled
        self._save_config()

    # ── ESI Auth Methods ────────────────────────────────────────────────────

    def _update_esi_status(self):
        """Update the ESI auth status label."""
        count = len(self.esi_accounts)
        if count == 0:
            self._esi_status_label.config(text="No characters connected", fg=FG_DIM)
        else:
            self._esi_status_label.config(
                text=f"{count} character(s) connected", fg=FG_GREEN
            )

    def _rebuild_esi_char_list(self):
        """Rebuild the ESI character list in Settings.

        Everything grids directly into ``_esi_chars_frame`` on shared columns so
        the action buttons line up in stable columns no matter how long each
        character's name is:

          col 0  primary chip ("PRIMARY" on the primary account, blank
                 fixed-width placeholder otherwise) — keeps col 1 aligned
          col 1  character name (absorbs slack via columnconfigure weight=1)
          col 2  scope-warning flag (⚠ when the token predates esi-fittings)
          col 3  "Set Primary" — present on every row (disabled on primary)
          col 4  "Disconnect" — present on every row
          col 5  "Re-authorize" — only on missing-scope rows
        """
        for w in self._esi_chars_frame.winfo_children():
            w.destroy()
        if not self.esi_accounts:
            tk.Label(self._esi_chars_frame, text="No characters connected",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK
                     ).pack(anchor=tk.W)
            return
        # Column 1 (the name) absorbs all extra width so every widget to its
        # right stays column-aligned across rows regardless of name length.
        self._esi_chars_frame.columnconfigure(1, weight=1)
        for r, acct in enumerate(self.esi_accounts):
            is_primary = (acct is self.esi_auth)
            # Alternate row shading applied to the tk.Label cells (chip / name /
            # flag). ttk buttons keep their own themed style.
            row_bg = BG_DARK if (r % 2 == 0) else BG_PANEL

            # col 0 — primary chip (constant width=8 so col 1 aligns on all rows)
            if is_primary:
                chip = tk.Label(self._esi_chars_frame, text="PRIMARY",
                                font=("Consolas", 8, "bold"),
                                fg=BG_DARK, bg=FG_ACCENT, width=8)
            else:
                chip = tk.Label(self._esi_chars_frame, text="",
                                width=8, bg=row_bg)
            chip.grid(row=r, column=0, sticky="w", padx=(0, 8), pady=1)

            # col 1 — character name (stretches; everything right of it aligns)
            name = acct.character_name or "Unknown"
            fg = FG_GREEN if acct.is_authenticated else FG_DIM
            tk.Label(self._esi_chars_frame, text=name,
                     font=("Consolas", 10), fg=fg, bg=row_bg, anchor="w"
                     ).grid(row=r, column=1, sticky="we", padx=(0, 8), pady=1)

            # col 2 — scope-warning flag. If this character's token predates the
            # esi-fittings scopes it cannot import/push fits until re-authorized;
            # the ⚠ (with tooltip) plus the col-5 Re-authorize button replace the
            # old full-width notice row. Re-logging in as the same character
            # refreshes its tokens with the full current SCOPES.
            needs_reauth = (acct.is_authenticated
                            and not acct.has_scope(SCOPE_FITTINGS_READ))
            if needs_reauth:
                flag = tk.Label(self._esi_chars_frame, text="⚠",
                                font=("Consolas", 10), fg=FG_ORANGE, bg=row_bg)
                flag.bind(
                    "<Enter>",
                    lambda e: self._show_tooltip(
                        e, "Missing fittings scopes — Re-authorize to enable "
                           "in-game fittings import/push."))
                flag.bind("<Leave>", lambda e: self._hide_tooltip())
            else:
                flag = tk.Label(self._esi_chars_frame, text="",
                                width=2, bg=row_bg)
            flag.grid(row=r, column=2, sticky="w", padx=(0, 8), pady=1)

            # col 3 — "Set Primary" on every row (disabled, identical geometry,
            # on the primary account so the column stays constant).
            if is_primary:
                sp_btn = ttk.Button(self._esi_chars_frame, text="Set Primary",
                                    style="Dark.TButton", state=tk.DISABLED)
            else:
                sp_btn = ttk.Button(
                    self._esi_chars_frame, text="Set Primary",
                    style="Dark.TButton",
                    command=lambda a=acct: self._esi_set_primary(a))
            sp_btn.grid(row=r, column=3, sticky="w", padx=(0, 8), pady=1)

            # col 4 — "Disconnect" on every row
            ttk.Button(self._esi_chars_frame, text="Disconnect",
                       style="Dark.TButton",
                       command=lambda a=acct: self._esi_disconnect(a)
                       ).grid(row=r, column=4, sticky="w", padx=(0, 8), pady=1)

            # col 5 — "Re-authorize" only on missing-scope rows
            if needs_reauth:
                ttk.Button(self._esi_chars_frame, text="Re-authorize",
                           style="Dark.TButton", command=self._esi_login
                           ).grid(row=r, column=5, sticky="w",
                                  padx=(0, 8), pady=1)

    def _esi_set_primary(self, acct: ESIAuth):
        """Set a character as the primary ESI account."""
        self.esi_auth = acct
        # Persist so the choice survives app restarts
        if acct.character_id:
            self.config["primary_character_id"] = acct.character_id
            self._save_config()

        # Immediately reflect the switch in the UI (don't wait for next poll)
        self._rebuild_esi_char_list()
        self._update_esi_status()

        # Show pending state right away
        if hasattr(self, "_current_system_display"):
            name = acct.character_name or "..."
            self._current_system_display.config(
                text=f"System: (switching to {name}...)", fg=FG_DIM,
            )

        # Trigger one-off refreshes using the NEW primary character
        self._fetch_current_system_once()

        # Reset loss tracker — new FC = new fleet context
        try:
            self._loss_tracker.reset()
        except Exception:
            pass

        # Flush fleet polling miss counter and force a fresh poll
        self._no_fleet_misses = 0
        self.root.after(100, self._refresh_fleet_locations)

    def _set_origin_to_current_system(self, entry_widget):
        """Fill an origin field (Jump Range or Navigation) with the primary
        character's current system. Uses cached system name when fresh;
        falls back to a one-off ESI fetch otherwise."""
        # Prefer in-memory value populated by the periodic refresh
        current = getattr(self, "_current_system_name", "")
        if current:
            entry_widget.delete(0, tk.END)
            entry_widget.insert(0, current)
            return

        # No cached value — fetch directly (background thread)
        if not self.esi_auth or not self.esi_auth.is_authenticated:
            return
        target_auth = self.esi_auth

        def do_fetch():
            try:
                loc = target_auth.get_location()
                if not loc:
                    return
                sys_id = loc.get("solar_system_id")
                if not sys_id:
                    return
                sys_info = get_system_info(sys_id)
                sys_name = sys_info.get("name", "") if sys_info else ""
                if sys_name:
                    def apply():
                        try:
                            entry_widget.delete(0, tk.END)
                            entry_widget.insert(0, sys_name)
                        except tk.TclError:
                            pass
                    self.root.after(0, apply)
            except Exception as e:
                print(f"[CurrentSystem] Fetch error: {e}")

        threading.Thread(target=do_fetch, daemon=True).start()

    def _fetch_current_system_once(self):
        """One-shot fetch of the primary character's location — no rescheduling.
        Used for immediate refresh after primary switch."""
        if not self.esi_auth or not self.esi_auth.is_authenticated:
            return

        # Capture the currently-primary account locally so we can detect
        # if the user switches again before this fetch returns.
        target_auth = self.esi_auth

        def do_fetch():
            try:
                loc = target_auth.get_location()
                if loc:
                    sys_id = loc.get("solar_system_id")
                    if sys_id:
                        sys_info = get_system_info(sys_id)
                        sys_name = sys_info.get("name", "???") if sys_info else "???"
                        region_name = ""
                        if sys_info:
                            region_name = target_auth._get_region_name(sys_info)
                        # Drop stale result if user switched primary again
                        if self.esi_auth is not target_auth:
                            return
                        self._current_system_name = sys_name
                        self._current_system_region = region_name
                        region_str = f" ({region_name})" if region_name else ""
                        self.root.after(
                            0,
                            self._current_system_display.config,
                            {"text": f"System: {sys_name}{region_str}",
                             "fg": FG_GREEN},
                        )
            except Exception as e:
                print(f"[Primary Switch] Location fetch error: {e}")
        threading.Thread(target=do_fetch, daemon=True).start()

    def _esi_disconnect(self, acct: ESIAuth):
        """Disconnect a specific ESI character."""
        acct.logout()
        # Drop any command-burst state tied to the (now-stale) fleet context;
        # the next successful fleet poll rebuilds the roster from the new primary.
        self._clear_booster_state()
        if acct in self.esi_accounts:
            self.esi_accounts.remove(acct)
        if self.esi_auth is acct:
            self.esi_auth = self.esi_accounts[0] if self.esi_accounts else None
            # Persist the new primary choice
            if self.esi_auth and self.esi_auth.character_id:
                self.config["primary_character_id"] = self.esi_auth.character_id
            else:
                self.config.pop("primary_character_id", None)
            self._save_config()
            # Immediately refresh the current system display
            if self.esi_auth:
                self._fetch_current_system_once()
            elif hasattr(self, "_current_system_display"):
                self._current_system_display.config(text="System: --", fg=FG_DIM)
        self._rebuild_esi_char_list()
        self._update_esi_status()
        # Remove just the disconnected panel, then relayout the grid
        if hasattr(self, '_char_tab_content'):
            removed = False
            for p in list(self._char_panels):
                if p._acct is acct:
                    p.destroy()
                    self._char_panels.remove(p)
                    removed = True
                    break
            if removed:
                self._apply_char_filter()

    def _esi_login(self):
        """Start EVE SSO login flow for a new character."""
        esi_cfg = self.config.get("esi", {})
        if not esi_cfg.get("client_id"):
            self._esi_status_label.config(text="ESI not configured", fg=FG_RED)
            return
        self._esi_status_label.config(text="Opening browser...", fg=FG_YELLOW)
        self._esi_login_btn.config(state=tk.DISABLED)

        # Create a new ESIAuth instance for this login
        new_auth = ESIAuth(
            client_id=esi_cfg["client_id"],
            client_secret=esi_cfg.get("client_secret", ""),
            callback_url=esi_cfg.get("callback_url", "http://localhost:8834/callback"),
            token_file=os.path.join(app_dir(), "esi_tokens_new.json"),  # Temp, updated on save
        )

        def on_complete(success, info):
            self.root.after(0, self._esi_login_complete, success, info, new_auth)

        new_auth.login(on_complete=on_complete)

    def _esi_login_complete(self, success: bool, info: str, new_auth: ESIAuth):
        """Handle ESI login completion (runs on main thread)."""
        self._esi_login_btn.config(state=tk.NORMAL)
        if success:
            # Check for duplicate character
            for existing in self.esi_accounts:
                if existing.character_id == new_auth.character_id:
                    # Update existing account's tokens
                    existing._refresh_token = new_auth._refresh_token
                    existing._access_token = new_auth._access_token
                    existing._expires_at = new_auth._expires_at
                    existing._save_tokens()
                    # Clean up temp file
                    try:
                        temp = os.path.join(app_dir(), "esi_tokens_new.json")
                        if os.path.exists(temp):
                            os.remove(temp)
                    except OSError:
                        pass
                    self._esi_status_label.config(
                        text=f"Updated: {info}", fg=FG_GREEN
                    )
                    self._rebuild_esi_char_list()
                    # Re-auth may have granted new scopes (e.g. fittings), so
                    # rebuild the Characters-tab cards — the per-card
                    # "Re-authorize" notice is built from has_scope() at panel
                    # construction, so without a rebuild it lingered until the
                    # next program restart.
                    if hasattr(self, "_char_tab_content"):
                        self._populate_character_panels()
                    return

            # New character — add to accounts list
            self.esi_accounts.append(new_auth)
            if not self.esi_auth:
                self.esi_auth = new_auth
            self._esi_status_label.config(
                text=f"Added: {info}", fg=FG_GREEN
            )
            # Update tracked character if empty
            if not self._char_var.get() and new_auth.character_name:
                self._char_var.set(new_auth.character_name)
            self._rebuild_esi_char_list()
            # Refresh only the new character's panel
            if hasattr(self, '_char_tab_content'):
                self._refresh_single_character(new_auth)
        else:
            self._esi_status_label.config(
                text=f"Login failed: {info}", fg=FG_RED
            )

    def _esi_discover_ansiblex(self):
        """Discover Ansiblex gates via ESI and populate the text field."""
        if not self.esi_auth or not self.esi_auth.is_authenticated:
            self._esi_status_label.config(
                text="Login first to discover gates", fg=FG_ORANGE
            )
            return

        self._esi_status_label.config(text="Discovering gates...", fg=FG_YELLOW)
        self._esi_discover_btn.config(state=tk.DISABLED)

        def do_discover():
            gates = self.esi_auth.discover_ansiblex_gates()
            self.root.after(0, self._esi_ansiblex_done, gates)

        threading.Thread(target=do_discover, daemon=True).start()

    def _esi_ansiblex_done(self, gates: list[list[str]]):
        """Handle Ansiblex discovery completion."""
        self._esi_discover_btn.config(state=tk.NORMAL)
        if not gates:
            self._esi_status_label.config(
                text="No Ansiblex gates found", fg=FG_ORANGE
            )
            return

        # Populate the Ansiblex text field
        self._ansiblex_text.delete("1.0", tk.END)
        for pair in gates:
            self._ansiblex_text.insert(tk.END, f"{pair[0]}, {pair[1]}\n")

        self._esi_status_label.config(
            text=f"Found {len(gates)} Ansiblex gate(s)", fg=FG_GREEN
        )

        # Auto-save to config and re-resolve
        self.config["ansiblex_connections"] = gates
        self._save_config()
        threading.Thread(target=self._resolve_ansiblex_sync, daemon=True).start()

    def _get_staging_system(self) -> str:
        """Get the current staging system name."""
        return self.config.get("zkillboard", {}).get("staging_system", "")

    def _navigate_wh_route(self, destination: str):
        """Pre-fill WH Route tab with staging->destination and switch to it."""
        staging = self._get_staging_system()
        if not staging:
            return
        self._wh_origin.delete(0, tk.END)
        self._wh_origin.insert(0, staging)
        self._wh_dest.delete(0, tk.END)
        self._wh_dest.insert(0, destination)
        self.notebook.select(3)  # WH Route tab index
        self._do_wh_route()

    def _navigate_jump_range(self, destination: str, has_cyno: bool = False):
        """Pre-fill Jump Range tab with staging->destination and switch to it."""
        staging = self._get_staging_system()
        if not staging:
            return
        self._range_origin.delete(0, tk.END)
        self._range_origin.insert(0, staging)
        self._range_dest.delete(0, tk.END)
        self._range_dest.insert(0, destination)
        # Set ship type to Titan for bridge check
        self._ship_type_var.set("Titan")
        self.notebook.select(2)  # Jump Range tab index
        self._do_range_check()
        # Notate cyno beacon if present
        if has_cyno and hasattr(self, '_range_result_label'):
            current = self._range_result_label.cget("text")
            self._range_result_label.config(
                text=f"{current}  [PHAROLUX CYNO BEACON IN SYSTEM]",
                fg=FG_YELLOW,
            )

    def _clear_wh_log(self):
        self._wh_log.config(state=tk.NORMAL)
        self._wh_log.delete("1.0", tk.END)
        self._wh_log.config(state=tk.DISABLED)

    def _append_wh_log(self, text, tag=None):
        self._wh_log.config(state=tk.NORMAL)
        if tag:
            self._wh_log.insert(tk.END, text, tag)
        else:
            self._wh_log.insert(tk.END, text)
        self._wh_log.see(tk.END)
        self._wh_log.config(state=tk.DISABLED)

    # ── Character Management Tab ──────────────────────────────────────────────

    def _build_character_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Characters  ")

        # Top bar with refresh button
        top_bar = tk.Frame(tab, bg=BG_DARK)
        top_bar.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(top_bar, text="Character Management",
                 font=("Consolas", 14, "bold"), fg=FG_ACCENT, bg=BG_DARK
                 ).pack(side=tk.LEFT)
        self._char_refresh_btn = ttk.Button(
            top_bar, text="Refresh All", style="Dark.TButton",
            command=lambda: self._refresh_character_tab(force=True),
        )
        self._char_refresh_btn.pack(side=tk.RIGHT, padx=5)
        self._char_refresh_status = tk.Label(
            top_bar, text="", font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK,
        )
        self._char_refresh_status.pack(side=tk.RIGHT, padx=10)

        # Filter bar
        filter_frame = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                highlightbackground=BORDER_COLOR, highlightthickness=1)
        filter_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

        tk.Label(filter_frame, text="Filter:", font=("Consolas", 10, "bold"),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(10, 5), pady=5)

        self._char_filter_cap_var = tk.StringVar(value="")
        self._char_filter_cap = ttk.Combobox(
            filter_frame, textvariable=self._char_filter_cap_var,
            values=["", "FAX", "Dreads", "Blops", "Titans", "Cyno", "HIC/Dictor"],
            state="readonly", width=12,
        )
        self._char_filter_cap.pack(side=tk.LEFT, padx=5, pady=5)
        self._char_filter_cap.bind("<<ComboboxSelected>>", self._on_cap_filter_changed)

        tk.Label(filter_frame, text="Region:", font=("Consolas", 10, "bold"),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(15, 5), pady=5)

        self._char_filter_region_var = tk.StringVar(value="")
        self._char_filter_region = ttk.Combobox(
            filter_frame, textvariable=self._char_filter_region_var,
            values=[""], state="disabled", width=25,
        )
        self._char_filter_region.pack(side=tk.LEFT, padx=5, pady=5)
        self._char_filter_region.bind("<<ComboboxSelected>>", self._on_region_filter_changed)

        ttk.Button(filter_frame, text="Clear", style="Dark.TButton",
                   command=self._clear_char_filter).pack(side=tk.LEFT, padx=10, pady=5)

        self._char_filter_count_label = tk.Label(
            filter_frame, text="", font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL,
        )
        self._char_filter_count_label.pack(side=tk.RIGHT, padx=10, pady=5)

        # Scrollable content area
        canvas = tk.Canvas(tab, bg=BG_DARK, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=canvas.yview)
        self._char_tab_content = tk.Frame(canvas, bg=BG_DARK)
        self._char_tab_content.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        # Store the window id so we can resize it on canvas <Configure>
        self._char_canvas = canvas
        self._char_canvas_window = canvas.create_window(
            (0, 0), window=self._char_tab_content, anchor=tk.NW,
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Resize the content frame to match the canvas width so the grid
        # inside redistributes horizontally instead of cards being cut off.
        # Also updates wraplength on capability labels via _on_char_canvas_resize.
        canvas.bind("<Configure>", self._on_char_canvas_resize)

        # Mouse-wheel scrolling handled by the global router.
        self._register_scroll_canvas(canvas)

        self._char_panels: list[tk.Frame] = []
        # Instance-level caches (moved from class-level; see C1 fix)
        # Structure:
        #   _asset_cache: {character_id: (monotonic_ts, wall_ts, cap_data_dict)}
        #   _location_cache: {location_id: (monotonic_ts, system_name, region_name)}
        self._asset_cache: dict = {}
        self._location_cache: dict = {}
        self._char_last_refresh_wall: float = 0.0  # For "last refresh: Xs ago"

        # Two-column grid for character cards (switches to 1 col if only 1 char)
        self._CHAR_2COL_THRESHOLD = 2
        self._char_tab_content.grid_columnconfigure(0, weight=1, uniform="char")
        self._char_tab_content.grid_columnconfigure(1, weight=1, uniform="char")

        self._load_char_disk_cache()
        self._populate_character_panels()

    def _populate_character_panels(self):
        """Build the character panels from scratch (static layout, no ESI calls)."""
        for w in self._char_tab_content.winfo_children():
            w.destroy()
        self._char_panels = []

        if not self.esi_accounts:
            tk.Label(self._char_tab_content,
                     text="No characters connected.\nGo to Settings to add characters via EVE SSO.",
                     font=("Consolas", 11), fg=FG_DIM, bg=BG_DARK,
                     justify=tk.CENTER
                     ).grid(row=0, column=0, columnspan=2, pady=50)
            return

        for acct in self.esi_accounts:
            self._add_character_panel(acct)

        # Apply cached data immediately so panels don't show "loading...".
        # Cache tuple = (monotonic_ts, wall_ts, cap_data). Route through
        # the shared _update_panel_display helper so the cached-render path
        # and the live-ESI render path produce the same header layout —
        # previously the cached path stuffed a long "[cached Xs ago,
        # refreshing…]" suffix into loc_label, which in 2-column grid
        # mode overflowed the card width and caused the system name to
        # appear clipped/overlapped by the character name.
        for panel in self._char_panels:
            acct = panel._acct
            char_id = acct.character_id or 0
            cached = self._asset_cache.get(char_id)
            if cached:
                _mono, wall_ts, cap_data = cached
                # Rehydrate full info dict from disk-cached state so cyno,
                # dictor, current ship, etc. all show immediately (B5 fix)
                info = self._info_from_cap_data(cap_data)
                panel._info = info
                age_str = self._format_cache_age(wall_ts)
                self._update_panel_display(panel, info, cached_age_str=age_str)

        # Lay out the grid
        self._relayout_character_panels(self._char_panels)

    def _info_from_cap_data(self, cap_data: dict) -> dict:
        """Build an info dict from cached cap_data. Provides default values
        for fields that may not be present (older cache format)."""
        return {
            "system": cap_data.get("system", "???"),
            "region": cap_data.get("region", ""),
            "ship": cap_data.get("ship", "???"),
            "ship_type_id": cap_data.get("ship_type_id", 0),
            "ship_item_id": 0,
            "fax": cap_data.get("fax", []),
            "dreads": cap_data.get("dreads", []),
            "blops": cap_data.get("blops", []),
            "titans": cap_data.get("titans", []),
            "cyno": cap_data.get("cyno", False),
            "cyno_ozone": cap_data.get("cyno_ozone", 0),
            "cyno_low": cap_data.get("cyno_low", False),
            "dictor": cap_data.get("dictor", False),
        }

    @staticmethod
    def _format_cache_age(wall_ts: float) -> str:
        """Format a wall-clock cache age as 'Xs ago', 'Xm ago', or 'Xh ago'."""
        if not wall_ts:
            return "unknown age"
        delta = max(0, int(time.time() - wall_ts))
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"

    def _refresh_character_tab(self, force: bool = False):
        """Refresh all character panels with ESI data (runs in background).
        Skips if refreshed within the last 60 seconds unless force=True.
        force=True also bypasses the asset cache TTL so a full re-poll happens."""
        if not self.esi_accounts:
            self._populate_character_panels()
            return

        # Cooldown — don't re-poll if recently refreshed (unless forced)
        now_mono = time.monotonic()
        last_mono = getattr(self, '_char_last_refresh_mono', 0)
        if not force and (now_mono - last_mono) < 60:
            return
        self._char_last_refresh_mono = now_mono

        # Sync panels with accounts without destroying existing ones
        self._sync_character_panels()

        self._char_refresh_status.config(text="Refreshing…", fg=FG_YELLOW)
        self._char_refresh_btn.config(state=tk.DISABLED)
        # Snapshot panels so we don't race with _esi_disconnect
        target_panels = list(self._char_panels)

        # Tell _fetch_character_info to bypass TTL on forced refreshes
        self._char_force_refresh = force

        def do_refresh():
            results = []
            had_errors = False
            for panel in target_panels:
                acct = panel._acct
                try:
                    info = self._fetch_character_info(acct)
                    results.append((panel, info, None))
                except Exception as e:
                    had_errors = True
                    results.append((panel, None, str(e)))
            self._char_force_refresh = False
            self.root.after(0, self._apply_character_refresh, results, had_errors)

        threading.Thread(target=do_refresh, daemon=True).start()

    def _sync_character_panels(self):
        """Add panels for new accounts, remove panels for disconnected ones,
        without touching existing panels."""
        changed = False
        # Remove panels for accounts no longer connected
        for panel in list(self._char_panels):
            if panel._acct not in self.esi_accounts:
                panel.destroy()
                self._char_panels.remove(panel)
                changed = True

        # Add panels for newly connected accounts
        panel_accts = {p._acct for p in self._char_panels}
        for acct in self.esi_accounts:
            if acct not in panel_accts:
                self._add_character_panel(acct)
                changed = True

        if changed:
            # Re-apply current filter (which handles the grid layout)
            self._apply_char_filter()

    def _add_character_panel(self, acct: ESIAuth):
        """Add a single character panel to the tab."""
        panel = tk.Frame(self._char_tab_content, bg=BG_PANEL, bd=1,
                         relief=tk.RIDGE, highlightbackground=BORDER_COLOR,
                         highlightthickness=1)
        # Grid position is assigned by _relayout_character_panels

        header = tk.Frame(panel, bg=BG_PANEL, cursor="hand2")
        header.pack(fill=tk.X, padx=10, pady=(5, 2))

        is_primary = (acct is self.esi_auth)
        name = acct.character_name or "Unknown"
        primary_tag = " [PRIMARY]" if is_primary else ""

        panel._expanded = tk.BooleanVar(value=True)
        arrow_label = tk.Label(
            header, text="\u25BC", font=("Consolas", 9),
            fg=FG_ACCENT, bg=BG_PANEL,
        )
        arrow_label.pack(side=tk.LEFT, padx=(0, 5))
        panel._arrow = arrow_label

        # Star marker for "active in filtered ship" (U3) — hidden by default,
        # shown via _set_panel_active_marker when a filter highlights this panel
        star_label = tk.Label(
            header, text="\u2605", font=("Consolas", 12, "bold"),
            fg=FG_GREEN, bg=BG_PANEL,
        )
        # Don't pack yet — only appears when active under a filter
        panel._star = star_label

        name_label = tk.Label(
            header, text=f"{name}{primary_tag}",
            font=("Consolas", 12, "bold"),
            fg=FG_ACCENT if is_primary else FG_TEXT, bg=BG_PANEL,
        )
        name_label.pack(side=tk.LEFT)

        loc_label = tk.Label(
            header, text="  --  loading...",
            font=("Consolas", 10), fg=FG_DIM, bg=BG_PANEL,
        )
        loc_label.pack(side=tk.LEFT, padx=(10, 0))

        cap_frame = tk.Frame(panel, bg=BG_PANEL)
        cap_frame.pack(fill=tk.X, padx=20, pady=(0, 5))

        def toggle(p=panel):
            if p._expanded.get():
                p._cap_frame.pack_forget()
                p._arrow.config(text="\u25B6")
                p._expanded.set(False)
            else:
                p._cap_frame.pack(fill=tk.X, padx=20, pady=(0, 5))
                p._arrow.config(text="\u25BC")
                p._expanded.set(True)

        for widget in (header, arrow_label, name_label, loc_label):
            widget.bind("<Button-1>", lambda e, p=panel: toggle(p))

        # Re-auth notice for characters whose token predates the esi-fittings
        # scopes — without re-authorizing they can't import/push in-game fits.
        # The Re-authorize button reuses the SSO login flow (re-logging in as
        # the same character refreshes its tokens with the full current SCOPES).
        if acct.is_authenticated and not acct.has_scope(SCOPE_FITTINGS_READ):
            reauth_row = tk.Frame(panel, bg=BG_PANEL)
            reauth_row.pack(fill=tk.X, padx=20, pady=(0, 5))
            tk.Label(
                reauth_row,
                text="⚠ Re-authorize to enable in-game fittings import/push",
                font=("Consolas", 9), fg=FG_ORANGE, bg=BG_PANEL, anchor=tk.W,
            ).pack(side=tk.LEFT, padx=(0, 8))
            ttk.Button(
                reauth_row, text="Re-authorize", style="Dark.TButton",
                command=self._esi_login,
            ).pack(side=tk.LEFT)

        panel._acct = acct
        panel._loc_label = loc_label
        panel._cap_frame = cap_frame
        self._char_panels.append(panel)

    def _refresh_single_character(self, acct: ESIAuth):
        """Refresh only a single character's panel (background thread)."""
        # Find or add the panel
        panel = None
        for p in self._char_panels:
            if p._acct is acct:
                panel = p
                break
        if not panel:
            self._add_character_panel(acct)
            panel = self._char_panels[-1]

        def do_refresh():
            try:
                info = self._fetch_character_info(acct)
                self.root.after(0, self._apply_single_refresh, panel, info)
            except Exception as e:
                print(f"[Characters] Single-refresh error for "
                      f"{acct.character_name}: {e}")
                def show_err():
                    if self._panel_alive(panel):
                        panel._loc_label.config(
                            text=f"  —  ⚠ ESI error: {e}", fg=FG_RED,
                        )
                self.root.after(0, show_err)

        threading.Thread(target=do_refresh, daemon=True).start()

    def _apply_single_refresh(self, panel, info: dict):
        """Apply refresh data to a single panel. B1 guard + B7 unify:
        shares display logic with full refresh, and always reapplies the
        active filter so newly-added characters get filtered correctly."""
        if not self._panel_alive(panel):
            return
        panel._info = info
        self._update_panel_display(panel, info)
        self._update_filter_regions()
        self._apply_char_filter()  # B7 — keep filter consistent
        self._save_char_disk_cache()

    # Constants (class-level = fine, they're immutable)
    _ASSET_CACHE_TTL = 600  # 10 minutes (monotonic)
    _LOCATION_CACHE_TTL = 3600  # 1 hour (monotonic)
    _CHAR_CACHE_FILE = os.path.join(app_dir(), "char_cache.json")

    def _load_char_disk_cache(self):
        """Load character asset cache from disk for instant startup.
        Wall-clock timestamps are loaded; monotonic-time is reset to 0 so
        cached data always appears 'expired' for TTL purposes (forces a
        background refresh) but still renders in the UI immediately."""
        try:
            if os.path.exists(self._CHAR_CACHE_FILE):
                with open(self._CHAR_CACHE_FILE) as f:
                    data = json.load(f)
                for char_id_str, entry in data.get("assets", {}).items():
                    char_id = int(char_id_str)
                    wall_ts = entry.get("ts", 0)  # wall-clock only
                    cap_data = entry.get("data", {})
                    # Monotonic=0 means "never cached in this session" — next
                    # fetch will trigger a real ESI pull (no stale-forever risk)
                    self._asset_cache[char_id] = (0.0, wall_ts, cap_data)
                for loc_id_str, loc in data.get("locations", {}).items():
                    loc_id = int(loc_id_str)
                    # Accept both old 2-tuple and new 3-tuple entries
                    if len(loc) >= 2:
                        self._location_cache[loc_id] = (0.0, loc[0], loc[1])
                print(f"[Characters] Loaded disk cache: {len(self._asset_cache)} chars, "
                      f"{len(self._location_cache)} locations")
        except Exception as e:
            print(f"[Characters] Error loading disk cache: {e}")

    def _save_char_disk_cache(self):
        """Save character asset cache to disk."""
        try:
            data = {
                "assets": {
                    str(cid): {"ts": wall_ts, "data": cap_data}
                    for cid, (_mono, wall_ts, cap_data) in self._asset_cache.items()
                },
                "locations": {
                    str(lid): [name, region]
                    for lid, (_mono, name, region) in self._location_cache.items()
                },
            }
            # Atomic write (temp + os.replace) so a crash mid-write cannot
            # corrupt the cache. Preserve the previous json.dump kwargs:
            # default indent (None / compact) and ensure_ascii=True.
            atomic_write_json(
                self._CHAR_CACHE_FILE, data, indent=None, ensure_ascii=True)
        except Exception as e:
            log.exception("Failed to save character disk cache")
            print(f"[Characters] Error saving disk cache: {e}")

    def _on_char_canvas_resize(self, event):
        """Resize the scrollable content frame + all capability text to
        match the current canvas width so cards don't get cut off on
        narrower windows."""
        # Resize the window inside the canvas to match canvas width
        try:
            self._char_canvas.itemconfig(self._char_canvas_window, width=event.width)
        except Exception:
            return

        # Determine wraplength: 2-col mode splits width, minus padding/margins
        panel_count = len(self._char_panels)
        two_col = panel_count >= getattr(self, "_CHAR_2COL_THRESHOLD", 2)
        # Leave ~40px for card chrome (border, label column, padding)
        per_card = (event.width // 2) if two_col else event.width
        wrap = max(200, per_card - 80)

        # Apply new wraplength to every rendered capability text label
        for panel in self._char_panels:
            try:
                cap_frame = getattr(panel, "_cap_frame", None)
                if not cap_frame:
                    continue
                for row in cap_frame.winfo_children():
                    for child in row.winfo_children():
                        if isinstance(child, tk.Label):
                            try:
                                # Only adjust labels that already have wraplength
                                if int(child.cget("wraplength") or 0) > 0:
                                    child.config(wraplength=wrap)
                            except tk.TclError:
                                pass
            except tk.TclError:
                pass

    def _panel_alive(self, panel) -> bool:
        """True if the panel still exists and is in our tracked list.
        Used to guard root.after callbacks against disconnected accounts."""
        if panel not in self._char_panels:
            return False
        try:
            return bool(panel.winfo_exists())
        except tk.TclError:
            return False

    def _fetch_character_info(self, acct: ESIAuth) -> dict:
        """Fetch location, ship, and capital assets for a character.
        Location/ship are always fresh; assets are cached for 10 minutes."""
        from ship_classes import FAX, DREADNOUGHTS, BLACK_OPS, TITANS, CYNO_SHIPS, HICS, INTERDICTORS
        from zkill_monitor import resolve_name
        from jump_range import get_system_info

        # Cyno fitting check constants
        CYNO_MODULE_TYPES = {
            21096,  # Cynosural Field Generator I
            28646,  # Covert Cynosural Field Generator I
        }
        LIQUID_OZONE_TYPE_ID = 16273
        # Ozone cost per activation, by ship type_id (defaults to 250 base)
        # Force Recons get 80% role bonus → 250 * 0.20 = 50
        OZONE_COST_PER_ACTIVATION = {
            tid: 50 for tid in CYNO_SHIPS
        }
        DEFAULT_OZONE_COST = 250

        info = {"system": "???", "region": "", "ship": "???", "ship_type_id": 0,
                "ship_item_id": 0,
                "fax": [], "dreads": [], "blops": [], "titans": [],
                "cyno": False, "cyno_ozone": 0, "cyno_low": False,
                "dictor": False}

        if not acct.is_authenticated:
            info["system"] = "Not authenticated"
            return info

        # Current location (always fresh)
        loc = acct.get_location()
        if loc:
            sys_id = loc.get("solar_system_id")
            if sys_id:
                sys_info = get_system_info(sys_id)
                info["system"] = sys_info.get("name", "???") if sys_info else "???"
                info["region"] = acct._get_region_name(sys_info) if sys_info else ""

        # Current ship
        ship = acct.get_ship_type()
        in_force_recon = False
        if ship:
            ship_type_id = ship.get("ship_type_id")
            ship_item_id = ship.get("ship_item_id", 0)
            if ship_type_id:
                info["ship"] = resolve_name(ship_type_id, "type")
                info["ship_type_id"] = ship_type_id
                info["ship_item_id"] = ship_item_id
                if ship_type_id in CYNO_SHIPS:
                    in_force_recon = True
                if ship_type_id in HICS or ship_type_id in INTERDICTORS:
                    info["dictor"] = True

        # Capital ship assets — use cache (10 min TTL) to avoid frequent polling
        char_id = acct.character_id or 0
        now_mono = time.monotonic()
        cached = self._asset_cache.get(char_id)
        # Cache entries are (monotonic_ts, wall_ts, cap_data)
        # Fresh = cached AND within TTL (monotonic-time)
        # force_refresh bypasses the TTL
        force_refresh = getattr(self, "_char_force_refresh", False)
        if (cached and not force_refresh
                and (now_mono - cached[0]) < self._ASSET_CACHE_TTL):
            cap_data = cached[2]
            for k in ("fax", "dreads", "blops", "titans"):
                info[k] = cap_data.get(k, [])
            # Cyno verification from cached fitting check (per active ship)
            if in_force_recon:
                cyno_cache = cap_data.get("cyno_check", {})
                ship_check = cyno_cache.get(str(info["ship_item_id"]))
                if ship_check:
                    if ship_check.get("has_cyno") and ship_check.get("ozone", 0) > 0:
                        info["cyno"] = True
                        info["cyno_ozone"] = ship_check["ozone"]
                        cost = OZONE_COST_PER_ACTIVATION.get(
                            info["ship_type_id"], DEFAULT_OZONE_COST
                        )
                        info["cyno_low"] = info["cyno_ozone"] <= cost
            return info

        CAPITAL_TYPES = {
            "fax": FAX,
            "dreads": DREADNOUGHTS,
            "blops": BLACK_OPS,
            "titans": TITANS,
        }
        all_capital_ids = FAX | DREADNOUGHTS | BLACK_OPS | TITANS

        cyno_check_data: dict = {}

        try:
            assets = acct.get_assets()

            # Filter: assembled ships in personal hangars only
            _PERSONAL_FLAGS = {"Hangar", "AutoFit"}
            capital_assets = [
                a for a in assets
                if a.get("type_id") in all_capital_ids
                and a.get("is_singleton", False)
                and a.get("location_flag", "") in _PERSONAL_FLAGS
            ]

            if capital_assets:
                loc_cache = self._batch_resolve_asset_systems(acct, capital_assets, assets)

                for asset in capital_assets:
                    type_id = asset["type_id"]
                    ship_name = resolve_name(type_id, "type")
                    item_id = asset.get("item_id", 0)
                    loc_id = asset.get("location_id", 0)

                    loc_name, loc_region = loc_cache.get(
                        item_id, (str(loc_id), "")
                    )

                    entry = {"ship": ship_name, "location": loc_name, "region": loc_region}
                    for cat_key, cat_ids in CAPITAL_TYPES.items():
                        if type_id in cat_ids:
                            info[cat_key].append(entry)
                            break

            # Cyno fitting check: only meaningful if in a Force Recon
            if in_force_recon and info["ship_item_id"]:
                ship_iid = info["ship_item_id"]
                has_cyno = False
                ozone_amount = 0
                for a in assets:
                    if a.get("location_id") != ship_iid:
                        continue
                    flag = a.get("location_flag", "")
                    tid = a.get("type_id", 0)
                    # Cyno module fitted in any high slot
                    if flag.startswith("HiSlot") and tid in CYNO_MODULE_TYPES:
                        has_cyno = True
                    # Liquid ozone in cargo
                    elif flag == "Cargo" and tid == LIQUID_OZONE_TYPE_ID:
                        ozone_amount += a.get("quantity", 0)

                cyno_check_data[str(ship_iid)] = {
                    "has_cyno": has_cyno,
                    "ozone": ozone_amount,
                }

                if has_cyno and ozone_amount > 0:
                    info["cyno"] = True
                    info["cyno_ozone"] = ozone_amount
                    cost = OZONE_COST_PER_ACTIVATION.get(
                        info["ship_type_id"], DEFAULT_OZONE_COST
                    )
                    info["cyno_low"] = ozone_amount <= cost
        except Exception as e:
            print(f"[Characters] Asset fetch error for {acct.character_name}: {e}")

        # Deduplicate: group identical (ship, location) entries with counts
        from collections import Counter
        for cat_key in ("fax", "dreads", "blops", "titans"):
            entries = info[cat_key]
            if not entries:
                continue
            counts = Counter()
            entry_map = {}
            for e in entries:
                key = (e["ship"], e["location"])
                counts[key] += 1
                entry_map[key] = e
            deduped = []
            for key, count in counts.items():
                e = dict(entry_map[key])
                if count > 1:
                    e["ship"] = f"{e['ship']} x{count}"
                deduped.append(e)
            info[cat_key] = deduped

        # Cache the asset results (including cyno fitting check + ship state
        # so startup-from-disk-cache shows honest data — see B5 fix)
        cap_data = {k: info[k] for k in ("fax", "dreads", "blops", "titans")}
        cap_data["cyno_check"] = cyno_check_data
        cap_data["ship_type_id"] = info["ship_type_id"]
        cap_data["ship"] = info["ship"]
        cap_data["system"] = info["system"]
        cap_data["region"] = info["region"]
        cap_data["cyno"] = info["cyno"]
        cap_data["cyno_ozone"] = info["cyno_ozone"]
        cap_data["cyno_low"] = info["cyno_low"]
        cap_data["dictor"] = info["dictor"]
        self._asset_cache[char_id] = (time.monotonic(), time.time(), cap_data)

        return info

    def _batch_resolve_asset_systems(self, acct: ESIAuth,
                                       capital_assets: list[dict],
                                       all_assets: list[dict]) -> dict[int, tuple[str, str]]:
        """Resolve capital asset locations to (system_name, region_name).
        Returns {item_id: (system_name, region_name)}.
        Uses a shared cache to avoid re-resolving the same structures."""
        result = {}

        # Build a lookup of all assets by item_id for walking container chains
        asset_by_id = {a["item_id"]: a for a in all_assets}

        # First pass: walk location chains to find the real location_id per asset
        resolved_locs: dict[int, tuple[int, str]] = {}  # item_id -> (loc_id, loc_type)
        unique_locs: dict[int, str] = {}  # loc_id -> loc_type (to resolve)

        for asset in capital_assets:
            item_id = asset["item_id"]
            loc_id = asset.get("location_id", 0)
            loc_type = asset.get("location_type", "")

            # Walk up the chain if inside another item
            visited = set()
            while loc_type == "item" and loc_id in asset_by_id:
                if loc_id in visited:
                    break
                visited.add(loc_id)
                parent = asset_by_id[loc_id]
                loc_id = parent.get("location_id", 0)
                loc_type = parent.get("location_type", "")

            resolved_locs[item_id] = (loc_id, loc_type)
            # _location_cache entries are (monotonic_ts, system_name, region_name)
            # Consider stale after _LOCATION_CACHE_TTL (1 hour) so renamed
            # structures eventually refresh (B8 fix).
            cached_loc = self._location_cache.get(loc_id)
            now_mono = time.monotonic()
            if (cached_loc is None
                    or (now_mono - cached_loc[0]) > self._LOCATION_CACHE_TTL):
                unique_locs[loc_id] = loc_type

        # Second pass: resolve only uncached/expired locations
        for loc_id, loc_type in unique_locs.items():
            sys_name, region_name = self._resolve_single_location(
                acct, loc_id, loc_type
            )
            self._location_cache[loc_id] = (
                time.monotonic(), sys_name, region_name,
            )

        # Build result from cache ((_mono, sys, region) → (sys, region))
        for item_id, (loc_id, _) in resolved_locs.items():
            entry = self._location_cache.get(loc_id)
            if entry:
                result[item_id] = (entry[1], entry[2])
            else:
                result[item_id] = (str(loc_id), "")

        return result

    def _resolve_single_location(self, acct: ESIAuth, location_id: int,
                                   location_type: str) -> tuple[str, str]:
        """Resolve a single location_id to (system_name, region_name)."""
        from jump_range import get_system_info

        # Solar system directly
        if location_type == "solar_system" or 30_000_000 <= location_id <= 33_000_000:
            si = get_system_info(location_id)
            if si:
                region = acct._get_region_name(si)
                return si.get("name", str(location_id)), region

        # NPC station
        if location_type == "station" or 60_000_000 <= location_id <= 64_000_000:
            try:
                import requests as _req
                rate_limit('esi')
                resp = _req.get(
                    f"{ESI_BASE}/universe/stations/{location_id}/",
                    headers=ESI_HEADERS, timeout=8,
                )
                if resp.ok:
                    data = resp.json()
                    sys_id = data.get("system_id")
                    if sys_id:
                        si = get_system_info(sys_id)
                        if si:
                            region = acct._get_region_name(si)
                            return si.get("name", str(location_id)), region
            except Exception:
                pass

        # Player structure — try with character's auth
        if location_type == "other" or location_id > 1_000_000_000_000:
            struct_info = acct.esi_get(f"/universe/structures/{location_id}/")
            if struct_info:
                sys_id = struct_info.get("solar_system_id")
                if sys_id:
                    si = get_system_info(sys_id)
                    if si:
                        region = acct._get_region_name(si)
                        return si.get("name", str(location_id)), region

            # Structure lookup failed (403) — try /assets/names/ as last resort
            token = acct.access_token
            if token:
                # Try /assets/names/ for a human-readable location name
                try:
                    rate_limit('esi')
                    resp = acct._session.post(
                        f"{ESI_BASE}/characters/{acct.character_id}/assets/names/",
                        headers={**ESI_HEADERS, "Authorization": f"Bearer {token}"},
                        json=[location_id],
                        timeout=10,
                    )
                    if resp.ok:
                        names = resp.json()
                        if names and names[0].get("name"):
                            # Name might be structure name — extract system if possible
                            return names[0]["name"], ""
                except Exception:
                    pass

        return str(location_id), ""

    def _apply_character_refresh(self, results: list, had_errors: bool = False):
        """Apply fetched character data to the panels (runs on main thread)."""
        self._char_refresh_data = results

        for item in results:
            # Tuple shape: (panel, info_or_None, error_or_None)
            panel, info, err = item
            # B1: skip panels that were destroyed (account disconnected)
            if not self._panel_alive(panel):
                continue
            if info is None:
                # ESI fetch failed — show error state (U1/B6)
                panel._info = getattr(panel, "_info", None) or {}
                panel._loc_label.config(
                    text=f"  —  ⚠ ESI error: {err or 'fetch failed'}", fg=FG_RED,
                )
                # Keep any existing capabilities rendered (don't blank)
                continue
            panel._info = info
            self._update_panel_display(panel, info)

        # Update region dropdown based on current capability filter
        self._update_filter_regions()
        self._apply_char_filter()

        self._char_last_refresh_wall = time.time()
        self._update_refresh_status(had_errors=had_errors)
        self._char_refresh_btn.config(state=tk.NORMAL)

        # Persist cache to disk for fast startup next time
        self._save_char_disk_cache()

    def _update_panel_display(self, panel, info: dict,
                              cached_age_str: str = ""):
        """Unified display updater for a single panel (used by both
        full-refresh and single-character refresh paths — B7 fix).

        If cached_age_str is non-empty, the panel renders in the dimmed
        "cached, awaiting refresh" style. Both paths produce the SAME
        loc_label text shape so the header layout matches between cached
        and live renders (prevents the system name from being clipped /
        overlapped by the character name in narrow 2-column mode)."""
        if not self._panel_alive(panel):
            return
        region_str = f" ({info['region']})" if info.get("region") else ""
        loc_text = f"  —  {info['system']}{region_str}  —  {info['ship']}"
        if cached_age_str:
            # Compact suffix so the header stays the same shape as the
            # live render — detailed "refreshing…" state is already shown
            # by the tab-level _char_refresh_status label.
            loc_text += f"  [cached {cached_age_str}]"
            panel._loc_label.config(text=loc_text, fg=FG_DIM)
        else:
            panel._loc_label.config(text=loc_text, fg=FG_TEXT)
        self._render_capabilities(panel, info)

    def _update_refresh_status(self, had_errors: bool = False):
        """Update the 'Last refresh: Xs ago' status label (U2)."""
        if not hasattr(self, "_char_refresh_status"):
            return
        wall_ts = getattr(self, "_char_last_refresh_wall", 0)
        if wall_ts:
            age = self._format_cache_age(wall_ts)
            if had_errors:
                self._char_refresh_status.config(
                    text=f"Last refresh: {age} ⚠ some errors", fg=FG_ORANGE,
                )
            else:
                self._char_refresh_status.config(
                    text=f"Last refresh: {age}", fg=FG_GREEN,
                )
        else:
            self._char_refresh_status.config(text="", fg=FG_DIM)
        # Schedule a repeat update so the "Xs ago" stays current
        if hasattr(self, "_refresh_status_after_id"):
            try:
                self.root.after_cancel(self._refresh_status_after_id)
            except Exception:
                pass
        self._refresh_status_after_id = self.root.after(
            15000, lambda: self._update_refresh_status(had_errors=had_errors),
        )

    def _render_capabilities(self, panel, info: dict, only_cap: str = "",
                             only_region: str = ""):
        """Render capability rows in a panel's cap_frame.
        If only_cap is set (e.g. 'fax'), only show that capability.
        If only_region is set, asset-cap items are filtered to that region
        (a cap whose items are all elsewhere is skipped entirely), and the
        cyno/dictor rows only render when the character's current-location
        region matches."""
        for w in panel._cap_frame.winfo_children():
            w.destroy()

        # Compute a wraplength that fits the current canvas / card width
        try:
            canvas_w = self._char_canvas.winfo_width() if hasattr(self, "_char_canvas") else 0
        except tk.TclError:
            canvas_w = 0
        if canvas_w < 100:  # Not yet rendered — use a reasonable default
            canvas_w = 1100
        two_col = len(self._char_panels) >= getattr(self, "_CHAR_2COL_THRESHOLD", 2)
        per_card = (canvas_w // 2) if two_col else canvas_w
        wraplen = max(200, per_card - 80)

        all_caps = [
            ("fax", "FAX:", info.get("fax", [])),
            ("dreads", "Dreads:", info.get("dreads", [])),
            ("blops", "Blops:", info.get("blops", [])),
            ("titans", "Titans:", info.get("titans", [])),
        ]

        has_caps = False
        for key, label, items in all_caps:
            if not items:
                continue
            if only_cap and key != only_cap:
                continue
            # Filter items to the selected region (if any); a cap whose
            # entries are all in other regions is skipped entirely.
            items = _filter_cap_entries(items, only_region)
            if not items:
                continue
            has_caps = True
            row = tk.Frame(panel._cap_frame, bg=BG_PANEL)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label, font=("Consolas", 10, "bold"),
                     fg=FG_MAGENTA, bg=BG_PANEL, width=8, anchor=tk.W
                     ).pack(side=tk.LEFT)
            display = ", ".join(
                f"{e['ship']} @ {e['location']}" if isinstance(e, dict) else e
                for e in items
            )
            tk.Label(row, text=display,
                     font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                     anchor=tk.W, wraplength=wraplen, justify=tk.LEFT,
                     ).pack(side=tk.LEFT, padx=(5, 0), fill=tk.X, expand=True)

        # Cyno capability (current ship is Force Recon with cyno fitted + ozone)
        if (info.get("cyno") and (not only_cap or only_cap == "cyno")
                and (not only_region or info.get("region") == only_region)):
            has_caps = True
            row = tk.Frame(panel._cap_frame, bg=BG_PANEL)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text="Cyno:", font=("Consolas", 10, "bold"),
                     fg=FG_YELLOW, bg=BG_PANEL, width=8, anchor=tk.W
                     ).pack(side=tk.LEFT)
            tk.Label(row, text=f"Active — {info['ship']} in {info['system']}",
                     font=("Consolas", 9), fg=FG_GREEN, bg=BG_PANEL,
                     anchor=tk.W).pack(side=tk.LEFT, padx=(5, 0))
            ozone = info.get("cyno_ozone", 0)
            if info.get("cyno_low"):
                tk.Label(row, text=f"  [LOW OZONE - {ozone}]",
                         font=("Consolas", 9, "bold"), fg=FG_RED, bg=BG_PANEL,
                         anchor=tk.W).pack(side=tk.LEFT)
            else:
                tk.Label(row, text=f"  ({ozone} ozone)",
                         font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL,
                         anchor=tk.W).pack(side=tk.LEFT)

        # Dictor/HIC capability (current ship is interdictor or heavy interdictor)
        if (info.get("dictor") and (not only_cap or only_cap == "hic/dictor")
                and (not only_region or info.get("region") == only_region)):
            has_caps = True
            row = tk.Frame(panel._cap_frame, bg=BG_PANEL)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text="Dictor:", font=("Consolas", 10, "bold"),
                     fg=FG_ORANGE, bg=BG_PANEL, width=8, anchor=tk.W
                     ).pack(side=tk.LEFT)
            tk.Label(row, text=f"Active — {info['ship']} in {info['system']}",
                     font=("Consolas", 9), fg=FG_GREEN, bg=BG_PANEL,
                     anchor=tk.W).pack(side=tk.LEFT, padx=(5, 0))

        if not has_caps:
            tk.Label(panel._cap_frame,
                     text="No capabilities detected (asset scope may not be granted)",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL,
                     ).pack(anchor=tk.W)

    # ── Character Filter Logic ─────────────────────────────────────────────

    def _on_cap_filter_changed(self, event=None):
        """Handle capability filter dropdown change."""
        cap = self._char_filter_cap_var.get()
        if cap:
            self._char_filter_region.config(state="readonly")
            self._update_filter_regions()
        else:
            self._char_filter_region.config(state="disabled")
            self._char_filter_region_var.set("")
        self._apply_char_filter()

    def _on_region_filter_changed(self, event=None):
        """Handle region filter dropdown change."""
        self._apply_char_filter()

    def _clear_char_filter(self):
        """Clear both filter dropdowns."""
        self._char_filter_cap_var.set("")
        self._char_filter_region_var.set("")
        self._char_filter_region.config(state="disabled")
        self._char_filter_count_label.config(text="")
        self._apply_char_filter()

    def _get_regions_for_capability(self, cap_key: str) -> set[str]:
        """Get all regions where a given capability exists across all characters."""
        regions = set()
        for panel in self._char_panels:
            info = getattr(panel, '_info', None)
            if not info:
                continue
            if cap_key in ("cyno", "hic/dictor"):
                flag = "cyno" if cap_key == "cyno" else "dictor"
                if info.get(flag) and info.get("region"):
                    regions.add(info["region"])
            else:
                for entry in info.get(cap_key, []):
                    if isinstance(entry, dict) and entry.get("region"):
                        regions.add(entry["region"])
        return regions

    def _update_filter_regions(self):
        """Update the region dropdown based on selected capability."""
        cap = self._char_filter_cap_var.get()
        if not cap:
            self._char_filter_region["values"] = [""]
            return
        cap_key = cap.lower()
        regions = self._get_regions_for_capability(cap_key)
        values = [""] + sorted(regions)
        self._char_filter_region["values"] = values
        # Reset region selection if current value is no longer valid
        if self._char_filter_region_var.get() not in values:
            self._char_filter_region_var.set("")

    def _char_matches_filter(self, info: dict, cap_key: str, region: str) -> bool:
        """Check if a character's info matches the current filter."""
        if cap_key in ("cyno", "hic/dictor"):
            if not info.get("cyno" if cap_key == "cyno" else "dictor"):
                return False
            if region and info.get("region") != region:
                return False
            return True
        else:
            entries = info.get(cap_key, [])
            if not entries:
                return False
            if not region:
                return True
            # Check if any entry is in the selected region
            for e in entries:
                if isinstance(e, dict) and e.get("region") == region:
                    return True
            return False

    def _is_char_active_in_cap(self, info: dict, cap_key: str) -> bool:
        """True if the character is CURRENTLY flying a ship matching the filter.
        (Their current ship type matches, not just something in their hangar.)"""
        from ship_classes import FAX, DREADNOUGHTS, BLACK_OPS, TITANS
        ship_tid = info.get("ship_type_id", 0)
        if not ship_tid:
            return False
        if cap_key == "cyno":
            return bool(info.get("cyno"))
        if cap_key == "hic/dictor":
            return bool(info.get("dictor"))
        if cap_key == "fax":
            return ship_tid in FAX
        if cap_key == "dreads":
            return ship_tid in DREADNOUGHTS
        if cap_key == "blops":
            return ship_tid in BLACK_OPS
        if cap_key == "titans":
            return ship_tid in TITANS
        return False

    def _set_panel_highlight(self, panel, active: bool):
        """Apply/remove 'active ship' highlight on a character panel."""
        if active:
            panel.config(highlightbackground=FG_GREEN, highlightthickness=2)
        else:
            panel.config(highlightbackground=BORDER_COLOR, highlightthickness=1)
        # Toggle the star marker next to the name (U3)
        star = getattr(panel, "_star", None)
        if star is not None:
            try:
                if active:
                    # Pack before the arrow → appears at the very left
                    star.pack(side=tk.LEFT, padx=(0, 4), before=panel._arrow)
                else:
                    star.pack_forget()
            except tk.TclError:
                pass

    def _relayout_character_panels(self, visible_panels: list, active_set=None):
        """Arrange character panels in the 2-column grid.
        visible_panels is the ordered list of panels to show (active first).
        Hidden panels (not in the list) are grid-removed.
        active_set (optional): set of panels to mark with active highlight."""
        active_set = active_set or set()

        # Hide everything first
        for panel in self._char_panels:
            try:
                panel.grid_forget()
            except tk.TclError:
                pass

        # With only 1 character, use single column; otherwise 2
        use_two_cols = len(visible_panels) >= self._CHAR_2COL_THRESHOLD

        for i, panel in enumerate(visible_panels):
            if not self._panel_alive(panel):
                continue
            if use_two_cols:
                row, col = divmod(i, 2)
                # Span both columns if this is the last odd-indexed item
                if (i == len(visible_panels) - 1
                        and len(visible_panels) % 2 == 1):
                    panel.grid(row=row, column=0, columnspan=2,
                               sticky="nsew", padx=4, pady=3)
                else:
                    panel.grid(row=row, column=col,
                               sticky="nsew", padx=4, pady=3)
            else:
                panel.grid(row=i, column=0, columnspan=2,
                           sticky="nsew", padx=4, pady=3)
            # Apply active highlight state
            self._set_panel_highlight(panel, panel in active_set)

        # Clear highlight + star on hidden panels
        visible_set = set(visible_panels)
        for panel in self._char_panels:
            if panel not in visible_set:
                self._set_panel_highlight(panel, False)

    def _apply_char_filter(self):
        """Show/hide character panels based on filter selections.
        Characters actively flying a matching ship are highlighted and
        pinned to the top of the list."""
        cap = self._char_filter_cap_var.get()
        region = self._char_filter_region_var.get()

        if not cap:
            # No filter — show all in original order, no highlights
            for panel in self._char_panels:
                info = getattr(panel, '_info', None)
                if info:
                    self._render_capabilities(panel, info)
            self._relayout_character_panels(self._char_panels, active_set=set())
            self._char_filter_count_label.config(text="")
            return

        cap_key = cap.lower()

        # Separate matching panels into "active in ship" vs "otherwise matching"
        active_panels = []
        passive_panels = []
        for panel in self._char_panels:
            info = getattr(panel, '_info', None)
            if not info or not self._char_matches_filter(info, cap_key, region):
                continue
            if self._is_char_active_in_cap(info, cap_key):
                active_panels.append(panel)
            else:
                passive_panels.append(panel)

        # Render capability content for every visible panel first
        for panel in active_panels + passive_panels:
            self._render_capabilities(panel, getattr(panel, '_info', {}),
                                       only_cap=cap_key, only_region=region)

        # Lay out active first, then passive; active ones get the highlight
        visible = active_panels + passive_panels
        self._relayout_character_panels(visible, active_set=set(active_panels))

        total = len(self._char_panels)
        label = f"{len(visible)}/{total} characters"
        if active_panels:
            label += f"  ({len(active_panels)} active)"
        if region:
            label += f" in {region}"
        self._char_filter_count_label.config(text=label, fg=FG_ACCENT)

    # ── Fittings Tab ──────────────────────────────────────────────────────────
    #
    # Hosts a nested ttk.Notebook with three sub-tabs: Fittings (library
    # master/detail, fully implemented below), Doctrines and MOTD (placeholders
    # replaced by Phases 6 and 7). The shared services self.type_catalog and
    # self.fittings are constructed in __init__ (after esi_auth).

    # Canonical slot display order for the read-only module list.
    _FIT_SLOT_ORDER = ("high", "med", "low", "rig", "subsystem", "service")
    _FIT_SLOT_LABELS = {
        "high": "High Slots", "med": "Mid Slots", "low": "Low Slots",
        "rig": "Rigs", "subsystem": "Subsystems", "service": "Service Slots",
    }

    def _build_fitting_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Fittings  ")
        self._fitting_subnb = ttk.Notebook(tab, style="Dark.TNotebook")
        self._fitting_subnb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._build_fittings_subtab()     # Task 5.2/5.3 — full implementation
        self._build_doctrines_subtab()    # Phase 6 — placeholder
        self._build_motd_subtab()         # Phase 7 — placeholder
        # Refresh the MOTD dropdowns whenever its sub-tab is shown so doctrines
        # created/imported after this tab was built still appear there.
        self._fitting_subnb.bind(
            "<<NotebookTabChanged>>", self._on_fitting_subnb_changed)

    def _on_fitting_subnb_changed(self, event=None):
        """Inner sub-notebook tab changed: when the MOTD sub-tab becomes the
        selected one, refresh its doctrine + FC dropdowns so they reflect the
        current library (the MOTD tab is built once, but doctrines/characters
        can change after that). Detected by tab TEXT, never a hardcoded index."""
        nb = getattr(self, "_fitting_subnb", None)
        if nb is None:
            return
        try:
            text = nb.tab(nb.select(), "text") or ""
        except tk.TclError:
            return
        if "MOTD" in text:
            self._motd_refresh_doctrines()
            self._motd_refresh_fc_choices()
            # Rebuild the include-tag checkboxes and preview from the selected
            # doctrine's CURRENT members. Fixing a doctrine in the Doctrines tab
            # (adding ships, or tagging previously-untagged ones) is reflected
            # the next time the MOTD tab is shown, instead of staying blank.
            # Done directly (not via _motd_on_doctrine_change) so any explicitly
            # loaded-fits fallback from a linked MOTD is preserved.
            self._motd_rebuild_tag_checkboxes()
            self._rebuild_motd_preview()
            # Run the fleet-boss check automatically on MOTD-tab open so the
            # Set button enables without the user clicking "Refresh fleet".
            self._motd_refresh_fleet_status()

    # ── Doctrines sub-tab (Tasks 6.1 / 6.2) ───────────────────────────────────

    # Canonical tag display order for the role-grouped doctrine detail. Members
    # carrying a tag outside this list are grouped last under "Other".
    _DOCTRINE_TAG_ORDER = (
        "DPS", "Logistics", "Links",
        "Support - EWAR", "Support - Webs", "Defenders", "Tackle", "Special",
    )

    def _build_doctrines_subtab(self):
        """Doctrine manager: master list of doctrines (left) + a role-grouped,
        editable detail pane (right). New/Import/Export live above the list."""
        tab = tk.Frame(self._fitting_subnb, bg=BG_DARK)
        self._fitting_subnb.add(tab, text="  Doctrines  ")

        # ── Toolbar: New / Import / Export ───────────────────────────────────
        toolbar = tk.Frame(tab, bg=BG_DARK)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Button(toolbar, text="New doctrine", style="Green.TButton",
                   command=self._new_doctrine).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Import file", style="Dark.TButton",
                   command=self._import_doctrine).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Export file", style="Dark.TButton",
                   command=self._export_doctrine).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Manage tags…", style="Dark.TButton",
                   command=self._manage_tags_dialog).pack(side=tk.LEFT, padx=2)

        # ── Master / detail split ────────────────────────────────────────────
        body = tk.Frame(tab, bg=BG_DARK)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        body.columnconfigure(0, weight=2, uniform="doc")
        body.columnconfigure(1, weight=5, uniform="doc")
        body.rowconfigure(0, weight=1)

        # Left: doctrines list (Treeview).
        left = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                        highlightbackground=BORDER_COLOR, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        tk.Label(left, text="DOCTRINES", font=("Consolas", 9, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).grid(
                     row=0, column=0, sticky="w", padx=6, pady=(6, 2))

        tree_wrap = tk.Frame(left, bg=BG_PANEL)
        tree_wrap.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        columns = ("name", "fits")
        self._doctrine_tree = ttk.Treeview(
            tree_wrap, columns=columns, show="headings",
            style="Dark.Treeview", selectmode="browse")
        self._doctrine_tree.heading("name", text="Name")
        self._doctrine_tree.heading("fits", text="#Fits")
        self._doctrine_tree.column("name", width=150, anchor=tk.W)
        self._doctrine_tree.column("fits", width=44, anchor=tk.CENTER,
                                   stretch=False)
        self._doctrine_tree.grid(row=0, column=0, sticky="nsew")
        self._doctrine_tree.bind("<<TreeviewSelect>>",
                                 self._on_doctrine_select)

        tree_sb = ttk.Scrollbar(tree_wrap, orient="vertical",
                                command=self._doctrine_tree.yview)
        self._doctrine_tree.configure(yscrollcommand=tree_sb.set)
        tree_sb.grid(row=0, column=1, sticky="ns")

        # Right: detail (scrollable).
        right = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                         highlightbackground=BORDER_COLOR, highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        detail_canvas = tk.Canvas(right, bg=BG_PANEL, highlightthickness=0)
        detail_canvas.grid(row=0, column=0, sticky="nsew")
        detail_sb = ttk.Scrollbar(right, orient="vertical",
                                  command=detail_canvas.yview)
        detail_sb.grid(row=0, column=1, sticky="ns")
        detail_canvas.configure(yscrollcommand=detail_sb.set)
        self._register_scroll_canvas(detail_canvas)

        self._doctrine_detail = tk.Frame(detail_canvas, bg=BG_PANEL)
        _detail_win = detail_canvas.create_window(
            (0, 0), window=self._doctrine_detail, anchor="nw")

        def _on_detail_config(event=None):
            detail_canvas.configure(scrollregion=detail_canvas.bbox("all"))
        self._doctrine_detail.bind("<Configure>", _on_detail_config)

        def _on_canvas_config(event):
            detail_canvas.itemconfig(_detail_win, width=event.width)
        detail_canvas.bind("<Configure>", _on_canvas_config)

        # Populate.
        self._refresh_doctrine_list()
        self._show_doctrine_detail(None)

    # ── Doctrine list / detail rendering (Task 6.1) ───────────────────────────

    def _doctrine_list_visible(self) -> bool:
        """True once the doctrines sub-tab has been built (its tree exists)."""
        return getattr(self, "_doctrine_tree", None) is not None

    def _refresh_doctrine_list(self):
        """Clear + repopulate the doctrines Treeview, preserving the current
        selection when possible. Safe to call before the sub-tab exists."""
        tree = getattr(self, "_doctrine_tree", None)
        if tree is None:
            return
        # Red foreground for doctrines that have any untagged fit, + a hover
        # tooltip (bound once).
        tree.tag_configure("warn", foreground=FG_RED)
        if not getattr(self, "_doctrine_tree_motion_bound", False):
            tree.bind("<Motion>", self._on_doctrine_tree_motion, add="+")
            tree.bind("<Leave>", lambda e: self._hide_tooltip(), add="+")
            self._doctrine_tree_motion_bound = True
        prev = self._doctrine_selected_id
        for iid in tree.get_children():
            tree.delete(iid)
        self._doctrine_warn = {}
        restored = False
        for doc in sorted(self.fittings.list_doctrines(),
                          key=lambda d: (d.name or "").lower()):
            # Warn when the doctrine has members and at least one carries no tags
            # (such fits can't be selected by role for the MOTD).
            warn = bool(doc.members) and any(
                not (m.tags or []) for m in doc.members)
            self._doctrine_warn[doc.id] = warn
            display = f"⚠  {doc.name}" if warn else doc.name
            tree.insert("", tk.END, iid=doc.id,
                        values=(display, len(doc.members)),
                        tags=("warn",) if warn else ())
            if doc.id == prev:
                restored = True
        if restored:
            tree.selection_set(prev)
        elif prev is not None and self.fittings.get_doctrine(prev) is None:
            # Selected doctrine was deleted — clear the detail pane.
            self._doctrine_selected_id = None
            self._show_doctrine_detail(None)
        # Keep the Fleet-Management doctrine dropdown in sync (Phase C).
        try:
            self._refresh_fleet_doctrine_combo()
        except Exception:
            pass

    def _on_doctrine_tree_motion(self, event):
        """Show a warning tooltip while hovering a doctrine that has untagged
        fits (re-shown only when the hovered row changes, to avoid flicker)."""
        tree = self._doctrine_tree
        row = tree.identify_row(event.y)
        if getattr(self, "_doctrine_warn", {}).get(row):
            if row != getattr(self, "_doctrine_warn_hover", None):
                self._doctrine_warn_hover = row
                self._show_tooltip(
                    event,
                    "Some fits without tags assigned — MOTD function may not "
                    "work properly.")
        else:
            self._doctrine_warn_hover = None
            self._hide_tooltip()

    def _on_doctrine_select(self, event=None):
        tree = self._doctrine_tree
        sel = tree.selection()
        if not sel:
            return
        self._doctrine_selected_id = sel[0]
        self._show_doctrine_detail(sel[0])

    def _clear_doctrine_detail(self):
        for w in self._doctrine_detail.winfo_children():
            w.destroy()

    def _group_members_by_tag(self, doctrine):
        """Return an ordered list of (tag_label, [members]) groups in canonical
        tag order. A member appears under each of its tags; an untagged member
        falls into a trailing 'Untagged' group. Tags not in the canonical order
        are appended (sorted) before 'Untagged'."""
        by_tag: dict[str, list] = {}
        untagged: list = []
        extra_tags: list[str] = []
        for mem in doctrine.members:
            if not mem.tags:
                untagged.append(mem)
                continue
            for tag in mem.tags:
                by_tag.setdefault(tag, []).append(mem)
                if (tag not in self._DOCTRINE_TAG_ORDER
                        and tag not in extra_tags):
                    extra_tags.append(tag)
        groups: list[tuple[str, list]] = []
        for tag in self._DOCTRINE_TAG_ORDER:
            if by_tag.get(tag):
                groups.append((tag, by_tag[tag]))
        for tag in sorted(extra_tags):
            groups.append((tag, by_tag[tag]))
        if untagged:
            groups.append(("Untagged", untagged))
        return groups

    def _show_doctrine_detail(self, doctrine_id):
        """Render the selected doctrine: editable name/description, members
        grouped by tag (canonical order) with per-member tag chips and
        edit/remove controls, plus an 'Add fit' affordance."""
        self._clear_doctrine_detail()
        parent = self._doctrine_detail

        if not doctrine_id:
            tk.Label(parent, text="Select a doctrine, or create one with "
                                  "'New doctrine'.",
                     font=("Consolas", 10), fg=FG_DIM, bg=BG_PANEL,
                     wraplength=420, justify=tk.LEFT).pack(
                         anchor=tk.W, padx=10, pady=10)
            return
        doctrine = self.fittings.get_doctrine(doctrine_id)
        if doctrine is None:
            tk.Label(parent, text="Doctrine not found.",
                     font=("Consolas", 10), fg=FG_RED, bg=BG_PANEL).pack(
                         anchor=tk.W, padx=10, pady=10)
            return

        # Header: name + rename/delete.
        head = tk.Frame(parent, bg=BG_PANEL)
        head.pack(fill=tk.X, padx=10, pady=(10, 0))
        tk.Label(head, text=doctrine.name, font=("Consolas", 13, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL, anchor=tk.W, justify=tk.LEFT,
                 wraplength=380).pack(side=tk.LEFT)

        head_btns = tk.Frame(parent, bg=BG_PANEL)
        head_btns.pack(fill=tk.X, padx=10, pady=(4, 0))
        ttk.Button(head_btns, text="Rename", style="Dark.TButton",
                   command=lambda: self._rename_doctrine(doctrine.id)).pack(
                       side=tk.LEFT, padx=2)
        ttk.Button(head_btns, text="Edit description", style="Dark.TButton",
                   command=lambda: self._edit_doctrine_desc(doctrine.id)).pack(
                       side=tk.LEFT, padx=2)
        ttk.Button(head_btns, text="Delete", style="Red.TButton",
                   command=lambda: self._delete_doctrine(doctrine.id)).pack(
                       side=tk.LEFT, padx=2)

        # Description.
        if (doctrine.description or "").strip():
            tk.Label(parent, text=doctrine.description, font=("Consolas", 9),
                     fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W, justify=tk.LEFT,
                     wraplength=420).pack(anchor=tk.W, padx=12, pady=(6, 0))

        # Add-fit affordance.
        add_row = tk.Frame(parent, bg=BG_PANEL)
        add_row.pack(fill=tk.X, padx=10, pady=(8, 2))
        ttk.Button(add_row, text="Add fit…", style="Green.TButton",
                   command=lambda: self._add_fit_to_doctrine(doctrine.id)).pack(
                       side=tk.LEFT, padx=2)
        ttk.Button(add_row, text="Add tag…", style="Dark.TButton",
                   command=lambda: self._add_custom_tag(doctrine.id)).pack(
                       side=tk.LEFT, padx=2)
        ttk.Button(add_row, text="Exemptions…", style="Dark.TButton",
                   command=lambda: self._edit_doctrine_exemptions(doctrine.id)).pack(
                       side=tk.LEFT, padx=2)

        if not doctrine.members:
            tk.Label(parent, text="No fits yet — use 'Add fit…' to add ships "
                                  "to this doctrine.",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL,
                     wraplength=420, justify=tk.LEFT).pack(
                         anchor=tk.W, padx=12, pady=(8, 10))
            return

        # Members grouped by tag (canonical order).
        links_range = self._doctrine_links_range(doctrine)
        for tag_label, members in self._group_members_by_tag(doctrine):
            tk.Label(parent, text=tag_label, font=("Consolas", 10, "bold"),
                     fg=FG_GREEN, bg=BG_PANEL).pack(
                         anchor=tk.W, padx=12, pady=(8, 2))
            for mem in members:
                self._render_doctrine_member_row(parent, doctrine, mem, links_range)

    def _render_doctrine_member_row(self, parent, doctrine, mem, links_range):
        """One member row: fit name + its tag-chip cluster within this doctrine
        + Tags/Remove controls."""
        fit = self.fittings.get_fit(mem.fit_id)
        name = fit.name if fit is not None else f"(missing fit {mem.fit_id})"
        hull = f"  ·  {fit.hull_name}" if fit is not None and fit.hull_name \
            else ""

        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill=tk.X, padx=14, pady=1)

        info = tk.Frame(row, bg=BG_PANEL)
        info.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(info, text=f"{name}{hull}", font=("Consolas", 9),
                 fg=FG_TEXT if fit is not None else FG_RED, bg=BG_PANEL,
                 anchor=tk.W, justify=tk.LEFT, wraplength=300).pack(anchor=tk.W)
        # Tag-chip cluster: shows all tags this member carries in this doctrine.
        if mem.tags:
            chips = tk.Frame(info, bg=BG_PANEL)
            chips.pack(anchor=tk.W, pady=(1, 0))
            for tag in mem.tags:
                tk.Label(chips, text=f" {tag} ", font=("Consolas", 8),
                         fg=BG_DARK, bg=FG_ACCENT, padx=2).pack(
                             side=tk.LEFT, padx=(0, 3))
        else:
            tk.Label(info, text="(no tags)", font=("Consolas", 8),
                     fg=FG_DIM, bg=BG_PANEL).pack(anchor=tk.W, pady=(1, 0))

        ctrls = tk.Frame(row, bg=BG_PANEL)
        ctrls.pack(side=tk.RIGHT)
        # Effective ideal summary (explicit value, tag default, or "off").
        eff = fleet_guidance.resolve_composition_ideal(mem, links_range)
        if mem.ideal_mode == "off":
            summary = "off"
        elif eff is None:
            summary = "—"  # em dash: no guidance for this fit
        else:
            hi = "∞" if eff.max is None else str(eff.max)  # infinity
            unit = "%" if eff.mode == "percent" else "#"
            auto = "" if mem.ideal_mode in ("percent", "count") else " (auto)"
            summary = f"{eff.min}-{hi}{unit}{auto}"
        tk.Label(ctrls, text=f"Ideal {summary}", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(ctrls, text="Ideal…", style="Dark.TButton",
                   command=lambda d=doctrine.id, f=mem.fit_id:
                       self._edit_member_ideal(d, f)).pack(side=tk.LEFT, padx=1)
        ttk.Button(ctrls, text="Tags", style="Dark.TButton",
                   command=lambda: self._edit_member_tags(
                       doctrine.id, mem.fit_id)).pack(side=tk.LEFT, padx=1)
        ttk.Button(ctrls, text="Remove", style="Red.TButton",
                   command=lambda: self._remove_doctrine_member(
                       doctrine.id, mem.fit_id)).pack(side=tk.LEFT, padx=1)

    def _doctrine_links_range(self, doctrine):
        """Computed ideal links-ship count range for this doctrine (or None)."""
        parsed = []
        for m in doctrine.members:
            if "Links" in (m.tags or []):
                fit = self.fittings.get_fit(m.fit_id)
                if fit is not None:
                    parsed.append(fit.parsed)
        return fleet_guidance.links_ideal_range(parsed, self.type_catalog)

    def _edit_member_ideal(self, doctrine_id, fit_id):
        """Modal: pick %/#/off and a min-max range for a member's ideal."""
        doctrine = self.fittings.get_doctrine(doctrine_id)
        if doctrine is None:
            return
        mem = next((m for m in doctrine.members if m.fit_id == fit_id), None)
        if mem is None:
            return
        links_range = self._doctrine_links_range(doctrine)
        eff = fleet_guidance.resolve_composition_ideal(mem, links_range)

        win = tk.Toplevel(self.root)
        win.title("Ideal composition")
        win.configure(bg=BG_DARK)
        win.transient(self.root)
        try:
            win.grab_set()
        except tk.TclError:
            pass

        mode_var = tk.StringVar(
            value=(mem.ideal_mode if mem.ideal_mode in ("percent", "count", "off")
                   else (eff.mode if eff else "off")))
        min_var = tk.StringVar(
            value=str(mem.ideal_min if mem.ideal_min is not None
                      else (eff.min if eff else "")))
        max_var = tk.StringVar(
            value=("" if (mem.ideal_max is None and (eff is None or eff.max is None))
                   else str(mem.ideal_max if mem.ideal_max is not None else eff.max)))

        tk.Label(win, text="Mode:", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).pack(anchor=tk.W, padx=12, pady=(12, 2))
        mode_row = tk.Frame(win, bg=BG_DARK)
        mode_row.pack(anchor=tk.W, padx=12)
        for label, val in (("% of fleet", "percent"), ("# pilots", "count"),
                           ("off", "off")):
            tk.Radiobutton(mode_row, text=label, value=val, variable=mode_var,
                           font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                           selectcolor=BG_ENTRY, activebackground=BG_DARK,
                           activeforeground=FG_TEXT).pack(side=tk.LEFT, padx=4)

        rng = tk.Frame(win, bg=BG_DARK)
        rng.pack(anchor=tk.W, padx=12, pady=(8, 0))
        tk.Label(rng, text="Min:", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).pack(side=tk.LEFT)
        tk.Entry(rng, textvariable=min_var, width=5, font=("Consolas", 10),
                 bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE).pack(
                     side=tk.LEFT, padx=(2, 8))
        tk.Label(rng, text="Max (blank = none):", font=("Consolas", 10),
                 fg=FG_TEXT, bg=BG_DARK).pack(side=tk.LEFT)
        tk.Entry(rng, textvariable=max_var, width=5, font=("Consolas", 10),
                 bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE).pack(
                     side=tk.LEFT, padx=2)

        def _save():
            mode = mode_var.get()
            if mode == "off":
                self.fittings.set_member_ideal(doctrine_id, fit_id, "off", None, None)
            else:
                def _int(s):
                    s = (s or "").strip()
                    return int(s) if s.isdigit() else None
                self.fittings.set_member_ideal(
                    doctrine_id, fit_id, mode, _int(min_var.get()), _int(max_var.get()))
            self.fittings.save()
            win.destroy()
            # Ideal changes don't affect the doctrine-list untagged-warning or fit-list membership counts, so refresh only the detail + MOTD preview.
            self._show_doctrine_detail(doctrine_id)
            if hasattr(self, "_rebuild_motd_preview"):
                self._rebuild_motd_preview()

        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btns, text="Save", style="Green.TButton",
                   command=_save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.RIGHT)
        self.root.wait_window(win)

    # ── Doctrine CRUD controllers (Task 6.1) ──────────────────────────────────

    def _new_doctrine(self):
        name = self._prompt_text_line("New Doctrine", "Doctrine name:", "")
        if name is None:
            return
        name = name.strip()
        if not name:
            return
        did = self.fittings.add_doctrine(name)
        self.fittings.save()
        self._doctrine_selected_id = did
        self._refresh_doctrine_list()
        self._motd_refresh_doctrines()
        self._refresh_fit_list(self._fit_search_var.get())
        self._show_doctrine_detail(did)

    def _rename_doctrine(self, doctrine_id):
        doctrine = self.fittings.get_doctrine(doctrine_id)
        if doctrine is None:
            return
        new_name = self._prompt_text_line(
            "Rename Doctrine", "Name:", doctrine.name)
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name or new_name == doctrine.name:
            return
        doctrine.name = new_name
        self.fittings.update_doctrine(doctrine)
        self.fittings.save()
        self._refresh_doctrine_list()
        self._show_doctrine_detail(doctrine_id)

    def _edit_doctrine_desc(self, doctrine_id):
        doctrine = self.fittings.get_doctrine(doctrine_id)
        if doctrine is None:
            return
        new_desc = self._prompt_text_block(
            "Edit Description", "Description for this doctrine:",
            doctrine.description or "")
        if new_desc is None:
            return
        doctrine.description = new_desc
        self.fittings.update_doctrine(doctrine)
        self.fittings.save()
        self._show_doctrine_detail(doctrine_id)

    def _delete_doctrine(self, doctrine_id):
        doctrine = self.fittings.get_doctrine(doctrine_id)
        if doctrine is None:
            return
        if not messagebox.askyesno(
                "Delete Doctrine",
                f"Delete doctrine '{doctrine.name}'?\n\nThe fits themselves "
                "stay in the library."):
            return
        self.fittings.delete_doctrine(doctrine_id)
        self.fittings.save()
        if self._doctrine_selected_id == doctrine_id:
            self._doctrine_selected_id = None
        self._refresh_doctrine_list()
        self._motd_refresh_doctrines()
        self._refresh_fit_list(self._fit_search_var.get())
        self._show_doctrine_detail(None)

    # ── Doctrine import / export (Task 6.1) ───────────────────────────────────

    def _export_doctrine(self, doctrine_id=None):
        """Export a doctrine to a self-contained .fctdoc (JSON) file."""
        if doctrine_id is None:
            doctrine_id = self._doctrine_selected_id
        if not doctrine_id:
            messagebox.showinfo(
                "Export doctrine",
                "Select a doctrine to export first.")
            return
        doctrine = self.fittings.get_doctrine(doctrine_id)
        if doctrine is None:
            return
        safe_name = re.sub(r"[^A-Za-z0-9 _-]", "_", doctrine.name or "doctrine")
        path = filedialog.asksaveasfilename(
            title="Export doctrine",
            defaultextension=".fctdoc",
            initialfile=f"{safe_name}.fctdoc",
            filetypes=[("FCTool doctrine", "*.fctdoc"), ("All files", "*.*")])
        if not path:
            return
        try:
            payload = self.fittings.export_doctrines([doctrine_id])
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            messagebox.showerror("Export failed",
                                 f"Could not write the doctrine file:\n{e}")
            return
        messagebox.showinfo(
            "Export doctrine",
            f"Exported '{doctrine.name}' to:\n{path}")

    def _import_doctrine(self):
        """Import a .fctdoc share file: read JSON -> import_share -> summary."""
        path = filedialog.askopenfilename(
            title="Import doctrine",
            filetypes=[("FCTool doctrine", "*.fctdoc"),
                       ("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            messagebox.showerror("Import failed",
                                 f"Could not read the doctrine file:\n{e}")
            return
        try:
            summary = self.fittings.import_share(payload)
            self.fittings.save()
        except Exception as e:
            messagebox.showerror("Import failed",
                                 f"Could not import the doctrine:\n{e}")
            return
        # Refresh both the doctrine list and the fittings list (new fits may
        # have been added to the library).
        self._refresh_doctrine_list()
        self._motd_refresh_doctrines()
        self._refresh_fit_list(self._fit_search_var.get())
        self._show_doctrine_detail(self._doctrine_selected_id)
        messagebox.showinfo(
            "Import doctrine",
            f"Imported {summary.doctrines_added} doctrine(s).\n\n"
            f"Fits added: {summary.fits_added}\n"
            f"Fits reused (already in library): {summary.fits_reused}")

    # ── Doctrine membership + tags (Task 6.2) ─────────────────────────────────

    def _add_fit_to_doctrine(self, doctrine_id):
        """Bulk-add library fits to a doctrine via the shared multi-select
        picker. Candidates are fits not already in the doctrine; the chosen
        fits all receive the SAME tag set (prompted once) on add."""
        doctrine = self.fittings.get_doctrine(doctrine_id)
        if doctrine is None:
            return
        existing = {m.fit_id for m in doctrine.members}
        candidates = [f for f in sorted(self.fittings.list_fits(),
                                        key=lambda f: (f.name or "").lower())
                      if f.id not in existing]
        if not candidates:
            messagebox.showinfo(
                "Add fits",
                "Every fit in the library is already in this doctrine, or the "
                "library is empty. Import fits on the Fittings sub-tab first.")
            return

        # Build picker items ship-first ("ShipClass — FitName"), consistent
        # with the pyfa/ESI import pickers. Resolve ship class via the SDE
        # catalog, falling back to the stored hull name.
        items = []
        for f in candidates:
            ship = ""
            try:
                ship = self.type_catalog.resolve_name(f.hull_type_id) or ""
            except Exception:
                ship = ""
            if not ship:
                ship = f.hull_name or "?"
            items.append({
                "id": f.id,
                "label": f"{ship}  —  {f.name or '?'}",
                "row_data": f,
            })

        def _on_import(chosen, ctl):
            if not chosen:
                return
            # Prompt tags ONCE. Any chosen tags apply to every selected fit;
            # leaving them all UNCHECKED adds the fits TAGLESS so you can tag
            # each one individually afterwards (per-fit "Tags" in the doctrine
            # detail). Cancel aborts the add entirely.
            tags = self._prompt_tag_multiselect(
                "Tags for these fits",
                "Optional: choose tags to apply to ALL selected fits.\n"
                "Leave everything unchecked to add them WITHOUT tags — you can "
                "tag each fit individually later.",
                selected=[])
            if tags is None:
                return
            for item in chosen:
                self.fittings.add_fit_to_doctrine(
                    doctrine_id, item["row_data"].id, tags)
            self.fittings.save()
            self._refresh_doctrine_list()
            self._show_doctrine_detail(doctrine_id)
            self._refresh_fit_list(self._fit_search_var.get())
            self._motd_refresh_doctrines()
            ctl.close()

        self._show_multi_select_picker(
            items, on_import=_on_import, title="Add fits to doctrine")

    def _edit_member_tags(self, doctrine_id, fit_id):
        """Multi-select the tag vocabulary for one (doctrine, fit) membership."""
        doctrine = self.fittings.get_doctrine(doctrine_id)
        if doctrine is None:
            return
        mem = next((m for m in doctrine.members if m.fit_id == fit_id), None)
        if mem is None:
            return
        fit = self.fittings.get_fit(fit_id)
        label_name = fit.name if fit is not None else fit_id
        tags = self._prompt_tag_multiselect(
            "Edit tags",
            f"Tags for '{label_name}' in '{doctrine.name}':",
            selected=list(mem.tags))
        if tags is None:
            return
        self.fittings.set_member_tags(doctrine_id, fit_id, tags)
        self.fittings.save()
        self._refresh_doctrine_list()
        self._refresh_fit_list(self._fit_search_var.get())
        self._show_doctrine_detail(doctrine_id)

    def _remove_doctrine_member(self, doctrine_id, fit_id):
        self.fittings.remove_fit_from_doctrine(doctrine_id, fit_id)
        self.fittings.save()
        self._refresh_doctrine_list()
        self._refresh_fit_list(self._fit_search_var.get())
        self._show_doctrine_detail(doctrine_id)

    def _add_fit_to_doctrine_from_fit(self, fit_id):
        """Cross-link from the Fittings detail pane: pick a doctrine (excluding
        ones already containing this fit), choose tags, then add the fit."""
        fit = self.fittings.get_fit(fit_id)
        if fit is None:
            return
        candidates = []
        for doc in sorted(self.fittings.list_doctrines(),
                          key=lambda d: (d.name or "").lower()):
            if not any(m.fit_id == fit_id for m in doc.members):
                candidates.append(doc)
        if not candidates:
            if self.fittings.list_doctrines():
                messagebox.showinfo(
                    "Add to doctrine",
                    f"'{fit.name}' is already in every doctrine.")
            else:
                messagebox.showinfo(
                    "Add to doctrine",
                    "No doctrines yet. Create one on the Doctrines sub-tab "
                    "first.")
            return

        win = tk.Toplevel(self.root)
        win.title("Add fit to doctrine")
        win.configure(bg=BG_DARK)
        win.geometry("400x420")
        try:
            win.transient(self.root)
        except tk.TclError:
            pass

        tk.Label(win, text=f"Add '{fit.name}' to which doctrine?",
                 font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                 anchor=tk.W, justify=tk.LEFT, wraplength=370).pack(
                     anchor=tk.W, padx=12, pady=(12, 2))

        listbox = tk.Listbox(
            win, font=("Consolas", 10), bg=BG_ENTRY, fg=FG_TEXT,
            selectbackground="#1a5a90", selectforeground=FG_WHITE,
            borderwidth=1, relief=tk.RIDGE, activestyle="none",
            exportselection=False)
        listbox.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))
        for doc in candidates:
            listbox.insert(tk.END, f"{doc.name}  ({len(doc.members)} fits)")

        def _do_pick():
            sel = listbox.curselection()
            if not sel:
                return
            doctrine = candidates[sel[0]]
            win.destroy()
            tags = self._prompt_tag_multiselect(
                "Tags for this fit",
                f"Choose tags for '{fit.name}' in '{doctrine.name}':",
                selected=[])
            if tags is None:
                return
            self.fittings.add_fit_to_doctrine(doctrine.id, fit_id, tags)
            self.fittings.save()
            if self._doctrine_list_visible():
                self._refresh_doctrine_list()
                if self._doctrine_selected_id == doctrine.id:
                    self._show_doctrine_detail(doctrine.id)
            self._motd_refresh_doctrines()
            self._refresh_fit_list(self._fit_search_var.get())
            self._show_fit_detail(fit_id)

        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btns, text="Add", style="Green.TButton",
                   command=_do_pick).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.RIGHT)
        listbox.bind("<Double-Button-1>", lambda e: _do_pick())

    def _add_custom_tag(self, doctrine_id=None):
        """Append a custom tag to the library's tag vocabulary."""
        name = self._prompt_text_line(
            "Add Tag", "New tag name (added to the vocabulary):", "")
        if name is None:
            return
        name = name.strip()
        if not name:
            return
        if name in self.fittings.tags:
            messagebox.showinfo("Add tag", f"'{name}' is already a tag.")
            return
        self.fittings.add_tag(name)
        self.fittings.save()
        messagebox.showinfo(
            "Add tag",
            f"Added tag '{name}'. It is now available when tagging fits.")

    def _prompt_tag_multiselect(self, title, label, selected):
        """Modal multi-select of the library tag vocabulary via checkbuttons.
        Returns the chosen list of tags, or None if cancelled. Includes an
        inline 'Add tag' button that extends the vocabulary live."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG_DARK)
        win.geometry("360x420")
        win.minsize(360, 340)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass
        result = {"value": None}

        tk.Label(win, text=label, font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK, anchor=tk.W, justify=tk.LEFT, wraplength=330).pack(
                     anchor=tk.W, padx=12, pady=(12, 4))

        list_wrap = tk.Frame(win, bg=BG_PANEL, bd=1, relief=tk.RIDGE)
        canvas = tk.Canvas(list_wrap, bg=BG_PANEL, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.configure(yscrollcommand=sb.set)
        inner = tk.Frame(canvas, bg=BG_PANEL)
        _win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(_win, width=e.width))

        selected_set = set(selected or [])
        tag_vars: dict[str, tk.BooleanVar] = {}

        def _rebuild_checks():
            for w in inner.winfo_children():
                w.destroy()
            tag_vars.clear()
            for tag in self.fittings.tags:
                v = tk.BooleanVar(value=tag in selected_set)
                tag_vars[tag] = v
                cb = tk.Checkbutton(
                    inner, text=tag, variable=v, font=("Consolas", 9),
                    fg=FG_TEXT, bg=BG_PANEL, selectcolor=BG_ENTRY,
                    activebackground=BG_PANEL, activeforeground=FG_WHITE,
                    anchor=tk.W, highlightthickness=0)
                cb.pack(fill=tk.X, anchor=tk.W, padx=4, pady=1)
        _rebuild_checks()

        def _add_tag_inline():
            name = self._prompt_text_line("Add Tag", "New tag name:", "")
            if name is None:
                return
            name = name.strip()
            if not name:
                return
            # Preserve current checkbox selections across the rebuild.
            for tag, v in tag_vars.items():
                if v.get():
                    selected_set.add(tag)
                else:
                    selected_set.discard(tag)
            if name not in self.fittings.tags:
                self.fittings.add_tag(name)
                self.fittings.save()
            selected_set.add(name)
            _rebuild_checks()

        add_row = tk.Frame(win, bg=BG_DARK)
        ttk.Button(add_row, text="Add tag…", style="Dark.TButton",
                   command=_add_tag_inline).pack(side=tk.LEFT)

        def _ok():
            result["value"] = [t for t, v in tag_vars.items() if v.get()]
            win.destroy()

        def _cancel():
            win.destroy()

        btns = tk.Frame(win, bg=BG_DARK)
        ttk.Button(btns, text="OK", style="Green.TButton",
                   command=_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=_cancel).pack(side=tk.RIGHT)
        # Pack order matters: pin the two action rows to the bottom FIRST so they
        # always reserve their space, then let list_wrap expand into what's left.
        # This prevents the OK/Cancel row from being clipped on a short window.
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=12)
        add_row.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(4, 0))
        list_wrap.pack(fill=tk.BOTH, expand=True, padx=12)
        win.bind("<Escape>", lambda e: _cancel())
        win.protocol("WM_DELETE_WINDOW", _cancel)
        self.root.wait_window(win)
        return result["value"]

    def _edit_doctrine_exemptions(self, doctrine_id, _test_no_wait=False):
        """Modal editor for a doctrine's ideal-% exemptions.

        Shows the doctrine's effective_exemptions as removable rows (the STANDARD
        seed is shown when the doctrine has never been customized). 'Add…' offers
        a ship GROUP, a ship TYPE, or the 'Capitals (all)' meta; 'Reset to
        standard' clears the customization (stores None). Save routes through
        :meth:`_commit_doctrine_exemptions` (store mutator + save + refresh).

        Returns the Toplevel (tests pass ``_test_no_wait=True`` to skip
        wait_window). Mirrors the themed-Toplevel + transient + grab_set pattern
        of the tag dialogs; the add/remove/reset list logic lives in the pure
        module helpers (_add_exemption_entry/_remove_exemption_entry) so it is
        unit-testable without Tk."""
        from type_catalog import SHIP_GROUP_NAMES
        doctrine = self.fittings.get_doctrine(doctrine_id)
        if doctrine is None:
            return None

        # Working copy of the effective list; None sentinel tracks "reset to
        # standard" so Save can persist None (fall back to the seed).
        eff = fleet_guidance.effective_exemptions(doctrine)
        state = {"entries": [dict(e) for e in eff], "reset": False}

        win = tk.Toplevel(self.root)
        win.title(f"Exemptions — {doctrine.name}")
        win.configure(bg=BG_DARK)
        win.geometry("420x460")
        win.minsize(380, 360)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass

        tk.Label(win,
                 text="Ships exempted from the ideal-% denominator (they inflate "
                      "fleet size without being part of the composition target). "
                      "Doctrine hulls are never excluded.",
                 font=("Consolas", 9), fg=FG_TEXT, bg=BG_DARK,
                 anchor=tk.W, justify=tk.LEFT, wraplength=390).pack(
                     anchor=tk.W, padx=12, pady=(12, 4))

        seed_lbl = tk.Label(win, text="", font=("Consolas", 8, "italic"),
                            fg=FG_DIM, bg=BG_DARK, anchor=tk.W, justify=tk.LEFT,
                            wraplength=390)
        seed_lbl.pack(anchor=tk.W, padx=12, pady=(0, 2))

        list_wrap = tk.Frame(win, bg=BG_PANEL, bd=1, relief=tk.RIDGE)
        canvas = tk.Canvas(list_wrap, bg=BG_PANEL, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.configure(yscrollcommand=sb.set)
        inner = tk.Frame(canvas, bg=BG_PANEL)
        _win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(_win, width=e.width))

        def _entry_label(e):
            kind = e.get("kind")
            if kind == "capital":
                return "Capitals (all)"
            if kind == "group":
                return f"Group · {e.get('name') or e.get('id')}"
            if kind == "type":
                return f"Ship · {e.get('name') or e.get('id')}"
            return str(e)

        def _rebuild_rows():
            for w in inner.winfo_children():
                w.destroy()
            is_seed = (not state["reset"]
                       and getattr(doctrine, "exemptions", None) is None)
            seed_lbl.config(
                text="Showing the standard exemption set (not yet customized "
                     "for this doctrine)." if is_seed else "")
            if not state["entries"]:
                tk.Label(inner, text="  No exemptions — every present pilot "
                                     "counts toward the %.",
                         font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL,
                         anchor=tk.W).pack(fill=tk.X, anchor=tk.W, padx=4, pady=2)
                return
            for idx, e in enumerate(state["entries"]):
                row = tk.Frame(inner, bg=BG_PANEL)
                row.pack(fill=tk.X, anchor=tk.W, padx=4, pady=1)
                tk.Label(row, text=_entry_label(e), font=("Consolas", 9),
                         fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W).pack(
                             side=tk.LEFT, fill=tk.X, expand=True)
                ttk.Button(row, text="✕", style="Red.TButton", width=2,
                           command=lambda i=idx: _remove(i)).pack(side=tk.RIGHT)

        def _remove(idx):
            state["entries"] = _remove_exemption_entry(state["entries"], idx)
            state["reset"] = False
            _rebuild_rows()

        def _add_entry(entry):
            state["entries"] = _add_exemption_entry(state["entries"], entry)
            state["reset"] = False
            _rebuild_rows()

        def _add_capital():
            _add_entry({"kind": "capital"})

        def _add_group():
            name_to_id = {v: k for k, v in SHIP_GROUP_NAMES.items()}
            names = self.type_catalog.ship_group_names()
            picked = self._prompt_pick_from_list(
                "Add ship group", "Exempt which ship group?", names)
            if not picked:
                return
            gid = name_to_id.get(picked)
            if gid is None:
                return
            _add_entry({"kind": "group", "id": gid, "name": picked})

        def _add_type():
            picked = self._prompt_pick_from_list(
                "Add ship type", "Exempt which ship type?",
                self.type_catalog.ship_type_names())
            if not picked:
                return
            tid = self.type_catalog.resolve_id(picked)
            if tid is None:
                return
            _add_entry({"kind": "type", "id": tid, "name": picked})

        def _reset():
            state["entries"] = []
            state["reset"] = True
            seed_lbl.config(text="Will reset to the standard exemption set on Save.")
            _rebuild_rows()

        add_row = tk.Frame(win, bg=BG_DARK)
        ttk.Button(add_row, text="Add group…", style="Dark.TButton",
                   command=_add_group).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(add_row, text="Add ship…", style="Dark.TButton",
                   command=_add_type).pack(side=tk.LEFT, padx=4)
        ttk.Button(add_row, text="Add capitals", style="Dark.TButton",
                   command=_add_capital).pack(side=tk.LEFT, padx=4)
        ttk.Button(add_row, text="Reset to standard", style="Dark.TButton",
                   command=_reset).pack(side=tk.LEFT, padx=4)

        def _save():
            entries = None if state["reset"] else state["entries"]
            self._commit_doctrine_exemptions(doctrine, entries)
            win.destroy()

        def _cancel():
            win.destroy()

        btns = tk.Frame(win, bg=BG_DARK)
        ttk.Button(btns, text="Save", style="Green.TButton",
                   command=_save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=_cancel).pack(side=tk.RIGHT)
        # Pin the action rows to the bottom first so they never clip.
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=12)
        add_row.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(4, 0))
        list_wrap.pack(fill=tk.BOTH, expand=True, padx=12)

        _rebuild_rows()
        win.bind("<Escape>", lambda e: _cancel())
        win.protocol("WM_DELETE_WINDOW", _cancel)
        if not _test_no_wait:
            self.root.wait_window(win)
        return win

    def _manage_tags_dialog(self):
        """Modal manager for the library tag vocabulary: add custom tags and
        delete them (built-in DEFAULT_TAGS are protected). Deleting a tag also
        strips it from every doctrine member that carries it."""
        win = tk.Toplevel(self.root)
        win.title("Manage Tags")
        win.configure(bg=BG_DARK)
        win.geometry("380x460")
        win.minsize(360, 360)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass

        tk.Label(win,
                 text="Add, rename, or remove custom tags. Built-in tags "
                      "cannot be changed.",
                 font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                 anchor=tk.W, justify=tk.LEFT, wraplength=350).pack(
                     anchor=tk.W, padx=12, pady=(12, 4))

        list_wrap = tk.Frame(win, bg=BG_PANEL, bd=1, relief=tk.RIDGE)
        canvas = tk.Canvas(list_wrap, bg=BG_PANEL, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.configure(yscrollcommand=sb.set)
        inner = tk.Frame(canvas, bg=BG_PANEL)
        _win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(_win, width=e.width))

        def _close():
            win.destroy()

        def _rebuild():
            for w in inner.winfo_children():
                w.destroy()
            for tag in self.fittings.tags:
                row = tk.Frame(inner, bg=BG_PANEL)
                row.pack(fill=tk.X, anchor=tk.W, padx=4, pady=1)
                tk.Label(row, text=tag, font=("Consolas", 9), fg=FG_TEXT,
                         bg=BG_PANEL, anchor=tk.W).pack(
                             side=tk.LEFT, fill=tk.X, expand=True)
                if tag not in fit_models.DEFAULT_TAGS:
                    ttk.Button(row, text="Delete", style="Red.TButton",
                               command=lambda t=tag: _delete(t)).pack(
                                   side=tk.RIGHT)
                    ttk.Button(row, text="Rename", style="Dark.TButton",
                               command=lambda t=tag: _rename(t)).pack(
                                   side=tk.RIGHT, padx=(0, 4))

        def _refresh_after_tag_change():
            """Refresh every UI surface a tag add/delete/rename can affect.
            Mirrors what the delete handler refreshes so renames stay in sync."""
            _rebuild()
            self._refresh_doctrine_list()
            if self._doctrine_selected_id:
                self._show_doctrine_detail(self._doctrine_selected_id)
            self._refresh_fit_list(self._fit_search_var.get())

        def _rename(tag):
            new_name = simpledialog.askstring(
                "Rename tag",
                f"New name for tag '{tag}':",
                initialvalue=tag, parent=win)
            if new_name is None:
                return
            new_name = new_name.strip()
            if not new_name:
                messagebox.showwarning(
                    "Rename tag", "The new tag name cannot be blank.")
                return
            if new_name == tag:
                return
            if new_name in self.fittings.tags:
                messagebox.showwarning(
                    "Rename tag", f"'{new_name}' is already a tag.")
                return
            try:
                # rename_tag persists internally (calls save()); do NOT save
                # again here.
                ok = self.fittings.rename_tag(tag, new_name)
            except Exception:
                log.exception("Failed to rename tag %r to %r", tag, new_name)
                messagebox.showwarning(
                    "Rename tag",
                    f"Could not rename '{tag}'. See the log for details.")
                return
            if not ok:
                messagebox.showwarning(
                    "Rename tag",
                    f"Could not rename '{tag}' to '{new_name}'. The original "
                    "tag may no longer exist, or the new name is already in "
                    "use.")
                return
            _refresh_after_tag_change()

        def _delete(tag):
            used = sum(1 for d in self.fittings.list_doctrines()
                       for m in d.members if tag in m.tags)
            if used > 0:
                if not messagebox.askyesno(
                        "Delete tag",
                        f"Delete tag '{tag}'? It is used by {used} fit(s) "
                        "across your doctrines and will be removed from them."):
                    return
            self.fittings.remove_tag(tag)
            self.fittings.save()
            _refresh_after_tag_change()

        entry_var = tk.StringVar()

        def _add():
            name = entry_var.get().strip()
            if not name:
                return
            if name in self.fittings.tags:
                messagebox.showinfo("Add tag", f"'{name}' is already a tag.")
                return
            self.fittings.add_tag(name)
            self.fittings.save()
            entry_var.set("")
            _rebuild()

        add_row = tk.Frame(win, bg=BG_DARK)
        entry = tk.Entry(add_row, textvariable=entry_var, font=("Consolas", 10),
                         bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                         borderwidth=1, relief=tk.RIDGE)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda e: _add())
        ttk.Button(add_row, text="Add", style="Dark.TButton",
                   command=_add).pack(side=tk.RIGHT, padx=(4, 0))

        btns = tk.Frame(win, bg=BG_DARK)
        ttk.Button(btns, text="Close", style="Dark.TButton",
                   command=_close).pack(side=tk.RIGHT)

        # Pack order matters: pin the two action rows to the bottom FIRST so they
        # always reserve their space, then let list_wrap expand into what's left.
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=12)
        add_row.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(4, 0))
        list_wrap.pack(fill=tk.BOTH, expand=True, padx=12)

        _rebuild()
        win.bind("<Escape>", lambda e: _close())
        win.protocol("WM_DELETE_WINDOW", _close)
        self.root.wait_window(win)

    # ── MOTD writer sub-tab (Phase 7: Tasks 7.1 / 7.2 / 7.3) ──────────────────

    # Tags pre-checked by default on a fresh MOTD (the common doctrine roles).
    _MOTD_DEFAULT_TAGS = ("DPS", "Logistics", "Links")
    # Debounce window (ms) for live preview rebuilds while typing/toggling.
    _MOTD_PREVIEW_DEBOUNCE_MS = 250
    # Per-attribute soft limit for a single <url=fitting:...> link (spec §3.5,
    # medium confidence). We warn, never block, when a link's DNA blows past it.
    _MOTD_LINK_ATTR_WARN = 128
    # "(leave blank)" sentinel shown in the FC/Anchor dropdown.
    _MOTD_FC_BLANK = "(leave blank)"

    def _build_motd_subtab(self):
        """MOTD writer: compose a fleet MOTD from a doctrine (FC link, doctrine
        name, role-grouped clickable fit links, optional logi channel + free
        header/footer), preview it live with a length-budget meter, and either
        set it on the active character's fleet (boss-only) or copy the markup.
        Also imports an existing fleet MOTD back into the tool.

        Tasks 7.1 (inputs + channel picker), 7.2 (preview/meter/set/copy) and
        7.3 (import current MOTD + save/restore template)."""
        tab = tk.Frame(self._fitting_subnb, bg=BG_DARK)
        self._fitting_subnb.add(tab, text="  MOTD  ")

        # Per-tag include vars, keyed by tag name (rebuilt on doctrine change).
        self._motd_tag_vars: dict[str, tk.BooleanVar] = {}
        self._motd_preview_job = None        # pending root.after debounce id
        self._motd_set_btn = None
        self._motd_fleet_id = None           # resolved active-FC fleet id
        self._motd_is_boss = False
        # FALLBACK source of fit links (a list of (dna, name) tuples, or None),
        # used ONLY when the tag-based fits are empty (e.g. a tagless imported
        # doctrine). Cleared on doctrine change / tag toggle so editing reverts
        # to live tag-based fits; restored by _apply_motd_fields after the
        # doctrine-change clear. See _current_motd_markup / _capture_motd_fields.
        self._motd_loaded_fits = None

        # Linked-MOTD auto-push state. The link is deliberately session-scoped:
        # restoring it across restarts caused the app to push a freshly-opened
        # (default) MOTD over the fleet's real one at startup, so it always
        # starts OFF regardless of any persisted "motd_link" value.
        self._motd_link_enabled = _motd_link_initial_state(self.config)
        self._motd_last_push_ts = None          # time.monotonic() of last successful push
        self._motd_last_check_ts = None         # time.monotonic() of last auto-update check
        self._motd_last_pushed_markup = None    # for change-detection (no redundant writes)
        self._motd_link_state = "off"           # off|waiting|ok|not_boss|overbudget|error

        # ── Master split: inputs (left) | preview (right) ────────────────────
        body = tk.Frame(tab, bg=BG_DARK)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        body.columnconfigure(0, weight=2, uniform="motd")
        body.columnconfigure(1, weight=3, uniform="motd")
        body.rowconfigure(0, weight=1)

        # Left: scrollable inputs panel.
        left = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                        highlightbackground=BORDER_COLOR, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        in_canvas = tk.Canvas(left, bg=BG_PANEL, highlightthickness=0)
        in_canvas.grid(row=0, column=0, sticky="nsew")
        in_sb = ttk.Scrollbar(left, orient="vertical", command=in_canvas.yview)
        in_sb.grid(row=0, column=1, sticky="ns")
        in_canvas.configure(yscrollcommand=in_sb.set)
        self._register_scroll_canvas(in_canvas)

        inputs = tk.Frame(in_canvas, bg=BG_PANEL)
        _in_win = in_canvas.create_window((0, 0), window=inputs, anchor="nw")
        inputs.bind("<Configure>",
                    lambda e: in_canvas.configure(
                        scrollregion=in_canvas.bbox("all")))
        in_canvas.bind("<Configure>",
                       lambda e: in_canvas.itemconfig(_in_win, width=e.width))

        def _lbl(parent, text):
            return tk.Label(parent, text=text, font=("Consolas", 9, "bold"),
                            fg=FG_ACCENT, bg=BG_PANEL)

        # Doctrine dropdown.
        _lbl(inputs, "DOCTRINE").pack(anchor=tk.W, padx=8, pady=(8, 2))
        self._motd_doctrine_var = tk.StringVar()
        self._motd_doctrine_combo = ttk.Combobox(
            inputs, textvariable=self._motd_doctrine_var, state="readonly",
            font=("Consolas", 10))
        self._motd_doctrine_combo.pack(fill=tk.X, padx=8)
        self._motd_doctrine_combo.bind(
            "<<ComboboxSelected>>", self._motd_on_doctrine_change)

        # Linked (saved) MOTD dropdown: lists the saved MOTDs attached to the
        # currently-selected doctrine; picking one re-applies its saved fields.
        _lbl(inputs, "MOTD TEMPLATE").pack(anchor=tk.W, padx=8, pady=(10, 2))
        saved_row = tk.Frame(inputs, bg=BG_PANEL)
        saved_row.pack(fill=tk.X, padx=8)
        self._motd_saved_var = tk.StringVar()
        self._motd_saved_combo = ttk.Combobox(
            saved_row, textvariable=self._motd_saved_var, state="readonly",
            font=("Consolas", 10))
        self._motd_saved_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._motd_saved_combo.bind(
            "<<ComboboxSelected>>", self._on_saved_motd_change)
        ttk.Button(saved_row, text="Rename", style="Dark.TButton", width=7,
                   command=self._rename_linked_motd).pack(side=tk.LEFT,
                                                          padx=(5, 0))
        ttk.Button(saved_row, text="Delete", style="Red.TButton", width=7,
                   command=self._delete_linked_motd).pack(side=tk.LEFT,
                                                          padx=(5, 0))

        # FC / Anchor dropdown.
        _lbl(inputs, "FC / ANCHOR").pack(anchor=tk.W, padx=8, pady=(10, 2))
        self._motd_fc_var = tk.StringVar()
        self._motd_fc_combo = ttk.Combobox(
            inputs, textvariable=self._motd_fc_var, state="readonly",
            font=("Consolas", 10))
        self._motd_fc_combo.pack(fill=tk.X, padx=8)
        self._motd_fc_combo.bind(
            "<<ComboboxSelected>>", lambda e: self._on_motd_fc_change())

        # Staging system (optional, checkbox-gated). Defaults to the FC's
        # configured staging system (zkillboard.staging_system, a system NAME);
        # the box is pre-checked when one is configured. Produces an in-game
        # SYSTEM link in the MOTD. Mirrors the logi/cap channel row below.
        _default_staging = self.config.get("zkillboard", {}).get(
            "staging_system", "") or ""
        self._motd_staging_enabled = tk.BooleanVar(
            value=bool(_default_staging.strip()))
        self._motd_staging_var = tk.StringVar(value=_default_staging)
        st_chk = tk.Checkbutton(
            inputs, text="Include staging system",
            variable=self._motd_staging_enabled,
            font=("Consolas", 9, "bold"), fg=FG_ACCENT, bg=BG_PANEL,
            activebackground=BG_PANEL, activeforeground=FG_ACCENT,
            selectcolor=BG_ENTRY, anchor=tk.W,
            command=self._motd_on_staging_toggle)
        st_chk.pack(anchor=tk.W, padx=6, pady=(10, 2))
        self._motd_staging_entry = AutocompleteEntry(
            inputs, list(getattr(self, "_system_names", []) or []),
            labels=dict(getattr(self, "_system_labels", {}) or {}),
            textvariable=self._motd_staging_var,
            font=("Consolas", 10), bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, borderwidth=1, relief=tk.RIDGE)
        self._motd_staging_entry.pack(fill=tk.X, padx=8)
        # The StringVar trace covers both typed and programmatic changes; no
        # separate <KeyRelease> binding needed (and AutocompleteEntry owns it).
        self._motd_staging_var.trace_add(
            "write", lambda *a: self._schedule_motd_preview())

        # Logi / cap channel: AutocompleteEntry + Scan.
        _lbl(inputs, "LOGI / CAP CHANNEL").pack(anchor=tk.W, padx=8, pady=(10, 2))
        ch_row = tk.Frame(inputs, bg=BG_PANEL)
        ch_row.pack(fill=tk.X, padx=8)
        # Seed completions from the shared discovered-channel cache (the full
        # set, not the noise-filtered intel suggestions) so previously-scanned
        # channels are available immediately, regardless of tab build order.
        _cached_channels = (self.config.get("intel_channels", {})
                            .get("cached_discovered", []) or [])
        self._motd_channel_entry = AutocompleteEntry(
            ch_row, list(_cached_channels),
            font=("Consolas", 10), bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, width=22,
            borderwidth=1, relief=tk.RIDGE,
            # Selecting a channel from the dropdown must also refresh the preview
            # (a dropdown pick doesn't fire <KeyRelease>).
            on_select=self._schedule_motd_preview)
        self._motd_channel_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # add="+" so we don't clobber AutocompleteEntry's own <KeyRelease>.
        self._motd_channel_entry.bind(
            "<KeyRelease>", lambda e: self._schedule_motd_preview(), add="+")
        ttk.Button(ch_row, text="Scan", style="Dark.TButton", width=6,
                   command=self._motd_scan_channels).pack(side=tk.LEFT, padx=(5, 0))
        self._motd_channel_status = tk.Label(
            inputs, text="", font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL)
        self._motd_channel_status.pack(anchor=tk.W, padx=8)

        # Per-tag include checkboxes (rebuilt per doctrine).
        _lbl(inputs, "INCLUDE FITS").pack(anchor=tk.W, padx=8, pady=(10, 2))
        self._motd_tag_frame = tk.Frame(inputs, bg=BG_PANEL)
        self._motd_tag_frame.pack(fill=tk.X, padx=8)

        # Intro / outro free text — WYSIWYG markup editors (replacing the old
        # plain header/footer Entrys). Each is a compact toolbar + Text that
        # serialises to EVE markup via get_markup() / restores via set_markup().
        # They map to the same saved-MOTD "header"/"footer" keys (now markup).
        _lbl(inputs, "INTRO (header)").pack(anchor=tk.W, padx=8, pady=(10, 2))
        self._motd_intro = MarkupEditor(
            inputs, height=3, on_change=self._schedule_motd_preview,
            bg_panel=BG_PANEL, bg_entry=BG_ENTRY, fg_text=FG_TEXT,
            fg_white=FG_WHITE, fg_accent=FG_ACCENT, border=BORDER_COLOR)
        self._motd_intro.pack(fill=tk.X, padx=8)

        _lbl(inputs, "OUTRO (footer)").pack(anchor=tk.W, padx=8, pady=(10, 2))
        self._motd_outro = MarkupEditor(
            inputs, height=3, on_change=self._schedule_motd_preview,
            bg_panel=BG_PANEL, bg_entry=BG_ENTRY, fg_text=FG_TEXT,
            fg_white=FG_WHITE, fg_accent=FG_ACCENT, border=BORDER_COLOR)
        self._motd_outro.pack(fill=tk.X, padx=8, pady=(0, 4))

        # Save (link to doctrine) + import.
        tmpl_row = tk.Frame(inputs, bg=BG_PANEL)
        tmpl_row.pack(fill=tk.X, padx=8, pady=(10, 8))
        ttk.Button(tmpl_row, text="Link to doctrine", style="Green.TButton",
                   command=self._link_motd_to_doctrine).pack(side=tk.LEFT, padx=2)
        ttk.Button(tmpl_row, text="Import current MOTD", style="Dark.TButton",
                   command=self._import_current_motd).pack(side=tk.LEFT, padx=2)

        # Right: preview + meter + actions.
        right = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                         highlightbackground=BORDER_COLOR, highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # Dual preview: Raw markup (top) + Rendered (bottom), both read-only.
        panes = tk.Frame(right, bg=BG_PANEL)
        panes.grid(row=1, column=0, sticky="nsew", padx=8, pady=(8, 0))
        panes.columnconfigure(0, weight=1)
        panes.rowconfigure(1, weight=1)   # Raw pane grows
        panes.rowconfigure(3, weight=1)   # Rendered pane grows

        tk.Label(panes, text="RAW MARKUP", font=("Consolas", 9, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).grid(
                     row=0, column=0, sticky="w", pady=(0, 2))
        self._motd_preview = scrolledtext.ScrolledText(
            panes, font=("Consolas", 9), bg=BG_ENTRY, fg=FG_TEXT,
            insertbackground=FG_WHITE, wrap=tk.WORD, height=6,
            borderwidth=1, relief=tk.RIDGE, state=tk.DISABLED)
        self._motd_preview.grid(row=1, column=0, sticky="nsew")
        self._theme_scrolledtext_bar(self._motd_preview)

        tk.Label(panes, text="RENDERED", font=("Consolas", 9, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).grid(
                     row=2, column=0, sticky="w", pady=(6, 2))
        self._motd_rendered = scrolledtext.ScrolledText(
            panes, font=("Consolas", 10), bg=BG_ENTRY, fg=FG_TEXT,
            insertbackground=FG_WHITE, wrap=tk.WORD, height=6,
            borderwidth=1, relief=tk.RIDGE, state=tk.DISABLED)
        self._motd_rendered.grid(row=3, column=0, sticky="nsew")
        self._theme_scrolledtext_bar(self._motd_rendered)
        # Cache of configured render tags, keyed by
        # (hex|None, bold, italic, underline, size|None, is_link).
        self._motd_render_tags = {}
        # Monotonic id for per-link hover tags (reset each render — see
        # _render_motd_markup, which deletes the prior link tags first).
        self._motd_link_seq = 0

        # Length meter: a colored bar + numeric label.
        meter_wrap = tk.Frame(right, bg=BG_PANEL)
        meter_wrap.grid(row=2, column=0, sticky="ew", padx=8, pady=(6, 0))
        meter_wrap.columnconfigure(0, weight=1)
        self._motd_meter_canvas = tk.Canvas(
            meter_wrap, height=14, bg=BG_ENTRY, highlightthickness=1,
            highlightbackground=BORDER_COLOR)
        self._motd_meter_canvas.grid(row=0, column=0, sticky="ew")
        self._motd_meter_label = tk.Label(
            meter_wrap, text="0 / 3000", font=("Consolas", 9),
            fg=FG_DIM, bg=BG_PANEL, width=14, anchor=tk.E)
        self._motd_meter_label.grid(row=0, column=1, padx=(6, 0))

        # Non-blocking warnings (over-budget / long link attribute).
        self._motd_warn_label = tk.Label(
            right, text="", font=("Consolas", 8), fg=FG_YELLOW, bg=BG_PANEL,
            anchor=tk.W, justify=tk.LEFT, wraplength=380)
        self._motd_warn_label.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 0))

        # Actions: Set as fleet MOTD (boss-gated) + Copy.
        act_row = tk.Frame(right, bg=BG_PANEL)
        act_row.grid(row=4, column=0, sticky="ew", padx=8, pady=8)
        self._motd_set_btn = ttk.Button(
            act_row, text="Set as fleet MOTD", style="Green.TButton",
            command=self._set_fleet_motd, state=tk.DISABLED)
        self._motd_set_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(act_row, text="Copy markup", style="Dark.TButton",
                   command=self._copy_motd).pack(side=tk.LEFT, padx=2)
        ttk.Button(act_row, text="Clear MOTD", style="Red.TButton",
                   command=self._clear_motd).pack(side=tk.LEFT, padx=2)
        ttk.Button(act_row, text="Refresh fleet", style="Dark.TButton",
                   command=self._motd_refresh_fleet_status).pack(
                       side=tk.LEFT, padx=2)
        # Auto-update controls on their own row so the interval ("every N s")
        # and the link indicator are never clipped when the window is narrow.
        link_row = tk.Frame(right, bg=BG_PANEL)
        link_row.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 4))
        self._motd_link_var = tk.BooleanVar(value=self._motd_link_enabled)
        tk.Checkbutton(
            link_row, text="Auto-update MOTD", variable=self._motd_link_var,
            font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL, selectcolor=BG_ENTRY,
            activebackground=BG_PANEL, activeforeground=FG_YELLOW,
            command=self._motd_toggle_link).pack(side=tk.LEFT, padx=(0, 2))
        tk.Label(link_row, text="every", font=("Consolas", 8), fg=FG_DIM,
                 bg=BG_PANEL).pack(side=tk.LEFT, padx=(6, 1))
        self._motd_link_interval_var = tk.IntVar(value=self._motd_link_interval_s())
        _ivl = tk.Spinbox(
            link_row, from_=10, to=300, increment=5, width=4,
            textvariable=self._motd_link_interval_var, font=("Consolas", 8),
            bg=BG_ENTRY, fg=FG_TEXT, command=self._motd_link_interval_changed)
        _ivl.pack(side=tk.LEFT)
        _ivl.bind("<FocusOut>", lambda e: self._motd_link_interval_changed())
        _ivl.bind("<Return>", lambda e: self._motd_link_interval_changed())
        tk.Label(link_row, text="s", font=("Consolas", 8), fg=FG_DIM,
                 bg=BG_PANEL).pack(side=tk.LEFT, padx=(1, 6))
        self._motd_link_indicator = tk.Label(
            link_row, text="○ not linked", font=("Consolas", 8), fg=FG_DIM,
            bg=BG_PANEL, cursor="question_arrow")
        self._motd_link_indicator.pack(side=tk.LEFT, padx=2)
        _link_tip = ("When ON, re-checks the MOTD every N seconds (adjustable) "
                     "and re-pushes it to your fleet whenever the composition/"
                     "deltas change. The counter is seconds since the last "
                     "check. Requires you to be the current fleet boss; only "
                     "re-pushes when the text actually changes. Switches on "
                     "automatically after you 'Set as fleet MOTD' once this "
                     "session (startup stays off); toggle it off any time.")
        self._motd_link_indicator.bind(
            "<Enter>", lambda e, t=_link_tip: self._show_tooltip(e, t))
        self._motd_link_indicator.bind("<Leave>", lambda e: self._hide_tooltip())
        self._motd_fleet_status = tk.Label(
            right, text="", font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL,
            anchor=tk.W, justify=tk.LEFT, wraplength=380)
        self._motd_fleet_status.grid(row=6, column=0, sticky="ew", padx=8,
                                     pady=(0, 8))

        # Populate dropdowns, then first preview. The tab opens clean (no sticky
        # single template); linked MOTDs are loaded on demand from the dropdown.
        self._motd_refresh_doctrines()
        self._motd_refresh_fc_choices()
        self._motd_rebuild_tag_checkboxes()
        self._motd_refresh_saved_dropdown()
        self._rebuild_motd_preview()
        self._motd_link_tick()
        self._motd_autopush_loop()

    # ── MOTD: input population (Task 7.1) ─────────────────────────────────────

    def _motd_refresh_doctrines(self):
        """Fill the doctrine dropdown from the library, preserving selection."""
        combo = getattr(self, "_motd_doctrine_combo", None)
        if combo is None:
            return
        names = sorted((d.name or "") for d in self.fittings.list_doctrines())
        combo["values"] = names
        cur = self._motd_doctrine_var.get()
        if cur not in names:
            self._motd_doctrine_var.set(names[0] if names else "")

    def _motd_refresh_fc_choices(self):
        """Fill the FC/Anchor dropdown with authed characters + '(leave blank)'.

        Default is the primary character (``self.esi_auth``); the blank option
        omits the FC line entirely from the MOTD."""
        combo = getattr(self, "_motd_fc_combo", None)
        if combo is None:
            return
        names = [a.character_name or "Unknown" for a in self.esi_accounts]
        values = names + [self._MOTD_FC_BLANK]
        combo["values"] = values
        cur = self._motd_fc_var.get()
        if cur not in values:
            default = None
            if self.esi_auth is not None:
                default = self.esi_auth.character_name
            self._motd_fc_var.set(default or (names[0] if names else
                                              self._MOTD_FC_BLANK))

    def _active_fleet_doctrine(self):
        """The doctrine driving Fleet-Mgmt guidance: the explicit Fleet-tab pick
        if set, else the MOTD-selected doctrine, else None."""
        name = ""
        var = getattr(self, "_fleet_doctrine_var", None)
        if var is not None:
            name = var.get() or ""
        if not name:
            mvar = getattr(self, "_motd_doctrine_var", None)
            if mvar is not None:
                name = mvar.get() or ""
        if not name:
            return None
        for d in self.fittings.list_doctrines():
            if (d.name or "") == name:
                return d
        return None

    # ── Ideal-% exemptions (Fleet-guidance denominator) ─────────────────────
    def _resolve_exemptions_for_counts(self, doc, ship_counts):
        """Resolve (exempt_type_ids, doctrine_hull_ids) for a doctrine + the
        PRESENT fleet ship_counts.

        ``exempt_type_ids`` is the subset of the ``ship_counts`` keys that match
        the doctrine's effective exemptions (STANDARD_EXEMPTIONS when never
        customized). Only ids actually present are classified — the group/capital
        resolvers (ship_classes.get_group_id / is_capital, cached) are only hit
        for present hulls, keeping the per-poll cost cheap. ``doctrine_hull_ids``
        is the set of hull type ids the doctrine uses; the engine treats a hull in
        that set as an exact-hull override (never excluded even if it matches an
        exemption). Both are passed straight into compute_fleet_guidance."""
        doctrine_hull_ids = set(
            fleet_composer.build_tag_index(doc, self.fittings).keys())
        exemptions = fleet_guidance.effective_exemptions(doc)
        exempt_type_ids: set[int] = set()
        if exemptions:
            for tid in ship_counts:
                if fleet_guidance.is_exempt_type(
                        tid, exemptions,
                        ship_classes.get_group_id, ship_classes.is_capital):
                    exempt_type_ids.add(tid)
        return exempt_type_ids, doctrine_hull_ids

    def _exclusion_note_text(self, rep):
        """One-line dim note for the guidance panel when pilots were removed from
        the ideal-% denominator, or None when nothing was excluded. ``rep`` is a
        GuidanceReport (or None)."""
        n = getattr(rep, "excluded_from_pct", 0) if rep is not None else 0
        if not n:
            return None
        return f"{n} excluded from % (caps/recon)"

    def _commit_doctrine_exemptions(self, doc, entries):
        """Persist a doctrine's ideal-% exemptions and refresh the affected views.

        ``entries`` is the explicit exemption list, or None for
        "reset to standard" (fall back to STANDARD_EXEMPTIONS). Routes through the
        store mutator + save, then re-renders the doctrine detail pane and the
        MOTD preview so the adjusted denominator/annotations reflect the change."""
        self.fittings.set_doctrine_exemptions(doc.id, entries)
        self.fittings.save()
        self._show_doctrine_detail(doc.id)
        self._rebuild_motd_preview()

    def _refresh_fleet_doctrine_combo(self):
        """Keep the Fleet-tab doctrine dropdown's values in sync with the library.
        Preserves the current selection if it still exists."""
        combo = getattr(self, "_fleet_doctrine_combo", None)
        if combo is None:
            return
        names = sorted((d.name or "") for d in self.fittings.list_doctrines())
        try:
            combo["values"] = [""] + names
        except Exception:
            pass

    def _on_fleet_doctrine_change(self):
        """Persist the Fleet-tab doctrine pick and re-render the role sections."""
        var = getattr(self, "_fleet_doctrine_var", None)
        if var is None:
            return
        self.config.setdefault("fleet", {})["active_doctrine"] = var.get()
        self._save_config()
        self._refresh_specialized_roles_from_cache()

    def _auto_select_fleet_doctrine(self, doctrine_name):
        """After a successful MOTD push, point the Fleet-Management tab's doctrine
        selector at the MOTD's doctrine so live guidance/fleet-feedback tracks what
        was just broadcast. No-op if the name is blank, unknown, or already active."""
        if not doctrine_name:
            return
        fvar = getattr(self, "_fleet_doctrine_var", None)
        if fvar is None:
            return
        if not any((d.name or "") == doctrine_name
                   for d in self.fittings.list_doctrines()):
            return
        if (fvar.get() or "") == doctrine_name:
            return
        fvar.set(doctrine_name)
        # Reuse the existing handler: persists active_doctrine to config + saves +
        # refreshes the Specialized Roles / guidance sections.
        self._on_fleet_doctrine_change()

    def _open_active_doctrine(self):
        """Navigate to the active doctrine in Fittings ▸ Doctrines (best-effort)."""
        doc = self._active_fleet_doctrine()
        if doc is None:
            return
        try:
            # Switch to the Fittings top-level tab and its Doctrines sub-tab,
            # then select the doctrine in the tree (which fires the tree-select
            # binding and renders the detail). Only render directly on a tree miss.
            self.notebook.select(FITTINGS_TAB_INDEX)
            subnb = getattr(self, "_fitting_subnb", None)
            if subnb is not None:
                subnb.select(DOCTRINES_SUBTAB_INDEX)
            tree = getattr(self, "_doctrine_tree", None)
            rendered = False
            if tree is not None:
                try:
                    if doc.id in tree.get_children(""):
                        tree.selection_set(doc.id)
                        tree.see(doc.id)
                        rendered = True
                except Exception:
                    pass
            if not rendered:
                self._doctrine_selected_id = doc.id
                if hasattr(self, "_show_doctrine_detail"):
                    self._show_doctrine_detail(doc.id)
        except Exception as exc:
            print(f"[Fleet] open doctrine navigation failed: {exc}")

    def _active_doctrine_obj(self):
        """Resolve config['fleet']['active_doctrine'] (name or id) to a Doctrine."""
        key = self.config.get("fleet", {}).get("active_doctrine", "")
        if not key:
            return None
        for d in self.fittings.list_doctrines():
            if d.id == key or d.name == key:
                return d
        return None

    _FLEET_INFO_TTL_S = 4.0   # get_fleet_info() cache lifetime (see _cached_fleet_info)

    def _cached_fleet_info(self, auth):
        """auth.get_fleet_info() behind a short monotonic TTL, keyed by the
        character so switching the selected FC never returns a stale hit. Lets
        _fleet_boss_session and _fleet_boss_info share one ESI round-trip when
        the Fleet Templates window calls both on entering live mode."""
        cid = getattr(auth, "character_id", None)
        now = time.monotonic()
        cached = self._fleet_info_cache
        if cached is not None:
            ts, ckey, info = cached
            if ckey == cid and (now - ts) < self._FLEET_INFO_TTL_S:
                return info
        info = auth.get_fleet_info()
        self._fleet_info_cache = (now, cid, info)
        return info

    def _fleet_boss_session(self):
        """Current fleet-boss AuthEsiSession, or None if no authed boss."""
        import fleet_esi
        auth = self._motd_selected_fc_auth() or self.esi_auth
        if auth is None or not auth.is_authenticated:
            return None
        if not auth.has_scope("esi-fleets.write_fleet.v1"):
            return None
        info = self._cached_fleet_info(auth)
        if not info or not auth.is_boss(info, auth.character_id):
            return None
        return fleet_esi.AuthEsiSession(auth)

    def _fleet_boss_info(self):
        auth = self._motd_selected_fc_auth() or self.esi_auth
        if auth is None or not auth.is_authenticated:
            return None
        info = self._cached_fleet_info(auth)
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
        names += self.fleet_templates.cached_character_names()
        return sorted(set(names))

    def _resolve_names(self, names):
        """Resolve a list of typed pilot names to {name_lower: character_id}.

        Backed by esi_auth.resolve_names_to_ids (public POST /universe/ids/,
        batched <=500/request with a split-on-failure fallback — see
        esi_auth._resolve_chunk_recursive). Safe on a worker thread. Keys are
        lower-cased so callers can match case-insensitively against typed input.
        Returns {} when there is no authenticated character (graceful no-auth) or
        on any error, so names simply stay unvalidated."""
        auth = getattr(self, "esi_auth", None)
        if auth is None or not getattr(auth, "is_authenticated", False):
            return {}
        cleaned = [n for n in (names or []) if isinstance(n, str) and n.strip()]
        if not cleaned:
            return {}
        try:
            raw = auth.resolve_names_to_ids(cleaned)   # {proper_name: id}, <=500/batch
        except Exception:
            return {}
        out = {}
        for name, cid in (raw or {}).items():
            if isinstance(name, str) and isinstance(cid, int):
                out[name.strip().lower()] = cid
        return out

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
            resolve_names_provider=self._resolve_names,
        )
        # Clear the handle when the window closes so re-open works.
        win = self._fleet_template_window
        orig_destroy = win.destroy

        def _wrapped_destroy():
            orig_destroy()
            self._fleet_template_window = None

        win.destroy = _wrapped_destroy
        win.win.protocol("WM_DELETE_WINDOW", _wrapped_destroy)

    def _refresh_specialized_roles_from_cache(self):
        """Re-run the specialized-role render using the last polled fleet snapshot,
        so a doctrine change re-colours the guided sections without waiting for the
        next fleet poll. No-op when there is no cached snapshot yet."""
        cached = getattr(self, "_last_specialized_args", None)
        if not cached:
            return
        members, ship_counts, total = cached
        try:
            self._update_specialized_roles(members, ship_counts, total)
        except Exception as exc:
            print(f"[Fleet] re-render specialized roles failed: {exc}")

    def _motd_selected_doctrine(self):
        """Return the Doctrine matching the dropdown name, or None."""
        name = self._motd_doctrine_var.get()
        if not name:
            return None
        for d in self.fittings.list_doctrines():
            if (d.name or "") == name:
                return d
        return None

    def _motd_selected_fc_auth(self):
        """Return the ESIAuth for the FC dropdown selection, or None if blank
        / no match (used both for the FC link identity and fleet resolution)."""
        name = self._motd_fc_var.get()
        if not name or name == self._MOTD_FC_BLANK:
            return None
        for a in self.esi_accounts:
            if (a.character_name or "Unknown") == name:
                return a
        return None

    def _motd_on_doctrine_change(self, event=None):
        """Doctrine changed: rebuild the per-tag checkboxes from its tags,
        refresh the linked-MOTD dropdown for the new doctrine, and refresh the
        preview.

        Changing the doctrine invalidates any explicit loaded-fits fallback
        (those belonged to the previously-loaded MOTD), so it is cleared here.
        ``_apply_motd_fields`` calls this first and re-sets the loaded fits
        AFTER, so loading a saved MOTD survives this clear."""
        self._motd_loaded_fits = None
        self._motd_rebuild_tag_checkboxes()
        self._motd_refresh_saved_dropdown()
        self._rebuild_motd_preview()

    def _on_motd_fc_change(self):
        """FC changed: re-resolve fleet/boss status (changes which fleet the
        Set button targets) and refresh the preview's FC line."""
        self._motd_refresh_fleet_status()
        self._schedule_motd_preview()

    def _motd_on_staging_toggle(self):
        """Checkbox toggle for 'Include staging system'.

        When enabled and the staging entry is empty, auto-populate it with the
        user's designated staging system (config zkillboard.staging_system) so the
        preview reflects it without the user typing/selecting anything. Setting the
        StringVar fires its write-trace, which schedules the preview; the explicit
        schedule below covers the disable case (and a no-fill enable)."""
        if (getattr(self, "_motd_staging_enabled", None) is not None
                and self._motd_staging_enabled.get()):
            cur = (self._motd_staging_var.get() or "").strip()
            if not cur:
                designated = (self.config.get("zkillboard", {})
                              .get("staging_system", "") or "").strip()
                if designated:
                    self._motd_staging_var.set(designated)
        self._schedule_motd_preview()

    def _motd_rebuild_tag_checkboxes(self):
        """Rebuild the include-tag checkboxes from the selected doctrine's tags.

        Tags present on the doctrine's members (in canonical order, then any
        extras) each get a checkbox; DPS/Logistics/Links default checked. Prior
        check states are preserved across rebuilds for tags that persist."""
        frame = getattr(self, "_motd_tag_frame", None)
        if frame is None:
            return
        prev = {t: v.get() for t, v in self._motd_tag_vars.items()}
        for child in frame.winfo_children():
            child.destroy()
        self._motd_tag_vars = {}

        doctrine = self._motd_selected_doctrine()
        tags: list[str] = []
        if doctrine is not None:
            present = set()
            for mem in doctrine.members:
                present.update(mem.tags or [])
            for t in self._DOCTRINE_TAG_ORDER:
                if t in present:
                    tags.append(t)
            for t in sorted(present):
                if t not in tags:
                    tags.append(t)

        if not tags:
            tk.Label(frame, text="(no tagged fits in this doctrine)",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL).pack(
                         anchor=tk.W)
            return

        # Preserve the live check state (prev) across rebuilds, falling back to
        # the default-on roles for newly-appearing tags. Loading a saved/linked
        # MOTD applies its tag set afterwards via _apply_motd_fields.
        for t in tags:
            if t in prev:
                default = prev[t]
            else:
                default = t in self._MOTD_DEFAULT_TAGS
            var = tk.BooleanVar(value=default)
            self._motd_tag_vars[t] = var
            cb = tk.Checkbutton(
                frame, text=t, variable=var, font=("Consolas", 10),
                fg=FG_TEXT, bg=BG_PANEL, selectcolor=BG_ENTRY,
                activebackground=BG_PANEL, activeforeground=FG_WHITE,
                anchor=tk.W, command=self._on_motd_tag_toggle)
            cb.pack(anchor=tk.W)

    def _on_motd_tag_toggle(self):
        """A USER toggle of an include-tag checkbox: drop any explicit loaded-fits
        fallback (the user is now driving the fit set via live tags) and refresh
        the preview. Only fires on user clicks — the programmatic ``var.set()``
        in :meth:`_apply_motd_fields` does NOT invoke a Checkbutton command, so a
        loaded MOTD's explicit fits survive an apply."""
        self._motd_loaded_fits = None
        self._schedule_motd_preview()

    # ── MOTD: saved (doctrine-linked) MOTDs ───────────────────────────────────

    _MOTD_SAVED_BLANK = "—"

    def _saved_motds(self):
        """Return the list of saved-MOTD dicts from config (never None)."""
        fit_cfg = self.config.get("fittings", {})
        saved = fit_cfg.get("saved_motds")
        return saved if isinstance(saved, list) else []

    def _capture_motd_fields(self) -> dict:
        """Read the current MOTD inputs into a dict (without name/doctrine,
        which the caller sets). Factored so the save path can be exercised in
        tests without touching the dialogs."""
        tags = [t for t, v in self._motd_tag_vars.items() if v.get()]
        staging_enabled = bool(self._motd_staging_enabled.get()) \
            if getattr(self, "_motd_staging_enabled", None) is not None else False
        staging = (self._motd_staging_var.get() or "") \
            if getattr(self, "_motd_staging_var", None) is not None else ""
        channel = self._motd_channel_entry.get() \
            if getattr(self, "_motd_channel_entry", None) is not None else ""
        # Snapshot the CURRENT fits so a saved MOTD remembers them as a fallback
        # (e.g. a tagless imported doctrine, whose tags yield nothing). Prefer
        # the live tag-based fits; fall back to any explicit loaded fits. Stored
        # as [dna, name] pairs (JSON-friendly lists, not tuples).
        fits_by_tag = self._motd_build_fits_by_tag(self._motd_selected_doctrine())
        fits_pairs: list[list] = []
        for group in fits_by_tag.values():
            for dna, name in group:
                fits_pairs.append([dna, name])
        if not fits_pairs and self._motd_loaded_fits:
            fits_pairs = [[dna, name] for dna, name in self._motd_loaded_fits]
        return {
            "fc": self._motd_fc_var.get(),
            "staging_enabled": staging_enabled,
            "staging": staging,
            "channel": channel,
            "header": self._motd_intro.get_markup(),
            "footer": self._motd_outro.get_markup(),
            "tags": tags,
            "fits": fits_pairs,
        }

    def _apply_motd_fields(self, data: dict):
        """Apply a saved-MOTD dict back onto the builder inputs.

        Sets the doctrine (rebuilding its tag checkboxes), then the scalar
        fields, then checks exactly the tags named in ``data['tags']`` (the rest
        unchecked). Defensive when the saved doctrine no longer exists — the
        other fields still apply. Schedules a preview refresh at the end."""
        if not isinstance(data, dict):
            return

        # Doctrine first so the tag checkboxes reflect the right doctrine.
        doctrine = data.get("doctrine") or ""
        combo = getattr(self, "_motd_doctrine_combo", None)
        if combo is not None and doctrine in (combo["values"] or ()):
            self._motd_doctrine_var.set(doctrine)
        # Rebuild tag checkboxes for the (possibly changed) doctrine. Use the
        # full doctrine-change handler so the linked-MOTD dropdown stays in sync.
        self._motd_on_doctrine_change()

        # Scalar fields.
        self._motd_fc_var.set(data.get("fc", "") or "")
        if getattr(self, "_motd_staging_enabled", None) is not None:
            self._motd_staging_enabled.set(bool(data.get("staging_enabled")))
        if getattr(self, "_motd_staging_var", None) is not None:
            self._motd_staging_var.set(data.get("staging", "") or "")
        entry = getattr(self, "_motd_channel_entry", None)
        if entry is not None:
            try:
                entry.delete(0, tk.END)
                entry.insert(0, data.get("channel", "") or "")
            except Exception:
                pass
        self._motd_intro.set_markup(data.get("header", "") or "")
        self._motd_outro.set_markup(data.get("footer", "") or "")

        # Tags: check exactly the saved set, uncheck the rest. Programmatic
        # var.set() does NOT fire the Checkbutton command, so this does not
        # clear the loaded fits set just below.
        want = set(data.get("tags") or [])
        for t, v in self._motd_tag_vars.items():
            v.set(t in want)

        # Explicit loaded fits last: _motd_on_doctrine_change (called above)
        # cleared self._motd_loaded_fits, so restore the saved MOTD's fits HERE
        # as the fallback used when the checked tags yield nothing.
        self._motd_loaded_fits = [
            tuple(x) for x in (data.get("fits") or [])
        ] or None

        self._schedule_motd_preview()

    def _motd_refresh_saved_dropdown(self):
        """Populate the LINKED MOTD combo with a leading blank plus the names of
        saved MOTDs whose doctrine matches the currently-selected doctrine."""
        combo = getattr(self, "_motd_saved_combo", None)
        if combo is None:
            return
        doctrine = self._motd_doctrine_var.get()
        names = [m.get("name", "") for m in self._saved_motds()
                 if (m.get("doctrine") or "") == doctrine and m.get("name")]
        values = [self._MOTD_SAVED_BLANK] + sorted(names, key=str.lower)
        combo["values"] = values
        if self._motd_saved_var.get() not in values:
            self._motd_saved_var.set(self._MOTD_SAVED_BLANK)

    def _on_saved_motd_change(self, event=None):
        """A linked MOTD was picked: load its saved fields onto the builder."""
        name = self._motd_saved_var.get()
        if not name or name == self._MOTD_SAVED_BLANK:
            return
        doctrine = self._motd_doctrine_var.get()
        for m in self._saved_motds():
            if (m.get("doctrine") or "") == doctrine and m.get("name") == name:
                self._apply_motd_fields(m)
                return

    def _delete_linked_motd(self):
        """Delete the currently-selected linked (saved) MOTD after confirmation.

        Removes the entry matching the selected (doctrine, name) from
        config["fittings"]["saved_motds"], persists, refreshes the dropdown, and
        resets the selection to blank. No-op when nothing is selected."""
        name = self._motd_saved_var.get()
        if not name or name == self._MOTD_SAVED_BLANK:
            messagebox.showinfo(
                "Delete MOTD template",
                "Select an MOTD template from the dropdown first.")
            return
        doctrine = self._motd_doctrine_var.get()
        if not messagebox.askyesno(
                "Delete MOTD template",
                f"Delete MOTD template '{name}'"
                + (f" from doctrine '{doctrine}'?" if doctrine else "?")):
            return
        fit_cfg = self.config.setdefault("fittings", {})
        saved = fit_cfg.get("saved_motds")
        if isinstance(saved, list):
            fit_cfg["saved_motds"] = [
                m for m in saved
                if not ((m.get("doctrine") or "") == doctrine
                        and m.get("name") == name)]
            self._save_config()
        self._motd_saved_var.set(self._MOTD_SAVED_BLANK)
        self._motd_refresh_saved_dropdown()
        status = getattr(self, "_motd_fleet_status", None)
        if status is not None:
            status.config(text=f"Deleted MOTD template '{name}'.", fg=FG_GREEN)

    def _rename_linked_motd(self):
        """Rename the currently-selected linked (saved) MOTD template in place.

        Identity is keyed on (doctrine, name). Prompts for a new name,
        validates it (non-blank, not colliding with another template under the
        SAME doctrine), updates the stored template's name key in place,
        persists via the same mechanism the save/delete handlers use, and
        refreshes the dropdown/selector. No-op when nothing is selected."""
        name = self._motd_saved_var.get()
        if not name or name == self._MOTD_SAVED_BLANK:
            messagebox.showinfo(
                "Rename MOTD template",
                "Select an MOTD template from the dropdown first.")
            return
        doctrine = self._motd_doctrine_var.get()
        new_name = simpledialog.askstring(
            "Rename MOTD template",
            "New name for this MOTD template:",
            initialvalue=name, parent=self.root)
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name:
            messagebox.showwarning(
                "Rename MOTD template",
                "The new template name cannot be blank.")
            return
        if new_name == name:
            return
        # Collision check within the same doctrine only.
        if any((m.get("doctrine") or "") == doctrine
               and m.get("name") == new_name
               for m in self._saved_motds()):
            messagebox.showwarning(
                "Rename MOTD template",
                f"A MOTD template named '{new_name}' already exists under "
                + (f"doctrine '{doctrine}'." if doctrine
                   else "this (blank) doctrine."))
            return
        try:
            fit_cfg = self.config.setdefault("fittings", {})
            saved = fit_cfg.get("saved_motds")
            target = None
            if isinstance(saved, list):
                for m in saved:
                    if (m.get("doctrine") or "") == doctrine \
                            and m.get("name") == name:
                        target = m
                        break
            if target is None:
                messagebox.showwarning(
                    "Rename MOTD template",
                    f"Could not find template '{name}' to rename. It may have "
                    "been removed.")
                return
            target["name"] = new_name
            self._save_config()
        except Exception:
            log.exception(
                "Failed to rename MOTD template %r to %r (doctrine %r)",
                name, new_name, doctrine)
            messagebox.showwarning(
                "Rename MOTD template",
                f"Could not rename '{name}'. See the log for details.")
            return
        self._motd_saved_var.set(new_name)
        self._motd_refresh_saved_dropdown()
        status = getattr(self, "_motd_fleet_status", None)
        if status is not None:
            status.config(
                text=f"Renamed MOTD template '{name}' to '{new_name}'.",
                fg=FG_GREEN)

    def _save_linked_motd(self, doctrine_name: str, motd_name: str):
        """Capture the current builder fields and persist them as a saved MOTD
        linked to ``doctrine_name`` under ``motd_name``.

        Replaces any existing saved MOTD with the same (doctrine, name); else
        appends. Persists, then refreshes the linked-MOTD dropdown. Testable —
        no dialogs."""
        data = self._capture_motd_fields()
        data["name"] = motd_name
        data["doctrine"] = doctrine_name

        fit_cfg = self.config.setdefault("fittings", {})
        saved = fit_cfg.get("saved_motds")
        if not isinstance(saved, list):
            saved = []
            fit_cfg["saved_motds"] = saved
        for i, m in enumerate(saved):
            if (m.get("doctrine") or "") == doctrine_name \
                    and m.get("name") == motd_name:
                saved[i] = data
                break
        else:
            saved.append(data)
        self._save_config()

        # Reflect the new save in the dropdown (and select it when the current
        # doctrine matches, so it reads back as the active linked MOTD).
        self._motd_refresh_saved_dropdown()
        if self._motd_doctrine_var.get() == doctrine_name:
            combo = getattr(self, "_motd_saved_combo", None)
            if combo is not None and motd_name in (combo["values"] or ()):
                self._motd_saved_var.set(motd_name)
        status = getattr(self, "_motd_fleet_status", None)
        if status is not None:
            status.config(
                text=f"MOTD template '{motd_name}' saved to doctrine "
                     f"'{doctrine_name}'.", fg=FG_GREEN)

    def _link_motd_to_doctrine(self):
        """'Link to doctrine' button: pick an existing doctrine + name, then
        save the current builder fields as a doctrine-linked MOTD."""
        doctrines = sorted((d.name or "") for d in self.fittings.list_doctrines()
                           if (d.name or ""))
        if not doctrines:
            messagebox.showinfo(
                "Link to doctrine",
                "No doctrines exist yet. Create a doctrine first (Doctrines "
                "tab) or import a MOTD to create one.")
            return

        # Small modal: pick a doctrine, then name the linked MOTD.
        dlg = tk.Toplevel(self.root)
        dlg.title("Link MOTD to doctrine")
        dlg.configure(bg=BG_PANEL)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        tk.Label(dlg, text="Link this MOTD to which doctrine?",
                 font=("Consolas", 10, "bold"), fg=FG_ACCENT, bg=BG_PANEL).pack(
                     anchor=tk.W, padx=12, pady=(12, 4))
        pick_var = tk.StringVar(
            value=self._motd_doctrine_var.get()
            if self._motd_doctrine_var.get() in doctrines else doctrines[0])
        pick = ttk.Combobox(dlg, textvariable=pick_var, state="readonly",
                            values=doctrines, font=("Consolas", 10), width=34)
        pick.pack(fill=tk.X, padx=12)

        result = {"ok": False}

        def _confirm():
            result["ok"] = True
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        btns = tk.Frame(dlg, bg=BG_PANEL)
        btns.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btns, text="Continue", style="Green.TButton",
                   command=_confirm).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=_cancel).pack(side=tk.LEFT, padx=2)
        dlg.bind("<Return>", lambda e: _confirm())
        dlg.bind("<Escape>", lambda e: _cancel())
        try:
            dlg.grab_set()
        except Exception:
            pass
        self.root.wait_window(dlg)

        if not result["ok"]:
            return
        doctrine_name = pick_var.get()
        if not doctrine_name:
            return
        motd_name = simpledialog.askstring(
            "Name this MOTD",
            "Name for this MOTD template:",
            initialvalue=doctrine_name, parent=self.root)
        if not motd_name or not motd_name.strip():
            return
        motd_name = motd_name.strip()
        # Confirm before silently clobbering an existing template under the
        # same doctrine. The non-colliding save path is unchanged.
        collides = any(
            (m.get("doctrine") or "") == doctrine_name
            and m.get("name") == motd_name
            for m in self._saved_motds())
        if collides and not messagebox.askyesno(
                "Overwrite?",
                f"A MOTD template named '{motd_name}' already exists under "
                f"doctrine '{doctrine_name}'. Overwrite it?"):
            return
        self._save_linked_motd(doctrine_name, motd_name)

    def _create_doctrine_from_motd(self, raw, fittings):
        """Create a new doctrine from an imported MOTD: import its linked fits
        (TAGLESS), and POPULATE the live builder fields from it (it does NOT save
        a doctrine-linked MOTD — the user creates those via 'Link to doctrine').

        ``raw`` is the imported MOTD markup (already loaded into the header var
        by the import path). ``fittings`` is the list of ``{dna, name}`` dicts
        parsed from it. Returns ``(doctrine_id, name, added, reused, failed)``
        or ``None`` if the user cancels the name prompt. Testable — the only
        dialog is the name prompt (monkeypatchable)."""
        name = simpledialog.askstring(
            "Create doctrine",
            "Name for the new doctrine:",
            initialvalue="Imported doctrine", parent=self.root)
        if not name or not name.strip():
            return None
        name = name.strip()

        doctrine_id = self.fittings.add_doctrine(name)

        # Pre-index existing fits by content hash for de-dupe.
        existing = {}
        for f in self.fittings.list_fits():
            try:
                existing[fit_models.fit_content_hash(f.parsed)] = f.id
            except Exception:
                pass

        added = reused = failed = 0
        for entry in fittings or []:
            dna = (entry or {}).get("dna", "")
            fit_name = (entry or {}).get("name") or ""
            if not dna:
                failed += 1
                continue
            try:
                parsed = fit_parser.parse_dna(dna, self.type_catalog).fit
                h = fit_models.fit_content_hash(parsed)
            except Exception:
                failed += 1
                continue
            fid = existing.get(h)
            if fid is None:
                fit = fit_models.Fit(
                    id="",
                    name=fit_name or parsed.ship_name or "",
                    hull_type_id=parsed.ship_type_id,
                    hull_name=parsed.ship_name or "",
                    source="dna",
                    raw_text="",
                    parsed=parsed,
                    # Store the canonical (to_dna) form so the library fit isn't
                    # saddled with a legacy bare-id T3 DNA from the source MOTD.
                    dna=self._canonical_fit_dna(dna, parsed),
                    notes="",
                    esi_fitting_ids={},
                    created="",
                    modified="",
                )
                try:
                    fid = self.fittings.add_fit(fit)
                    existing[h] = fid
                    added += 1
                except Exception:
                    failed += 1
                    continue
            else:
                reused += 1
            # TAGLESS membership (empty tag list).
            self.fittings.add_fit_to_doctrine(doctrine_id, fid, [])

        try:
            self.fittings.save()
        except Exception:
            pass

        # Refresh the doctrine views + select the NEW doctrine in the MOTD combo
        # FIRST, so the fields we populate below are captured against the right
        # doctrine. _motd_on_doctrine_change rebuilds the (empty, tagless) tag
        # set and clears any loaded-fits fallback — so set loaded fits AFTER it.
        self._refresh_doctrine_list()
        self._motd_refresh_doctrines()
        if name in (self._motd_doctrine_combo["values"] or ()):
            self._motd_doctrine_var.set(name)
        self._motd_on_doctrine_change()

        # Parse the imported MOTD and POPULATE the builder fields so the captured
        # linked MOTD is COMPLETE (staging/channel/fits restore on later load).
        # Previously the import path only dumped raw markup into the header, so
        # these editable fields were empty and the saved MOTD lost them. Falls
        # back to the caller-supplied fittings if the parse found no fit links.
        parsed = motd_builder.parse_motd(raw)
        self._motd_populate_fields_from_parsed(parsed, fallback_fittings=fittings)

        # Do NOT dump the raw markup into the intro anymore — the fields above
        # now carry the imported content, and build_motd re-renders it cleanly.
        if getattr(self, "_motd_intro", None) is not None:
            self._motd_intro.set_markup("")

        # Do NOT auto-save a doctrine-linked MOTD here — the user creates linked
        # MOTDs explicitly via "Link to doctrine". Just rebuild the preview from the
        # populated fields so the imported content is visible and editable.
        self._rebuild_motd_preview()
        try:
            self._refresh_fit_list(self._fit_search_var.get())
        except Exception:
            pass

        return doctrine_id, name, added, reused, failed

    def _motd_populate_fields_from_parsed(self, parsed, fallback_fittings=None):
        """Populate the editable MOTD builder fields from a parsed MOTD dict.

        Sets the staging checkbox + system name, the logi/cap channel entry, a
        best-effort FC selection, and the explicit loaded-fits fallback. Does
        NOT touch the doctrine selection or the header — the caller owns those
        (the import-create path selects the new doctrine first; the decline path
        leaves the doctrine cleared). ``parsed`` is the dict from
        :func:`motd_builder.parse_motd`; ``fallback_fittings`` is the raw
        ``[{dna,name}]`` list used for the loaded-fits fallback when the parse
        found no fit links."""
        # Staging system → check the box + fill the system name.
        if parsed.get("staging"):
            if getattr(self, "_motd_staging_enabled", None) is not None:
                self._motd_staging_enabled.set(True)
            if getattr(self, "_motd_staging_var", None) is not None:
                self._motd_staging_var.set(parsed["staging"]["name"])

        # Logi/cap channel → fill the channel entry with the display name.
        if parsed.get("channel"):
            entry = getattr(self, "_motd_channel_entry", None)
            if entry is not None:
                try:
                    entry.delete(0, tk.END)
                    entry.insert(0, parsed["channel"]["name"])
                except Exception:
                    pass

        # Best-effort FC: select a loaded character whose name matches the FC in
        # the imported MOTD; otherwise leave the current selection untouched.
        if parsed.get("fc"):
            fc_name = parsed["fc"].get("name") or ""
            combo = getattr(self, "_motd_fc_combo", None)
            values = (combo["values"] if combo is not None else ()) or ()
            if fc_name in values:
                self._motd_fc_var.set(fc_name)

        # Explicit loaded-fits fallback: the parsed fit links (these survive even
        # when the doctrine is TAGLESS, so its checked tags yield no fits). Falls
        # back to the caller-supplied fittings if the parse found none.
        self._motd_loaded_fits = [
            (f["dna"], f["name"])
            for f in (parsed.get("fittings") or fallback_fittings or [])
        ] or None

    def _motd_scan_channels(self):
        """Discover chat channels (off the Tk thread) to seed the channel
        AutocompleteEntry, mirroring :meth:`_scan_intel_channels`.

        discover_channels() does directory + header I/O, so it runs on a worker
        thread; the resulting names are applied back on the Tk main thread."""
        try:
            logs_path = resolve_eve_logs_path(
                self.config.get("eve_logs_path", ""))
        except Exception:
            logs_path = self.config.get("eve_logs_path", "")
        if not logs_path or not os.path.isdir(logs_path):
            self._motd_channel_status.config(
                text="Set a valid EVE Chat Logs path first (Settings)",
                fg=FG_ORANGE)
            return
        self._motd_channel_status.config(text="Scanning...", fg=FG_ACCENT)

        def worker():
            try:
                found = discover_channels(logs_path, tracked_character=None,
                                          max_age_days=30)
                names = [d["name"] for d in found]
            except Exception:
                names = []
            self.root.after(0, self._apply_motd_scanned_channels, names)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_motd_scanned_channels(self, names):
        """Apply channel-scan results on the Tk thread: cache the discovered
        names (shared with the intel cache), update the entry's completion pool,
        and the status line."""
        ic = self.config.setdefault("intel_channels", {})
        ic["cached_discovered"] = list(names)
        self._save_config()
        entry = getattr(self, "_motd_channel_entry", None)
        if entry is not None:
            try:
                entry.update_completions(list(names))
            except Exception:
                pass
        self._motd_channel_status.config(
            text=f"Found {len(names)} channel(s)", fg=FG_GREEN)

    # ── MOTD: live preview + length meter (Task 7.2) ──────────────────────────

    def _schedule_motd_preview(self, *args):
        """Debounce a preview rebuild via root.after (coalesces rapid edits)."""
        job = getattr(self, "_motd_preview_job", None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._motd_preview_job = self.root.after(
            self._MOTD_PREVIEW_DEBOUNCE_MS, self._rebuild_motd_preview)

    def _motd_fit_compact_label(self, fit):
        """Return a short link label for a fit: the ship CLASS (hull) name.

        Prefers the stored ``hull_name``; falls back to resolving
        ``hull_type_id`` via the shared type catalog, then to the full
        ``fit.name`` if neither yields a usable class name. Used by
        :meth:`_motd_build_fits_by_tag` when ``compact=True`` to shrink the
        MOTD (e.g. "Muninn" instead of "MWD Heavy Muninn")."""
        name = (getattr(fit, "hull_name", "") or "").strip()
        if name:
            return name
        try:
            resolved = self.type_catalog.resolve_name(
                getattr(fit, "hull_type_id", 0)) or ""
        except Exception:
            resolved = ""
        resolved = resolved.strip()
        return resolved or fit.name

    def _motd_build_fits_by_tag(self, doctrine, compact: bool = False):
        """Assemble ``{tag: [(dna, label), ...]}`` for the checked tags of a
        doctrine. Pure assembly — no business logic. Fits with no DNA are skipped.

        Two MOTD conventions are applied here:

        * The link LABEL is ALWAYS the ship class name (e.g. "Tengu") via
          :meth:`_motd_fit_compact_label`, not the saved fit name — the DNA is
          unchanged so the link still rebuilds the exact fit on click, and the
          fit's name in the library is untouched. (``compact`` is retained for
          call-site compatibility but no longer changes the label.)
        * Role precedence: a fit tagged DPS *and* at least one other role is
          placed under the OTHER role(s) only, never DPS — e.g. a ship tagged
          DPS + Links shows under Links."""
        fits_by_tag: dict[str, list[tuple[str, str]]] = {}
        if doctrine is None:
            return fits_by_tag
        checked = {t for t, v in self._motd_tag_vars.items() if v.get()}
        for mem in doctrine.members:
            fit = self.fittings.get_fit(mem.fit_id)
            if fit is None or not fit.dna:
                continue
            label = self._motd_fit_compact_label(fit)
            member_tags = list(mem.tags or [])
            non_dps = [t for t in member_tags if t != "DPS"]
            # Drop DPS when the fit also carries a non-DPS role.
            effective_tags = non_dps if ("DPS" in member_tags and non_dps) \
                else member_tags
            for tag in effective_tags:
                if tag not in checked:
                    continue
                fits_by_tag.setdefault(tag, []).append(
                    (self._canonical_fit_dna(fit.dna, fit.parsed), label))
        return fits_by_tag

    def _motd_resolve_channel_id(self, channel_name):
        """Resolve a logi/cap channel NAME to its numeric chat-channel id.

        Reads the channel's chat-log header via
        :func:`intel_monitor.read_channel_id` (a single small header read),
        using the same logs path the channel scan uses. The result is cached
        per channel name (``self._motd_channel_id_cache``) so a header is read
        at most once per distinct channel between cache resets — repeated
        previews/builds for the same channel reuse the cached id rather than
        hitting disk on every debounce.

        Returns the id as a string (leading minus preserved) or ``None`` when
        the channel is blank, no matching log exists, or no id could be read
        (in which case the caller falls back to a plain-text channel)."""
        name = (channel_name or "").strip()
        if not name:
            return None
        cache = getattr(self, "_motd_channel_id_cache", None)
        if cache is None:
            cache = self._motd_channel_id_cache = {}
        if name in cache:
            return cache[name]
        # Not cached: resolving reads chat-log headers off disk, which can be
        # slow on a large Chatlogs folder — NEVER do it on the Tk thread (that
        # froze the UI for ~30s). Kick off a one-shot background resolution and
        # return None now (plain-text channel); when it lands we cache the id
        # and re-render the preview so the link appears.
        pending = getattr(self, "_motd_channel_id_pending", None)
        if pending is None:
            pending = self._motd_channel_id_pending = set()
        if name not in pending:
            pending.add(name)

            def worker(n=name):
                try:
                    logs_path = resolve_eve_logs_path(
                        self.config.get("eve_logs_path", ""))
                except Exception:
                    logs_path = self.config.get("eve_logs_path", "")
                try:
                    cid = intel_monitor.read_channel_id(logs_path, n)
                except Exception:
                    cid = None

                def done():
                    self._motd_channel_id_cache[n] = cid
                    self._motd_channel_id_pending.discard(n)
                    self._rebuild_motd_preview()

                try:
                    self.root.after(0, done)
                except Exception:
                    pass

            threading.Thread(target=worker, daemon=True).start()
        return None

    def _canonical_fit_dna(self, dna: str, parsed=None) -> str:
        """Re-encode a fit DNA through ``parse_dna`` → ``to_dna`` so it always
        uses the modern, client-correct form.

        Chiefly this rewrites legacy bare-id Tech-III subsystems (``id``) to the
        ``id;1`` quantity form: the bare-id form makes the live client mis-render
        a subsystem as the hull (the reported "Tengu propulsion" bug). Imported
        MOTD DNA is the only fit source that isn't already routed through
        ``to_dna``, so a legacy MOTD's raw DNA would otherwise reach the client
        unchanged. Pass a pre-parsed ``ParsedFit`` to skip a re-parse. Returns
        ``dna`` unchanged if it cannot be parsed (defensive — never drops a
        link); idempotent for already-canonical DNA."""
        try:
            if parsed is None:
                parsed = fit_parser.parse_dna(dna, self.type_catalog).fit
            # Limitation mirrors fit_dna.to_dna: DNA cannot represent a module's
            # offline state, so re-encoding here drops offline modules to online.
            return fit_dna.to_dna(parsed, self.type_catalog)
        except Exception:
            return dna

    def _heal_fit_dna(self):
        """One-time: re-encode legacy bare-id T3 fit DNA to the canonical id;1
        form so pre-fix imports are correct for copy-DNA / in-game links."""
        changed = False
        for fit in self.fittings.list_fits():
            try:
                canon = self._canonical_fit_dna(fit.dna, fit.parsed)
            except Exception:
                continue
            if canon and canon != fit.dna:
                fit.dna = canon
                changed = True
        if changed:
            try:
                self.fittings.save()
            except Exception:
                pass

    def _motd_fit_deltas(self) -> dict:
        """Map ``hull_type_id -> +/- pilot delta`` for the active doctrine's guided
        fits versus the live fleet. Returns ``{}`` when there is no active doctrine
        or no live-fleet snapshot (so the MOTD builds with no annotations).

        Note: the active Fleet-tab doctrine may differ from the doctrine whose
        fits the MOTD emits, so deltas only land where hulls coincide (matching
        is by ``hull_type_id``, intentional)."""
        doc = self._active_fleet_doctrine()
        cached = getattr(self, "_last_specialized_args", None)
        if doc is None or not cached:
            return {}
        _members, counts, total = cached
        if not counts or not total:
            return {}
        cmd = sum(c for tid, c in counts.items()
                  if tid in ship_classes.ALL_LINKS_COMMAND)
        frac = (cmd / total) if total else 0.0
        try:
            exempt_ids, hull_ids = self._resolve_exemptions_for_counts(
                doc, counts)
            rep = fleet_guidance.compute_fleet_guidance(
                doc, self.fittings.get_fit, self.type_catalog,
                counts, total, command_ship_fraction=frac,
                exempt_type_ids=exempt_ids, doctrine_hull_ids=hull_ids)
        except Exception as exc:
            print(f"[MOTD] fit-delta compute failed: {exc}")
            return {}
        return {f.hull_type_id: f.delta for f in rep.fits if f.delta != 0}

    def _current_motd_markup(self, compact: bool = False):
        """Build the MOTD markup string from the current input selections.

        When ``compact`` is True, fit link labels collapse to the ship class
        name (see :meth:`_motd_build_fits_by_tag`) to save room; otherwise full
        fit names are used. The text colour is left at ``build_motd``'s white
        default (``0xffffffff``) — the in-game default red is hard to read — so
        the wrapper is always emitted and counted by the length meter.

        Side effect: sets ``self._motd_staging_warn`` to a non-blocking warning
        string (or "") describing an unresolvable staging system, which
        :meth:`_rebuild_motd_preview` folds into the warnings line."""
        doctrine = self._motd_selected_doctrine()
        fits_by_tag = self._motd_build_fits_by_tag(doctrine, compact=compact)

        # Explicit-fits fallback: when the checked tags produce NO fits (e.g. a
        # tagless imported doctrine) but a saved/imported MOTD carried explicit
        # fit links, render those instead so the saved fits still appear. When
        # tags DO produce fits we ignore the override — tags are live/current.
        if (self._motd_loaded_fits
                and not any(fits_by_tag.get(t) for t in fits_by_tag)):
            fits_by_tag = {"Fits": list(self._motd_loaded_fits)}

        # Canonicalise every fit DNA so the emitted <url=fitting:...> links use
        # the client-correct form regardless of how the fit was stored. This is
        # the single chokepoint for preview / Set / Copy, so a legacy bare-id
        # T3 DNA (from an imported MOTD or an older library fit) is normalised to
        # the "id;1" subsystem form here — the bare-id form makes the client
        # mis-render a subsystem as the hull ("Tengu propulsion"). Idempotent
        # for already-canonical DNA; raw DNA is kept if it cannot be parsed.
        # Each displayed DNA is parsed exactly once here and the parsed fit feeds
        # BOTH the T3 canonicalisation (passed in to skip a re-parse) and the live-
        # fleet delta lookup. No live fleet / no active doctrine => empty deltas =>
        # labels untouched. Parsing/canonicalisation failures keep the raw DNA.
        deltas = self._motd_fit_deltas()

        def _finalize(dna, name):
            try:
                parsed = fit_parser.parse_dna(dna, self.type_catalog).fit
            except Exception:
                return (self._canonical_fit_dna(dna), name, 0)
            canon = self._canonical_fit_dna(dna, parsed)
            d = deltas.get(parsed.ship_type_id) if deltas else 0
            return (canon, name, d or 0)

        fits_by_tag = {tag: [_finalize(dna, name) for dna, name in fits]
                       for tag, fits in fits_by_tag.items()}

        fc_auth = self._motd_selected_fc_auth()
        fc_name = fc_auth.character_name if fc_auth else None
        fc_cid = fc_auth.character_id if fc_auth else None

        channel = (self._motd_channel_entry.get().strip()
                   if getattr(self, "_motd_channel_entry", None) else "")
        # Resolve the channel name to its numeric id so the Logi line becomes a
        # clickable joinChannel link; None falls back to plain text. Cached per
        # name to avoid re-reading the log header on every debounce.
        channel_id = (self._motd_resolve_channel_id(channel)
                      if channel else None)

        # Optional staging system → in-game SYSTEM link. Only when the checkbox
        # is on and the entry resolves to a real system; otherwise warn (non-
        # blocking) and omit the line. resolve_name is a pure local lookup.
        self._motd_staging_warn = ""
        staging_name = None
        staging_system_id = None
        if (getattr(self, "_motd_staging_enabled", None) is not None
                and self._motd_staging_enabled.get()):
            raw = (self._motd_staging_var.get() or "").strip()
            if raw:
                import system_coords
                sid = system_coords.resolve_name(raw)
                if sid is not None:
                    staging_name = raw
                    staging_system_id = sid
                else:
                    self._motd_staging_warn = (
                        f"Staging system '{raw}' did not resolve to a known "
                        f"system — the staging line was omitted.")

        return motd_builder.build_motd(
            fc_name=fc_name,
            fc_character_id=fc_cid,
            doctrine_name=(doctrine.name if doctrine else ""),
            fits_by_tag=fits_by_tag,
            channel=channel or None,
            channel_id=channel_id,
            header=self._motd_intro.get_markup(),
            footer=self._motd_outro.get_markup(),
            staging_name=staging_name,
            staging_system_id=staging_system_id,
        )

    def _motd_budget(self) -> int:
        """The configured raw-markup MOTD ceiling (defaults to ~3000)."""
        try:
            val = int(self.config.get("fittings", {}).get(
                "motd_budget", motd_builder.MOTD_BUDGET_DEFAULT))
            return val if val > 0 else motd_builder.MOTD_BUDGET_DEFAULT
        except (TypeError, ValueError):
            return motd_builder.MOTD_BUDGET_DEFAULT

    def _motd_output_markup(self):
        """Return ``(markup, compacted)`` — the MOTD to preview/copy/push.

        Builds with full fit names first; only when that overflows the budget
        does it rebuild with compact (ship-class) labels to claw back room, and
        only adopts the compact form if it is actually shorter (a doctrine
        already named by hull yields no gain — full names are kept then). Used
        by the preview, the Set-as-fleet-MOTD push, and Copy so all three agree
        on the same output."""
        budget = self._motd_budget()
        markup = self._current_motd_markup()
        compacted = False
        if motd_builder.estimate_length(markup) > budget:
            compact_markup = self._current_motd_markup(compact=True)
            if (motd_builder.estimate_length(compact_markup)
                    < motd_builder.estimate_length(markup)):
                markup = compact_markup
                compacted = True
        return markup, compacted

    def _rebuild_motd_preview(self):
        """Rebuild the preview Text, the length meter, and the warnings line.

        Meter color: green < 80% of budget, yellow < 100%, red >= 100%. Warns
        (non-blocking) if any single ``<url=fitting:...>`` attribute exceeds
        ``_MOTD_LINK_ATTR_WARN`` chars (spec §3.5)."""
        self._motd_preview_job = None
        preview = getattr(self, "_motd_preview", None)
        if preview is None:
            return

        budget = self._motd_budget()
        markup, compacted = self._motd_output_markup()

        # Raw pane: the built markup verbatim.
        preview.config(state=tk.NORMAL)
        preview.delete("1.0", tk.END)
        preview.insert("1.0", markup)
        preview.config(state=tk.DISABLED)

        # Rendered pane: parse the markup into styled segments and lay them out
        # with Tk tags (colours/bold/italic/underline/size + link styling).
        self._render_motd_markup(markup)

        length = motd_builder.estimate_length(markup)
        frac = (length / budget) if budget else 0.0
        if frac < 0.8:
            color = FG_GREEN
        elif frac < 1.0:
            color = FG_YELLOW
        else:
            color = FG_RED

        canvas = self._motd_meter_canvas
        canvas.delete("all")
        try:
            w = canvas.winfo_width() or 1
        except Exception:
            w = 1
        fill_w = int(min(frac, 1.0) * w)
        if fill_w > 0:
            canvas.create_rectangle(0, 0, fill_w, 14, fill=color, width=0)
        self._motd_meter_label.config(
            text=f"{length} / {budget}", fg=color)

        # Non-blocking warnings.
        warns = []
        staging_warn = getattr(self, "_motd_staging_warn", "")
        if staging_warn:
            warns.append(staging_warn)
        if compacted:
            warns.append("Shortened fit names to ship class to fit the MOTD "
                         "length limit.")
        if length >= budget:
            warns.append(f"Over budget by {length - budget} chars — the server "
                         f"may truncate this MOTD.")
        long_links = [
            m.group("dna")
            for m in motd_builder._FITTING_RE.finditer(markup)
            if len(m.group("dna")) > self._MOTD_LINK_ATTR_WARN
        ]
        if long_links:
            warns.append(
                f"{len(long_links)} fit link(s) exceed "
                f"{self._MOTD_LINK_ATTR_WARN} chars in their DNA — these may "
                f"not render in-game.")
        self._motd_warn_label.config(text="  ".join(warns))

    def _motd_render_tag(self, hexcolor, bold, italic, underline, size,
                         is_link):
        """Return (configuring on first use) a cached Tk tag for the Rendered
        pane matching the given style. Keyed so identical styles reuse one tag,
        avoiding per-rebuild tag churn."""
        key = (hexcolor, bold, italic, underline, size, is_link)
        tag = self._motd_render_tags.get(key)
        if tag is not None:
            return tag
        tag = "r%d" % len(self._motd_render_tags)
        # Font: Consolas at the run's size (or the pane default 10), with the
        # requested weight/slant/underline. Links are always underlined.
        fnt = tkfont.Font(
            family="Consolas", size=(size or 10),
            weight=("bold" if bold else "normal"),
            slant=("italic" if italic else "roman"),
            underline=bool(underline or is_link))
        fg = FG_ACCENT if is_link else (hexcolor or FG_TEXT)
        self._motd_rendered.tag_configure(tag, font=fnt, foreground=fg)
        self._motd_render_tags[key] = tag
        return tag

    def _render_motd_markup(self, markup: str):
        """Lay the built ``markup`` into the read-only Rendered pane with Tk tags.

        Parses via :func:`motd_markup.parse_markup`; each segment's text is
        inserted with a cached style tag (colour via ``eve``→hex already done by
        the parser, bold/italic/underline/size). Link segments are shown
        underlined + accent-coloured with a hover tooltip carrying the target
        (non-clickable — this is a preview). Newline segments emit ``"\\n"``."""
        rendered = getattr(self, "_motd_rendered", None)
        if rendered is None:
            return

        rendered.config(state=tk.NORMAL)
        # Drop the prior render's per-link hover tags (and their bindings) so
        # they do not accumulate across rebuilds; the cached *style* tags
        # (_motd_render_tags) are reused and intentionally kept.
        for tag in rendered.tag_names():
            if tag.startswith("lnk"):
                rendered.tag_delete(tag)
        self._motd_link_seq = 0
        rendered.delete("1.0", tk.END)

        for seg in motd_markup.parse_markup(markup or ""):
            if seg.newline:
                rendered.insert(tk.END, "\n")
                continue
            if not seg.text:
                continue
            is_link = seg.link is not None
            tag = self._motd_render_tag(
                seg.color, seg.bold, seg.italic, seg.underline, seg.size,
                is_link)
            if is_link:
                # A per-insertion unique tag carries the hover tooltip so each
                # link run shows its own target; the style tag handles the look.
                link_tag = "lnk%d" % self._motd_link_seq
                self._motd_link_seq += 1
                rendered.insert(tk.END, seg.text, (tag, link_tag))
                target = seg.link
                rendered.tag_bind(
                    link_tag, "<Enter>",
                    lambda e, t=target: self._show_tooltip(e, t))
                rendered.tag_bind(
                    link_tag, "<Leave>", lambda e: self._hide_tooltip())
            else:
                rendered.insert(tk.END, seg.text, (tag,))

        rendered.config(state=tk.DISABLED)

    # ── MOTD: fleet resolution + set/copy (Task 7.2) ──────────────────────────

    def _motd_refresh_fleet_status(self):
        """Resolve the active FC character's current fleet + boss flag off the
        Tk thread (reuses ESIAuth.get_fleet_info / is_boss) and update the Set
        button + status line on the Tk thread.

        For instant feedback in the common case, when the selected FC is the
        primary/polled character we first SEED the Set button from the fleet
        state cached by :meth:`_refresh_fleet_locations` (the ~15s poll), then
        confirm with a fresh async ESI check below. Seeding only applies to the
        primary character; other characters rely solely on the async check."""
        auth = self._motd_selected_fc_auth() or self.esi_auth
        if auth is None or not auth.is_authenticated:
            self._motd_fleet_id = None
            self._motd_is_boss = False
            self._apply_motd_fleet_status(
                None, False, "No authenticated FC character selected.")
            return

        # Instant feedback: seed from the polled cache when the selected FC is
        # the primary (polled) character, so the Set button enables immediately
        # for the common case while the async check below confirms.
        if (auth is self.esi_auth
                and hasattr(self, "_last_polled_fleet_id")):
            cached_id = self._last_polled_fleet_id
            cached_boss = bool(getattr(self, "_last_polled_fleet_is_boss",
                                       False))
            if cached_boss and cached_id:
                self._apply_motd_fleet_status(
                    cached_id, True,
                    "You are the fleet boss — Set is enabled.")

        self._motd_fleet_status.config(text="Checking fleet...", fg=FG_ACCENT)

        def worker():
            fleet_id = None
            is_boss = False
            msg = "Not in a fleet."
            try:
                info = auth.get_fleet_info()
                if info:
                    fleet_id = info.get("fleet_id")
                    is_boss = auth.is_boss(info, auth.character_id)
                    msg = ("You are the fleet boss — Set is enabled."
                           if is_boss else
                           "In a fleet but not the boss — only the boss can "
                           "set the MOTD.")
            except Exception as e:
                msg = f"Could not read fleet status: {e}"
            self.root.after(0, self._apply_motd_fleet_status,
                            fleet_id, is_boss, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_motd_fleet_status(self, fleet_id, is_boss, msg):
        """Apply fleet-resolution results on the Tk thread: store fleet/boss and
        enable the Set button only when the selected FC is the fleet boss."""
        self._motd_fleet_id = fleet_id
        self._motd_is_boss = bool(is_boss)
        btn = getattr(self, "_motd_set_btn", None)
        if btn is not None:
            btn.config(state=(tk.NORMAL if self._motd_is_boss else tk.DISABLED))
        color = FG_GREEN if is_boss else (FG_YELLOW if fleet_id else FG_DIM)
        self._motd_fleet_status.config(text=msg, fg=color)

    def _set_fleet_motd(self):
        """Write the composed MOTD to the active FC's fleet (boss-only).

        Gated on the resolved boss flag; if the markup is over budget, confirms
        first. The PUT runs on a daemon thread; 204 -> success, anything else ->
        an error (403 not-boss / over-length truncation surfaced)."""
        if not self._motd_is_boss or not self._motd_fleet_id:
            messagebox.showwarning(
                "Cannot set MOTD",
                "The selected FC character must be the current fleet boss. "
                "Use 'Refresh fleet' after forming/joining a fleet.")
            return
        auth = self._motd_selected_fc_auth() or self.esi_auth
        if auth is None or not auth.is_authenticated:
            messagebox.showwarning("Cannot set MOTD",
                                   "No authenticated FC character selected.")
            return

        markup, _compacted = self._motd_output_markup()
        length = motd_builder.estimate_length(markup)
        budget = self._motd_budget()
        if length >= budget:
            if not messagebox.askyesno(
                    "Over budget",
                    f"This MOTD is {length} chars (budget {budget}). The "
                    f"server may truncate it. Set it anyway?"):
                return

        fleet_id = self._motd_fleet_id
        self._motd_fleet_status.config(text="Setting MOTD...", fg=FG_ACCENT)

        pushed_doctrine = ""
        _mvar = getattr(self, "_motd_doctrine_var", None)
        if _mvar is not None:
            pushed_doctrine = _mvar.get() or ""

        def worker():
            ok = False
            err = None
            try:
                ok = auth.set_fleet_motd(fleet_id, markup)
            except Exception as e:
                err = str(e)
            self.root.after(0, _done, ok, err)

        def _done(ok, err):
            if ok:
                self._motd_fleet_status.config(
                    text="MOTD set successfully (204).", fg=FG_GREEN)
                self._auto_select_fleet_doctrine(pushed_doctrine)
                # First successful manual push this session arms the auto-update
                # link (startup stays OFF; see _motd_link_initial_state).
                self._motd_arm_link(markup)
            else:
                detail = (f"\n\n{err}" if err else
                          "\n\nESI rejected the request (403 if you are no "
                          "longer the boss, or the MOTD was too long).")
                self._motd_fleet_status.config(
                    text="Failed to set MOTD.", fg=FG_RED)
                messagebox.showerror("Set MOTD failed",
                                     f"Could not set the fleet MOTD.{detail}")

        threading.Thread(target=worker, daemon=True).start()

    def _motd_arm_link(self, markup):
        """Arm the auto-update link after a successful manual "Set as fleet
        MOTD" push. Main-thread only.

        The link is session-scoped (never persisted, always OFF at startup — see
        _motd_link_initial_state). But once the FC has deliberately pushed a MOTD
        this session, keeping it in sync no longer risks clobbering the fleet's
        real MOTD with a default, so a successful first manual push switches
        auto-update ON without an extra click. Records the just-pushed markup and
        timestamp so the autopush loop treats it as already-synced and does not
        immediately re-push the identical text. The user can still toggle it off.
        """
        self._motd_link_enabled = True
        var = getattr(self, "_motd_link_var", None)
        if var is not None:
            var.set(True)
        self._motd_link_state = "ok"
        self._motd_last_pushed_markup = markup
        self._motd_last_push_ts = time.monotonic()
        self._motd_last_check_ts = time.monotonic()
        self._motd_update_link_indicator()

    def _motd_toggle_link(self):
        """Enable/disable linked auto-push of the MOTD.

        The link is session-scoped and intentionally NOT persisted: on the next
        launch it must start OFF so a freshly-opened (default) MOTD is never
        pushed over the fleet's real one. See _motd_link_initial_state."""
        self._motd_link_enabled = bool(self._motd_link_var.get())
        self._motd_link_state = "waiting" if self._motd_link_enabled else "off"
        if self._motd_link_enabled:
            import time
            self._motd_last_check_ts = time.monotonic()
            self._motd_maybe_autopush()   # push now if something changed
        self._motd_update_link_indicator()

    def _motd_maybe_autopush(self):
        """If linked, recompute the MOTD and re-push it to the fleet when it has
        changed since the last push. Called on the main thread each fleet poll.
        Markup is computed here (reads Tk widgets); boss resolution + the PUT run
        on a worker thread."""
        if not getattr(self, "_motd_link_enabled", False):
            return
        auth = self._motd_selected_fc_auth() or self.esi_auth
        if auth is None or not auth.is_authenticated:
            return
        try:
            markup, _ = self._motd_output_markup()
        except Exception:
            return
        if motd_builder.estimate_length(markup) >= self._motd_budget():
            self._motd_link_state = "overbudget"
            self._motd_update_link_indicator()
            return
        if markup == self._motd_last_pushed_markup:
            return  # unchanged → no redundant ESI write

        def worker():
            ok = False
            reason = None
            try:
                info = auth.get_fleet_info()
                fid = info["fleet_id"] if info else None
                is_boss = bool(info and auth.is_boss(info, auth.character_id))
                if not fid or not is_boss:
                    reason = "not_boss"
                else:
                    ok = auth.set_fleet_motd(fid, markup)
                    if not ok:
                        reason = "error"
            except Exception as e:
                reason = str(e) or "error"
            self.root.after(0, _done, ok, reason, markup)

        def _done(ok, reason, pushed):
            import time
            if ok:
                self._motd_last_push_ts = time.monotonic()
                self._motd_last_pushed_markup = pushed
                self._motd_link_state = "ok"
            elif reason == "not_boss":
                self._motd_link_state = "not_boss"   # keep linked, just can't push
            else:
                # Hard failure (e.g. 403) — drop the link so we stop hammering ESI.
                self._motd_link_enabled = False
                if getattr(self, "_motd_link_var", None) is not None:
                    self._motd_link_var.set(False)
                self._motd_link_state = "error"
            self._motd_update_link_indicator()

        threading.Thread(target=worker, daemon=True).start()

    def _motd_update_link_indicator(self):
        ind = getattr(self, "_motd_link_indicator", None)
        if ind is None:
            return
        if not getattr(self, "_motd_link_enabled", False):
            ind.config(text="○ not linked", fg=FG_DIM)
            return
        state = getattr(self, "_motd_link_state", "waiting")
        if state == "error":
            ind.config(text="○ link dropped (push failed)", fg=FG_RED)
            return
        if state == "overbudget":
            ind.config(text="🔗 over budget — not pushed", fg=FG_YELLOW)
            return
        if state == "not_boss":
            ind.config(text="🔗 linked (not boss)", fg=FG_YELLOW)
            return
        ts = getattr(self, "_motd_last_check_ts", None)
        if ts is None:
            ind.config(text="🔗 linked • starting…", fg=FG_ACCENT)
            return
        import time
        age = int(time.monotonic() - ts)
        ind.config(text=f"🔗 linked • {age}s", fg=FG_GREEN)

    def _motd_link_tick(self):
        """1 s heartbeat that refreshes the 'Ns' indicator text."""
        self._motd_update_link_indicator()
        try:
            self.root.after(1000, self._motd_link_tick)
        except (tk.TclError, RuntimeError):
            pass

    def _motd_link_interval_s(self) -> int:
        """Configured auto-update interval in seconds (clamped 10..300)."""
        try:
            v = int(self.config.get("fittings", {}).get("motd_link_interval_s", 15))
        except (TypeError, ValueError):
            v = 15
        return max(10, min(300, v))

    def _motd_link_interval_changed(self):
        try:
            v = int(self._motd_link_interval_var.get())
        except (tk.TclError, ValueError):
            return
        v = max(10, min(300, v))
        if v != self._motd_link_interval_var.get():
            self._motd_link_interval_var.set(v)
        self.config.setdefault("fittings", {})["motd_link_interval_s"] = v
        try:
            self._save_config()
        except Exception:
            pass

    def _motd_autopush_loop(self):
        """Dedicated auto-update timer (decoupled from the fleet poll). Runs every
        configured interval; when linked, stamps the check time and re-pushes the
        MOTD if it changed. Always re-arms at the (possibly updated) interval."""
        if getattr(self, "_motd_link_enabled", False):
            import time
            self._motd_last_check_ts = time.monotonic()
            self._motd_maybe_autopush()
        try:
            self._motd_autopush_after_id = self.root.after(
                self._motd_link_interval_s() * 1000, self._motd_autopush_loop)
        except (tk.TclError, RuntimeError):
            pass

    def _copy_motd(self):
        """Copy the raw MOTD markup to the clipboard (manual-paste fallback)."""
        markup, _compacted = self._motd_output_markup()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(markup)
            self._motd_fleet_status.config(
                text="Markup copied to clipboard.", fg=FG_GREEN)
        except Exception as e:
            messagebox.showerror("Copy failed", f"Could not copy markup:\n{e}")

    # ── MOTD: import current MOTD + template persistence (Task 7.3) ────────────

    def _import_current_motd(self):
        """Load the active FC's current fleet MOTD, parse it, and offer to import
        any embedded fit DNAs into the library + save the raw MOTD as a named
        template snapshot.

        Reads ``get_fleet(fleet_id)['motd']`` on a daemon thread (reusing the
        get_fleet_info fleet path), then parses with motd_builder.parse_motd."""
        auth = self._motd_selected_fc_auth() or self.esi_auth
        if auth is None or not auth.is_authenticated:
            messagebox.showwarning("Import MOTD",
                                   "No authenticated FC character selected.")
            return
        self._motd_fleet_status.config(text="Loading current MOTD...",
                                       fg=FG_ACCENT)

        def worker():
            raw = None
            err = None
            try:
                info = auth.get_fleet_info()
                fleet_id = info.get("fleet_id") if info else None
                if not fleet_id:
                    err = "The selected FC character is not in a fleet."
                else:
                    fleet = auth.get_fleet(fleet_id)
                    if not isinstance(fleet, dict):
                        err = ("Could not read the fleet (only the fleet boss "
                               "may read the MOTD).")
                    else:
                        raw = fleet.get("motd", "") or ""
            except Exception as e:
                err = str(e)
            self.root.after(0, self._apply_imported_motd, raw, err)

        threading.Thread(target=worker, daemon=True).start()

    def _clear_motd_builder(self):
        """Reset the MOTD builder to an empty state.

        Clears the header/footer, turns the staging checkbox off and empties
        its entry, blanks the logi/cap channel, deselects the doctrine (which
        empties the per-fit/tag include checkboxes), and rebuilds the now-empty
        preview. Used by 'Clear MOTD' and by the import path (so an imported
        MOTD replaces, rather than appends to, the current builder)."""
        # Intro / outro free text (markup editors).
        if getattr(self, "_motd_intro", None) is not None:
            self._motd_intro.set_markup("")
        if getattr(self, "_motd_outro", None) is not None:
            self._motd_outro.set_markup("")

        # Staging system: checkbox off + entry empty.
        if getattr(self, "_motd_staging_enabled", None) is not None:
            self._motd_staging_enabled.set(False)
        if getattr(self, "_motd_staging_var", None) is not None:
            self._motd_staging_var.set("")

        # Logi / cap channel entry.
        entry = getattr(self, "_motd_channel_entry", None)
        if entry is not None:
            try:
                entry.delete(0, tk.END)
            except Exception:
                pass

        # Drop any explicit loaded-fits fallback so a cleared builder renders no
        # fits (this method bypasses _motd_on_doctrine_change, which would
        # otherwise clear it).
        self._motd_loaded_fits = None

        # Deselect the doctrine and clear the linked-MOTD selection, then rebuild
        # the (now empty) tag checkboxes so the per-fit include boxes are cleared
        # along with their selection. Clearing both means the user does not have
        # to manually deselect/reselect a doctrine or linked MOTD to reload.
        if getattr(self, "_motd_doctrine_var", None) is not None:
            self._motd_doctrine_var.set("")
        if getattr(self, "_motd_saved_var", None) is not None:
            self._motd_saved_var.set(self._MOTD_SAVED_BLANK)
        self._motd_refresh_saved_dropdown()
        self._motd_rebuild_tag_checkboxes()

        # Rebuild the (now empty) preview immediately.
        self._rebuild_motd_preview()

    def _clear_motd(self):
        """'Clear MOTD' button: wipe the local builder, then — if the active FC
        is the current fleet boss — offer to also clear the in-game fleet MOTD
        (the user needs a way to remove a too-large MOTD already pushed).

        Always clears locally first. When boss + fleet are known, asks for
        confirmation and, if accepted, pushes an EMPTY MOTD on a daemon thread
        (reusing the :meth:`_set_fleet_motd` threading pattern). When not boss
        or not in a fleet, only the local builder is cleared."""
        self._clear_motd_builder()

        if not getattr(self, "_motd_is_boss", False) or not getattr(
                self, "_motd_fleet_id", None):
            self._motd_fleet_status.config(
                text="Cleared the local MOTD builder.", fg=FG_GREEN)
            return

        if not messagebox.askyesno(
                "Clear MOTD",
                "Also clear the current in-game fleet MOTD?"):
            self._motd_fleet_status.config(
                text="Cleared the local MOTD builder.", fg=FG_GREEN)
            return

        auth = self._motd_selected_fc_auth() or self.esi_auth
        if auth is None or not auth.is_authenticated:
            messagebox.showwarning("Clear MOTD",
                                   "No authenticated FC character selected.")
            return

        fleet_id = self._motd_fleet_id
        self._motd_fleet_status.config(text="Clearing in-game MOTD...",
                                       fg=FG_ACCENT)

        def worker():
            ok = False
            err = None
            try:
                ok = auth.set_fleet_motd(fleet_id, "")
            except Exception as e:
                err = str(e)
            self.root.after(0, _done, ok, err)

        def _done(ok, err):
            if ok:
                self._motd_fleet_status.config(
                    text="In-game MOTD cleared (204).", fg=FG_GREEN)
            else:
                detail = (f"\n\n{err}" if err else
                          "\n\nESI rejected the request (403 if you are no "
                          "longer the boss).")
                self._motd_fleet_status.config(
                    text="Failed to clear in-game MOTD.", fg=FG_RED)
                messagebox.showerror(
                    "Clear MOTD failed",
                    f"Could not clear the fleet MOTD.{detail}")

        threading.Thread(target=worker, daemon=True).start()

    def _apply_imported_motd(self, raw, err):
        """Tk-thread handler for an imported MOTD: clear the existing builder,
        load the imported MOTD as the sole content, then offer to create a new
        doctrine from it (importing its linked fits tagless + saving the MOTD as
        a doctrine-linked saved MOTD). Falls back to a plain fit-import offer
        when the user declines doctrine creation."""
        if err is not None or raw is None:
            self._motd_fleet_status.config(text="Import failed.", fg=FG_RED)
            messagebox.showerror("Import MOTD",
                                 err or "Could not load the current MOTD.")
            return

        parsed = motd_builder.parse_motd(raw)
        # Disambiguate the "imported but no fits" case: a non-empty MOTD that
        # yields zero fit links almost always means the fit markup was not
        # recognized (rather than the MOTD genuinely containing none). Flag it
        # with a warning colour and an explicit note so the user knows the import
        # itself worked even though no fits came across.
        if raw and not parsed["fittings"]:
            self._motd_fleet_status.config(
                text=f"Imported MOTD ({len(raw)} chars, 0 fit link(s)) "
                     f"— no fit links recognized in the MOTD markup.",
                fg=FG_ORANGE)
        else:
            self._motd_fleet_status.config(
                text=f"Imported MOTD ({len(raw)} chars, "
                     f"{len(parsed['fittings'])} fit link(s)).", fg=FG_GREEN)

        # Clear the existing builder FIRST so the imported MOTD replaces it
        # (the user reported the import being ADDED to the existing build), then
        # PARSE the imported MOTD into the editable fields (staging/channel/FC/
        # fits) rather than dumping the raw markup into the header. This keeps
        # the builder editable and means the saved linked MOTD captures the
        # imported staging/channel/fits instead of empty fields.
        self._clear_motd_builder()
        fittings = parsed.get("fittings") or []
        self._motd_populate_fields_from_parsed(parsed, fallback_fittings=fittings)
        self._rebuild_motd_preview()

        # Primary offer: build a new doctrine from this MOTD — import its linked
        # fits (tagless) and populate the builder. It does NOT save a linked
        # MOTD; the user creates those explicitly via "Link to doctrine".
        if messagebox.askyesno(
                "Create doctrine?",
                "Create a new doctrine from this MOTD and import its linked "
                "fits? (You can save it as an MOTD template afterwards via "
                "'Link to doctrine'.)"):
            result = self._create_doctrine_from_motd(raw, fittings)
            if result is not None:
                _did, name, added, reused, failed = result
                messagebox.showinfo(
                    "Doctrine created",
                    f"Created doctrine '{name}' with "
                    f"{added + reused} fit(s) ({added} new, {reused} reused"
                    + (f", {failed} unparsable" if failed else "")
                    + ").")
        # Fallback: still let the user import the embedded fits into the library
        # without creating a doctrine.
        elif fittings and messagebox.askyesno(
                "Import fits",
                f"Import {len(fittings)} fit link(s) from this MOTD into the "
                f"library? Duplicates are skipped automatically."):
            added, reused, failed = self._import_motd_fits(fittings)
            messagebox.showinfo(
                "Fit import",
                f"Imported {added} new fit(s); {reused} already in the "
                f"library; {failed} could not be parsed.")

        # Rebuild the preview from the populated fields as the LAST step. The
        # modal dialogs above run nested Tk event loops that can fire a
        # debounced preview rebuild mid-flow; a final clean rebuild (after any
        # doctrine creation has settled the fields) ensures the preview reflects
        # the final builder state rather than a stale intermediate.
        self._rebuild_motd_preview()

    def _import_motd_fits(self, fittings):
        """Import a list of ``{dna, name}`` dicts into the library, de-duped by
        content hash (reusing the Phase-5 ``_add_parsed_fit`` path). Returns
        ``(added, reused, failed)`` counts. Runs on the Tk thread."""
        added = reused = failed = 0
        existing_hashes = {}
        for f in self.fittings.list_fits():
            try:
                existing_hashes[fit_models.fit_content_hash(f.parsed)] = f.id
            except Exception:
                pass
        for entry in fittings:
            dna = entry.get("dna", "")
            name = entry.get("name") or "Imported Fit"
            if not dna:
                failed += 1
                continue
            try:
                result = fit_parser.parse_dna(dna, self.type_catalog)
                parsed = result.fit
                h = fit_models.fit_content_hash(parsed)
            except Exception:
                failed += 1
                continue
            if h in existing_hashes:
                reused += 1
                continue
            fid = self._add_parsed_fit(parsed, source="dna", raw_text=dna,
                                       name=name)
            if fid:
                existing_hashes[h] = fid
                added += 1
            else:
                failed += 1
        if added:
            # Refresh doctrine dropdown is unaffected, but new fits may change
            # the library view; the fittings sub-tab refreshes itself.
            self._motd_refresh_doctrines()
        return added, reused, failed

    # ── Fittings library sub-tab (Task 5.2) ───────────────────────────────────

    def _build_fittings_subtab(self):
        tab = tk.Frame(self._fitting_subnb, bg=BG_DARK)
        self._fitting_subnb.add(tab, text="  Fittings  ")

        # ── Toolbar: search + import buttons ─────────────────────────────────
        toolbar = tk.Frame(tab, bg=BG_DARK)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))

        tk.Label(toolbar, text="Search:", font=("Consolas", 10),
                 fg=FG_TEXT, bg=BG_DARK).pack(side=tk.LEFT)
        self._fit_search_var = tk.StringVar()
        self._fit_search_var.trace_add(
            "write", lambda *a: self._refresh_fit_list(self._fit_search_var.get()))
        search_entry = tk.Entry(
            toolbar, textvariable=self._fit_search_var, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE, width=26,
            borderwidth=1, relief=tk.RIDGE)
        search_entry.pack(side=tk.LEFT, padx=(5, 15))

        self._esi_import_btn = ttk.Button(
            toolbar, text="Import from EVE", style="Green.TButton",
            command=self._import_esi_fittings)
        self._esi_import_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Import from pyfa", style="Green.TButton",
                   command=self._import_pyfa).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Paste EFT/DNA", style="Green.TButton",
                   command=self._import_paste_fit).pack(side=tk.LEFT, padx=2)

        # ── Master / detail split ────────────────────────────────────────────
        body = tk.Frame(tab, bg=BG_DARK)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        body.columnconfigure(0, weight=3, uniform="fit")
        body.columnconfigure(1, weight=4, uniform="fit")
        body.rowconfigure(0, weight=1)

        # Left: fittings list (Treeview)
        left = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                        highlightbackground=BORDER_COLOR, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        tk.Label(left, text="LIBRARY", font=("Consolas", 9, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).grid(
                     row=0, column=0, sticky="w", padx=6, pady=(6, 2))

        tree_wrap = tk.Frame(left, bg=BG_PANEL)
        tree_wrap.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        columns = ("name", "hull", "tags", "doctrines")
        self._fit_tree = ttk.Treeview(
            tree_wrap, columns=columns, show="headings",
            style="Dark.Treeview", selectmode="browse")
        self._fit_tree.heading(
            "name", text="Name",
            command=lambda: self._on_fit_tree_sort("name"))
        self._fit_tree.heading(
            "hull", text="Hull",
            command=lambda: self._on_fit_tree_sort("hull"))
        self._fit_tree.heading(
            "tags", text="Tags",
            command=lambda: self._on_fit_tree_sort("tags"))
        self._fit_tree.heading(
            "doctrines", text="#Doc",
            command=lambda: self._on_fit_tree_sort("doctrines"))
        self._fit_tree.column("name", width=150, anchor=tk.W)
        self._fit_tree.column("hull", width=110, anchor=tk.W)
        self._fit_tree.column("tags", width=120, anchor=tk.W)
        self._fit_tree.column("doctrines", width=44, anchor=tk.CENTER, stretch=False)
        self._fit_tree.grid(row=0, column=0, sticky="nsew")
        self._fit_tree.bind("<<TreeviewSelect>>", self._on_fit_tree_select)

        tree_sb = ttk.Scrollbar(tree_wrap, orient="vertical",
                                command=self._fit_tree.yview)
        self._fit_tree.configure(yscrollcommand=tree_sb.set)
        tree_sb.grid(row=0, column=1, sticky="ns")

        # Right: fixed action bar (row 0) + scrollable detail (row 1)
        right = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.GROOVE,
                         highlightbackground=BORDER_COLOR, highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right.rowconfigure(0, weight=0)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # Fixed action-button bar — does NOT scroll, always visible at top.
        self._fit_actions = tk.Frame(right, bg=BG_PANEL)
        self._fit_actions.grid(row=0, column=0, columnspan=2, sticky="ew")

        detail_canvas = tk.Canvas(right, bg=BG_PANEL, highlightthickness=0)
        detail_canvas.grid(row=1, column=0, sticky="nsew")
        detail_sb = ttk.Scrollbar(right, orient="vertical",
                                  command=detail_canvas.yview)
        detail_sb.grid(row=1, column=1, sticky="ns")
        detail_canvas.configure(yscrollcommand=detail_sb.set)
        self._register_scroll_canvas(detail_canvas)

        self._fit_detail = tk.Frame(detail_canvas, bg=BG_PANEL)
        _detail_win = detail_canvas.create_window(
            (0, 0), window=self._fit_detail, anchor="nw")

        def _on_detail_config(event=None):
            detail_canvas.configure(scrollregion=detail_canvas.bbox("all"))
        self._fit_detail.bind("<Configure>", _on_detail_config)

        def _on_canvas_config(event):
            detail_canvas.itemconfig(_detail_win, width=event.width)
        detail_canvas.bind("<Configure>", _on_canvas_config)

        # Populate.
        self._refresh_fit_list()
        self._show_fit_detail(None)

    # ── Fittings library controllers (Task 5.2) ───────────────────────────────

    def _doctrine_count_for_fit(self, fit_id: str) -> int:
        """How many doctrines reference this fit (for the list's #Doc column)."""
        count = 0
        try:
            for doc in self.fittings.list_doctrines():
                if any(m.fit_id == fit_id for m in doc.members):
                    count += 1
        except Exception:
            pass
        return count

    def _fit_member_tags(self, fit_id: str) -> list[str]:
        """Union of this fit's per-membership tags across all doctrines."""
        tags: list[str] = []
        try:
            for doc in self.fittings.list_doctrines():
                for m in doc.members:
                    if m.fit_id == fit_id:
                        for t in m.tags:
                            if t not in tags:
                                tags.append(t)
        except Exception:
            pass
        return tags

    # Base (undecorated) heading text per sortable column.
    _FIT_HEADINGS = {
        "name": "Name",
        "hull": "Hull",
        "tags": "Tags",
        "doctrines": "#Doc",
    }

    def _update_fit_headings(self):
        """Refresh column heading text so the active sort column shows a
        ▲/▼ direction marker and the others show their plain label."""
        tree = getattr(self, "_fit_tree", None)
        if tree is None:
            return
        arrow = " ▼" if self._fit_sort_reverse else " ▲"
        for col, base in self._FIT_HEADINGS.items():
            text = base + arrow if col == self._fit_sort_column else base
            tree.heading(col, text=text)

    def _on_fit_tree_sort(self, col: str):
        """Header click: sort by ``col`` ascending, or flip direction if it is
        already the active column. Re-renders via _refresh_fit_list."""
        if col == self._fit_sort_column:
            self._fit_sort_reverse = not self._fit_sort_reverse
        else:
            self._fit_sort_column = col
            self._fit_sort_reverse = False
        self._refresh_fit_list(self._fit_search_var.get())

    def _refresh_fit_list(self, filter_text: str = ""):
        """Clear + repopulate the fittings Treeview, filtering case-insensitively
        on name / hull / tags and sorting by the active column (click-to-sort).
        Preserves the current selection when possible."""
        tree = getattr(self, "_fit_tree", None)
        if tree is None:
            return
        prev = self._fit_selected_id
        for iid in tree.get_children():
            tree.delete(iid)
        needle = (filter_text or "").strip().lower()

        # Build the (filtered) row set with the values each column sorts on.
        rows = []
        for fit in self.fittings.list_fits():
            tags = self._fit_member_tags(fit.id)
            tags_str = ", ".join(tags)
            hay = " ".join([fit.name or "", fit.hull_name or "", tags_str]).lower()
            if needle and needle not in hay:
                continue
            n_doc = self._doctrine_count_for_fit(fit.id)
            rows.append((fit, tags_str, n_doc))

        # Sort by the active column. #Doc is a NUMERIC sort; the rest are
        # case-insensitive string sorts. Tie-break on name for stable ordering.
        col = self._fit_sort_column
        if col == "hull":
            key = lambda r: ((r[0].hull_name or "").lower(),
                             (r[0].name or "").lower())
        elif col == "tags":
            key = lambda r: (r[1].lower(), (r[0].name or "").lower())
        elif col == "doctrines":
            key = lambda r: (r[2], (r[0].name or "").lower())
        else:  # "name"
            key = lambda r: (r[0].name or "").lower()
        rows.sort(key=key, reverse=self._fit_sort_reverse)

        restored = False
        for fit, tags_str, n_doc in rows:
            tree.insert("", tk.END, iid=fit.id,
                        values=(fit.name, fit.hull_name, tags_str, n_doc))
            if fit.id == prev:
                restored = True

        self._update_fit_headings()

        if restored:
            tree.selection_set(prev)
        elif prev is not None and self.fittings.get_fit(prev) is None:
            # Selected fit was deleted/filtered away — clear the detail pane.
            self._fit_selected_id = None
            self._show_fit_detail(None)

    def _on_fit_tree_select(self, event=None):
        tree = self._fit_tree
        sel = tree.selection()
        if not sel:
            return
        self._fit_selected_id = sel[0]
        self._show_fit_detail(sel[0])

    def _clear_fit_detail(self):
        for w in self._fit_detail.winfo_children():
            w.destroy()
        # Also clear the fixed action bar so stale buttons don't linger.
        actions = getattr(self, "_fit_actions", None)
        if actions is not None:
            for w in actions.winfo_children():
                w.destroy()

    def _show_fit_detail(self, fit_id):
        """Render the selected fit: hull/name header, slot-grouped read-only
        module list, drones/cargo, notes, doctrine membership, and actions."""
        self._clear_fit_detail()
        parent = self._fit_detail

        if not fit_id:
            tk.Label(parent, text="Select a fitting to view details.",
                     font=("Consolas", 10), fg=FG_DIM, bg=BG_PANEL,
                     wraplength=360, justify=tk.LEFT).pack(
                         anchor=tk.W, padx=10, pady=10)
            return
        fit = self.fittings.get_fit(fit_id)
        if fit is None:
            tk.Label(parent, text="Fitting not found.",
                     font=("Consolas", 10), fg=FG_RED, bg=BG_PANEL).pack(
                         anchor=tk.W, padx=10, pady=10)
            return

        # Fixed action bar (top, non-scrolling): 8 buttons in a wrapped grid
        # (2 rows × 4 columns) with uniform column weights so they size evenly.
        actions = self._fit_actions
        for c in range(4):
            actions.grid_columnconfigure(c, weight=1, uniform="fitbtn")
        action_specs = [
            ("Rename", "Dark.TButton", lambda: self._rename_fit(fit.id)),
            ("Edit notes", "Dark.TButton", lambda: self._edit_fit_notes(fit.id)),
            ("Replace fit text", "Dark.TButton",
             lambda: self._replace_fit_text(fit.id)),
            ("Copy EFT", "Dark.TButton", lambda: self._copy_fit_eft(fit.id)),
            ("Copy DNA", "Dark.TButton", lambda: self._copy_fit_dna(fit.id)),
            ("Save to in-game", "Dark.TButton",
             lambda: self._save_fit_to_ingame(fit.id)),
            ("Add to doctrine…", "Dark.TButton",
             lambda: self._add_fit_to_doctrine_from_fit(fit.id)),
            ("Delete", "Red.TButton", lambda: self._delete_fit(fit.id)),
        ]
        for idx, (label, style, cmd) in enumerate(action_specs):
            ttk.Button(actions, text=label, style=style, command=cmd).grid(
                row=idx // 4, column=idx % 4, sticky="ew", padx=2, pady=3)

        # Header: name + hull + source.
        tk.Label(parent, text=fit.name, font=("Consolas", 13, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL, anchor=tk.W, justify=tk.LEFT,
                 wraplength=380).pack(anchor=tk.W, padx=10, pady=(10, 0))
        tk.Label(parent, text=f"{fit.hull_name}  ·  source: {fit.source}",
                 font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL).pack(
                     anchor=tk.W, padx=10, pady=(0, 6))

        # Slot-grouped module list.
        parsed = fit.parsed
        by_slot: dict[str, list] = {}
        for m in parsed.modules:
            by_slot.setdefault(m.slot or "other", []).append(m)
        sub_names = [self.type_catalog.resolve_name(t) or f"Type {t}"
                     for t in (parsed.subsystems or [])]
        for slot in self._FIT_SLOT_ORDER:
            mods = by_slot.get(slot) or []
            extra = sub_names if slot == "subsystem" else []
            if not mods and not extra:
                continue
            tk.Label(parent, text=self._FIT_SLOT_LABELS.get(slot, slot.title()),
                     font=("Consolas", 9, "bold"), fg=FG_GREEN, bg=BG_PANEL
                     ).pack(anchor=tk.W, padx=12, pady=(6, 0))
            for m in mods:
                line = m.name or f"Type {m.type_id}"
                if m.charge_name:
                    line += f", {m.charge_name}"
                if m.offline:
                    line += " /offline"
                tk.Label(parent, text=f"  {line}", font=("Consolas", 9),
                         fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W, justify=tk.LEFT,
                         wraplength=380).pack(anchor=tk.W, padx=14)
            for nm in extra:
                tk.Label(parent, text=f"  {nm}", font=("Consolas", 9),
                         fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W, justify=tk.LEFT,
                         wraplength=380).pack(anchor=tk.W, padx=14)
        # Any modules with an unrecognized slot bucket.
        other_mods = [m for s, ms in by_slot.items()
                      if s not in self._FIT_SLOT_ORDER for m in ms]
        if other_mods:
            tk.Label(parent, text="Other", font=("Consolas", 9, "bold"),
                     fg=FG_GREEN, bg=BG_PANEL).pack(anchor=tk.W, padx=12, pady=(6, 0))
            for m in other_mods:
                tk.Label(parent, text=f"  {m.name or m.type_id}",
                         font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                         anchor=tk.W).pack(anchor=tk.W, padx=14)

        if parsed.drones:
            tk.Label(parent, text="Drones", font=("Consolas", 9, "bold"),
                     fg=FG_GREEN, bg=BG_PANEL).pack(anchor=tk.W, padx=12, pady=(6, 0))
            for d in parsed.drones:
                tk.Label(parent, text=f"  {d.name or d.type_id} x{d.quantity}",
                         font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                         anchor=tk.W).pack(anchor=tk.W, padx=14)
        if parsed.cargo:
            tk.Label(parent, text="Cargo", font=("Consolas", 9, "bold"),
                     fg=FG_GREEN, bg=BG_PANEL).pack(anchor=tk.W, padx=12, pady=(6, 0))
            for c in parsed.cargo:
                tk.Label(parent, text=f"  {c.name or c.type_id} x{c.quantity}",
                         font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                         anchor=tk.W).pack(anchor=tk.W, padx=14)

        # Notes.
        if (fit.notes or "").strip():
            tk.Label(parent, text="Notes", font=("Consolas", 9, "bold"),
                     fg=FG_GREEN, bg=BG_PANEL).pack(anchor=tk.W, padx=12, pady=(8, 0))
            tk.Label(parent, text=fit.notes, font=("Consolas", 9),
                     fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W, justify=tk.LEFT,
                     wraplength=380).pack(anchor=tk.W, padx=14)

        # Doctrine membership.
        member_docs = []
        for doc in self.fittings.list_doctrines():
            mem = next((m for m in doc.members if m.fit_id == fit.id), None)
            if mem is not None:
                member_docs.append((doc.name, mem.tags))
        tk.Label(parent, text="Doctrines", font=("Consolas", 9, "bold"),
                 fg=FG_GREEN, bg=BG_PANEL).pack(anchor=tk.W, padx=12, pady=(8, 0))
        if member_docs:
            for dname, dtags in member_docs:
                tag_txt = f" [{', '.join(dtags)}]" if dtags else ""
                tk.Label(parent, text=f"  {dname}{tag_txt}",
                         font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                         anchor=tk.W, wraplength=380, justify=tk.LEFT).pack(
                             anchor=tk.W, padx=14)
        else:
            tk.Label(parent, text="  (not in any doctrine)",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL).pack(
                         anchor=tk.W, padx=14)

    def _rename_fit(self, fit_id):
        fit = self.fittings.get_fit(fit_id)
        if fit is None:
            return
        new_name = self._prompt_text_line("Rename Fitting", "Name:", fit.name)
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name or new_name == fit.name:
            return
        fit.name = new_name
        self.fittings.update_fit(fit)
        self.fittings.save()
        self._refresh_fit_list(self._fit_search_var.get())
        self._show_fit_detail(fit_id)

    def _edit_fit_notes(self, fit_id):
        fit = self.fittings.get_fit(fit_id)
        if fit is None:
            return
        new_notes = self._prompt_text_block(
            "Edit Notes", "Notes for this fit:", fit.notes or "")
        if new_notes is None:
            return
        fit.notes = new_notes
        self.fittings.update_fit(fit)
        self.fittings.save()
        self._show_fit_detail(fit_id)

    def _replace_fit_text(self, fit_id):
        """Re-paste the fit body; keep id/name/membership, rebuild parsed/dna/raw."""
        fit = self.fittings.get_fit(fit_id)
        if fit is None:
            return

        def _on_parsed(parse_result, source, raw_text):
            warnings = list(parse_result.warnings)
            parsed = parse_result.fit
            try:
                dna = fit_dna.to_dna(parsed, self.type_catalog)
            except Exception:
                dna = fit.dna
            fit.source = source
            fit.raw_text = raw_text
            fit.parsed = parsed
            fit.dna = dna
            fit.hull_type_id = parsed.ship_type_id
            fit.hull_name = parsed.ship_name or fit.hull_name
            self.fittings.update_fit(fit)
            self.fittings.save()
            self._refresh_fit_list(self._fit_search_var.get())
            self._show_fit_detail(fit_id)
            if warnings:
                messagebox.showwarning(
                    "Replaced with warnings",
                    "Fit replaced. Some items were not recognized:\n\n"
                    + "\n".join(warnings[:12]))

        self._open_paste_dialog(
            title="Replace Fit Text",
            instruction="Paste the new EFT or DNA for this fit:",
            on_success=_on_parsed)

    def _copy_fit_eft(self, fit_id):
        fit = self.fittings.get_fit(fit_id)
        if fit is None:
            return
        text = fit.raw_text if (fit.raw_text or "").strip() else self._render_eft(fit)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _copy_fit_dna(self, fit_id):
        fit = self.fittings.get_fit(fit_id)
        if fit is None:
            return
        dna = fit.dna
        if not dna:
            try:
                dna = fit_dna.to_dna(fit.parsed, self.type_catalog)
            except Exception:
                dna = ""
        self.root.clipboard_clear()
        self.root.clipboard_append(dna)

    def _render_eft(self, fit) -> str:
        """Re-emit a minimal EFT block from parsed contents (fallback when a fit
        has no raw_text, e.g. ESI/DNA-sourced). Modules are grouped by slot in
        the canonical order; charges and drones/cargo use the ` xN` form."""
        parsed = fit.parsed
        lines = [f"[{fit.hull_name}, {fit.name}]"]
        by_slot: dict[str, list] = {}
        for m in parsed.modules:
            by_slot.setdefault(m.slot or "other", []).append(m)
        sub_names = [self.type_catalog.resolve_name(t) or f"Type {t}"
                     for t in (parsed.subsystems or [])]
        first = True
        for slot in self._FIT_SLOT_ORDER:
            mods = by_slot.get(slot) or []
            extra = sub_names if slot == "subsystem" else []
            if not mods and not extra:
                continue
            if not first:
                lines.append("")
            first = False
            for m in mods:
                line = m.name or f"Type {m.type_id}"
                if m.charge_name:
                    line += f", {m.charge_name}"
                if m.offline:
                    line += " /offline"
                lines.append(line)
            for nm in extra:
                lines.append(nm)
        if parsed.drones:
            lines.append("")
            for d in parsed.drones:
                lines.append(f"{d.name or d.type_id} x{d.quantity}")
        if parsed.cargo:
            lines.append("")
            for c in parsed.cargo:
                lines.append(f"{c.name or c.type_id} x{c.quantity}")
        return "\n".join(lines)

    def _delete_fit(self, fit_id):
        fit = self.fittings.get_fit(fit_id)
        if fit is None:
            return
        if not messagebox.askyesno(
                "Delete Fitting",
                f"Delete '{fit.name}'?\n\nThis also removes it from any doctrine."):
            return
        self.fittings.delete_fit(fit_id)   # cascades doctrine membership
        self.fittings.save()
        if self._fit_selected_id == fit_id:
            self._fit_selected_id = None
        self._refresh_fit_list(self._fit_search_var.get())
        self._show_fit_detail(None)

    def _save_fit_to_ingame(self, fit_id):
        """Push a fit to the primary character's in-game Fittings via ESI."""
        fit = self.fittings.get_fit(fit_id)
        if fit is None:
            return
        char = self.esi_auth
        if char is None or not char.character_id:
            messagebox.showwarning(
                "No character",
                "Connect a character (Characters tab) before saving to in-game "
                "Fittings.")
            return
        if not char.has_scope(SCOPE_FITTINGS_WRITE):
            messagebox.showwarning(
                "Re-authorize required",
                f"{char.character_name or 'This character'} was authorized "
                "before in-game fittings support was added, so it cannot save "
                "fits to its in-game Fittings yet.\n\nOpen the Characters or "
                "Settings tab and click \"Re-authorize\" for this character, "
                "then try again.")
            return
        self._push_fit_to_eve(fit, char)

    # ── Shared dialogs / prompts ──────────────────────────────────────────────

    def _prompt_text_line(self, title, label, initial=""):
        """A small modal single-line text prompt. Returns the string or None."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG_DARK)
        win.geometry("420x130")
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass
        result = {"value": None}

        tk.Label(win, text=label, font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).pack(anchor=tk.W, padx=12, pady=(12, 2))
        var = tk.StringVar(value=initial)
        entry = tk.Entry(win, textvariable=var, font=("Consolas", 10),
                         bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                         width=46, borderwidth=1, relief=tk.RIDGE)
        entry.pack(fill=tk.X, padx=12)
        entry.focus_set()
        entry.icursor(tk.END)

        def _ok():
            result["value"] = var.get()
            win.destroy()

        def _cancel():
            win.destroy()

        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btns, text="OK", style="Green.TButton",
                   command=_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=_cancel).pack(side=tk.RIGHT)
        entry.bind("<Return>", lambda e: _ok())
        win.bind("<Escape>", lambda e: _cancel())
        win.protocol("WM_DELETE_WINDOW", _cancel)
        self.root.wait_window(win)
        return result["value"]

    def _prompt_pick_from_list(self, title, label, options):
        """A modal single-choice picker over ``options`` (a list of strings) with
        a type-to-filter box. Returns the chosen string, or None if cancelled or
        the list is empty. Used by the exemptions editor's group/type add flows."""
        options = list(options or [])
        if not options:
            messagebox.showinfo(title, "Nothing to choose from.")
            return None
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG_DARK)
        win.geometry("380x440")
        win.minsize(340, 320)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass
        result = {"value": None}

        tk.Label(win, text=label, font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK, anchor=tk.W, justify=tk.LEFT, wraplength=350).pack(
                     anchor=tk.W, padx=12, pady=(12, 2))
        filt = tk.StringVar()
        entry = tk.Entry(win, textvariable=filt, font=("Consolas", 10),
                         bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                         borderwidth=1, relief=tk.RIDGE)
        entry.pack(fill=tk.X, padx=12, pady=(0, 4))
        entry.focus_set()

        lb = tk.Listbox(win, font=("Consolas", 9), bg=BG_PANEL, fg=FG_TEXT,
                        selectbackground=FG_ACCENT, selectforeground=BG_DARK,
                        highlightthickness=0, activestyle="none")

        def _repopulate(*_a):
            needle = (filt.get() or "").strip().lower()
            lb.delete(0, tk.END)
            for opt in options:
                if not needle or needle in opt.lower():
                    lb.insert(tk.END, opt)
            if lb.size():
                lb.selection_set(0)
        filt.trace_add("write", _repopulate)

        def _ok(*_a):
            sel = lb.curselection()
            if sel:
                result["value"] = lb.get(sel[0])
            win.destroy()

        def _cancel(*_a):
            win.destroy()

        btns = tk.Frame(win, bg=BG_DARK)
        ttk.Button(btns, text="OK", style="Green.TButton",
                   command=_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=_cancel).pack(side=tk.RIGHT)
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=12)
        lb.pack(fill=tk.BOTH, expand=True, padx=12)
        _repopulate()
        lb.bind("<Double-Button-1>", _ok)
        entry.bind("<Return>", _ok)
        win.bind("<Escape>", _cancel)
        win.protocol("WM_DELETE_WINDOW", _cancel)
        self.root.wait_window(win)
        return result["value"]

    def _prompt_text_block(self, title, label, initial=""):
        """A modal multi-line text prompt. Returns the string or None."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG_DARK)
        win.geometry("520x320")
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass
        result = {"value": None}

        tk.Label(win, text=label, font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).pack(anchor=tk.W, padx=12, pady=(12, 2))
        txt = scrolledtext.ScrolledText(
            win, font=("Consolas", 10), bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, height=10, wrap=tk.WORD,
            borderwidth=1, relief=tk.RIDGE)
        txt.pack(fill=tk.BOTH, expand=True, padx=12)
        self._theme_scrolledtext_bar(txt)
        txt.insert("1.0", initial)
        txt.focus_set()

        def _ok():
            result["value"] = txt.get("1.0", tk.END).rstrip("\n")
            win.destroy()

        def _cancel():
            win.destroy()

        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btns, text="Save", style="Green.TButton",
                   command=_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=_cancel).pack(side=tk.RIGHT)
        win.bind("<Escape>", lambda e: _cancel())
        win.protocol("WM_DELETE_WINDOW", _cancel)
        self.root.wait_window(win)
        return result["value"]

    def _open_paste_dialog(self, title, instruction, on_success):
        """Open a paste-fit modal: a Text box + status label. On submit, parse on
        a daemon thread via fit_parser.detect_and_parse and marshal the result
        back to the Tk thread. `on_success(parse_result, source, raw_text)` runs
        on the Tk thread when parsing yields a valid hull. Parse failures show a
        status message and never close the dialog."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG_DARK)
        win.geometry("560x420")
        try:
            win.transient(self.root)
        except tk.TclError:
            pass

        tk.Label(win, text=instruction, font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).pack(anchor=tk.W, padx=12, pady=(12, 4))
        txt = scrolledtext.ScrolledText(
            win, font=("Consolas", 9), bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, height=16, wrap=tk.NONE,
            borderwidth=1, relief=tk.RIDGE)
        txt.pack(fill=tk.BOTH, expand=True, padx=12)
        self._theme_scrolledtext_bar(txt)
        txt.focus_set()

        status = tk.Label(win, text="", font=("Consolas", 9), fg=FG_DIM,
                          bg=BG_DARK, anchor=tk.W, justify=tk.LEFT, wraplength=520)
        status.pack(fill=tk.X, padx=12, pady=(4, 0))

        state = {"busy": False}

        def _submit():
            if state["busy"]:
                return
            raw_text = txt.get("1.0", tk.END).rstrip("\n")
            if not raw_text.strip():
                status.config(text="Paste a fit first.", fg=FG_ORANGE)
                return
            state["busy"] = True
            status.config(text="Parsing...", fg=FG_ACCENT)

            def worker():
                try:
                    result = fit_parser.detect_and_parse(
                        raw_text, self.type_catalog)
                    err = None
                except fit_parser.FitParseError as e:
                    result, err = None, str(getattr(e, "message", e) or e)
                except Exception as e:  # pragma: no cover - defensive
                    result, err = None, str(e)
                self.root.after(0, _apply, result, err, raw_text)

            threading.Thread(target=worker, daemon=True).start()

        def _apply(result, err, raw_text):
            state["busy"] = False
            if err is not None or result is None:
                status.config(text=f"Could not parse: {err}", fg=FG_RED)
                return
            # Detect source from the text shape (matches detect_and_parse).
            source = "dna" if self._looks_like_dna(raw_text) else "eft"
            try:
                on_success(result, source, raw_text)
            finally:
                win.destroy()

        def _cancel():
            win.destroy()

        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btns, text="Import", style="Green.TButton",
                   command=_submit).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=_cancel).pack(side=tk.RIGHT)
        win.bind("<Escape>", lambda e: _cancel())
        win.protocol("WM_DELETE_WINDOW", _cancel)

    @staticmethod
    def _looks_like_dna(text: str) -> bool:
        """Heuristic mirroring fit_parser.detect_and_parse: DNA has no '['
        header and matches the leading numeric-id grammar."""
        stripped = (text or "").lstrip("﻿").strip()
        if not stripped or stripped.startswith("["):
            return False
        return bool(re.match(r"^\d+(:[\d;_]*)*::", stripped))

    # ── Import dialogs (Task 5.3) ──────────────────────────────────────────────

    def _import_paste_fit(self):
        """Paste EFT/DNA -> detect_and_parse (threaded) -> new Fit -> add."""
        def _on_parsed(parse_result, source, raw_text):
            warnings = list(parse_result.warnings)
            parsed = parse_result.fit
            name = parsed.name_hint or self._prompt_text_line(
                "Name Fitting", "Name for this fit:",
                parsed.ship_name or "New Fit")
            if name is None:
                return
            name = (name or "").strip() or (parsed.ship_name or "New Fit")
            self._add_parsed_fit(parsed, source=source, raw_text=raw_text,
                                 name=name)
            if warnings:
                messagebox.showwarning(
                    "Imported with warnings",
                    "Fit imported. Some items were not recognized:\n\n"
                    + "\n".join(warnings[:12]))

        self._open_paste_dialog(
            title="Paste EFT / DNA",
            instruction="Paste an EFT block or a fitting DNA string:",
            on_success=_on_parsed)

    def _add_parsed_fit(self, parsed, source, raw_text, name, notes=""):
        """Build a fit_models.Fit from a ParsedFit and add it to the library.
        Returns the new fit id (or None on failure). Runs on the Tk thread."""
        try:
            dna = fit_dna.to_dna(parsed, self.type_catalog)
        except Exception:
            dna = ""
        fit = fit_models.Fit(
            id="",                                  # assigned by add_fit
            name=name,
            hull_type_id=parsed.ship_type_id,
            hull_name=parsed.ship_name or "",
            source=source,
            raw_text=raw_text,
            parsed=parsed,
            dna=dna,
            notes=notes,
            esi_fitting_ids={},
            created="",                             # stamped by the store
            modified="",
        )
        try:
            fid = self.fittings.add_fit(fit)
            self.fittings.save()
        except Exception as e:
            messagebox.showerror("Import failed", f"Could not add fit:\n{e}")
            return None
        self._fit_selected_id = fid
        self._refresh_fit_list(self._fit_search_var.get())
        self._show_fit_detail(fid)
        return fid

    def _import_pyfa(self):
        """Import from a pyfa saveddata.db (browse-by-ship+name), falling back to
        an EFT-text export file when the DB is missing/unreadable."""
        cfg = self.config.get("fittings", {})
        start = cfg.get("pyfa_path") or None
        self._pyfa_status_win = None

        def worker():
            db_path = None
            fits = None
            err = None
            try:
                db_path = pyfa_import.find_pyfa_db(start)
                if db_path:
                    fits = pyfa_import.list_pyfa_fits(db_path)
            except pyfa_import.PyfaImportError as e:
                err = str(e)
            except Exception as e:
                err = str(e)
            self.root.after(0, _apply, db_path, fits, err)

        def _apply(db_path, fits, err):
            if db_path and fits is not None:
                # Persist the directory we read from.
                try:
                    cfg2 = self.config.setdefault("fittings", {})
                    cfg2["pyfa_path"] = os.path.dirname(db_path)
                    self._save_config()
                except Exception:
                    pass
                if not fits:
                    messagebox.showinfo(
                        "pyfa import",
                        "The pyfa database has no saved fits.")
                    return
                self._show_pyfa_picker(db_path, fits)
            else:
                # No DB found/readable. First offer to locate it, since users
                # who ran pyfa with -s/--savepath keep saveddata.db elsewhere.
                locate = messagebox.askyesno(
                    "pyfa not found",
                    "Couldn't find pyfa's saved fits (saveddata.db). "
                    "Locate it now?\n\n"
                    "pyfa stores all fits in a single file 'saveddata.db', by "
                    "default in your user folder under .pyfa "
                    "(Windows: %USERPROFILE%\\.pyfa). If you used pyfa's "
                    "-s/--savepath option it may be elsewhere.")
                if locate:
                    self._set_pyfa_folder()
                    return
                # Otherwise fall back to an EFT-text export file.
                msg = ("Could not read a pyfa database"
                       + (f" ({err})" if err else "")
                       + ".\n\nChoose an EFT-text export file (.txt/.cfg) "
                         "to import instead.")
                messagebox.showinfo("pyfa import", msg)
                self._import_eft_text_file()

        threading.Thread(target=worker, daemon=True).start()

    def _set_pyfa_folder(self):
        """Let the user point FCTool at a pyfa saveddata.db (for non-default
        install/savepath locations), persist its directory, then re-list."""
        path = filedialog.askopenfilename(
            title="Locate your pyfa saveddata.db",
            filetypes=[("pyfa database", "saveddata.db"),
                       ("SQLite db", "*.db"),
                       ("All files", "*.*")])
        if not path:
            return
        cfg = self.config.setdefault("fittings", {})
        cfg["pyfa_path"] = os.path.dirname(path)
        self._save_config()
        self._import_pyfa()

    def _import_eft_text_file(self):
        """Fallback path: pick an EFT-text export file and parse it."""
        path = filedialog.askopenfilename(
            title="Select EFT text export",
            filetypes=[("EFT/text exports", "*.txt *.cfg"), ("All files", "*.*")])
        if not path:
            return

        def worker():
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw_text = f.read()
                result = fit_parser.detect_and_parse(raw_text, self.type_catalog)
                err = None
            except Exception as e:
                result, raw_text, err = None, "", str(e)
            self.root.after(0, _apply, result, raw_text, err)

        def _apply(result, raw_text, err):
            if err is not None or result is None:
                messagebox.showerror("Import failed",
                                     f"Could not parse the file:\n{err}")
                return
            parsed = result.fit
            name = parsed.name_hint or (parsed.ship_name or "Imported Fit")
            self._add_parsed_fit(parsed, source="eft", raw_text=raw_text,
                                 name=name)
            if result.warnings:
                messagebox.showwarning(
                    "Imported with warnings",
                    "Fit imported. Some items were not recognized:\n\n"
                    + "\n".join(result.warnings[:12]))

        threading.Thread(target=worker, daemon=True).start()

    def _show_multi_select_picker(self, items, on_import, *, title,
                                  window_size=(540, 600), extra_buttons=None):
        """Generic searchable + multi-select checkbox picker.

        Shared by the pyfa and ESI import flows so both have the identical
        layout: a search box that filters which rows are *visible* without
        touching checked state, a toolbar with a live "(N selected)" count and
        Select all/none (over the visible/filtered rows), a scrollable list of
        one tk.Checkbutton per item (persistent BooleanVar keyed by item id,
        default UNCHECKED), and a bottom row of Import selected / Import all /
        Cancel.

        Parameters
        ----------
        items : list[dict]
            Each dict is ``{"id": <unique hashable>, "label": <display str>,
            "row_data": <opaque>}``. The label already encodes ship + name and
            is what search matches against (lowercased).
        on_import : callable
            ``on_import(chosen_items, ctl)`` is invoked when the user clicks
            Import selected (the checked items) or Import all (every item).
            ``ctl`` is a tiny controller the callback uses to drive progress
            and closing; the callback owns its own threading and MUST call
            ``ctl.close()`` when finished.
        title : str
            Window title (also used to label the picker).
        window_size : (int, int)
            Initial window geometry.
        extra_buttons : list[(str, callable)] | None
            Optional ``(label, command)`` tuples appended to the toolbar. Each
            command is called with the controller, e.g.
            ``lambda ctl: (ctl.close(), self._set_pyfa_folder())``.
        """
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG_DARK)
        win.geometry(f"{window_size[0]}x{window_size[1]}")
        try:
            win.transient(self.root)
        except tk.TclError:
            pass

        # Persistent checked-state vars keyed by item id, so filtering never
        # drops a selection. Built once and reused for the window's lifetime.
        check_vars: dict = {}
        for it in items:
            check_vars[it["id"]] = tk.BooleanVar(value=False)

        tk.Label(win,
                 text="Select fits to import (search by ship or fit name):",
                 font=("Consolas", 10),
                 fg=FG_TEXT, bg=BG_DARK).pack(anchor=tk.W, padx=12, pady=(12, 2))
        search_var = tk.StringVar()
        search = tk.Entry(win, textvariable=search_var, font=("Consolas", 10),
                          bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                          borderwidth=1, relief=tk.RIDGE)
        search.pack(fill=tk.X, padx=12, pady=(0, 4))

        # Toolbar: select-all/none (over the *visible* rows) + a live count.
        toolbar = tk.Frame(win, bg=BG_DARK)
        toolbar.pack(fill=tk.X, padx=12, pady=(0, 4))
        count_label = tk.Label(toolbar, text="(0 selected)", font=("Consolas", 9),
                               fg=FG_DIM, bg=BG_DARK, anchor=tk.E)
        count_label.pack(side=tk.RIGHT)

        # Bottom controls are packed FIRST so the expanding list never squeezes
        # them off-screen (Tk pack gives prior siblings their space first).
        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=12)
        status = tk.Label(win, text="", font=("Consolas", 9), fg=FG_DIM,
                          bg=BG_DARK, anchor=tk.W)
        status.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(4, 0))

        # Scrollable frame of checkbuttons (reuse the canvas+scrollbar pattern).
        body = tk.Frame(win, bg=BG_DARK)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(body, bg=BG_PANEL, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))
        sb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        sb.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        canvas.configure(yscrollcommand=sb.set)
        inner = tk.Frame(canvas, bg=BG_PANEL)
        _win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(_win_id, width=e.width))
        self._register_scroll_canvas(canvas)

        def _update_count(*_a):
            n = sum(1 for v in check_vars.values() if v.get())
            count_label.config(text=f"({n} selected)")
            import_sel_btn.config(text=f"Import selected ({n})")

        # Build every checkbutton once; filtering only re-packs rows whose
        # visibility actually changes. Each row carries a precomputed lowercased
        # label (so the filter never re-lowercases per keystroke) and a "shown"
        # flag tracking whether it is currently packed.
        row_widgets: list = []   # list of dicts: {item, cb, label_lc, shown}
        for it in items:
            cb = tk.Checkbutton(
                inner, text=it["label"], variable=check_vars[it["id"]],
                font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                selectcolor=BG_ENTRY, activebackground=BG_PANEL,
                activeforeground=FG_WHITE, anchor=tk.W, highlightthickness=0,
                command=_update_count)
            row_widgets.append({
                "item": it,
                "cb": cb,
                "label_lc": it["label"].lower(),
                "shown": False,
            })

        # Track which items are currently visible (for select-all over filter).
        visible_items: list = []
        # Pending debounce after-id so rapid keystrokes coalesce into one filter.
        filter_after = {"id": None}

        def _apply_filter():
            """Show only rows matching the needle. Incremental: a row is only
            (re-)packed or hidden when its visibility actually flips, so a row
            that stays visible/hidden is left untouched (no churn). Checked
            state lives in persistent BooleanVars, untouched here."""
            needle = search_var.get().strip().lower()
            visible_items.clear()
            for row in row_widgets:
                should_show = (not needle) or (needle in row["label_lc"])
                if should_show:
                    visible_items.append(row["item"])
                    if not row["shown"]:
                        row["cb"].pack(fill=tk.X, anchor=tk.W, padx=4, pady=1)
                        row["shown"] = True
                elif row["shown"]:
                    row["cb"].pack_forget()
                    row["shown"] = False
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _schedule_filter(*_a):
            """Debounce: run _apply_filter ~180ms after the last keystroke,
            cancelling any previously-scheduled run so it fires once."""
            if filter_after["id"] is not None:
                try:
                    self.root.after_cancel(filter_after["id"])
                except (tk.TclError, ValueError):
                    pass
            filter_after["id"] = self.root.after(180, _run_scheduled_filter)

        def _run_scheduled_filter():
            filter_after["id"] = None
            # The window may have been destroyed between scheduling and firing
            # (e.g. user typed then cancelled); touching dead widgets is a no-op.
            try:
                _apply_filter()
            except tk.TclError:
                pass
        search_var.trace_add("write", _schedule_filter)

        def _select_all():
            for it in visible_items:
                check_vars[it["id"]].set(True)
            _update_count()

        def _select_none():
            for v in check_vars.values():
                v.set(False)
            _update_count()

        ttk.Button(toolbar, text="Select all", style="Dark.TButton",
                   command=_select_all).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Select none", style="Dark.TButton",
                   command=_select_none).pack(side=tk.LEFT, padx=4)

        # ── Controller handed to the import callback ──────────────────────────
        class _Controller:
            """Minimal surface the callback drives during import."""

            def __init__(self, app, window, status_label, action_btns):
                self.root = app.root
                self._window = window
                self._status = status_label
                self._action_btns = action_btns

            def set_status(self, text):
                try:
                    self._status.config(text=text, fg=FG_ACCENT)
                except tk.TclError:
                    pass

            def disable(self):
                for b in self._action_btns:
                    try:
                        b.config(state=tk.DISABLED)
                    except tk.TclError:
                        pass

            def close(self):
                try:
                    self._window.destroy()
                except tk.TclError:
                    pass

        def _import_selected():
            chosen = [it for it in items if check_vars[it["id"]].get()]
            if not chosen:
                return
            on_import(chosen, ctl)

        def _import_all():
            on_import(list(items), ctl)

        cancel_btn = ttk.Button(btns, text="Cancel", style="Dark.TButton",
                                command=win.destroy)
        cancel_btn.pack(side=tk.RIGHT)
        import_all_btn = ttk.Button(btns, text="Import all", style="Dark.TButton",
                                    command=_import_all)
        import_all_btn.pack(side=tk.RIGHT, padx=4)
        import_sel_btn = ttk.Button(btns, text="Import selected (0)",
                                    style="Green.TButton",
                                    command=_import_selected)
        import_sel_btn.pack(side=tk.RIGHT, padx=4)

        ctl = _Controller(self, win,
                          status, (import_sel_btn, import_all_btn, cancel_btn))

        # Extra toolbar buttons (e.g. "Change pyfa folder…") get the controller.
        for label, command in (extra_buttons or []):
            ttk.Button(toolbar, text=label, style="Dark.TButton",
                       command=(lambda c=command: c(ctl))).pack(side=tk.LEFT,
                                                                padx=4)

        # Initial population runs synchronously (no debounce) so the list is
        # fully visible the moment the window opens.
        _apply_filter()
        _update_count()

    def _show_pyfa_picker(self, db_path, fits):
        """Searchable, multi-select picker over pyfa fits with bulk import.

        Scales to hundreds of fits via the shared multi-select picker: one
        persistent BooleanVar per fit so a search filters which rows are
        *visible* without losing checked state, and bulk import runs off the Tk
        thread with a single save() at the end and content-hash de-dupe against
        the library.
        """
        # Resolve each fit's ship class name once (local bundled SDE lookup) so
        # the picker shows "ShipClass — FitName" (ship first) and is searchable
        # by either. Then group by ship class, then fit name.
        for f in fits:
            if "ship_name" not in f:
                try:
                    f["ship_name"] = self.type_catalog.resolve_name(
                        f.get("ship_type_id")) or ""
                except Exception:
                    f["ship_name"] = ""
        fits.sort(key=lambda f: ((f.get("ship_name") or "").lower(),
                                 (f.get("name") or "").lower()))

        items = []
        for f in fits:
            ship = f.get("ship_name") or "?"
            fit_name = f.get("name") or "?"
            items.append({
                "id": f["fit_id"],
                "label": f"{ship}  —  {fit_name}",
                "row_data": f,
            })

        # ── Bulk import (threaded, single save, de-dupe) ──────────────────────
        def _on_import(chosen, ctl):
            if not chosen:
                return
            ctl.disable()
            ctl.set_status(f"Importing 0/{len(chosen)}…")

            def worker():
                total = len(chosen)
                imported = skipped = failed = 0
                # De-dupe: collect existing content hashes up front so re-runs
                # of "Import all" are idempotent.
                existing = set()
                for ef in self.fittings.list_fits():
                    try:
                        existing.add(fit_models.fit_content_hash(ef.parsed))
                    except Exception:
                        pass
                for i, item in enumerate(chosen, start=1):
                    entry = item["row_data"]
                    try:
                        parsed = pyfa_import.read_pyfa_fit(
                            db_path, entry["fit_id"], self.type_catalog)
                        h = fit_models.fit_content_hash(parsed)
                        if h in existing:
                            skipped += 1
                        else:
                            name = (parsed.name_hint or entry.get("name")
                                    or parsed.ship_name or "pyfa Fit")
                            try:
                                dna = fit_dna.to_dna(parsed, self.type_catalog)
                            except Exception:
                                dna = ""
                            fit = fit_models.Fit(
                                id="",
                                name=name,
                                hull_type_id=parsed.ship_type_id,
                                hull_name=parsed.ship_name or "",
                                source="pyfa",
                                raw_text="",
                                parsed=parsed,
                                dna=dna,
                                notes="",
                                esi_fitting_ids={},
                                created="",
                                modified="",
                            )
                            self.fittings.add_fit(fit)
                            existing.add(h)
                            imported += 1
                    except Exception:
                        failed += 1
                    # Per-fit save would be O(n^2); update progress occasionally.
                    if i % 5 == 0 or i == total:
                        self.root.after(
                            0, ctl.set_status, f"Importing {i}/{total}…")
                self.root.after(0, _done, imported, skipped, failed)

            def _done(imported, skipped, failed):
                # Single save at the very end (one disk write for the batch).
                try:
                    self.fittings.save()
                except Exception as e:
                    messagebox.showerror("Import failed",
                                         f"Could not save imported fits:\n{e}")
                try:
                    self._refresh_fit_list(self._fit_search_var.get())
                except Exception:
                    pass
                ctl.close()
                messagebox.showinfo(
                    "pyfa import",
                    f"Imported {imported}, skipped {skipped} duplicate(s), "
                    f"{failed} failed.")

            threading.Thread(target=worker, daemon=True).start()

        self._show_multi_select_picker(
            items, on_import=_on_import, title="Import from pyfa",
            extra_buttons=[("Change pyfa folder…",
                            lambda ctl: (ctl.close(), self._set_pyfa_folder()))])

    def _import_esi_fittings(self):
        """Import the active character's in-game fittings via ESI (threaded),
        then show a checklist to pick which to add.

        Guarded against re-entry: a second click while the (slow) ESI fetch is
        in flight is ignored (``_esi_import_busy``) and the button is disabled
        for the duration, so we never spawn a second worker + picker window.
        Before parsing, every referenced type_id is primed in ONE ESI batch so
        the per-fit parse is all-local (no per-module serial /universe/names/)."""
        # Re-entry guard: ignore the click if an import is already in flight.
        if self._esi_import_busy:
            return

        char = self.esi_auth
        if char is None or not char.character_id:
            messagebox.showwarning(
                "No character",
                "Connect a character (Characters tab) before importing from EVE.")
            return
        if not char.has_scope(SCOPE_FITTINGS_READ):
            messagebox.showwarning(
                "Re-authorize required",
                f"{char.character_name or 'This character'} was authorized "
                "before in-game fittings support was added, so it cannot read "
                "in-game fittings yet.\n\nOpen the Characters or Settings tab "
                "and click \"Re-authorize\" for this character, then try again.")
            return
        char_id = char.character_id

        # Mark busy + disable the button now (on the Tk thread). Both are reset
        # in _finish, which runs on every terminating path.
        self._esi_import_busy = True
        self._set_esi_import_btn_state(tk.DISABLED)

        def worker():
            try:
                raw = char.get_fittings(char_id) or []
                err = None
            except Exception as e:
                raw, err = [], str(e)

            # Single batch prime: collect EVERY referenced type_id (each item's
            # type_id across all fittings + each fitting's ship_type_id) and
            # resolve all unknowns in ONE ESI call. This eliminates the old
            # 20-30s serial /universe/names/ fallback (one call per unknown id).
            if err is None:
                try:
                    all_ids = set()
                    for f in raw:
                        try:
                            ship_id = f.get("ship_type_id", 0)
                            if ship_id:
                                all_ids.add(ship_id)
                            for it in (f.get("items", []) or []):
                                tid = it.get("type_id")
                                if tid:
                                    all_ids.add(tid)
                        except Exception:
                            continue
                    if all_ids:
                        self.type_catalog.prime(all_ids)
                except Exception:
                    pass  # priming is best-effort; parse still works locally

            # Map each ESI fitting -> ParsedFit (off the Tk thread; now all-local
            # because every referenced id was just primed into the catalog).
            entries = []
            for f in raw:
                try:
                    items = f.get("items", []) or []
                    parsed = fit_dna.esi_items_to_parsed(items, self.type_catalog)
                    parsed.ship_type_id = f.get("ship_type_id", 0)
                    parsed.ship_name = (
                        self.type_catalog.resolve_name(parsed.ship_type_id)
                        or "")
                    parsed.name_hint = f.get("name")
                    entries.append({
                        "name": f.get("name") or parsed.ship_name or "Fit",
                        "ship_name": parsed.ship_name,
                        "parsed": parsed,
                    })
                except Exception:
                    continue
            self.root.after(0, _apply, entries, err)

        def _finish():
            """Reset the busy flag + re-enable the button. Runs on the Tk thread
            on EVERY terminating path (error, empty, or picker opened)."""
            self._esi_import_busy = False
            self._set_esi_import_btn_state(tk.NORMAL)

        def _apply(entries, err):
            try:
                if err is not None:
                    messagebox.showerror(
                        "Import from EVE failed",
                        f"Could not read in-game fittings:\n{err}\n\n"
                        "If this character was authorized before fittings support "
                        "was added, re-authorize it on the Characters tab.")
                    return
                if not entries:
                    messagebox.showinfo(
                        "Import from EVE",
                        "No in-game fittings found for this character.")
                    return
                self._show_esi_import_picker(entries)
            finally:
                _finish()

        threading.Thread(target=worker, daemon=True).start()

    def _set_esi_import_btn_state(self, state):
        """Best-effort enable/disable of the 'Import from EVE' button (Tk thread)."""
        btn = self._esi_import_btn
        if btn is None:
            return
        try:
            btn.config(state=state)
        except tk.TclError:
            pass

    def _show_esi_import_picker(self, entries):
        """Searchable, multi-select picker over ESI in-game fittings.

        Uses the shared multi-select picker (same layout as the pyfa flow):
        ship-first labels, search + checkboxes with preserved state, content-
        hash de-dupe against the library, and a single save() at the end.
        """
        items = []
        for i, entry in enumerate(entries):
            ship = entry.get("ship_name") or ""
            name = entry.get("name") or "?"
            label = f"{ship}  —  {name}" if ship else name
            items.append({"id": i, "label": label, "row_data": entry})

        def _on_import(chosen, ctl):
            if not chosen:
                return
            ctl.disable()
            ctl.set_status(f"Importing 0/{len(chosen)}…")

            def worker():
                total = len(chosen)
                imported = skipped = failed = 0
                # De-dupe: collect existing content hashes up front so re-runs
                # are idempotent (parity with the pyfa flow).
                existing = set()
                for ef in self.fittings.list_fits():
                    try:
                        existing.add(fit_models.fit_content_hash(ef.parsed))
                    except Exception:
                        pass
                for i, item in enumerate(chosen, start=1):
                    entry = item["row_data"]
                    try:
                        parsed = entry["parsed"]
                        h = fit_models.fit_content_hash(parsed)
                        if h in existing:
                            skipped += 1
                        else:
                            name = (entry.get("name") or parsed.name_hint
                                    or parsed.ship_name or "Fit")
                            try:
                                dna = fit_dna.to_dna(parsed, self.type_catalog)
                            except Exception:
                                dna = ""
                            fit = fit_models.Fit(
                                id="",
                                name=name,
                                hull_type_id=parsed.ship_type_id,
                                hull_name=parsed.ship_name or "",
                                source="esi",
                                raw_text="",
                                parsed=parsed,
                                dna=dna,
                                notes="",
                                esi_fitting_ids={},
                                created="",
                                modified="",
                            )
                            self.fittings.add_fit(fit)
                            existing.add(h)
                            imported += 1
                    except Exception:
                        failed += 1
                    if i % 5 == 0 or i == total:
                        self.root.after(
                            0, ctl.set_status, f"Importing {i}/{total}…")
                self.root.after(0, _done, imported, skipped, failed)

            def _done(imported, skipped, failed):
                # Single save at the very end (one disk write for the batch).
                try:
                    self.fittings.save()
                except Exception as e:
                    messagebox.showerror("Import failed",
                                         f"Could not save imported fits:\n{e}")
                try:
                    self._refresh_fit_list(self._fit_search_var.get())
                except Exception:
                    pass
                ctl.close()
                messagebox.showinfo(
                    "Import from EVE",
                    f"Imported {imported}, skipped {skipped} duplicate(s), "
                    f"{failed} failed.")

            threading.Thread(target=worker, daemon=True).start()

        self._show_multi_select_picker(
            items, on_import=_on_import,
            title="Import from EVE (in-game fittings)")

    def _push_fit_to_eve(self, fit, char):
        """Push a fit to a character's in-game Fittings via the store wrapper
        (threaded). The wrapper builds the body, deletes any prior id, records
        the new fitting_id and saves; we just surface the result."""
        char_id = char.character_id

        def worker():
            try:
                ok = self.fittings.push_fit_to_character(
                    fit.id, char_id, self.esi_auth)
                err = None
            except Exception as e:
                ok, err = False, str(e)
            self.root.after(0, _apply, ok, err)

        def _apply(ok, err):
            if ok:
                messagebox.showinfo(
                    "Saved to in-game Fittings",
                    f"'{fit.name}' was saved to {char.character_name}'s in-game "
                    "Fittings.")
                self._show_fit_detail(fit.id)
            else:
                detail = f"\n\n{err}" if err else ""
                messagebox.showerror(
                    "Save failed",
                    "Could not save the fit to in-game Fittings. The character "
                    "may need to re-authorize (Characters tab) to grant the "
                    "fittings write scope, or ESI rejected the fit."
                    + detail)

        threading.Thread(target=worker, daemon=True).start()

    # ── Settings Tab ──────────────────────────────────────────────────────────

    def _build_settings_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Settings  ")

        # ── Save bar at top (always visible) ─────────────────────────────
        save_bar = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                            highlightbackground=BORDER_COLOR, highlightthickness=1)
        save_bar.pack(fill=tk.X, padx=10, pady=(5, 0))
        ttk.Button(save_bar, text="Save Settings",
                   style="Green.TButton",
                   command=self._save_settings).pack(side=tk.LEFT, padx=10, pady=5)
        self._save_status = tk.Label(save_bar, text="",
                                      font=("Consolas", 10), fg=FG_GREEN, bg=BG_PANEL)
        self._save_status.pack(side=tk.LEFT, padx=15)

        canvas = tk.Canvas(tab, bg=BG_DARK, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=BG_DARK)

        scroll_frame.bind("<Configure>",
                          lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._register_scroll_canvas(canvas)

        # ── EVE Logs Path ────────────────────────────────────────────────────
        self._add_section(scroll_frame, "EVE Chat Logs")
        path_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        path_frame.pack(fill=tk.X, padx=20, pady=2)
        self._logs_path_var = tk.StringVar(
            value=self.config.get("eve_logs_path", ""))
        tk.Entry(path_frame, textvariable=self._logs_path_var,
                 font=("Consolas", 10), bg=BG_ENTRY, fg=FG_WHITE,
                 insertbackground=FG_WHITE, width=60,
                 borderwidth=1, relief=tk.RIDGE).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(path_frame, text="Browse", style="Dark.TButton",
                   command=self._browse_logs).pack(side=tk.LEFT)

        # ── Character / Fleet Selection ───────────────────────────────────
        char_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        char_frame.pack(fill=tk.X, padx=20, pady=2)
        tk.Label(char_frame, text="Track character:", font=("Consolas", 10),
                 fg=FG_TEXT, bg=BG_DARK, width=28, anchor=tk.W).pack(side=tk.LEFT)
        self._char_var = tk.StringVar(value=self.config.get("tracked_character", ""))
        self._char_combo = ttk.Combobox(char_frame, textvariable=self._char_var,
                                         font=("Consolas", 10), width=25)
        # Populate from cache so dropdown is ready on startup (no manual scan needed)
        cached_chars = self.config.get("cached_tracked_characters", [])
        if cached_chars:
            self._char_combo["values"] = [""] + cached_chars
        self._char_combo.pack(side=tk.LEFT, padx=5)
        # Apply character change instantly — no need to hit "Save Settings"
        self._char_combo.bind("<<ComboboxSelected>>",
                               lambda e: self._on_tracked_character_change())
        ttk.Button(char_frame, text="Scan Characters", style="Dark.TButton",
                   command=self._scan_characters).pack(side=tk.LEFT, padx=5)
        tk.Label(char_frame, text="(blank = all accounts)",
                 font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK).pack(side=tk.LEFT, padx=5)

        # ── Staging System ────────────────────────────────────────────────
        staging_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        staging_frame.pack(fill=tk.X, padx=20, pady=2)
        tk.Label(staging_frame, text="Staging System:", font=("Consolas", 10),
                 fg=FG_TEXT, bg=BG_DARK, width=28, anchor=tk.W).pack(side=tk.LEFT)
        self._staging_entry = AutocompleteEntry(
            staging_frame, self._system_names,
            font=("Consolas", 10), bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, width=25,
            borderwidth=1, relief=tk.RIDGE,
            on_select=self._autosave_staging_system,
        )
        self._staging_entry.pack(side=tk.LEFT, padx=5)
        # Pre-fill with saved staging system
        saved_staging = self.config.get("zkillboard", {}).get("staging_system", "")
        if saved_staging:
            self._staging_entry.insert(0, saved_staging)
        # Persist typed values on focus-out (dropdown picks fire on_select).
        self._staging_entry.bind("<FocusOut>", self._autosave_staging_system, add="+")
        tk.Label(staging_frame, text="(used for route calcs & jump range)",
                 font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK).pack(side=tk.LEFT, padx=5)

        # ── ESI SSO Characters ──────────────────────────────────────────
        self._add_section(scroll_frame, "EVE SSO Characters")

        # Character list frame
        self._esi_chars_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        self._esi_chars_frame.pack(fill=tk.X, padx=20, pady=2)
        self._rebuild_esi_char_list()

        # Add Character + Discover buttons
        esi_btn_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        esi_btn_frame.pack(fill=tk.X, padx=20, pady=2)

        self._esi_login_btn = ttk.Button(
            esi_btn_frame, text="Add Character", style="Dark.TButton",
            command=self._esi_login,
        )
        self._esi_login_btn.pack(side=tk.LEFT, padx=(0, 5))

        self._esi_discover_btn = ttk.Button(
            esi_btn_frame, text="Discover Ansiblex Gates", style="Dark.TButton",
            command=self._esi_discover_ansiblex,
        )
        self._esi_discover_btn.pack(side=tk.LEFT, padx=5)

        self._esi_status_label = tk.Label(
            esi_btn_frame, text="", font=("Consolas", 9),
            fg=FG_DIM, bg=BG_DARK, anchor=tk.W,
        )
        self._esi_status_label.pack(side=tk.LEFT, padx=10)

        # ── Client previews & labels ──────────────────────────────────────
        self._add_section(scroll_frame, "Client previews & labels")
        self._build_preview_section(scroll_frame)

        # ── Autostart ────────────────────────────────────────────────────
        self._autostart_var = tk.BooleanVar(value=self.config.get("autostart", False))
        auto_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        auto_frame.pack(fill=tk.X, padx=20, pady=2)
        tk.Checkbutton(auto_frame, text="Start FCTool on Windows startup",
                       variable=self._autostart_var,
                       font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                       selectcolor=BG_ENTRY, activebackground=BG_DARK,
                       activeforeground=FG_TEXT).pack(anchor=tk.W)

        # ── Sound ────────────────────────────────────────────────────────
        self._sound_var = tk.BooleanVar(value=self.config.get("sound_on_ready", False))
        sound_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        sound_frame.pack(fill=tk.X, padx=20, pady=2)
        tk.Checkbutton(sound_frame, text="Play sound when X-up threshold reached",
                       variable=self._sound_var,
                       font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                       selectcolor=BG_ENTRY, activebackground=BG_DARK,
                       activeforeground=FG_TEXT,
                       command=self._on_sound_toggle).pack(anchor=tk.W)

        # ── Ansiblex Jump Gates ──────────────────────────────────────────
        self._add_section(scroll_frame, "Ansiblex Jump Gates")
        tk.Label(scroll_frame, text="One pair per line: SystemA, SystemB",
                 font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK).pack(anchor=tk.W, padx=20)
        ansiblex_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        ansiblex_frame.pack(fill=tk.X, padx=20, pady=2)
        self._ansiblex_text = tk.Text(
            ansiblex_frame, height=4, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
            borderwidth=1, relief=tk.RIDGE, width=50,
        )
        self._ansiblex_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # Pre-fill existing Ansiblex connections
        for pair in self.config.get("ansiblex_connections", []):
            if len(pair) == 2:
                self._ansiblex_text.insert(tk.END, f"{pair[0]}, {pair[1]}\n")

        # ── Intel Channels ────────────────────────────────────────────────
        self._build_intel_channels_settings(scroll_frame)

        # ── X-Up Settings ────────────────────────────────────────────────────
        self._add_section(scroll_frame, "Fleet Management")
        xup = self.config.get("xup", {})

        self._setting_entries = {}
        self._add_setting(scroll_frame, "Trigger Word", "xup_trigger",
                          xup.get("trigger_word", "x"),
                          tooltip=("The word a pilot types in the fleet channel "
                                   "to x-up. Each pilot is counted once; an alert "
                                   "fires when the count reaches the threshold."))
        self._add_setting(scroll_frame, "Clear Word", "xup_fire",
                          xup.get("fire_word", "FIRE"),
                          tooltip=("Resets the x-up tally to zero, clearing "
                                   "everyone who has x'd up, so you can start a "
                                   "fresh count."))
        self._add_setting(scroll_frame, "Channel Name", "xup_channel",
                          xup.get("channel_name", "Fleet"))

        # ── zKillboard Settings ──────────────────────────────────────────────
        self._add_section(scroll_frame, "zKillboard")
        zk = self.config.get("zkillboard", {})

        self._zkill_enabled_var = tk.BooleanVar(value=zk.get("enabled", True))
        cb_frame2 = tk.Frame(scroll_frame, bg=BG_DARK)
        cb_frame2.pack(fill=tk.X, padx=20, pady=2)
        tk.Checkbutton(cb_frame2, text="Enable zKillboard Monitoring",
                       variable=self._zkill_enabled_var,
                       font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                       selectcolor=BG_ENTRY, activebackground=BG_DARK,
                       activeforeground=FG_TEXT).pack(anchor=tk.W)

        # Min-pilots gate lives on the Intelligence tab's filter panel
        # (config["intel_filter"]["min_pilots"]) — the single source of truth.
        # Region/alliance filters are now on the zKill tab inline filter panel

        # (Save button is at the top of the settings tab)

    def _register_scroll_canvas(self, canvas):
        """Register a scrollable canvas so the global mouse-wheel router can
        scroll it when the pointer is over it (or any of its children)."""
        if not hasattr(self, "_scroll_canvases"):
            self._scroll_canvases = set()
        self._scroll_canvases.add(canvas)

    def _on_global_mousewheel(self, event):
        """Route the wheel to the scrollable canvas under the pointer.

        Tk does not auto-deliver <MouseWheel> to the hovered widget, so we find
        it via winfo_containing and walk up to the nearest registered scroll
        canvas. Handles Windows/Mac (event.delta) and Linux (Button-4/5)."""
        scroll_canvases = getattr(self, "_scroll_canvases", None)
        if not scroll_canvases:
            return None
        if event.num == 4:
            amount = -1
        elif event.num == 5:
            amount = 1
        else:
            amount = -1 * int(event.delta / 120)
            if amount == 0 and event.delta:
                amount = -1 if event.delta > 0 else 1
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            return None
        while w is not None:
            if w in scroll_canvases:
                w.yview_scroll(amount, "units")
                return "break"
            w = getattr(w, "master", None)
        return None

    # Eve-O overlay controller cadences (ms)
    _OVERLAY_TICK_MS = 250          # when Eve-O detected + enabled
    _OVERLAY_PROBE_MS = 2000        # when enabled but Eve-O not detected
    _OVERLAY_STALE_SECS = 300       # drop a CharState label after 5 min stale

    _OVERLAY_LOCSHIP_EVERY = 10.0    # seconds between location+ship polls / char
    _OVERLAY_ONLINE_EVERY = 60.0     # seconds between online polls / char

    # ── Native preview controller config ───────────────────────────────────
    _PREVIEW_DEFAULTS = {
        "mode": "off",              # "off" | "eveo_labels" | "native"
        "tile_w": 384, "tile_body_h": 216,
        "uniform_size": True,       # one resize applies to all tiles (EVE-O parity);
                                    # False → per-char cfg['sizes'] overrides apply
        "opacity_inactive": 0.85, "opacity_hover": 1.0,
        "layouts": {}, "sizes": {},
        "login_position": [5, 5],
        "lock_layout": False,
        "snap": True, "grid": False, "grid_w": 100, "grid_h": 50,
        "hotkeys": {"focus": {}, "groups": [{"next": [], "prev": [], "order": []}],
                    "minimize_all": []},
        "hide_active": False, "hide_login": False,
        "hide_on_lost_focus": False, "hide_delay_ticks": 4,
        "disabled_chars": [],
        "minimize_inactive": False, "never_minimize": [],
        "highlight_active": True, "highlight_color": "#00d4ff", "highlight_px": 3,
        "zoom_enabled": False, "zoom_factor": 2.0, "zoom_anchor": "nw",
        "captions": True, "labels_on_video": True, "show_role_chip": True,
        "intel_flash": False, "intel_flash_color": "#ff3b30",
        "intel_flash_secs": 10, "intel_report_types": ["hostile"],
        "doctrine_tag_captions": True,       # caveat #4 (Task B1)
        "damage_flash": True,                # caveat #3 / P1 (Task B6)
        # Default mode is 'any' (log-only, no HP/ESI gate): flash on ANY windowed
        # incoming damage. 'threshold' keeps the pct-of-reference behaviour but
        # degrades to any-damage when HP is unknown (never a silent no-flash).
        "damage_flash_mode": "any",
        "damage_flash_pct": 10, "damage_flash_window_s": 5,
        "damage_flash_cooldown_s": 3, "damage_flash_reference": "weakest",
        "damage_flash_color": "#ff3b30",
    }

    def _preview_cfg(self):
        # Config is not deep-merged (house rule) — fill defaults per key.
        # NOTE: reference the defaults via the CLASS, not self — the unit tests bind
        # this method onto a bare SimpleNamespace host (house pattern; same reason
        # _overlay_poll_plan reads FCToolGUI._OVERLAY_LOCSHIP_EVERY at fc_gui.py:11152,
        # a class constant defined at :11141).
        cfg = self.config.setdefault("preview", {})
        migrated = "mode" in cfg
        for key, default in FCToolGUI._PREVIEW_DEFAULTS.items():
            cfg.setdefault(key, json.loads(json.dumps(default)))
        if not migrated and self.config.get("overlay", {}).get("enabled"):
            cfg["mode"] = "eveo_labels"   # one-time legacy migration; sticky thereafter
        return cfg

    @staticmethod
    def _overlay_poll_plan(names, last, now, online_ok):
        """Return the list of (name_lower, kind) fetches due, where kind is
        'locship' or 'online'. Pure: given names to poll, a {(name,kind): ts}
        last-poll map, the current time, and an {name: has_online_scope} map.

        location+ship every _OVERLAY_LOCSHIP_EVERY s; online every
        _OVERLAY_ONLINE_EVERY s and only when online_ok[name] is truthy."""
        loc_every = FCToolGUI._OVERLAY_LOCSHIP_EVERY
        online_every = FCToolGUI._OVERLAY_ONLINE_EVERY
        due = []
        for name in names:
            key = (name, "locship")
            if now - last.get(key, float("-inf")) >= loc_every:
                due.append(key)
            if online_ok.get(name):
                okey = (name, "online")
                if now - last.get(okey, float("-inf")) >= online_every:
                    due.append(okey)
        return due

    _OVERLAY_DEFAULTS = {
        # Default label color is high-legibility white (#ffffff), not the old
        # low-contrast teal — existing users keep their saved overlay.color;
        # only this default changes. Text is drawn as a crisp black outline +
        # white fill (see eveo_overlay), readable on any thumbnail.
        "enabled": False, "font_size": 11, "color": "#ffffff",
        "anchor": "top-left", "dpi_awareness": "auto", "rules": [],
        "overrides": {},
    }

    def _overlay_cfg(self) -> dict:
        """The config['overlay'] dict with spec defaults filled in (config is
        NOT deep-merged from DEFAULT_CONFIG, so every key is .get-defaulted)."""
        cfg = self.config.setdefault("overlay", {})
        for k, v in self._OVERLAY_DEFAULTS.items():
            if k not in cfg:
                cfg[k] = ([] if isinstance(v, list) else
                          {} if isinstance(v, dict) else v)
        return cfg

    # `when` kinds whose rule needs no match value (the value field is hidden /
    # disabled and no suggestions apply).
    _OVERLAY_VALUELESS_WHENS = ("docked", "offline", "capital", "subcap")

    def _overlay_rule_value_suggestions(self, when: str) -> list[str]:
        """Autocomplete suggestions for a rule's value, SCOPED to the rule
        category so systems never bleed into ship_group etc.:

          ship_group -> distinct ship GROUP names (incl. "Shuttle")
          ship_type  -> ship TYPE names (incl. "Amarr Shuttle" and other
                        shuttles — sourced from ship-only catalog names)
          system     -> system names (self._system_names)
          docked/offline/capital/subcap -> [] (valueless)

        Pure w.r.t. the chosen `when`: given the kind it reads the correct
        source and cross-contaminates none of the others. Never raises; returns
        [] on any failure so the dialog degrades to free-text entry."""
        w = (when or "").strip()
        try:
            if w == "system":
                return list(getattr(self, "_system_names", []) or [])
            tc = getattr(self, "type_catalog", None)
            if w == "ship_group":
                return tc.ship_group_names() if tc is not None else []
            if w == "ship_type":
                return tc.ship_type_names() if tc is not None else []
        except Exception:
            return []
        return []

    def _overlay_rules(self) -> list:
        raw = self._overlay_cfg().get("rules", []) or []
        out = []
        for r in raw:
            try:
                out.append(overlay_rules.OverlayRule(
                    when=r["when"], value=r.get("value", ""), label=r.get("label", "")))
            except Exception:
                continue
        return out

    def _overlay_state_for(self, thumb):
        """CharState for a thumb — the poller snapshot if present AND fresh
        (within _OVERLAY_STALE_SECS), else a name-only CharState. A stale state
        is dropped rather than shown, so we never label wrong/old info."""
        key = thumb.char_name.strip().lower()
        st = self._overlay_states.get(key)
        if st is not None:
            ts = self._overlay_state_ts.get(key)
            if ts is not None and (time.monotonic() - ts) <= self._OVERLAY_STALE_SECS:
                return st
        return overlay_rules.CharState(character_id=0, name=thumb.char_name)

    def _overlay_compose_items(self):
        """Join current thumbs with CharState + rules/overrides → list of
        (rect, label). Empty labels are kept out by OverlayWindow.set_labels."""
        rules = self._overlay_rules()
        overrides = self._overlay_cfg().get("overrides", {}) or {}
        items = []
        for thumb in self._overlay_thumbs_fn():
            state = self._overlay_state_for(thumb)
            label = overlay_rules.label_for(state, rules, overrides)
            items.append((thumb.rect, label))
        return items

    def _overlay_status_text(self, thumb_count: int, matched: int,
                             preview_running: bool = False) -> str:
        """Status line for the overlay Settings row. Three live states:
          ● N thumbnails · M matched        — thumbnails are visible
          ◐ Eve-O running · thumbnails …    — the process is up but Eve-O has
                                              hidden its thumbnails (its config
                                              only shows them when an EVE client
                                              is focused; select an EVE client)
          ○ Eve-O Preview not detected      — no thumbnails and no process
        The middle state relies on process-based detection (preview_running),
        so it fires even when every Eve-O window is hidden/minimised."""
        if not self._overlay_cfg().get("enabled", False):
            return "⏸ off"
        if thumb_count <= 0:
            if preview_running:
                return ("◐ Eve-O running · thumbnails hidden "
                        "(select an EVE client)")
            return "○ Eve-O Preview not detected"
        return f"● {thumb_count} thumbnails · {matched} matched"

    def _overlay_tick(self):
        """One controller tick. Reschedules itself while enabled. Enumerates
        thumbs (sub-ms, inline on the Tk thread), composes labels, redraws +
        re-asserts topmost. Cadence drops to a slow probe when Eve-O is absent."""
        cfg = self._overlay_cfg()
        if not cfg.get("enabled", False):
            self._overlay_after_id = None
            return
        try:
            items = self._overlay_compose_items()
        except Exception:
            log.exception("[overlay] compose failed; disabling for session")
            self._overlay_disable_session()
            return
        thumb_count = len(items)
        matched = sum(1 for _, t in items if t)
        try:
            # set_labels owns the topmost re-assert: on a non-empty draw it calls
            # retop() itself, so the controller does NOT call retop() again here
            # (single owner — avoids a double SetWindowPos per tick).
            self._overlay.set_labels(items)
        except Exception:
            log.exception("[overlay] draw failed; disabling for session")
            self._overlay_disable_session()
            return
        # live status label (if the Settings section is built)
        if getattr(self, "_overlay_status_label", None) is not None:
            # Only probe the process list when NO thumbnails are visible — that
            # is the sole case where the "hidden thumbnails" middle state can
            # apply, and it keeps the sub-ms hot path free of the snapshot when
            # thumbnails are up.
            running = False
            if thumb_count <= 0:
                prfn = getattr(self, "_overlay_preview_running_fn", None)
                if prfn is not None:
                    try:
                        running = bool(prfn())
                    except Exception:
                        running = False
            try:
                self._overlay_status_label.config(
                    text=self._overlay_status_text(thumb_count, matched, running))
            except tk.TclError:
                pass
        # getattr-with-default so the tick works both on the real class (class
        # constants below) and on the test's SimpleNamespace host, whose bind
        # loop does not copy these two cadence constants across.
        tick_ms = getattr(self, "_OVERLAY_TICK_MS", 250)
        probe_ms = getattr(self, "_OVERLAY_PROBE_MS", 2000)
        delay = tick_ms if thumb_count else probe_ms
        self._overlay_after_id = self.root.after(delay, self._overlay_tick)

    def _overlay_ensure_window(self):
        if self._overlay is None:
            cfg = self._overlay_cfg()
            self._overlay = OverlayWindow(self.root, {
                "font_size": cfg.get("font_size", 11),
                "color": cfg.get("color", "#ffffff"),
                "anchor": cfg.get("anchor", "top-left"),
            })
        return self._overlay

    def _overlay_boot_if_enabled(self):
        # Single boot slot for both the legacy overlay and the native preview
        # controller. _preview_cfg() applies the one-time overlay.enabled ->
        # eveo_labels migration, so route on preview.mode here.
        mode = self._preview_cfg().get("mode", "off")
        if mode == "native":
            enable_native = getattr(self, "_preview_enable_native", None)
            if enable_native is not None:      # A8 wires the native controller
                enable_native()
            return
        if mode == "eveo_labels" or self._overlay_cfg().get("enabled", False):
            self._overlay_enable()

    def _overlay_seed_rules_if_empty(self):
        """Plant the starter label rules once, the first time either consumer
        of the shared rules is enabled (Eve-O overlay labels OR native tile
        captions — both read the same overlay.rules list)."""
        cfg = self._overlay_cfg()
        if not cfg.get("rules"):
            cfg["rules"] = [
                {"when": r.when, "value": r.value, "label": r.label}
                for r in overlay_rules.seed_rules()
            ]

    def _overlay_enable(self):
        cfg = self._overlay_cfg()
        cfg["enabled"] = True
        self._overlay_seed_rules_if_empty()
        try:
            self._overlay_ensure_window()
        except Exception:
            log.exception("[overlay] could not create overlay window; disabling")
            self._overlay_disable_session()
            return
        self._overlay_start_poller()       # daemon ESI poller (Phase 2)
        if self._overlay_after_id is None:
            self._overlay_tick()

    # ── Native preview controller (Task A8) ──────────────────────────────────
    def _preview_palette(self) -> dict:
        """Palette dict handed to each TileWindow (module color constants)."""
        return {
            "BG_PANEL": BG_PANEL, "BG_DARK": BG_DARK, "FG_TEXT": FG_TEXT,
            "FG_ACCENT": FG_ACCENT, "FG_DIM": FG_DIM,
        }

    def _preview_make_tile(self, char_key, x, y, w, body_h):
        """Construct + place a real TileWindow. Injectable: the unit tests bind a
        recording factory over this name so no Tk/DWM window is ever created."""
        tile = TileWindow(
            self.root, char_key, self._preview_palette(),
            on_activate=self._preview_on_tile_activate,
            on_minimize=self._preview_on_tile_minimize,
            on_move_end=self._preview_on_tile_move_end,
            on_resize_end=self._preview_on_tile_resize_end,
            on_exclude=self._preview_on_tile_exclude,             # C4: Shift+Left
            on_switch_external=self._preview_on_tile_switch_external,  # C4: Ctrl+Shift+Left
            lock_layout=bool(self._preview_cfg().get("lock_layout", False)),
        )
        tile.place(x, y, w, body_h)
        return tile

    def _preview_tile_rect(self, client, cfg):
        """Resolve (x, y, w, body_h) for a NEW tile: saved layout for the char,
        else login-stack position for a login screen, else a default grid spawn.
        Never mutates saved layouts."""
        w, body_h = self._preview_resolve_size(cfg, client.key)
        layouts = cfg.get("layouts", {}) or {}
        saved = layouts.get(client.key)
        if saved and len(saved) >= 4:
            # Saved rect carries its own w/body_h; under uniform_size the tick's
            # _preview_apply_tile_size re-places it at the global size next cycle.
            return (int(saved[0]), int(saved[1]), int(saved[2]), int(saved[3]))
        if client.is_login:
            n = sum(1 for c in self._preview_clients.values() if c.is_login)
            base = tuple(cfg.get("login_position", [5, 5]))
            x, y = preview_layout.login_stack_pos(n, base)
            return (x, y, w, body_h)
        # default spawn: cascade by current tile count so new clients don't stack
        idx = len(self._preview_tiles)
        return (10 + idx * 24, 10 + idx * 24, w, body_h)

    def _preview_style_tile(self, tile, key, cfg):
        """Apply opacity + hover-zoom config and the active flag to one tile
        (Task C1). Guarded so recording fakes without these hooks stay no-ops."""
        conf_hover = getattr(tile, "configure_hover", None)
        if conf_hover is not None:
            conf_hover(inactive=float(cfg.get("opacity_inactive", 0.85)),
                       hover=float(cfg.get("opacity_hover", 1.0)))
        conf_zoom = getattr(tile, "configure_zoom", None)
        if conf_zoom is not None:
            conf_zoom(enabled=bool(cfg.get("zoom_enabled", False)),
                      factor=float(cfg.get("zoom_factor", 2.0)),
                      anchor=str(cfg.get("zoom_anchor", "nw")))
        set_active = getattr(tile, "set_active", None)
        if set_active is not None:
            set_active(bool(key) and key == self._preview_last_key)
        # caption-onvideo: push the on-video label style from config['overlay']
        # (color/font_size/anchor) so a freshly-spawned tile already matches the
        # saved settings; live edits go through _overlay_apply_style → all tiles.
        set_style = getattr(tile, "set_label_style", None)
        if set_style is not None:
            ocfg = self._overlay_cfg()
            set_style(color=ocfg.get("color", "#ffffff"),
                      size=int(ocfg.get("font_size", 11)),
                      anchor=ocfg.get("anchor", "top-left"))
        # BUG B: keep the tile's lock_layout flag in lockstep with config so a
        # live toggle gates drag-moves without respawning the tile.
        set_lock = getattr(tile, "set_lock_layout", None)
        if set_lock is not None:
            set_lock(bool(cfg.get("lock_layout", False)))
        # C4: keep the cycle-exclusion badge in sync (survives retire/respawn).
        set_excluded = getattr(tile, "set_excluded", None)
        if set_excluded is not None:
            set_excluded(bool(key) and key in self._preview_excluded)

    def _preview_spawn_tile(self, client):
        cfg = self._preview_cfg()
        x, y, w, body_h = self._preview_tile_rect(client, cfg)
        tile = self._preview_make_tile(client.key, x, y, w, body_h)
        try:
            tile.attach_source(client.hwnd)
        except OSError:
            # source vanished between enumerate and attach — drop the tile;
            # the next tick respawns it if the client is still around.
            try:
                tile.destroy()
            except Exception:
                pass
            return
        self._preview_tiles[client.hwnd] = tile
        self._preview_tile_rects[client.hwnd] = (x, y, w, body_h)
        self._preview_style_tile(tile, client.key, cfg)

    def _preview_rekey_tile(self, old, new):
        """Same hwnd, changed title (login<->char or char rename). Re-key the tile
        and move it to the new key's saved layout if one exists. Saved layouts of
        the OLD key are left untouched (foot-gun fix)."""
        tile = self._preview_tiles.get(new.hwnd)
        if tile is None:
            self._preview_spawn_tile(new)
            return
        try:
            tile.set_key(new.key)
            cfg = self._preview_cfg()
            saved = (cfg.get("layouts", {}) or {}).get(new.key)
            if saved and len(saved) >= 4:
                r = (int(saved[0]), int(saved[1]), int(saved[2]), int(saved[3]))
                tile.place(*r)
                self._preview_tile_rects[new.hwnd] = r
        except OSError:
            self._preview_retire_tile(new.hwnd)

    def _preview_retire_tile(self, hwnd):
        """Detach + destroy one tile. Saved layouts are NEVER cleared here."""
        self._preview_tile_rects.pop(hwnd, None)
        tile = self._preview_tiles.pop(hwnd, None)
        if tile is None:
            return
        try:
            tile.detach()
        except Exception:
            pass
        try:
            tile.destroy()
        except Exception:
            pass

    def _preview_retire_all_tiles(self):
        for hwnd in list(self._preview_tiles):
            self._preview_retire_tile(hwnd)

    def _preview_switch_to(self, client):
        """Foreground `client`, and (C3) minimize the PREVIOUS active client when
        `minimize_inactive` is on and that client isn't exempt. The single
        activation choke-point every switch path routes through (tile click,
        focus hotkey, cycle hotkey) so minimize-inactive is honored uniformly.

        The previous active client is the one at `_preview_last_key`; we minimize
        it only if it is a DIFFERENT, still-live client whose key is not in
        `never_minimize` (EVE-O PriorityClients parity). Activation itself is
        always via window_activator (the compliance choke-point) — focus APIs
        only, nothing injected into any EVE client (spec §4)."""
        cfg = self._preview_cfg()
        prev_key = self._preview_last_key
        if (cfg.get("minimize_inactive", False)
                and prev_key and prev_key != client.key
                and prev_key not in set(cfg.get("never_minimize", []))):
            for other in self._preview_clients.values():
                if other.key == prev_key:
                    window_activator.minimize(other.hwnd)
                    break
        self._preview_last_key = client.key
        window_activator.activate(client.hwnd)
        # BUG A (occlusion): the just-activated EVE client jumps to the top of the
        # z-order. Without an immediate re-assert the tiles vanish behind it for up
        # to one tick (~250 ms). Re-top every tile (and the overlay) ONCE right now
        # so they ride above the activated client instantly. retop() is pure
        # z-order (HWND_TOPMOST, SWP_NOACTIVATE|NOMOVE|NOSIZE) — no focus change,
        # so it never steals focus back from the client we just activated.
        for tile in list(self._preview_tiles.values()):
            try:
                tile.retop()
            except Exception:
                pass
        overlay = getattr(self, "_overlay", None)
        if overlay is not None:
            try:
                overlay.retop()
            except Exception:
                pass

    def _preview_on_tile_activate(self, key):
        for c in self._preview_clients.values():
            if c.key == key:
                self._preview_switch_to(c)
                return

    def _preview_on_tile_minimize(self, key):
        for c in self._preview_clients.values():
            if c.key == key:
                window_activator.minimize(c.hwnd)
                return

    def _preview_on_tile_exclude(self, key):
        """Shift+Left on a tile: toggle that character's session-only exclusion
        from hotkey cycling (C4). Excluded keys are skipped by cycle_next (the
        drain passes live_keys - excluded). Purely in-memory: nothing is written
        to config, so it resets each session. Pushes the strip badge to the tile."""
        if not key:
            return
        if key in self._preview_excluded:
            self._preview_excluded.discard(key)
        else:
            self._preview_excluded.add(key)
        excluded = key in self._preview_excluded
        for c in self._preview_clients.values():
            if c.key == key:
                tile = self._preview_tiles.get(c.hwnd)
                if tile is not None and hasattr(tile, "set_excluded"):
                    tile.set_excluded(excluded)
                break

    def _preview_on_tile_switch_external(self):
        """Ctrl+Shift+Left on a tile: focus the last non-EVE, non-ours window the
        tick observed in the foreground (captured in _preview_last_external_hwnd).
        A no-op when nothing has been captured yet. Activation goes through
        window_activator (the compliance choke-point) — focus APIs only (spec §4)."""
        hwnd = self._preview_last_external_hwnd
        if hwnd:
            window_activator.activate(hwnd)

    def _preview_on_tile_move_end(self, key, x, y):
        if not key:
            return
        cfg = self._preview_cfg()
        w = cfg.get("tile_w", 384)
        body_h = cfg.get("tile_body_h", 216)
        hwnd = None
        tile = None
        for c in self._preview_clients.values():
            if c.key == key:
                hwnd = c.hwnd
                tile = self._preview_tiles.get(c.hwnd)
                break
        if tile is not None:
            w = getattr(tile, "_w", w) or w
            body_h = getattr(tile, "_body_h", body_h) or body_h
        cfg.setdefault("layouts", {})[key] = [int(x), int(y), int(w), int(body_h)]
        if hwnd is not None:
            self._preview_tile_rects[hwnd] = (int(x), int(y), int(w), int(body_h))
        self._save_config()

    def _preview_apply_tile_size(self, hwnd, tile, key, cfg):
        """If the tile's resolved (w, body_h) differs from what it currently shows,
        re-place it at its saved top-left with the new size. Called each tick so a
        global (uniform) size change or a per-char override reaches every live
        tile. Skips a tile mid-corner-resize so the drag isn't fought."""
        if getattr(tile, "_corner_resizing", False):
            return
        w, body_h = self._preview_resolve_size(cfg, key)
        rect = self._preview_tile_rects.get(hwnd)
        if rect is not None and len(rect) >= 4:
            x, y, cur_w, cur_body = int(rect[0]), int(rect[1]), rect[2], rect[3]
        else:
            x, y, cur_w, cur_body = 10, 10, None, None
        if cur_w == w and cur_body == body_h:
            return                                   # already the target size
        try:
            tile.place(x, y, w, body_h)
        except Exception:
            return
        self._preview_tile_rects[hwnd] = (x, y, w, body_h)

    def _preview_resolve_size(self, cfg, key):
        """Resolve (w, body_h) for a char's tile. When uniform_size is True (EVE-O
        parity) every tile uses the GLOBAL tile_w/tile_body_h; when False, a
        per-char cfg['sizes'][key] override wins if present, else the global size.
        Never mutates cfg."""
        gw = int(cfg.get("tile_w", 384))
        gh = int(cfg.get("tile_body_h", 216))
        if cfg.get("uniform_size", True):
            return gw, gh
        override = (cfg.get("sizes", {}) or {}).get(key)
        if override and len(override) >= 2:
            return int(override[0]), int(override[1])
        return gw, gh

    def _preview_on_tile_resize_end(self, key, w, body_h):
        """Persist a finished tile resize (corner-hover OR the legacy Ctrl/L+R
        drag — both land here). Branches on uniform_size:
          - True  (EVE-O parity): update the GLOBAL tile_w/tile_body_h so the next
            tick re-places every tile at the new size. Per-char 'sizes' overrides
            are LEFT INTACT (house rule: never delete saved data) — they simply
            don't apply while uniform_size is on.
          - False: write ONLY cfg['sizes'][key] = [w, body_h]; the global size and
            every other tile are untouched.
        The char's saved layout rect (x,y,w,body_h) is updated in both cases so a
        respawn restores at the resized dimensions."""
        if not key:
            return
        cfg = self._preview_cfg()
        w, body_h = int(w), int(body_h)
        if cfg.get("uniform_size", True):
            cfg["tile_w"] = w
            cfg["tile_body_h"] = body_h
        else:
            cfg.setdefault("sizes", {})[key] = [w, body_h]
        layouts = cfg.setdefault("layouts", {})
        prev = layouts.get(key) or [10, 10, w, body_h]
        layouts[key] = [int(prev[0]), int(prev[1]), w, body_h]
        for c in self._preview_clients.values():
            if c.key == key:
                self._preview_tile_rects[c.hwnd] = (
                    int(prev[0]), int(prev[1]), w, body_h)
                break
        self._save_config()

    def _preview_drain_hotkeys(self):
        """Drain the hotkey worker queue and act on each event. Focus APIs only —
        the compliance choke-point is window_activator; nothing is injected into
        any EVE client (spec §4)."""
        svc = self._preview_hotkeys
        if svc is None:
            return
        live = list(self._preview_clients.values())
        by_key = {c.key: c for c in live if not c.is_login}
        while True:
            try:
                hk_id = svc.events.get_nowait()
            except queue.Empty:
                break
            action = self._preview_hotkey_map.get(hk_id)
            if not action:
                continue
            kind = action[0]
            if kind == "focus":
                c = by_key.get(action[1])
                if c is not None:
                    self._preview_switch_to(c)          # C3: minimize-inactive aware
            elif kind == "cycle":
                _, group, direction = action
                cfg = self._preview_cfg()
                groups = cfg.get("hotkeys", {}).get("groups", [])
                order = groups[group].get("order", []) if group < len(groups) else []
                # C4: skip session-excluded characters (Shift+Left toggles them).
                live_keys = set(by_key) - self._preview_excluded
                nxt = preview_layout.cycle_next(
                    order, self._preview_last_key, live_keys, direction)
                c = by_key.get(nxt)
                if c is not None:
                    self._preview_switch_to(c)          # C3: minimize-inactive aware
            elif kind == "minall":
                cfg = self._preview_cfg()
                never = set(cfg.get("never_minimize", []))
                for c in by_key.values():
                    if c.key not in never:
                        window_activator.minimize(c.hwnd)

    @staticmethod
    def _preview_hotkey_bindings(hotkeys, live_keys):
        """Pure builder: turn the saved `hotkeys` config sub-dict into
        (bindings {id:(mods,vk)}, actions {id:action_tuple}, errors [str]).

        Each hotkey STRING (e.g. "F13", "Control+F9") is parsed via
        hotkey_service.parse_hotkey; a distinct sequential id (starting at 1,
        RegisterHotKey ids must be positive) is minted per binding. Invalid
        strings are collected into `errors` (never raised) and skipped, so one
        bad key never blocks the rest. `live_keys` is accepted for signature
        symmetry with the tick (focus rows are keyed by char, already lowercased
        in config); no filtering is done here — an unbound-but-saved focus key
        stays registered so it works the moment that client appears.

        Actions match the drain in _preview_drain_hotkeys:
          focus       -> ("focus", char_key)
          group next  -> ("cycle", group_index, +1)
          group prev  -> ("cycle", group_index, -1)
          minimize_all-> ("minall",)
        """
        hotkeys = hotkeys or {}
        bindings, actions, errors = {}, {}, []
        next_id = 1

        def _bind(text, action):
            nonlocal next_id
            if not text or not str(text).strip():
                errors.append("empty hotkey")
                return
            try:
                mv = hotkey_service.parse_hotkey(str(text))
            except ValueError as exc:
                errors.append(str(exc))
                return
            bindings[next_id] = mv
            actions[next_id] = action
            next_id += 1

        for char_key, text in (hotkeys.get("focus", {}) or {}).items():
            _bind(text, ("focus", char_key))
        for gi, group in enumerate(hotkeys.get("groups", []) or []):
            for text in (group.get("next", []) or []):
                _bind(text, ("cycle", gi, +1))
            for text in (group.get("prev", []) or []):
                _bind(text, ("cycle", gi, -1))
        for text in (hotkeys.get("minimize_all", []) or []):
            _bind(text, ("minall",))
        return bindings, actions, errors

    def _preview_restart_hotkeys(self):
        """(Re)register the global hotkeys from current config. Lazily creates
        the HotkeyService (native-only, one worker thread) and rebuilds the
        id->action map in lockstep with the (re)started bindings. Returns the
        service so the settings modal can read .failures for conflict display."""
        cfg = self._preview_cfg()
        live_keys = {c.key for c in self._preview_clients.values() if not c.is_login}
        bindings, actions, _errors = self._preview_hotkey_bindings(
            cfg.get("hotkeys", {}), live_keys)
        self._preview_hotkey_map = actions
        svc = self._preview_hotkeys
        if svc is None:
            svc = self._preview_hotkey_factory()
            self._preview_hotkeys = svc
            svc.start(bindings)
        else:
            svc.restart(bindings)
        return svc

    def _preview_state_for(self, key):
        """Staleness-checked CharState for a client key (lowercased char name),
        or None. Native parallel of _overlay_state_for: the poller snapshot if
        present AND fresh (within _OVERLAY_STALE_SECS), else None so the caption
        falls back to a dim/unknown dot rather than showing stale info."""
        st = self._overlay_states.get(key)
        if st is not None:
            ts = self._overlay_state_ts.get(key)
            stale = getattr(self, "_OVERLAY_STALE_SECS",
                            FCToolGUI._OVERLAY_STALE_SECS)
            if ts is not None and (time.monotonic() - ts) <= stale:
                return st
        return None

    def _preview_caption_parts(self, client, state, rules, overrides,
                               role_chip, tag_index, doctrine_tag_captions):
        """Compose (name, dot_color, chip, tag_text) for one tile's caption.

        - Login clients render "login screen" with no status dot.
        - dot: green online, red offline, dim grey when unknown/stale (None state
          or None online).
        - name gains the ⚓ anchor glyph when the pilot is docked.
        - tag_text precedence (caveat #4): manual override > rule label >
          doctrine tag (first, sorted) > "". An empty-string override means
          "hide" and still wins.
        """
        if client.is_login:
            return ("login screen", None, "", "")
        name = client.char_name
        if state is None or state.online is None:
            dot = FG_DIM
        elif state.online:
            dot = FG_GREEN
        else:
            dot = FG_RED
        if state is not None and state.docked:
            name = f"{name} ⚓"
        # tag precedence (caveat #4): manual override > rule label >
        # doctrine tag (first, sorted) > "". An empty-string override = "hide".
        key = client.key
        norm = {k.strip().lower(): v for k, v in (overrides or {}).items()}
        rule_label = overlay_rules.label_for(state, rules, {}) if state else ""
        hull_tags = (tag_index.get(state.ship_type_id)
                     if (state is not None and tag_index) else None)
        if key and key in norm:                       # 1: manual override wins
            tag = norm[key] or ""
        elif rule_label:                              # 2: rule label
            tag = rule_label
        elif doctrine_tag_captions and hull_tags:     # 3: doctrine tag (sorted)
            tag = sorted(hull_tags)[0]
        else:                                         # 4: nothing
            tag = ""
        return (name, dot, role_chip or "", tag)

    def _preview_compose_captions(self, cur):
        """Set each live tile's caption from its staleness-checked ESI state +
        rules/overrides + (optionally) the active doctrine's tag for its hull.
        Built once per tick: one doctrine tag index shared across all tiles.

        Two channels (caption-onvideo):
          - the STRIP (name + status dot + role chip) is gated by `captions`;
          - the ON-VIDEO activity label ('<label> - <ShipType>') is drawn on the
            tile body at the configured corner, gated by `labels_on_video`. The
            activity label reuses the same rule/override/doctrine precedence
            (_preview_caption_parts' `tag`) and is joined with the pilot's ship
            TYPE name via preview_tile.format_tile_label."""
        cfg = self._preview_cfg()
        do_strip = bool(cfg.get("captions", True))
        do_video = bool(cfg.get("labels_on_video", False))
        rules = self._overlay_rules()
        overrides = self._overlay_cfg().get("overrides", {}) or {}
        doctrine_tag_captions = bool(cfg.get("doctrine_tag_captions", True))
        show_chip = bool(cfg.get("show_role_chip", True))
        doctrine = self._active_doctrine_obj()
        tag_index = fleet_composer.build_tag_index(doctrine, self.fittings)
        for hwnd, client in cur.items():
            tile = self._preview_tiles.get(hwnd)
            if tile is None:
                continue
            state = None if client.is_login else self._preview_state_for(client.key)
            role_chip = self._preview_role_chip(client) if show_chip else ""
            name, dot, chip, activity = self._preview_caption_parts(
                client, state, rules, overrides, role_chip, tag_index,
                doctrine_tag_captions)
            try:
                if do_strip:
                    tile.set_caption(name, dot, chip, activity)
                # On-video activity label: '<label> - <ShipType>'. Login screens
                # and rule-less pilots with no ship resolve to '' → hidden.
                # Composed ONCE here (rule/override/doctrine precedence evaluated
                # once) and stashed by hwnd; _preview_compose_video_labels consumes
                # the stash to draw via the topmost OverlayWindow. The child Canvas
                # is DWM-occluded, so we clear it and never draw the visible label
                # on the tile body.
                ship_name = "" if (client.is_login or state is None) \
                    else (state.ship_type_name or "")
                self._preview_video_labels[hwnd] = \
                    preview_tile.format_tile_label(activity, ship_name)
                tile.set_video_label("")
            except tk.TclError:
                pass

    def _preview_compose_video_labels(self, cur):
        """caption-onvideo: draw each live tile's on-video label ('<label> - <ShipType>')
        via the separate topmost, click-through OverlayWindow — NOT a child widget of
        the tile. The DWM compositor draws each tile's live thumbnail ON TOP of any
        child widget inside the thumbnail rect, so a child Canvas is occluded; the
        overlay is a distinct always-on-top window that composites above the tiles.

        Gated by config['preview']['labels_on_video']. The label TEXT is composed once
        in _preview_compose_captions and stashed in self._preview_video_labels (keyed by
        hwnd); this method only positions it. Empty-text items are skipped by the overlay
        (set_labels), and the overlay withdraws itself when nothing is drawn.

        native and eveo_labels are mutually exclusive and share the single self._overlay;
        this path never starts the eveo ESI poller (the native poller runs separately)."""
        do_video = bool(self._preview_cfg().get("labels_on_video", False))
        overlay = getattr(self, "_overlay", None)
        if not do_video:
            if overlay is not None:
                try:
                    overlay.set_labels([])   # clear any stale native labels
                except Exception:
                    pass
            return
        # Ensure the shared overlay Toplevel exists (idempotent). Do NOT call
        # _overlay_enable() / start the eveo poller — native mode owns its own ESI
        # poller; we only borrow the overlay's draw surface.
        overlay = self._overlay_ensure_window()
        # Push the current overlay style (size/color/anchor) from config['overlay']
        # straight to the overlay, mirroring _overlay_ensure_window's seeding. This
        # keeps the native labels styled by the same settings the eveo labels use,
        # without depending on the eveo settings UI vars existing.
        ocfg = self._overlay_cfg()
        try:
            overlay.set_font_size(int(ocfg.get("font_size", 11)))
            overlay.set_color(ocfg.get("color", "#ffffff"))
            overlay.set_anchor(ocfg.get("anchor", "top-left"))
        except Exception:
            pass
        items = []
        for hwnd in cur:
            tile = self._preview_tiles.get(hwnd)
            if tile is None:
                continue
            text = self._preview_video_labels.get(hwnd, "")
            if not text:
                continue
            rect = tile.body_screen_rect()
            if rect is None:
                continue
            items.append((rect, text))
        try:
            overlay.set_labels(items)
        except Exception:
            pass

    #: Fleet-role -> caption chip glyph. squad_member (and any unknown role)
    #: renders no chip so the common case stays clean.
    _PREVIEW_ROLE_CHIP = {
        "fleet_commander": "FC",
        "wing_commander": "WC",
        "squad_commander": "SC",
        "squad_member": "",
    }

    def _preview_role_chip(self, client):
        """Fleet-role chip text for a client (B4): look the pilot up in the
        fleet-template store by name and map its role to a short chip glyph.

        Login screens and pilots with no named slot (or the squad_member role)
        get an empty chip. Fails soft — any store error yields no chip."""
        if client.is_login or not client.char_name:
            return ""
        try:
            from fleet_template_store import find_character_role
            match = find_character_role(self.fleet_templates, client.char_name)
        except Exception:
            log.exception("[preview] role-chip lookup failed")
            return ""
        if match is None:
            return ""
        role, _wing, _squad = match
        return self._PREVIEW_ROLE_CHIP.get(role, "")

    # ── B3: intel flash — own-log system index + tile-border alerts ──────────
    def _preview_intel_note(self, index, report, now):
        """Update the per-system intel index from one IntelReport (Task B3).

        `index` maps solar_system_id -> (monotonic_ts, report_type). A report
        whose type is in `intel_report_types` (default just "hostile") stamps
        its system with `now`; a "clear" report for that system deletes the
        entry so a tile stops flashing once the field is called clear. Reports
        for unselected types or without a resolved system_id are ignored.

        Pure (no Tk / no I/O): the intel pipeline already marshals to the Tk
        thread before this runs (called from _intel_stream_ingest), so this is a
        plain-dict write on the Tk thread — no second writer to a shared map.
        """
        sid = getattr(report, "system_id", None)
        if not sid:
            return
        rtype = getattr(report, "report_type", "") or ""
        if rtype == "clear":
            index.pop(sid, None)
            return
        cfg = self._preview_cfg()
        selected = cfg.get("intel_report_types") or ["hostile"]
        if rtype in selected:
            index[sid] = (now, rtype)

    def _preview_should_flash(self, index, state, cfg, now):
        """True iff the pilot's system has a fresh intel note and flashing is on.

        Fresh = the stamped note is at most `intel_flash_secs` old. Requires
        `cfg["intel_flash"]`, a non-None state with a known solar_system_id that
        is present in `index`. Pure/side-effect-free."""
        if not cfg.get("intel_flash", False):
            return False
        if state is None:
            return False
        sid = getattr(state, "solar_system_id", None)
        if not sid:
            return False
        entry = index.get(sid)
        if not entry:
            return False
        ts, _kind = entry
        secs = cfg.get("intel_flash_secs", 10)
        return (now - ts) <= secs

    def _preview_on_damage(self, ev):
        """Tk-thread ingest of a GamelogMonitor DamageEvent (Task B6).

        The monitor polls on its own daemon thread and marshals here via
        root.after(0, ...), so this runs on the Tk thread — safe to touch the
        damage tracker (single Tk-thread writer). Keyed by lowercased char name,
        the same join key used for layouts/ESI states/tiles."""
        try:
            key = (getattr(ev, "character_name", "") or "").strip().lower()
            if not key:
                return
            self._preview_damage.add(key, getattr(ev, "amount", 0),
                                     time.monotonic())
        except Exception:
            log.exception("[preview] damage ingest failed")

    # ── C2: hide rules + per-character preview selection ────────────────────
    @staticmethod
    def _preview_shown_chars(all_known, disabled):
        """Set of char keys to show = every known char minus the disabled set.

        UX is show-oriented (a checked box = visible) but storage is
        `disabled_chars` (an unchecked box), so a brand-new character seen for
        the first time is shown by default. Both sides are lowercased so the
        subtraction is case-insensitive. Pure."""
        known = {str(k).strip().lower() for k in all_known if str(k).strip()}
        off = {str(k).strip().lower() for k in (disabled or []) if str(k).strip()}
        return known - off

    def _preview_all_known_chars(self):
        """Every character ever seen (lowercased): ESI account names ∪ live
        native client names ∪ any name with a saved layout. Drives the
        Previews… checklist and the GamelogMonitor tracked-set. Login screens
        contribute nothing (no char name). Fails soft to an empty set."""
        known = set()
        try:
            for a in getattr(self, "esi_accounts", []) or []:
                nm = (getattr(a, "character_name", "") or "").strip().lower()
                if nm:
                    known.add(nm)
            for c in self._preview_clients.values():
                if not c.is_login and c.char_name:
                    known.add(c.char_name.strip().lower())
            cfg = self._preview_cfg()
            for key in (cfg.get("layouts", {}) or {}):
                k = str(key).strip().lower()
                if k:
                    known.add(k)
        except Exception:
            log.exception("[preview] known-char union failed")
        return known

    @staticmethod
    def _preview_visibility(cur, fg_info, cfg, tick_count, lost_since):
        """Pure hide-rule resolver. Returns (hidden_hwnds:set, new_lost_since).

        Rules (spec §7/P11), combined by union:
          - hide_active: the foreground EVE client's own tile is withdrawn
            (its window already fills the screen, so its tile is redundant).
          - hide_login: login-screen tiles are withdrawn.
          - hide_on_lost_focus: when focus leaves both every EVE client AND our
            own windows for `hide_delay_ticks` consecutive ticks, every tile is
            withdrawn; the countdown resets the instant focus returns.

        `fg_info.active_hwnd` is the foreground EVE client's hwnd (or None) and
        `fg_info.focused` is True when the foreground window is an EVE client or
        one of ours. `lost_since` is the tick_count at which focus was first
        lost (or None while focused). No Tk / no ctypes — fully unit-tested."""
        hidden = set()
        if cfg.get("hide_active", False):
            ah = getattr(fg_info, "active_hwnd", None)
            if ah is not None and ah in cur:
                hidden.add(ah)
        if cfg.get("hide_login", False):
            for hwnd, client in cur.items():
                if client.is_login:
                    hidden.add(hwnd)
        new_lost = lost_since
        if cfg.get("hide_on_lost_focus", False):
            if getattr(fg_info, "focused", True):
                new_lost = None                     # focus back → reset countdown
            else:
                if new_lost is None:
                    new_lost = tick_count           # seed the countdown
                delay = int(cfg.get("hide_delay_ticks", 4))
                if tick_count - new_lost >= delay:
                    hidden.update(cur)              # hide every tile
        else:
            new_lost = None
        return hidden, new_lost

    def _preview_foreground_info(self, cur):
        """Snapshot the foreground window for the hide-on-lost-focus / hide-active
        rules AND the C4 active-highlight / switch-external features.
        `.active_hwnd` = the foreground hwnd when it is one of our tracked EVE
        clients (else None); `.focused` = True when the foreground window is an EVE
        client OR one of our own windows (tiles / the main FCTool window);
        `.external_hwnd` = the foreground hwnd when it is NEITHER an EVE client NOR
        one of ours (else None) — the Ctrl+Shift+Left "switch back" target (C4).
        Fails soft to focused=True (never hide on an errored probe)."""
        from types import SimpleNamespace
        info = SimpleNamespace(active_hwnd=None, focused=True, external_hwnd=None)
        w = getattr(self, "_preview_win32", None)
        if w is None:
            return info
        try:
            fg = w.get_foreground()
        except Exception:
            return info
        if not fg:
            return info
        if fg in cur:
            info.active_hwnd = fg
            info.focused = True
            return info
        our = set()
        for tile in self._preview_tiles.values():
            h = getattr(tile, "_hwnd", None)
            if h:
                our.add(h)
        try:
            root_hwnd = self.root.winfo_id()
            if root_hwnd:
                our.add(int(root_hwnd))
        except Exception:
            pass
        info.focused = fg in our
        if not info.focused:
            info.external_hwnd = fg      # neither an EVE client nor ours → switch target
        return info

    def _preview_native_tick_body(self):
        cfg = self._preview_cfg()
        if preview_running():                      # EVE-O still open → refuse to fight it
            self._preview_retire_all_tiles()
            return "○ EVE-O Preview detected — close it to enable native previews"
        try:
            disabled = set(cfg.get("disabled_chars", []))
            cur = {c.hwnd: c for c in self._preview_find_clients()
                   if c.key not in disabled}
            added, retitled, removed = eve_client_tracker.diff_clients(
                self._preview_clients, cur)
            for old in removed:
                self._preview_retire_tile(old.hwnd)                 # layouts untouched
            for old, new in retitled:
                self._preview_rekey_tile(old, new)                  # login→char and back
            for client in added:
                self._preview_spawn_tile(client)                    # saved | login stack | spawn
            # C2 hide rules: withdraw (never destroy) tiles the rules hide this
            # tick — hide-active / hide-login / hide-on-lost-focus. Layouts and
            # DWM registrations survive; the tile is just unmapped, so re-showing
            # is instant and no data is ever wiped.
            fg_info = self._preview_foreground_info(cur)
            # C4: remember the last non-EVE, non-ours foreground window so
            # Ctrl+Shift+Left on a tile can switch back to it. Only overwrite when
            # the probe actually saw an external window (never clobber to None).
            if getattr(fg_info, "external_hwnd", None):
                self._preview_last_external_hwnd = fg_info.external_hwnd
            # C4: which live EVE client counts as "active" for the highlight border.
            # Prefer the polled foreground client this tick; fall back to the last
            # activation anchor so a highlight persists between foreground probes.
            active_key = None
            if getattr(fg_info, "active_hwnd", None) in cur:
                active_key = cur[fg_info.active_hwnd].key
            elif self._preview_last_key:
                active_key = self._preview_last_key
            hidden, self._preview_lost_focus_since = self._preview_visibility(
                cur, fg_info, cfg, self._preview_tick_count,
                self._preview_lost_focus_since)
            for hwnd, client in cur.items():
                tile = self._preview_tiles.get(hwnd)
                if not tile:
                    continue
                if not eve_client_tracker.still_same_client(client):
                    self._preview_retire_tile(hwnd)                 # HWND reuse guard
                    continue
                if hwnd in hidden:
                    try:
                        tile.hide()                                 # withdraw; keep layout+DWM
                    except Exception:
                        pass
                    continue                                        # no style/border/retop
                try:
                    tile.show()                                     # idempotent re-map
                    tile.set_badge("MINIMIZED" if client.is_iconic
                                   else ("login screen" if client.is_login else None))
                    # C1: keep opacity/zoom config + active flag current. The
                    # active tile (last-activated client) rests at hover opacity.
                    self._preview_style_tile(tile, client.key, cfg)
                    # Uniform/individual sizing: re-place the tile if its resolved
                    # target size drifted from what it currently shows (e.g. a
                    # uniform_size resize on another tile bumped the global size, or
                    # the tile-w spin changed it). Keep x,y; only w/body_h move.
                    self._preview_apply_tile_size(hwnd, tile, client.key, cfg)
                    if self._preview_tick_count % 8 == 0:
                        tile.refresh_source_size()                  # cheap re-letterbox
                    # B3: flash the border red while the pilot's system carries a
                    # fresh hostile intel note; otherwise fall through to the
                    # next border source so the flash expires on its own once the
                    # note ages out or clears. Border precedence is deterministic
                    # (plan §B6): damage flash > intel flash > active highlight >
                    # none. The active-highlight border (P12/C4) is the lowest
                    # non-empty source: the active EVE client's tile gets a steady
                    # highlight-colour frame, but a live damage/intel flash always
                    # overrides it this tick.
                    state = (None if client.is_login
                             else self._preview_state_for(client.key))
                    # C4/P12: highlight the active client's tile (steady frame).
                    highlight = (cfg.get("highlight_color", "#00d4ff")
                                 if (cfg.get("highlight_active", True)
                                     and not client.is_login
                                     and client.key == active_key)
                                 else None)
                    # Border precedence is fixed + deterministic (plan §B6):
                    #   damage flash > intel flash > active highlight > none.
                    # Damage flash: a fresh should_flash (re)arms a hold that runs
                    # `window_s` past the last hit (seeded in _preview_damage_until)
                    # so the pulse holds while damage keeps landing and fades once
                    # it stops. While the hold is live the border PULSES between a
                    # soft red and the peak colour (~2 Hz) via preview_tile.pulse_color,
                    # stepped by elapsed = now - pulse_start (seeded in
                    # _preview_damage_since). Damage wins even if the highlight would
                    # otherwise claim the border this same tick.
                    now = time.monotonic()
                    key = client.key
                    damaging = False
                    if cfg.get("damage_flash", True) and not client.is_login:
                        hp = self._preview_layer_hp.get(key)
                        hold_s = float(cfg.get("damage_flash_window_s", 5) or 5)
                        if self._preview_damage.should_flash(key, hp, cfg, now):
                            self._preview_damage_until[key] = now + hold_s
                            # Start (or keep) the pulse clock; don't reset it on a
                            # re-arm so the pulse phase stays continuous.
                            self._preview_damage_since.setdefault(key, now)
                        until = self._preview_damage_until.get(key)
                        if until is not None:
                            if now < until:
                                damaging = True
                            else:
                                self._preview_damage_until.pop(key, None)
                                self._preview_damage_since.pop(key, None)
                    if damaging:
                        peak = cfg.get("damage_flash_color", "#ff3b30")
                        started = self._preview_damage_since.get(key, now)
                        tile.set_border(preview_tile.pulse_color(
                            peak, now - started, 0.5))     # ~2 Hz soft pulse
                    elif self._preview_should_flash(self._preview_intel, state, cfg,
                                                    time.monotonic()):
                        tile.set_border(cfg.get("intel_flash_color", "#ff3b30"))
                    else:
                        tile.set_border(highlight)
                except Exception:
                    # Per-tile failure of ANY kind (DWM handle invalidated, a Tk
                    # op after a foreground change, a client that died mid-tick).
                    # Retire THIS tile ONLY and keep iterating the remaining
                    # clients — a single tile throwing must never nuke every tile
                    # or cancel the loop (BUG A). The client is still in `cur`, so
                    # the next tick respawns it with a fresh registration.
                    log.exception("[preview] tile %r failed this tick; retiring it",
                                  hwnd)
                    self._preview_retire_tile(hwnd)
            for hwnd, tile in self._preview_tiles.items():
                if hwnd in hidden:
                    continue                                        # withdrawn — nothing to retop
                tile.retop()
            if self._overlay is not None:
                self._overlay.retop()                               # labels above tiles
            # Publish the current client set BEFORE draining hotkeys / composing
            # captions: both resolve char keys against the live set (activation
            # by key needs the just-diffed `cur`, not the previous tick's set).
            self._preview_clients = cur
            self._preview_drain_hotkeys()
            self._preview_compose_captions(cur)                     # Task B1 fills this in
            self._preview_compose_video_labels(cur)                 # Task B2 on-video labels
            # C2: the shown (checked) character set drives damage scanning too, so
            # the GamelogMonitor tails only pilots the user chose to preview.
            self._preview_sync_gamelog_scope()
            self._preview_tick_count += 1
            # A fully-successful tick clears the consecutive-failure counter so a
            # transient blip never accumulates toward the disable threshold.
            self._preview_tick_fails = 0
            n = len(cur)
            return f"● {n} client{'s' if n != 1 else ''} · {len(self._preview_tiles)} tiles"
        except Exception:
            # BUG A: a single failed tick must NOT tear down the whole session.
            # Log, count the consecutive failure, and SKIP this tick (the caller
            # reschedules the after as normal). Only give up after >=5 ticks fail
            # back-to-back — a genuinely broken environment, not a transient throw.
            self._preview_tick_fails = getattr(self, "_preview_tick_fails", 0) + 1
            log.exception("[preview] native tick failed (%d in a row)",
                          self._preview_tick_fails)
            if self._preview_tick_fails >= 5:
                self._preview_disable_session()
                return "⚠ native previews disabled (repeated errors) — see log"
            return "⚠ native preview tick skipped (error) — see log"

    def _preview_start_gamelog(self):
        """Lazily create + start the UTF-8 Gamelog monitor feeding damage flash
        (Task B6). Resolves the Gamelogs dir beside the configured Chatlogs path
        (eve_paths.gamelogs_dir_for). The monitor polls on its own daemon thread
        and marshals each DamageEvent to the Tk thread via root.after(0, ...) —
        never touching Tk widgets or _preview_* dicts off-thread. Fail-soft: any
        error disables the monitor for the session without killing previews."""
        try:
            if self._preview_gamelog is not None:
                return
            chat_path = resolve_eve_logs_path(self.config.get("eve_logs_path", ""))
            logs_dir = gamelogs_dir_for(chat_path)
            mon = self._preview_gamelog_factory(
                on_event=lambda ev: self.root.after(0, self._preview_on_damage, ev),
                logs_dir=logs_dir)
            mon.start()
            self._preview_gamelog = mon
        except Exception:
            log.exception("[preview] gamelog monitor failed to start")
            self._preview_gamelog = None

    def _preview_sync_gamelog_scope(self):
        """Restrict the GamelogMonitor to the shown (checked) character set so
        damage scanning follows the same pilots the user chose to preview
        (Task C2). No-op when the monitor isn't running. Fails soft."""
        mon = getattr(self, "_preview_gamelog", None)
        if mon is None:
            return
        try:
            shown = self._preview_shown_chars(
                self._preview_all_known_chars(),
                self._preview_cfg().get("disabled_chars", []))
            mon.set_tracked_characters(shown)
        except Exception:
            log.exception("[preview] gamelog scope sync failed")

    def _preview_stop_gamelog(self):
        mon = self._preview_gamelog
        if mon is not None:
            try:
                mon.stop()
            except Exception:
                pass
            self._preview_gamelog = None

    def _preview_enable_native(self):
        self._preview_disabled_session = False
        # Native captions consume the SAME overlay.rules list as the Eve-O label
        # overlay — a native-only fresh user must get the seed rules too (same
        # condition _overlay_enable uses).
        self._overlay_seed_rules_if_empty()
        # C2: resolve the real foreground/win32 backend for the hide-on-lost-focus
        # and hide-active rules (lazy real singleton; tests inject their own fake
        # or leave it None to mean "no foreground info → always focused").
        if self._preview_win32 is None:
            try:
                self._preview_win32 = eve_client_tracker._real_win32()
            except Exception:
                self._preview_win32 = None
        try:
            self._preview_restart_hotkeys()   # lazy service create + register
        except Exception:
            log.exception("[preview] hotkey registration failed on enable")
        self._preview_start_gamelog()         # B6: damage-flash source
        # ESI poller: the single writer of _overlay_states / _overlay_state_ts /
        # _preview_layer_hp. In native mode _preview_tracked_names routes it to
        # the live client tiles, feeding captions (rule labels + doctrine tags),
        # status dots and the damage-flash base-HP pool. Idempotent (no-op while
        # a poller is already alive).
        self._overlay_start_poller()
        if self._preview_after_id is None:
            self._preview_tick()

    def _preview_disable_native(self):
        self._preview_teardown()

    def _preview_disable_session(self):
        """A tracker/Win32 failure disables native previews for THIS session only
        (the config mode is untouched); a single log line was already emitted."""
        self._preview_disabled_session = True
        self._preview_teardown()

    def _preview_teardown(self):
        if self._preview_after_id is not None:
            try:
                self.root.after_cancel(self._preview_after_id)
            except Exception:
                pass
            self._preview_after_id = None
        self._preview_retire_all_tiles()
        self._preview_clients = {}
        self._preview_stop_gamelog()          # B6: stop the damage-flash source
        # Stop the ESI poller and clear its outputs (symmetry with
        # _overlay_teardown; also covers _preview_disable_session). A mode
        # switch then leaves exactly ONE poller: the follow-up enable creates a
        # fresh stop Event + thread, and "off" leaves zero.
        self._overlay_stop_poller()
        self._overlay_states = {}
        self._overlay_state_ts = {}
        self._preview_layer_hp = {}
        self._preview_video_labels = {}
        # Clear any native on-video labels off the shared overlay so a mode switch
        # (native → off / eveo_labels) never leaves stale native labels lingering.
        # eveo_labels repaints its own labels on its next tick.
        if self._overlay is not None:
            try:
                self._overlay.set_labels([])
            except Exception:
                pass
        svc = self._preview_hotkeys
        if svc is not None:
            try:
                svc.stop()
            except Exception:
                pass
            self._preview_hotkeys = None

    def _preview_tick(self):
        """Reschedule the native controller while native mode is active (mirrors
        _overlay_tick's cadence tail). 250 ms with tiles, 2 s slow probe idle."""
        if self._preview_cfg().get("mode") != "native" or self._preview_disabled_session:
            self._preview_after_id = None
            return
        status = self._preview_native_tick_body()
        self._preview_status = status
        if getattr(self, "_preview_status_label", None) is not None:
            try:
                self._preview_status_label.config(text=status)
            except tk.TclError:
                pass
        if self._preview_disabled_session:
            self._preview_after_id = None
            return
        tick_ms = getattr(self, "_OVERLAY_TICK_MS", 250)
        probe_ms = getattr(self, "_OVERLAY_PROBE_MS", 2000)
        delay = tick_ms if self._preview_tiles else probe_ms
        self._preview_after_id = self.root.after(delay, self._preview_tick)

    def _overlay_disable(self):
        self._overlay_cfg()["enabled"] = False
        self._overlay_teardown()

    def _overlay_disable_session(self):
        """A Win32/draw failure disables the overlay for THIS session only (the
        config toggle is untouched) with a single log line already emitted."""
        self._overlay_teardown()

    def _overlay_teardown(self):
        if self._overlay_after_id is not None:
            try:
                self.root.after_cancel(self._overlay_after_id)
            except Exception:
                pass
            self._overlay_after_id = None
        self._overlay_stop_poller()
        self._overlay_states = {}
        self._overlay_state_ts = {}
        if self._overlay is not None:
            try:
                self._overlay.set_labels([])
            except Exception:
                pass
        if getattr(self, "_overlay_status_label", None) is not None:
            try:
                self._overlay_status_label.config(text=self._overlay_status_text(0, 0))
            except tk.TclError:
                pass

    def _overlay_build_state(self, auth, do_online: bool):
        """Fetch this ONE character's ESI location/ship (+ online when do_online)
        with its OWN auth, returning a CharState. Merges onto any prior state so
        a partial pass doesn't clobber the other field. Never raises."""
        name = getattr(auth, "character_name", "") or ""
        key = name.strip().lower()
        prior = self._overlay_states.get(key)

        prior_ship_type_id = prior.ship_type_id if prior else None
        ship_type_id = prior.ship_type_id if prior else None
        ship_type_name = prior.ship_type_name if prior else ""
        ship_group = prior.ship_group if prior else ""
        is_cap = prior.is_capital if prior else None
        sys_id = prior.solar_system_id if prior else None
        sys_name = prior.system_name if prior else ""
        docked = prior.docked if prior else False
        online = prior.online if prior else None

        try:
            ship = auth.get_ship_type() or {}
            if ship.get("ship_type_id"):
                ship_type_id = ship.get("ship_type_id")
                ship_type_name = ship.get("ship_name", "") or ship_type_name
                ship_group = ship_classes.get_group_name(ship_type_id) or ""
                is_cap = bool(ship_classes.is_capital(ship_type_id))
        except Exception:
            pass
        # B6: base layer HP for the damage-flash reference pool. Cached in
        # ship_classes; only (re)fetched when the hull changes (or never seen)
        # to avoid an ESI call every poll. Poller-thread write into the
        # _preview_layer_hp dict — the tick only reads it (single-writer, same
        # discipline as _overlay_states).
        try:
            if ship_type_id and (ship_type_id != prior_ship_type_id
                                 or key not in self._preview_layer_hp):
                self._preview_layer_hp[key] = ship_classes.get_layer_hp(ship_type_id)
        except Exception:
            pass
        try:
            loc = auth.get_location() or {}
            if loc:
                sys_id = loc.get("solar_system_id")
                docked = bool(loc.get("station_id") or loc.get("structure_id"))
                if sys_id:
                    info = get_system_info(sys_id)
                    if info and info.get("name"):
                        sys_name = info["name"]
        except Exception:
            pass
        if do_online:
            try:
                res = auth.esi_get(f"/characters/{auth.character_id}/online/")
                if isinstance(res, dict) and "online" in res:
                    online = bool(res["online"])
            except Exception:
                pass

        return overlay_rules.CharState(
            character_id=getattr(auth, "character_id", 0) or 0, name=name,
            online=online, ship_type_id=ship_type_id,
            ship_type_name=ship_type_name,
            ship_group=ship_group, is_capital=is_cap, solar_system_id=sys_id,
            system_name=sys_name, docked=docked)

    def _overlay_start_poller(self):
        """Start the daemon poller if not already running."""
        if self._overlay_poller is not None and self._overlay_poller.is_alive():
            return
        self._overlay_poller_stop = threading.Event()
        self._overlay_poller = threading.Thread(
            target=self._overlay_poll_loop, name="eveo-overlay-poller", daemon=True)
        self._overlay_poller.start()

    def _overlay_stop_poller(self):
        ev = getattr(self, "_overlay_poller_stop", None)
        if ev is not None:
            ev.set()
        self._overlay_poller = None

    def _preview_tracked_names(self):
        """Lowercased character names the ESI poller should refresh, routed by
        preview mode so the same poll loop serves both label modes:
          - eveo_labels: the currently-enumerated EVE-O thumbnails;
          - native: the live native-preview clients (login screens excluded).
        Both write into the single self._overlay_states dict — single-writer
        invariant preserved (only the poller thread writes it)."""
        if self._preview_cfg().get("mode") == "native":
            return [c.char_name.strip().lower()
                    for c in self._preview_clients.values() if not c.is_login]
        return [t.char_name.strip().lower() for t in self._overlay_thumbs_fn()]

    def _overlay_poll_loop(self):
        """Daemon: round-robin ESI polling for the currently-matched characters.
        Uses each character's own ESIAuth. Writes into self._overlay_states
        (with a fetch timestamp) consumed by the controller tick. Sleeps in
        short slices so a disable is responsive."""
        stop = self._overlay_poller_stop
        last: dict = {}          # (name_lower, kind) -> ts
        auth_by_name = {}
        online_ok = {}
        while not stop.is_set():
            try:
                # who is on screen right now? (mode-routed: eveo thumbs vs native
                # client tiles — either way it feeds the SAME _overlay_states dict)
                names = self._preview_tracked_names()
                if not names:
                    stop.wait(1.0)
                    continue
                # refresh the name->auth + scope maps cheaply
                for a in self.esi_accounts:
                    nm = (getattr(a, "character_name", "") or "").strip().lower()
                    if nm:
                        auth_by_name[nm] = a
                        online_ok[nm] = bool(
                            a.has_scope("esi-location.read_online.v1"))
                now = time.monotonic()
                due = self._overlay_poll_plan(
                    [n for n in names if n in auth_by_name], last, now, online_ok)
                for name, kind in due:
                    if stop.is_set():
                        break
                    auth = auth_by_name.get(name)
                    if auth is None:
                        continue
                    st = self._overlay_build_state(auth, do_online=(kind == "online"))
                    self._overlay_states[name] = st
                    self._overlay_state_ts[name] = time.monotonic()
                    last[(name, kind)] = time.monotonic()
                    stop.wait(0.2)         # stagger ESI requests
            except Exception:
                log.exception("[overlay] poller pass failed; continuing")
            stop.wait(1.0)

    _OVERLAY_COLOR_CYCLE = [
        ("Accent", FG_ACCENT), ("Green", FG_GREEN), ("Yellow", FG_YELLOW),
        ("Orange", FG_ORANGE), ("White", FG_WHITE),
    ]
    _OVERLAY_ANCHORS = ["Top-left", "Top-right", "Bottom-left", "Bottom-right"]
    _OVERLAY_ANCHOR_TO_CFG = {
        "Top-left": "top-left", "Top-right": "top-right",
        "Bottom-left": "bottom-left", "Bottom-right": "bottom-right",
    }
    _OVERLAY_CFG_TO_ANCHOR = {v: k for k, v in _OVERLAY_ANCHOR_TO_CFG.items()}

    def _build_preview_section(self, parent):
        cfg = self._overlay_cfg()
        pcfg = self._preview_cfg()

        # DRY tooltip binder — reuses the app-wide tooltip helper
        # (_show_tooltip/_hide_tooltip) so every preview control gets one concise
        # plain-English hint on hover. `t=text` captures per-widget copy.
        def _tip(widget, text):
            widget.bind("<Enter>", lambda e, t=text: self._show_tooltip(e, t))
            widget.bind("<Leave>", lambda e: self._hide_tooltip())
            # Record the copy on the widget too. Harmless at runtime, and it lets
            # headless tests assert tooltip content without relying on synthetic
            # <Enter> delivery (unreliable for classic tk widgets when unmapped).
            try:
                widget._fctool_tooltip = text
            except Exception:
                pass

        # Native rows collected here so the mode radio can enable/disable them
        # as a group (native controls are inert unless mode == "native").
        self._preview_native_widgets = []

        # Row 1: three mode BUTTONS (Off / Eve-O Preview Enhancement / FCPreview)
        # + live status label. The active mode's button turns green with a leading
        # checkmark; the others keep the dark style. Toggling mode never touches
        # saved layouts/hotkeys. Internal mode keys are unchanged (off / eveo_labels
        # / native) so no config migration is needed.
        row1 = tk.Frame(parent, bg=BG_DARK)
        row1.pack(fill=tk.X, padx=20, pady=2)
        # _preview_mode_var is retained (StringVar mirror of the current mode) so
        # existing callers/tests that read it keep working; the buttons drive it.
        self._preview_mode_var = tk.StringVar(value=pcfg.get("mode", "off"))
        self._preview_mode_buttons = {}
        for value, text in self._PREVIEW_MODE_BUTTONS:
            btn = tk.Button(
                row1, text=text, font=("Consolas", 10),
                relief=tk.RIDGE, bd=1, padx=8, pady=1, cursor="hand2",
                command=lambda v=value: self._preview_set_mode(v))
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self._preview_mode_buttons[value] = btn
        _tip(self._preview_mode_buttons["off"],
             "Off: no previews. 'Eve-O Preview Enhancement' labels your Eve-O "
             "thumbnails; 'FCPreview' shows native live previews inside FCTool.")
        _tip(self._preview_mode_buttons["eveo_labels"],
             "Eve-O Preview Enhancement: label the Eve-O Preview thumbnails you "
             "already run — FCTool draws captions on them, it doesn't make the "
             "thumbnails itself.")
        _tip(self._preview_mode_buttons["native"],
             "FCPreview: FCTool renders its own native live preview tiles of each "
             "EVE client inside the app (no Eve-O Preview needed).")
        self._preview_status_label = tk.Label(
            row1, text=self._preview_status_text(), font=("Consolas", 9),
            fg=FG_DIM, bg=BG_DARK, anchor=tk.W)
        self._preview_status_label.pack(side=tk.LEFT, padx=12)

        # Per-mode option panels. Only the active mode's panel is packed (below the
        # buttons row, above the fine print) so the settings window never shows
        # controls for a mode you aren't using. 'off' has no panel.
        self._preview_panel_off = None
        self._preview_panel_labels = tk.Frame(parent, bg=BG_DARK)
        self._preview_panel_native = tk.Frame(parent, bg=BG_DARK)

        # Shared overlay-label controls (size / color / anchor + Label rules…). The
        # same overlay cfg keys drive labels in BOTH Eve-O-Enhancement and FCPreview
        # (native captions reuse the same label rules), so this single frame is
        # re-homed into whichever active panel needs it (see _preview_show_mode_panel).
        self._preview_shared_overlay_frame = tk.Frame(parent, bg=BG_DARK)
        shared = self._preview_shared_overlay_frame

        # Row 2: size / color / anchor, grid-aligned, applied live
        row2 = tk.Frame(shared, bg=BG_DARK)
        row2.pack(fill=tk.X, pady=2)
        tk.Label(row2, text="Size", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).grid(row=0, column=0, padx=(0, 4), sticky=tk.W)
        self._overlay_size_var = tk.IntVar(value=int(cfg.get("font_size", 11)))
        size_spin = tk.Spinbox(
            row2, from_=8, to=24, width=4, textvariable=self._overlay_size_var,
            font=("Consolas", 10), bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, command=self._overlay_apply_style)
        # save-on-type + arrow (house rule)
        size_spin.bind("<KeyRelease>", lambda e: self._overlay_apply_style())
        size_spin.grid(row=0, column=1, padx=(0, 16))
        _tip(size_spin, "Font size of the on-video label text drawn over each "
                        "preview / thumbnail.")

        tk.Label(row2, text="Color", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).grid(row=0, column=2, padx=(0, 4), sticky=tk.W)
        self._overlay_color_val = cfg.get("color", "#ffffff")
        self._overlay_color_btn = tk.Button(
            row2, text="●", font=("Consolas", 12), width=3,
            fg=self._overlay_color_val, bg=BG_ENTRY, activebackground=BG_ENTRY,
            relief=tk.RIDGE, command=self._overlay_cycle_color)
        self._overlay_color_btn.grid(row=0, column=3, padx=(0, 16))
        _tip(self._overlay_color_btn,
             "Click to cycle the colour of the on-video label text.")

        tk.Label(row2, text="Position", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).grid(row=0, column=4, padx=(0, 4), sticky=tk.W)
        self._overlay_anchor_var = tk.StringVar(
            value=self._OVERLAY_CFG_TO_ANCHOR.get(cfg.get("anchor", "top-left"),
                                                  "Top-left"))
        anchor_combo = ttk.Combobox(
            row2, textvariable=self._overlay_anchor_var, values=self._OVERLAY_ANCHORS,
            state="readonly", width=12, font=("Consolas", 10))
        anchor_combo.grid(row=0, column=5)
        anchor_combo.bind("<<ComboboxSelected>>",
                          lambda e: self._overlay_apply_style())
        _tip(anchor_combo,
             "Which corner of the preview the on-video label sits in.")

        # Row 3 (shared): Label rules… button (rules + manual tags modal). Native
        # captions reuse these same rules, so it lives in the shared frame too.
        row3 = tk.Frame(shared, bg=BG_DARK)
        row3.pack(fill=tk.X, pady=2)
        labels_btn = ttk.Button(row3, text="Labels", style="Dark.TButton",
                                command=self._open_overlay_rules_dialog)
        labels_btn.pack(side=tk.LEFT)
        self._overlay_labels_button = labels_btn
        _labels_tip = (
            "Define how thumbnails/tiles are labelled from your own ESI data "
            "(ship group, type, system…). Overrides pin custom text per "
            "character. Shared by Eve-O Enhancement and FCPreview.")
        labels_btn.bind(
            "<Enter>", lambda e, t=_labels_tip: self._show_tooltip(e, t))
        labels_btn.bind("<Leave>", lambda e: self._hide_tooltip())

        # Row 4 (native): tile size / inactive opacity / captions / doctrine tag /
        # highlight / lock layout, all live-applied. Comfort & parity (zoom,
        # hide-*, minimize-inactive) and damage-flash (Task B6) extend this row.
        rowN = tk.Frame(self._preview_panel_native, bg=BG_DARK)
        rowN.pack(fill=tk.X, pady=2)
        # First native row — the shared overlay-label row is packed *before* it so
        # the label controls sit at the top of the FCPreview panel.
        self._preview_native_first_row = rowN
        w = self._preview_native_widgets

        tk.Label(rowN, text="Tile w", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).grid(row=0, column=0, padx=(0, 4), sticky=tk.W)
        self._preview_tilew_var = tk.IntVar(value=int(pcfg.get("tile_w", 384)))
        sw = tk.Spinbox(rowN, from_=160, to=960, increment=16, width=5,
                        textvariable=self._preview_tilew_var, font=("Consolas", 10),
                        bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                        command=self._preview_apply_native_state)
        sw.bind("<KeyRelease>", lambda e: self._preview_apply_native_state())
        sw.grid(row=0, column=1, padx=(0, 16))
        w.append(sw)
        _tip(sw, "Width in pixels of each native preview tile (height follows the "
                 "client's aspect ratio).")

        # Uniform-vs-individual tile sizing (EVE-O parity default ON): one resize
        # updates the global tile_w/tile_body_h and re-sizes every tile; OFF stores
        # a per-character override. Lives beside the tile-size spin.
        self._preview_uniform_var = tk.BooleanVar(
            value=bool(pcfg.get("uniform_size", True)))
        cbu = tk.Checkbutton(
            rowN, text="Uniform tile size", variable=self._preview_uniform_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbu.grid(row=0, column=6, padx=(0, 8))
        w.append(cbu)
        _tip(cbu, "On: resizing one preview resizes them all; Off: each preview "
                  "keeps its own size.")

        tk.Label(rowN, text="Inactive opacity", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).grid(row=0, column=2, padx=(0, 4), sticky=tk.W)
        self._preview_opacity_var = tk.DoubleVar(
            value=float(pcfg.get("opacity_inactive", 0.85)))
        so = tk.Spinbox(rowN, from_=0.2, to=1.0, increment=0.05, width=5,
                        textvariable=self._preview_opacity_var, font=("Consolas", 10),
                        bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                        command=self._preview_apply_native_state)
        so.bind("<KeyRelease>", lambda e: self._preview_apply_native_state())
        so.grid(row=0, column=3, padx=(0, 16))
        w.append(so)
        _tip(so, "Opacity of preview tiles for clients that are NOT the active "
                 "one (1.0 = fully opaque).")

        self._preview_captions_var = tk.BooleanVar(value=bool(pcfg.get("captions", True)))
        cbc = tk.Checkbutton(
            rowN, text="Captions", variable=self._preview_captions_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbc.grid(row=0, column=4, padx=(0, 8))
        w.append(cbc)
        _tip(cbc, "Show a text caption on each preview tile (character name or its "
                  "label rule).")

        self._preview_doctrine_tag_var = tk.BooleanVar(
            value=bool(pcfg.get("doctrine_tag_captions", True)))
        cbd = tk.Checkbutton(
            rowN, text="Default caption = doctrine tag",
            variable=self._preview_doctrine_tag_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbd.grid(row=0, column=5, padx=(0, 8))
        w.append(cbd)
        _tip(cbd, "Caption a hull with its active-doctrine tag unless a label "
                  "rule or override already labels it.")

        self._preview_labels_on_video_var = tk.BooleanVar(
            value=bool(pcfg.get("labels_on_video", True)))
        cblv = tk.Checkbutton(
            rowN, text="Label over video (Label - ShipType)",
            variable=self._preview_labels_on_video_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cblv.grid(row=1, column=0, columnspan=4, padx=(0, 8), pady=(4, 0), sticky=tk.W)
        w.append(cblv)
        _tip(cblv, "Draw the label and ship type over each preview's video "
                   "(e.g. 'Logi - Onyx') via a topmost overlay, in the corner and "
                   "colour set above.")

        # Row 5 (native): highlight active / lock layout / arrange buttons.
        rowN2 = tk.Frame(self._preview_panel_native, bg=BG_DARK)
        rowN2.pack(fill=tk.X, pady=2)
        self._preview_highlight_var = tk.BooleanVar(
            value=bool(pcfg.get("highlight_active", True)))
        cbh = tk.Checkbutton(
            rowN2, text="Highlight active", variable=self._preview_highlight_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbh.grid(row=0, column=0, padx=(0, 8))
        w.append(cbh)
        _tip(cbh, "Draw a highlight border around the preview of the currently "
                  "active EVE client.")

        self._preview_lock_var = tk.BooleanVar(value=bool(pcfg.get("lock_layout", False)))
        cbl = tk.Checkbutton(
            rowN2, text="Lock layout", variable=self._preview_lock_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbl.grid(row=0, column=1, padx=(0, 16))
        w.append(cbl)
        _tip(cbl, "Lock preview positions and sizes so they can't be dragged or "
                  "resized by accident.")

        # B3: intel flash — tile border flashes red while the pilot's system has
        # a fresh hostile intel note from your own chat logs. Default OFF.
        self._preview_intel_flash_var = tk.BooleanVar(
            value=bool(pcfg.get("intel_flash", False)))
        cbi = tk.Checkbutton(
            rowN2, text="Intel flash", variable=self._preview_intel_flash_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbi.grid(row=0, column=2, padx=(0, 16))
        w.append(cbi)
        _tip(cbi, "Flash a preview's border red when that pilot's system gets a "
                  "fresh hostile intel note from your own chat logs.")

        # B6: damage flash — tile border pulses red when windowed incoming
        # damage from your OWN combat Gamelogs crosses a % of base hull HP.
        # Default ON. Native-mode only (eveo_labels has no tiles to flash).
        self._preview_damage_flash_var = tk.BooleanVar(
            value=bool(pcfg.get("damage_flash", True)))
        cbdf = tk.Checkbutton(
            rowN2, text="Damage flash", variable=self._preview_damage_flash_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbdf.grid(row=0, column=3, padx=(0, 16))
        w.append(cbdf)
        _tip(cbdf, "Pulse a preview's border red when that character takes "
                   "incoming damage in your own combat logs (tune it in the row "
                   "below).")

        # C3: minimize-inactive — on a switch, the previously-active client is
        # minimized (unless it's in the never-minimize list). EVE-O parity.
        self._preview_minimize_inactive_var = tk.BooleanVar(
            value=bool(pcfg.get("minimize_inactive", False)))
        cbmi = tk.Checkbutton(
            rowN2, text="Minimize inactive",
            variable=self._preview_minimize_inactive_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbmi.grid(row=0, column=4, padx=(0, 8))
        w.append(cbmi)
        _tip(cbmi, "When you switch clients, minimize the one you just left "
                   "(except characters on the never-minimize list).")

        bnm = ttk.Button(rowN2, text="Never minimize…", style="Dark.TButton",
                         command=self._open_preview_never_minimize_dialog)
        bnm.grid(row=0, column=5, padx=(0, 6))
        w.append(bnm)
        _tip(bnm, "Pick characters that should stay open and never be minimized "
                  "by 'Minimize inactive'.")

        # Row 5b (native, Task C2): hide rules + the per-character "which previews
        # to show" entry point. Hiding a rule/character never wipes saved data.
        rowHide = tk.Frame(self._preview_panel_native, bg=BG_DARK)
        rowHide.pack(fill=tk.X, pady=2)
        self._preview_hide_active_var = tk.BooleanVar(
            value=bool(pcfg.get("hide_active", False)))
        cbha = tk.Checkbutton(
            rowHide, text="Hide active", variable=self._preview_hide_active_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbha.grid(row=0, column=0, padx=(0, 8))
        w.append(cbha)
        _tip(cbha, "Hide the preview of whichever client is currently active "
                   "(you're already looking at that window).")

        self._preview_hide_login_var = tk.BooleanVar(
            value=bool(pcfg.get("hide_login", False)))
        cbhl = tk.Checkbutton(
            rowHide, text="Hide login", variable=self._preview_hide_login_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbhl.grid(row=0, column=1, padx=(0, 8))
        w.append(cbhl)
        _tip(cbhl, "Hide previews of clients still sitting on the character-select "
                   "/ login screen.")

        self._preview_hide_lost_focus_var = tk.BooleanVar(
            value=bool(pcfg.get("hide_on_lost_focus", False)))
        cbhf = tk.Checkbutton(
            rowHide, text="Hide all on lost focus",
            variable=self._preview_hide_lost_focus_var,
            command=self._preview_apply_native_state, font=("Consolas", 10),
            fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_TEXT)
        cbhf.grid(row=0, column=2, padx=(0, 16))
        w.append(cbhf)
        _tip(cbhf, "Hide every preview whenever no EVE client has focus (e.g. "
                   "you've alt-tabbed away from the game).")

        bpv = ttk.Button(rowHide, text="Previews…", style="Dark.TButton",
                         command=self._open_preview_previews_dialog)
        bpv.grid(row=0, column=3, padx=(0, 6))
        w.append(bpv)
        _tip(bpv, "Choose which characters get a preview window.")
        self._preview_shown_summary_lbl = tk.Label(
            rowHide, text="", font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK)
        self._preview_shown_summary_lbl.grid(row=0, column=4, padx=(4, 0),
                                             sticky=tk.W)
        self._preview_update_shown_summary()

        # Row 6 (native): damage-flash tuning. Mode picks 'Any damage' (log-only
        # default; no HP/ESI gate) or 'Threshold' (pct-of-reference). The pct +
        # reference controls are shown ONLY in threshold mode; window/cooldown
        # apply to both (window is also the pulse hold). All live-applied.
        rowN3 = tk.Frame(self._preview_panel_native, bg=BG_DARK)
        rowN3.pack(fill=tk.X, pady=2)

        tk.Label(rowN3, text="Flash on", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).grid(row=0, column=0, padx=(0, 4), sticky=tk.W)
        # Human labels ↔ stored mode values.
        self._preview_dmg_mode_labels = {"any": "Any damage",
                                         "threshold": "Threshold"}
        self._preview_dmg_mode_from_label = {
            v: k for k, v in self._preview_dmg_mode_labels.items()}
        _mode0 = str(pcfg.get("damage_flash_mode", "any"))
        self._preview_dmg_mode_var = tk.StringVar(
            value=self._preview_dmg_mode_labels.get(_mode0, "Any damage"))
        mode_combo = ttk.Combobox(
            rowN3, textvariable=self._preview_dmg_mode_var,
            values=["Any damage", "Threshold"],
            state="readonly", width=11, font=("Consolas", 10))
        mode_combo.grid(row=0, column=1, padx=(0, 12))
        mode_combo.bind("<<ComboboxSelected>>",
                        lambda e: (self._preview_apply_dmg_mode_visibility(),
                                   self._preview_apply_native_state()))
        w.append(mode_combo)
        _tip(mode_combo,
             "Any damage: pulse on any incoming hit from your combat logs. "
             "Threshold: pulse only once damage passes a % of the ship's HP.")

        # pct label+spin (threshold-only; grid_remove'd in any mode below).
        self._preview_dmg_pct_lbl = tk.Label(rowN3, text="Flash %",
                                             font=("Consolas", 10), fg=FG_TEXT,
                                             bg=BG_DARK)
        self._preview_dmg_pct_lbl.grid(row=0, column=2, padx=(0, 4), sticky=tk.W)
        self._preview_dmg_pct_var = tk.IntVar(value=int(pcfg.get("damage_flash_pct", 10)))
        spct = tk.Spinbox(rowN3, from_=1, to=100, width=4,
                          textvariable=self._preview_dmg_pct_var, font=("Consolas", 10),
                          bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                          command=self._preview_apply_native_state)
        spct.bind("<KeyRelease>", lambda e: self._preview_apply_native_state())
        spct.grid(row=0, column=3, padx=(0, 12))
        self._preview_dmg_pct_spin = spct
        w.append(spct)
        _tip(spct, "Threshold mode only: how much damage (as a % of the reference "
                   "HP) within the window triggers a pulse.")

        tk.Label(rowN3, text="Window s", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).grid(row=0, column=4, padx=(0, 4), sticky=tk.W)
        self._preview_dmg_window_var = tk.IntVar(
            value=int(pcfg.get("damage_flash_window_s", 5)))
        swin = tk.Spinbox(rowN3, from_=1, to=60, width=4,
                          textvariable=self._preview_dmg_window_var, font=("Consolas", 10),
                          bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                          command=self._preview_apply_native_state)
        swin.bind("<KeyRelease>", lambda e: self._preview_apply_native_state())
        swin.grid(row=0, column=5, padx=(0, 12))
        w.append(swin)
        _tip(swin, "How many seconds of incoming damage are summed together, and "
                   "how long the pulse is held.")

        tk.Label(rowN3, text="Cooldown s", font=("Consolas", 10), fg=FG_TEXT,
                 bg=BG_DARK).grid(row=0, column=6, padx=(0, 4), sticky=tk.W)
        self._preview_dmg_cooldown_var = tk.IntVar(
            value=int(pcfg.get("damage_flash_cooldown_s", 3)))
        scd = tk.Spinbox(rowN3, from_=0, to=60, width=4,
                         textvariable=self._preview_dmg_cooldown_var, font=("Consolas", 10),
                         bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                         command=self._preview_apply_native_state)
        scd.bind("<KeyRelease>", lambda e: self._preview_apply_native_state())
        scd.grid(row=0, column=7, padx=(0, 12))
        w.append(scd)
        _tip(scd, "Minimum seconds between pulses on the same preview, so a steady "
                  "beating doesn't strobe.")

        # reference label+combo (threshold-only).
        self._preview_dmg_ref_lbl = tk.Label(rowN3, text="Reference",
                                            font=("Consolas", 10), fg=FG_TEXT,
                                            bg=BG_DARK)
        self._preview_dmg_ref_lbl.grid(row=0, column=8, padx=(0, 4), sticky=tk.W)
        self._preview_dmg_ref_var = tk.StringVar(
            value=str(pcfg.get("damage_flash_reference", "weakest")))
        ref_combo = ttk.Combobox(
            rowN3, textvariable=self._preview_dmg_ref_var,
            values=["weakest", "shield", "armor", "hull", "total"],
            state="readonly", width=8, font=("Consolas", 10))
        ref_combo.grid(row=0, column=9)
        ref_combo.bind("<<ComboboxSelected>>",
                       lambda e: self._preview_apply_native_state())
        self._preview_dmg_ref_combo = ref_combo
        w.append(ref_combo)
        _tip(ref_combo,
             "Threshold mode only: which base-hull HP pool the Flash % is measured "
             "against (weakest layer, a specific layer, or total).")
        # Apply initial threshold-only visibility from the loaded mode.
        self._preview_apply_dmg_mode_visibility()

        # Row 7 (native): arrange / hotkey buttons.
        rowN4 = tk.Frame(self._preview_panel_native, bg=BG_DARK)
        rowN4.pack(fill=tk.X, pady=2)
        bg = ttk.Button(rowN4, text="Arrange in grid", style="Dark.TButton",
                        command=self._preview_arrange_grid)
        bg.grid(row=0, column=0, padx=(0, 6))
        w.append(bg)
        _tip(bg, "Auto-place every preview tile into a tidy grid on your screen.")

        bgf = ttk.Button(rowN4, text="Arrange by fleet", style="Dark.TButton",
                         command=self._preview_arrange_by_fleet)
        bgf.grid(row=0, column=1, padx=(0, 6))
        w.append(bgf)
        _tip(bgf, "Arrange the preview tiles grouped by their fleet wing/squad "
                  "order.")

        bhk = ttk.Button(rowN4, text="Hotkeys…", style="Dark.TButton",
                         command=self._open_preview_hotkeys_dialog)
        bhk.grid(row=0, column=2, padx=(0, 6))
        w.append(bhk)
        _tip(bhk, "Set global hotkeys to switch/cycle EVE clients (focus only — no "
                  "input is ever sent to the game).")

        bimp = ttk.Button(rowN4, text="Import EVE-O layout…", style="Dark.TButton",
                          command=self._preview_import_eveo)
        bimp.grid(row=0, column=3, padx=(0, 6))
        w.append(bimp)
        _tip(bimp, "Import tile positions/sizes from your existing Eve-O Preview "
                   "configuration.")

        # Fine print (updated disclaimer — spec §9). Damage-flash fine print
        # (Task B6): base-hull-HP approximation + English-client + own-logs-only.
        # Stored so the active mode's panel packs *before* it (see
        # _preview_show_mode_panel) and the disclaimer stays at the bottom.
        row4 = tk.Frame(parent, bg=BG_DARK)
        row4.pack(fill=tk.X, padx=20, pady=(2, 6))
        self._preview_fineprint_row = row4
        tk.Label(
            row4,
            text=("Labels come from your own ESI data and your text only; intel "
                  "flash reads only your own chat logs. Damage flash is based on "
                  "base hull HP — fitted ships have more; English client only; it "
                  "reads only your own combat logs. Previews are view-only — clicks "
                  "and hotkeys only change window focus; no input is ever sent to "
                  "EVE clients."),
            font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK, justify=tk.LEFT,
            wraplength=760).pack(side=tk.LEFT)

        # Show the active mode's panel and paint the buttons for the first time.
        self._preview_show_mode_panel()
        self._preview_refresh_mode_buttons()
        self._preview_sync_native_widgets()

    # Button layout: (internal mode key, exact label). Labels are user-facing and
    # must stay verbatim; keys are the persisted mode values (no migration).
    _PREVIEW_MODE_BUTTONS = (
        ("off", "Off"),
        ("eveo_labels", "Eve-O Preview Enhancement"),
        ("native", "FCPreview"),
    )

    def _preview_refresh_mode_buttons(self):
        """Paint the three mode buttons: the active mode gets a leading checkmark
        + green bg / dark fg; the others keep the house dark-button style."""
        btns = getattr(self, "_preview_mode_buttons", None)
        if not btns:
            return
        active = self._preview_cfg().get("mode", "off")
        # keep the retained StringVar mirror in sync with the persisted mode
        try:
            if getattr(self, "_preview_mode_var", None) is not None:
                self._preview_mode_var.set(active)
        except tk.TclError:
            pass
        for mode, label in self._PREVIEW_MODE_BUTTONS:
            btn = btns.get(mode)
            if btn is None:
                continue
            try:
                if mode == active:
                    btn.configure(
                        text=f"✓ {label}", bg=FG_GREEN, fg=BG_DARK,
                        activebackground=FG_GREEN, activeforeground=BG_DARK)
                else:
                    btn.configure(
                        text=label, bg=BG_ENTRY, fg=FG_TEXT,
                        activebackground=BG_ENTRY, activeforeground=FG_TEXT)
            except tk.TclError:
                pass

    def _preview_show_mode_panel(self):
        """Pack exactly the active mode's option panel (labels/native), re-homing
        the shared overlay-label controls into it, and hide the others. 'off' shows
        no panel — just the buttons row + status line + fine print."""
        active = self._preview_cfg().get("mode", "off")
        panels = {
            "off": getattr(self, "_preview_panel_off", None),
            "eveo_labels": getattr(self, "_preview_panel_labels", None),
            "native": getattr(self, "_preview_panel_native", None),
        }
        before = getattr(self, "_preview_fineprint_row", None)
        shared = getattr(self, "_preview_shared_overlay_frame", None)
        # First detach every panel + the shared row so re-homing is clean.
        for p in panels.values():
            if p is not None:
                try:
                    p.pack_forget()
                except tk.TclError:
                    pass
        if shared is not None:
            try:
                shared.pack_forget()
            except tk.TclError:
                pass
        panel = panels.get(active)
        if panel is None:
            return
        # The labels + native panels both host the shared overlay-label row at
        # their top; re-parent it into the active panel before packing. In the
        # native panel the row-order already exists, so pack before the first
        # native row to keep the label controls on top.
        if shared is not None and active in ("eveo_labels", "native"):
            first = getattr(self, "_preview_native_first_row", None)
            try:
                if active == "native" and first is not None:
                    shared.pack(in_=panel, fill=tk.X, pady=(0, 2), before=first)
                else:
                    shared.pack(in_=panel, fill=tk.X, pady=(0, 2))
            except tk.TclError:
                pass
        try:
            if before is not None:
                panel.pack(fill=tk.X, padx=20, pady=2, before=before)
            else:
                panel.pack(fill=tk.X, padx=20, pady=2)
        except tk.TclError:
            pass

    def _preview_sync_native_widgets(self):
        """Enable native-only controls iff mode == native (spec §9 row gating)."""
        state = "normal" if self._preview_cfg().get("mode") == "native" else "disabled"
        for widget in getattr(self, "_preview_native_widgets", []):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass

    def _preview_status_text(self) -> str:
        """Live status for the settings label. `off`/`labels` are static; native
        surfaces the blocked-by-EVE-O notice or the controller's own tick status."""
        mode = self._preview_cfg().get("mode", "off")
        if mode == "off":
            return "⏸ off"
        if mode == "eveo_labels":
            return "◐ labelling EVE-O thumbnails"
        if preview_running():
            return "○ EVE-O Preview detected — close it to enable native tiles"
        return getattr(self, "_preview_status", "") or "● native previews"

    def _preview_set_mode(self, new_mode: str):
        """Switch preview mode: tear down the old mode, persist, boot the new one.
        NEVER clears saved layouts/hotkeys (the EVE-O config-wipe foot-gun we fix).
        The three modes are mutually exclusive controllers sharing one config dict."""
        cfg = self._preview_cfg()
        old = cfg.get("mode", "off")
        if old == new_mode:
            # Re-assert the requested mode's controller anyway (idempotent boot).
            pass
        # Tear down whatever is currently running.
        if old == "native":
            self._preview_disable_native()
        elif old == "eveo_labels":
            self._overlay_disable()
        cfg["mode"] = new_mode          # layouts/hotkeys untouched by design
        self._save_config()
        # Boot the newly-selected mode.
        if new_mode == "native":
            self._preview_disabled_session = False
            self._preview_enable_native()
        elif new_mode == "eveo_labels":
            self._overlay_enable()
        # keep the settings label + native-row gating in sync if the UI is built
        if getattr(self, "_preview_status_label", None) is not None:
            try:
                self._preview_status_label.config(text=self._preview_status_text())
            except tk.TclError:
                pass
        # swap the visible option panel + repaint the mode buttons (no-ops before
        # the settings UI is built — both guard on their widgets existing).
        self._preview_show_mode_panel()
        self._preview_refresh_mode_buttons()
        self._preview_sync_native_widgets()

    def _preview_apply_native_state(self):
        """Persist the native-row control values live. No path clears saved data."""
        cfg = self._preview_cfg()
        for var, key, cast in (
            ("_preview_tilew_var", "tile_w", int),
            ("_preview_uniform_var", "uniform_size", bool),
            ("_preview_opacity_var", "opacity_inactive", float),
            ("_preview_captions_var", "captions", bool),
            ("_preview_doctrine_tag_var", "doctrine_tag_captions", bool),
            ("_preview_labels_on_video_var", "labels_on_video", bool),
            ("_preview_highlight_var", "highlight_active", bool),
            ("_preview_lock_var", "lock_layout", bool),
            ("_preview_intel_flash_var", "intel_flash", bool),
            ("_preview_minimize_inactive_var", "minimize_inactive", bool),
            ("_preview_hide_active_var", "hide_active", bool),
            ("_preview_hide_login_var", "hide_login", bool),
            ("_preview_hide_lost_focus_var", "hide_on_lost_focus", bool),
            ("_preview_damage_flash_var", "damage_flash", bool),
            ("_preview_dmg_pct_var", "damage_flash_pct", int),
            ("_preview_dmg_window_var", "damage_flash_window_s", int),
            ("_preview_dmg_cooldown_var", "damage_flash_cooldown_s", int),
            ("_preview_dmg_ref_var", "damage_flash_reference", str),
        ):
            v = getattr(self, var, None)
            if v is None:
                continue
            try:
                cfg[key] = cast(v.get())
            except (tk.TclError, ValueError):
                pass
        # damage_flash_mode: the StringVar holds a human label; map it back to the
        # stored value ('any' | 'threshold'). Absent/unknown label → 'any'.
        mode_var = getattr(self, "_preview_dmg_mode_var", None)
        if mode_var is not None:
            try:
                label = mode_var.get()
                cfg["damage_flash_mode"] = getattr(
                    self, "_preview_dmg_mode_from_label", {}).get(label, "any")
            except tk.TclError:
                pass
        self._save_config()

    def _preview_apply_dmg_mode_visibility(self):
        """Show the pct + reference controls only in 'threshold' mode; hide them in
        'any' mode. Window/cooldown apply to both modes and stay visible. Idempotent
        and Tk-safe (grid_remove keeps the grid slot so re-showing restores it)."""
        try:
            label = self._preview_dmg_mode_var.get()
        except (AttributeError, tk.TclError):
            return
        threshold = (getattr(self, "_preview_dmg_mode_from_label", {})
                     .get(label, "any") == "threshold")
        for attr in ("_preview_dmg_pct_lbl", "_preview_dmg_pct_spin",
                     "_preview_dmg_ref_lbl", "_preview_dmg_ref_combo"):
            wdg = getattr(self, attr, None)
            if wdg is None:
                continue
            try:
                wdg.grid() if threshold else wdg.grid_remove()
            except tk.TclError:
                pass

    def _preview_arrange_grid(self):
        """Lay out every live non-login tile in a row-major grid, persist each
        char's rect (keyed by char name), and re-place its tile. Login screens
        keep their stacked positions and are never persisted."""
        live = [c for c in self._preview_clients.values() if not c.is_login]
        live.sort(key=lambda c: c.key)
        self._preview_arrange_ordered(live)

    def _preview_fleet_order_key(self):
        """Map lowercased char name -> (wing_idx, squad_idx, slot_idx) position
        in the fleet-template store (B4). First occurrence wins. Characters with
        no named slot are absent; callers sort them to the end."""
        order: dict[str, tuple] = {}
        try:
            wi = 0
            for t in self.fleet_templates.templates:
                for w in t.wings:
                    for si, sq in enumerate(w.squads):
                        for li, slot in enumerate(sq.slots):
                            name = (slot.character or "").strip().lower()
                            if name and name not in order:
                                order[name] = (wi, si, li)
                    wi += 1
        except Exception:
            log.exception("[preview] fleet order build failed")
        return order

    def _preview_arrange_by_fleet(self):
        """Arrange live non-login tiles ordered by their fleet-template position
        (wing → squad → slot), then the same grid + persist as 'Arrange in grid'.
        Pilots not in any template fall to the end, sorted by name (B4)."""
        order = self._preview_fleet_order_key()
        live = [c for c in self._preview_clients.values() if not c.is_login]
        live.sort(key=lambda c: (order.get(c.key, (1 << 30, 0, 0)), c.key))
        self._preview_arrange_ordered(live)

    def _preview_arrange_ordered(self, ordered_live):
        """Grid-place an already-ordered list of live clients, persist each rect
        (keyed by char name), and re-place its tile. Shared by the grid and
        by-fleet arrange buttons. No-op on an empty list."""
        if not ordered_live:
            return
        cfg = self._preview_cfg()
        tile_w = int(cfg.get("tile_w", 384))
        body_h = int(cfg.get("tile_body_h", 216))
        # C5: arrange across the full virtual desktop (all monitors), not just
        # the primary screen. _virtual_screen_bounds() wraps the SM_*VIRTUALSCREEN
        # GetSystemMetrics (read-only) and falls back to Tk's primary screen.
        try:
            x0, y0, x1, y1 = self._virtual_screen_bounds()
            bx, by = int(x0), int(y0)
            bw, bh = int(x1) - bx, int(y1) - by
        except Exception:
            bx, by, bw, bh = 0, 0, 1920, 1080
        # Lay out in a bounds-local (0,0,bw,bh) space so per-row math is correct,
        # then shift every rect onto the virtual desktop by the origin (bx, by).
        rects = [(bx + gx, by + gy, gw, gh) for gx, gy, gw, gh
                 in preview_layout.grid_arrange(
                     len(ordered_live), tile_w, body_h, (0, 0, bw, bh),
                     origin=(10, 10), gap=8)]
        layouts = cfg.setdefault("layouts", {})
        for client, (x, y, _w, _h) in zip(ordered_live, rects):
            layouts[client.key] = [int(x), int(y), tile_w, body_h]
            for tile in self._preview_tiles.values():
                if getattr(tile, "key", None) == client.key:
                    try:
                        tile.place(int(x), int(y), tile_w, body_h)
                    except Exception:
                        pass
                    break
        self._save_config()

    @staticmethod
    def parse_eveo_config(json_text: str) -> dict:
        """Pure parser for an EVE-O-Preview.json (Proopai fork). Returns
        {"layouts": {char_key: (x, y)}, "focus_hotkeys": {char_key: str},
         "cycle_order": [char_key, ...], "errors": [str, ...]}.

        - FlatLayout: {title: "x, y"} — title prefix stripped to lowercased char
          key; bare "EVE" login windows and unparseable points are skipped.
        - ClientHotkey: {title: hotkey} — validated through parse_hotkey; invalid
          entries (Win modifier, unknown key, …) are skipped and reported.
        - CycleGroup1ClientsOrder: {title: int} — cycle order = keys sorted by
          value ascending.
        Never raises: a bad JSON body returns empty maps + one error string."""
        errors: list = []
        try:
            data = json.loads(json_text)
            if not isinstance(data, dict):
                raise ValueError("top-level JSON is not an object")
        except Exception as e:
            return {"layouts": {}, "focus_hotkeys": {}, "cycle_order": [],
                    "errors": [f"could not parse EVE-O config: {e}"]}

        def _char_key(title: str):
            # Reuse the tracker's own title parser for exact prefix parity.
            char = eve_client_tracker._char_from_title(
                eve_client_tracker._normalize(str(title)))
            if char is None or char == "":   # non-EVE title or login screen
                return None
            return char.strip().lower()

        layouts: dict = {}
        for title, point in (data.get("FlatLayout") or {}).items():
            key = _char_key(title)
            if key is None:
                continue
            try:
                sx, sy = str(point).split(",")
                layouts[key] = (int(sx.strip()), int(sy.strip()))
            except (ValueError, AttributeError):
                errors.append(f"bad layout point for {title!r}: {point!r}")

        focus_hotkeys: dict = {}
        for title, hk in (data.get("ClientHotkey") or {}).items():
            key = _char_key(title)
            if key is None:
                continue
            text = str(hk).strip()
            if not text:
                continue
            try:
                hotkey_service.parse_hotkey(text)
            except ValueError as e:
                errors.append(f"invalid hotkey for {title!r}: {e}")
                continue
            focus_hotkeys[key] = text

        order_pairs = []
        for title, idx in (data.get("CycleGroup1ClientsOrder") or {}).items():
            key = _char_key(title)
            if key is None:
                continue
            try:
                order_pairs.append((int(idx), key))
            except (ValueError, TypeError):
                continue
        cycle_order = [k for _, k in sorted(order_pairs, key=lambda p: p[0])]

        return {"layouts": layouts, "focus_hotkeys": focus_hotkeys,
                "cycle_order": cycle_order, "errors": errors}

    def _preview_merge_eveo(self, parsed: dict) -> dict:
        """Fill-only merge of a parsed EVE-O config into the preview cfg. NEVER
        overwrites an existing FCTool layout/hotkey/cycle entry (the whole point
        of a one-time import). Returns a small {added_*: n} summary."""
        cfg = self._preview_cfg()
        tile_w = int(cfg.get("tile_w", 384))
        body_h = int(cfg.get("tile_body_h", 216))
        layouts = cfg.setdefault("layouts", {})
        added_layouts = 0
        for key, (x, y) in parsed["layouts"].items():
            if key not in layouts:
                layouts[key] = [int(x), int(y), tile_w, body_h]
                added_layouts += 1

        hk = cfg.setdefault("hotkeys", {})
        focus = hk.setdefault("focus", {})
        added_hotkeys = 0
        for key, text in parsed["focus_hotkeys"].items():
            if key not in focus:
                focus[key] = text
                added_hotkeys += 1

        groups = hk.setdefault("groups", [])
        if not groups:
            groups.append({"next": [], "prev": [], "order": []})
        order = groups[0].setdefault("order", [])
        added_order = 0
        for key in parsed["cycle_order"]:
            if key not in order:
                order.append(key)
                added_order += 1

        self._save_config()
        return {"added_layouts": added_layouts, "added_hotkeys": added_hotkeys,
                "added_order": added_order}

    def _preview_import_eveo(self):
        """Settings button: pick an EVE-O-Preview.json, parse it, fill-only merge
        into the preview cfg, and show an import summary. One-time convenience;
        it only reads the file — EVE-O's own config is never modified."""
        path = filedialog.askopenfilename(
            title="Import EVE-O Preview layout",
            initialfile="EVE-O-Preview.json",
            filetypes=[("EVE-O Preview config", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            text = _read_text_file_for_import(path)
        except Exception as e:
            log.exception("[preview] EVE-O import read failed")
            messagebox.showerror("Import EVE-O layout",
                                 f"Could not read the file:\n{e}")
            return
        parsed = self.parse_eveo_config(text)
        summary = self._preview_merge_eveo(parsed)
        lines = [
            f"Imported from:\n{path}\n",
            f"Layouts added: {summary['added_layouts']}",
            f"Focus hotkeys added: {summary['added_hotkeys']}",
            f"Cycle-order entries added: {summary['added_order']}",
            "\nExisting FCTool entries were kept (fill-only merge).",
        ]
        if parsed["errors"]:
            shown = parsed["errors"][:8]
            more = len(parsed["errors"]) - len(shown)
            lines.append("\nSkipped entries:")
            lines.extend(f"  • {e}" for e in shown)
            if more > 0:
                lines.append(f"  • …and {more} more")
        messagebox.showinfo("Import EVE-O layout", "\n".join(lines))

    def _overlay_toggle_changed(self):
        want = self._overlay_enabled_var.get()
        if want:
            self._overlay_enable()
        else:
            self._overlay_disable()
        self._save_config()

    def _overlay_cycle_color(self):
        colors = [c for _, c in self._OVERLAY_COLOR_CYCLE]
        try:
            idx = colors.index(self._overlay_color_val)
        except ValueError:
            idx = -1
        self._overlay_color_val = colors[(idx + 1) % len(colors)]
        self._overlay_color_btn.config(fg=self._overlay_color_val)
        self._overlay_apply_style()

    def _overlay_apply_style(self):
        cfg = self._overlay_cfg()
        try:
            cfg["font_size"] = int(self._overlay_size_var.get())
        except (tk.TclError, ValueError):
            pass
        cfg["color"] = self._overlay_color_val
        cfg["anchor"] = self._OVERLAY_ANCHOR_TO_CFG.get(
            self._overlay_anchor_var.get(), "top-left")
        if self._overlay is not None:
            try:
                self._overlay.set_font_size(cfg["font_size"])
                self._overlay.set_color(cfg["color"])
                self._overlay.set_anchor(cfg["anchor"])
            except Exception:
                pass
        # caption-onvideo (Change 2): push the same color/size/anchor to every
        # EXISTING native tile so editing settings updates the on-video label
        # live — this is what fixes bug (ii) (the native strip/label was styled
        # from FIXED Tk styling before; now it follows config['overlay']).
        for tile in getattr(self, "_preview_tiles", {}).values():
            set_style = getattr(tile, "set_label_style", None)
            if set_style is None:
                continue
            try:
                set_style(color=cfg["color"], size=cfg["font_size"],
                          anchor=cfg["anchor"])
            except Exception:
                pass
        self._save_config()

    def _open_overlay_rules_dialog(self):
        cfg = self._overlay_cfg()
        win = tk.Toplevel(self.root)
        win.title("Overlay label rules")
        win.configure(bg=BG_PANEL)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass

        tk.Label(win, text="Rules (first match wins):", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 0))
        tk.Label(win,
                 text="— first matching rule wins; a character override beats "
                      "all rules",
                 bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 8)).pack(anchor="w", padx=10, pady=(0, 2))
        rules_frame = tk.Frame(win, bg=BG_PANEL)
        rules_frame.pack(fill=tk.X, padx=10)
        # Static column headers gridded ABOVE the dynamic rows frame so they
        # persist across add/delete re-renders (the row renderer only ever
        # touches its own inner frame). The '✕' delete column has no header.
        _rh = lambda c, t: tk.Label(
            rules_frame, text=t, bg=BG_PANEL, fg=FG_DIM,
            font=("Consolas", 8, "bold")).grid(row=0, column=c, sticky="w",
                                               padx=2, pady=(0, 1))
        _rh(0, "When"); _rh(1, "Value"); _rh(2, "Label")
        rule_rows_frame = tk.Frame(rules_frame, bg=BG_PANEL)
        rule_rows_frame.grid(row=1, column=0, columnspan=4, sticky="we")

        when_values = ["ship_group", "ship_type", "system", "docked",
                       "offline", "capital", "subcap"]
        # rule_rows is the backing data list; each entry is a dict carrying the
        # row's live Tk vars/widgets plus its own '✕' delete button. Deletes
        # remove the entry from this list and re-render the whole frame from it,
        # so mid-list removals never leave stale grid indices / var mismatches.
        rule_rows = []   # list of dicts: {when, value_entry, label, del_btn}

        def _render_rule_rows():
            for w in rule_rows_frame.winfo_children():
                w.grid_forget()
            for r, row in enumerate(rule_rows):
                row["when_combo"].grid(row=r, column=0, padx=2, pady=1)
                row["value_entry"].grid(row=r, column=1, padx=2)
                row["label_entry"].grid(row=r, column=2, padx=2)
                row["del_btn"].grid(row=r, column=3, padx=(2, 0))

        def _delete_rule_row(row):
            try:
                rule_rows.remove(row)
            except ValueError:
                return
            for w in (row["when_combo"], row["value_entry"],
                      row["label_entry"], row["del_btn"]):
                try:
                    w.destroy()
                except tk.TclError:
                    pass
            _render_rule_rows()

        def _add_rule_row(when="ship_group", value="", label=""):
            wv = tk.StringVar(value=when)
            lv = tk.StringVar(value=label)
            when_combo = ttk.Combobox(rule_rows_frame, textvariable=wv,
                                      values=when_values, state="readonly",
                                      width=11, font=("Consolas", 9))
            # value: autocomplete SCOPED to the chosen `when` kind (ship_group ->
            # group names, ship_type -> ship type names incl. shuttles, system ->
            # system names). Valueless kinds disable the field entirely.
            ve = AutocompleteEntry(
                rule_rows_frame, self._overlay_rule_value_suggestions(when),
                font=("Consolas", 9), bg=BG_ENTRY, fg=FG_WHITE,
                insertbackground=FG_WHITE, width=18)
            if value:
                ve.insert(0, value)

            def _sync_value_source(*_a, _wv=wv, _ve=ve):
                kind = _wv.get().strip()
                # repopulate suggestions from the correct source for this kind
                try:
                    _ve.update_completions(
                        self._overlay_rule_value_suggestions(kind))
                except Exception:
                    pass
                # disable + clear the value field for valueless kinds; the OK
                # handler also ignores any value for these, so this is purely UX
                if kind in self._OVERLAY_VALUELESS_WHENS:
                    try:
                        _ve.delete(0, tk.END)
                        _ve.configure(state="disabled")
                    except tk.TclError:
                        pass
                else:
                    try:
                        _ve.configure(state="normal")
                    except tk.TclError:
                        pass

            when_combo.bind("<<ComboboxSelected>>", _sync_value_source)
            le = tk.Entry(rule_rows_frame, textvariable=lv, font=("Consolas", 9),
                          bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE, width=18)
            row = {"when": wv, "value_entry": ve, "label": lv,
                   "when_combo": when_combo, "label_entry": le}
            row["del_btn"] = ttk.Button(
                rule_rows_frame, text="✕", width=2, style="Dark.TButton",
                command=lambda _row=row: _delete_rule_row(_row))
            rule_rows.append(row)
            # apply the initial enabled/disabled state for the seeded `when`
            _sync_value_source()
            _render_rule_rows()

        for r in cfg.get("rules", []) or []:
            _add_rule_row(r.get("when", "ship_group"), r.get("value", ""),
                          r.get("label", ""))
        if not rule_rows:
            _add_rule_row()

        ttk.Button(win, text="+ Add rule", style="Dark.TButton",
                   command=lambda: _add_rule_row()).pack(anchor="w", padx=10, pady=(2, 8))

        tk.Label(win, text="Overrides (beat rules; empty label hides that char):",
                 bg=BG_PANEL, fg=FG_TEXT, font=("Consolas", 10, "bold")).pack(
                     anchor="w", padx=10, pady=(4, 0))
        tk.Label(win,
                 text="— pin exact text for one character; empty label hides "
                      "that character",
                 bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 8)).pack(anchor="w", padx=10, pady=(0, 2))
        ov_frame = tk.Frame(win, bg=BG_PANEL)
        ov_frame.pack(fill=tk.X, padx=10)
        # Static headers gridded above the dynamic override rows frame (persist
        # across add/delete). The '✕' delete column has no header.
        _oh = lambda c, t: tk.Label(
            ov_frame, text=t, bg=BG_PANEL, fg=FG_DIM,
            font=("Consolas", 8, "bold")).grid(row=0, column=c, sticky="w",
                                               padx=2, pady=(0, 1))
        _oh(0, "Character"); _oh(1, "Label")
        override_rows_frame = tk.Frame(ov_frame, bg=BG_PANEL)
        override_rows_frame.grid(row=1, column=0, columnspan=3, sticky="we")
        char_names = [a.character_name for a in self.esi_accounts
                      if getattr(a, "character_name", None)]
        override_rows = []   # list of dicts: {name, label, combo, label_entry, del_btn}

        def _render_override_rows():
            for w in override_rows_frame.winfo_children():
                w.grid_forget()
            for r, row in enumerate(override_rows):
                row["combo"].grid(row=r, column=0, padx=2, pady=1)
                row["label_entry"].grid(row=r, column=1, padx=2)
                row["del_btn"].grid(row=r, column=2, padx=(2, 0))

        def _delete_override_row(row):
            try:
                override_rows.remove(row)
            except ValueError:
                return
            for w in (row["combo"], row["label_entry"], row["del_btn"]):
                try:
                    w.destroy()
                except tk.TclError:
                    pass
            _render_override_rows()

        def _add_override_row(name="", label=""):
            nv = tk.StringVar(value=name)
            lv = tk.StringVar(value=label)
            combo = ttk.Combobox(override_rows_frame, textvariable=nv,
                                 values=char_names, width=20, font=("Consolas", 9))
            le = tk.Entry(override_rows_frame, textvariable=lv,
                          font=("Consolas", 9), bg=BG_ENTRY, fg=FG_WHITE,
                          insertbackground=FG_WHITE, width=18)
            row = {"name": nv, "label": lv, "combo": combo, "label_entry": le}
            row["del_btn"] = ttk.Button(
                override_rows_frame, text="✕", width=2, style="Dark.TButton",
                command=lambda _row=row: _delete_override_row(_row))
            override_rows.append(row)
            _render_override_rows()

        for name, label in (cfg.get("overrides", {}) or {}).items():
            _add_override_row(name, label)
        if not override_rows:
            _add_override_row()

        ttk.Button(win, text="+ Add override", style="Dark.TButton",
                   command=lambda: _add_override_row()).pack(anchor="w", padx=10, pady=(2, 8))

        def _ok():
            rules_out = []
            for row in rule_rows:
                when = row["when"].get().strip()
                value = row["value_entry"].get().strip()
                label = row["label"].get().strip()
                if not when:
                    continue
                # docked/offline/capital/subcap need no value; the rest need one
                if when in ("ship_group", "ship_type", "system") and not value:
                    continue
                if not label and when not in ("ship_group",):
                    # allow empty label only where a placeholder-only rule is
                    # meaningless; simplest: require a label
                    if not label:
                        continue
                rules_out.append({"when": when, "value": value, "label": label})
            overrides_out = {}
            for row in override_rows:
                name = row["name"].get().strip()
                if not name:
                    continue
                overrides_out[name] = row["label"].get()   # '' allowed = hide
            cfg["rules"] = rules_out
            cfg["overrides"] = overrides_out
            self._save_config()
            try:
                win.destroy()
            except tk.TclError:
                pass

        btns = tk.Frame(win, bg=BG_PANEL)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="OK", style="Dark.TButton",
                   command=_ok).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.LEFT, padx=2)
        # Test hooks: expose the per-row delete buttons so headless tests can
        # drive deletion without pixel-hunting inside the frames.
        win._rule_delete_buttons = lambda: [r["del_btn"] for r in rule_rows]
        win._override_delete_buttons = lambda: [r["del_btn"] for r in override_rows]
        self.root.wait_window(win)

    def _preview_hotkey_preset(self):
        """One-click EVE-O parity: set cycle group 0 to next=F14 / prev=F13,
        WITHOUT touching per-character focus keys (parity with EVE-O's default
        cycle bindings). Persists + re-registers if native is live."""
        cfg = self._preview_cfg()
        hk = cfg.setdefault("hotkeys", {})
        groups = hk.setdefault("groups", [])
        if not groups:
            groups.append({"next": [], "prev": [], "order": []})
        groups[0]["next"] = ["F14"]
        groups[0]["prev"] = ["F13"]
        self._save_config()
        if self._preview_hotkeys is not None:
            try:
                self._preview_restart_hotkeys()
            except Exception:
                log.exception("[preview] hotkey preset re-register failed")

    def _open_preview_hotkeys_dialog(self, _test_no_wait=False):
        """Modal to edit native-preview hotkeys: per-known-character focus keys,
        cycle group 0 next/prev + order, and minimize-all. Mirrors the overlay
        rules dialog conventions (themed Toplevel + transient + grab_set). On OK
        it rebuilds bindings, restarts the service, and surfaces any per-binding
        conflicts (RegisterHotKey error 1409) via hotkey_service.format_error.

        Returns the Toplevel (tests pass _test_no_wait=True to skip wait_window).
        """
        cfg = self._preview_cfg()
        hk = cfg.setdefault("hotkeys", {})
        hk.setdefault("focus", {})
        groups = hk.setdefault("groups", [])
        if not groups:
            groups.append({"next": [], "prev": [], "order": []})
        hk.setdefault("minimize_all", [])

        win = tk.Toplevel(self.root)
        win.title("Preview hotkeys")
        win.configure(bg=BG_PANEL)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass

        tk.Label(win, text="Per-character focus keys "
                 "(e.g. Control+F9 — brings that client to the front):",
                 bg=BG_PANEL, fg=FG_TEXT, font=("Consolas", 10, "bold")).pack(
                     anchor="w", padx=10, pady=(10, 2))
        focus_frame = tk.Frame(win, bg=BG_PANEL)
        focus_frame.pack(fill=tk.X, padx=10)

        # Seed the char Combobox from ESI accounts + currently-live client names.
        known = []
        for a in getattr(self, "esi_accounts", []) or []:
            nm = getattr(a, "character_name", None)
            if nm:
                known.append(nm)
        for c in self._preview_clients.values():
            if not c.is_login and c.char_name and c.char_name not in known:
                known.append(c.char_name)

        focus_rows = []   # {name: StringVar, key: StringVar, status: Label}

        def _add_focus_row(name="", key=""):
            r = len(focus_rows)
            nv = tk.StringVar(value=name)
            kv = tk.StringVar(value=key)
            ttk.Combobox(focus_frame, textvariable=nv, values=known, width=20,
                         font=("Consolas", 9)).grid(row=r, column=0, padx=2, pady=1)
            tk.Entry(focus_frame, textvariable=kv, font=("Consolas", 9), bg=BG_ENTRY,
                     fg=FG_WHITE, insertbackground=FG_WHITE, width=18).grid(
                         row=r, column=1, padx=2)
            st = tk.Label(focus_frame, text="", bg=BG_PANEL, fg=FG_DIM,
                          font=("Consolas", 9), width=26, anchor="w")
            st.grid(row=r, column=2, padx=2)
            focus_rows.append({"name": nv, "key": kv, "status": st})

        for name, key in (hk.get("focus", {}) or {}).items():
            _add_focus_row(name, key)
        if not focus_rows:
            _add_focus_row()

        ttk.Button(win, text="+ Add character", style="Dark.TButton",
                   command=lambda: _add_focus_row()).pack(
                       anchor="w", padx=10, pady=(2, 8))

        # Cycle group 0: next / prev (comma-separated keys) + preset button.
        tk.Label(win, text="Cycle group (next / previous client):", bg=BG_PANEL,
                 fg=FG_TEXT, font=("Consolas", 10, "bold")).pack(
                     anchor="w", padx=10, pady=(4, 2))
        cyc = tk.Frame(win, bg=BG_PANEL)
        cyc.pack(fill=tk.X, padx=10)
        tk.Label(cyc, text="Next", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).grid(row=0, column=0, padx=2, sticky="w")
        next_var = tk.StringVar(value=", ".join(groups[0].get("next", []) or []))
        tk.Entry(cyc, textvariable=next_var, font=("Consolas", 9), bg=BG_ENTRY,
                 fg=FG_WHITE, insertbackground=FG_WHITE, width=18).grid(
                     row=0, column=1, padx=2)
        tk.Label(cyc, text="Prev", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).grid(row=0, column=2, padx=2, sticky="w")
        prev_var = tk.StringVar(value=", ".join(groups[0].get("prev", []) or []))
        tk.Entry(cyc, textvariable=prev_var, font=("Consolas", 9), bg=BG_ENTRY,
                 fg=FG_WHITE, insertbackground=FG_WHITE, width=18).grid(
                     row=0, column=3, padx=2)

        def _apply_preset():
            self._preview_hotkey_preset()
            g0 = self._preview_cfg().get("hotkeys", {}).get("groups", [{}])[0]
            next_var.set(", ".join(g0.get("next", []) or []))
            prev_var.set(", ".join(g0.get("prev", []) or []))

        ttk.Button(win, text="Use EVE-O preset (F14 / F13)", style="Dark.TButton",
                   command=_apply_preset).pack(anchor="w", padx=10, pady=(2, 8))

        # Cycle order (one char key per line; blank = live sorted order).
        tk.Label(win, text="Cycle order (one character per line; blank = "
                 "all live, alphabetical):", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).pack(anchor="w", padx=10, pady=(0, 2))
        order_txt = tk.Text(win, height=4, width=40, font=("Consolas", 9),
                            bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE)
        order_txt.pack(anchor="w", padx=10, pady=(0, 8))
        order_txt.insert("1.0", "\n".join(groups[0].get("order", []) or []))

        # Minimize-all.
        tk.Label(win, text="Minimize all clients:", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 10, "bold")).pack(anchor="w", padx=10, pady=(4, 2))
        minall_var = tk.StringVar(value=", ".join(hk.get("minimize_all", []) or []))
        tk.Entry(win, textvariable=minall_var, font=("Consolas", 9), bg=BG_ENTRY,
                 fg=FG_WHITE, insertbackground=FG_WHITE, width=18).pack(
                     anchor="w", padx=10, pady=(0, 4))

        status_lbl = tk.Label(win, text="", bg=BG_PANEL, fg=FG_DIM,
                              font=("Consolas", 9), justify=tk.LEFT, wraplength=420)
        status_lbl.pack(anchor="w", padx=10, pady=(2, 4))

        def _split(text):
            return [p.strip() for p in str(text).replace("\n", ",").split(",")
                    if p.strip()]

        def _ok():
            focus_out = {}
            for row in focus_rows:
                name = row["name"].get().strip().lower()
                key = row["key"].get().strip()
                if name and key:
                    focus_out[name] = key
            hk["focus"] = focus_out
            groups[0]["next"] = _split(next_var.get())
            groups[0]["prev"] = _split(prev_var.get())
            groups[0]["order"] = [
                ln.strip().lower()
                for ln in order_txt.get("1.0", "end").splitlines() if ln.strip()]
            hk["minimize_all"] = _split(minall_var.get())
            self._save_config()
            # Re-register + surface conflicts. Restart even if native isn't live
            # yet only when a service already exists; otherwise enable path does it.
            failures = {}
            if self._preview_hotkeys is not None:
                try:
                    svc = self._preview_restart_hotkeys()
                    failures = dict(getattr(svc, "failures", {}) or {})
                except Exception:
                    log.exception("[preview] hotkey re-register failed")
            # Report any conflicts inline; the dialog stays open so the user can fix.
            _bindings, actions, errors = self._preview_hotkey_bindings(
                hk, {c.key for c in self._preview_clients.values()})
            msgs = list(errors)
            for hk_id, code in failures.items():
                act = actions.get(hk_id)
                label = _describe_action(act)
                msgs.append(f"{label}: {hotkey_service.format_error(code)}")
            if msgs:
                status_lbl.config(text="  •  ".join(msgs), fg="#ff9500")
            else:
                try:
                    win.destroy()
                except tk.TclError:
                    pass

        def _describe_action(act):
            if not act:
                return "hotkey"
            if act[0] == "focus":
                return f"focus {act[1]}"
            if act[0] == "cycle":
                return "cycle " + ("next" if act[2] > 0 else "prev")
            if act[0] == "minall":
                return "minimize all"
            return "hotkey"

        btns = tk.Frame(win, bg=BG_PANEL)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="OK", style="Dark.TButton",
                   command=_ok).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.LEFT, padx=2)
        if not _test_no_wait:
            self.root.wait_window(win)
        return win

    def _preview_update_shown_summary(self):
        """Refresh the '(N of M shown)' label next to the Previews… button."""
        lbl = getattr(self, "_preview_shown_summary_lbl", None)
        if lbl is None:
            return
        try:
            known = self._preview_all_known_chars()
            shown = self._preview_shown_chars(
                known, self._preview_cfg().get("disabled_chars", []))
            lbl.config(text=f"({len(shown)} of {len(known)} shown)")
        except tk.TclError:
            pass

    def _preview_apply_shown_chars(self):
        """Persist the Previews… checklist to `disabled_chars` (show-oriented:
        an UNchecked box disables that character). Never deletes a layout / size
        / hotkey — hiding a preview only stops it from being shown. Pushes the
        new shown set to the settings summary and the GamelogMonitor scope."""
        cfg = self._preview_cfg()
        disabled = []
        for key, var in getattr(self, "_preview_show_vars", {}).items():
            try:
                shown = bool(var.get())
            except tk.TclError:
                shown = True
            if not shown:
                disabled.append(key)
        cfg["disabled_chars"] = sorted(disabled)   # deterministic order
        self._save_config()
        self._preview_update_shown_summary()
        self._preview_sync_gamelog_scope()

    def _open_preview_previews_dialog(self, _test_no_wait=False):
        """First-class 'which previews to show' modal (Task C2). Lists every
        character ever seen (ESI accounts ∪ live clients ∪ saved layouts) with a
        checkbox = 'show this preview' (checked = visible). Unchecking hides +
        withdraws that tile on the next tick and NEVER deletes its layout / size /
        hotkey. Includes Show-all / Hide-all and a live '(N of M shown)' count.

        Returns the Toplevel (tests pass _test_no_wait=True to skip wait_window)."""
        known = sorted(self._preview_all_known_chars())
        disabled = {str(k).strip().lower()
                    for k in (self._preview_cfg().get("disabled_chars", []) or [])}

        win = tk.Toplevel(self.root)
        win.title("Which previews to show")
        win.configure(bg=BG_PANEL)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass

        tk.Label(win, text="Check a character to show its live preview. "
                 "Unchecking only hides it — layouts and hotkeys are kept.",
                 bg=BG_PANEL, fg=FG_TEXT, font=("Consolas", 10),
                 justify=tk.LEFT, wraplength=420).pack(
                     anchor="w", padx=10, pady=(10, 6))

        count_lbl = tk.Label(win, text="", bg=BG_PANEL, fg=FG_DIM,
                             font=("Consolas", 9))
        count_lbl.pack(anchor="w", padx=10)

        self._preview_show_vars = {}

        def _refresh_count():
            shown = sum(1 for v in self._preview_show_vars.values()
                        if _safe_get(v))
            count_lbl.config(text=f"({shown} of {len(self._preview_show_vars)} shown)")

        def _safe_get(v):
            try:
                return bool(v.get())
            except tk.TclError:
                return True

        list_frame = tk.Frame(win, bg=BG_PANEL)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        if not known:
            tk.Label(list_frame, text="(no characters seen yet)", bg=BG_PANEL,
                     fg=FG_DIM, font=("Consolas", 9)).pack(anchor="w")
        for key in known:
            var = tk.BooleanVar(value=(key not in disabled))
            self._preview_show_vars[key] = var
            tk.Checkbutton(
                list_frame, text=key, variable=var,
                command=_refresh_count, font=("Consolas", 10),
                fg=FG_TEXT, bg=BG_PANEL, selectcolor=BG_ENTRY,
                activebackground=BG_PANEL, activeforeground=FG_TEXT,
                anchor="w").pack(anchor="w", fill=tk.X)
        _refresh_count()

        def _set_all(value):
            for v in self._preview_show_vars.values():
                try:
                    v.set(value)
                except tk.TclError:
                    pass
            _refresh_count()

        tools = tk.Frame(win, bg=BG_PANEL)
        tools.pack(fill=tk.X, padx=10, pady=(2, 4))
        ttk.Button(tools, text="Show all", style="Dark.TButton",
                   command=lambda: _set_all(True)).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(tools, text="Hide all", style="Dark.TButton",
                   command=lambda: _set_all(False)).pack(side=tk.LEFT)

        def _ok():
            self._preview_apply_shown_chars()
            try:
                win.destroy()
            except tk.TclError:
                pass

        btns = tk.Frame(win, bg=BG_PANEL)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="OK", style="Dark.TButton",
                   command=_ok).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.LEFT, padx=2)
        if not _test_no_wait:
            self.root.wait_window(win)
        return win

    def _open_preview_never_minimize_dialog(self, _test_no_wait=False):
        """C3 'never minimize' modal (EVE-O PriorityClients parity). Lists every
        known character with a checkbox = 'exempt from minimize-inactive'. Checked
        keys are stored in `never_minimize` (exact lowercased-key match); they are
        never auto-minimized when you switch away. Editing this list never touches
        layouts, hotkeys, or the shown-characters set.

        Returns the Toplevel (tests pass _test_no_wait=True to skip wait_window)."""
        known = sorted(self._preview_all_known_chars())
        never = {str(k).strip().lower()
                 for k in (self._preview_cfg().get("never_minimize", []) or [])}

        win = tk.Toplevel(self.root)
        win.title("Never minimize")
        win.configure(bg=BG_PANEL)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass

        tk.Label(win, text="Check a character to keep it OPEN when you switch "
                 "away (exempt from Minimize inactive).",
                 bg=BG_PANEL, fg=FG_TEXT, font=("Consolas", 10),
                 justify=tk.LEFT, wraplength=420).pack(
                     anchor="w", padx=10, pady=(10, 6))

        self._preview_never_vars = {}
        list_frame = tk.Frame(win, bg=BG_PANEL)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        if not known:
            tk.Label(list_frame, text="(no characters seen yet)", bg=BG_PANEL,
                     fg=FG_DIM, font=("Consolas", 9)).pack(anchor="w")
        for key in known:
            var = tk.BooleanVar(value=(key in never))
            self._preview_never_vars[key] = var
            tk.Checkbutton(
                list_frame, text=key, variable=var, font=("Consolas", 10),
                fg=FG_TEXT, bg=BG_PANEL, selectcolor=BG_ENTRY,
                activebackground=BG_PANEL, activeforeground=FG_TEXT,
                anchor="w").pack(anchor="w", fill=tk.X)

        def _ok():
            self._preview_apply_never_minimize()
            try:
                win.destroy()
            except tk.TclError:
                pass

        btns = tk.Frame(win, bg=BG_PANEL)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="OK", style="Dark.TButton",
                   command=_ok).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.LEFT, padx=2)
        if not _test_no_wait:
            self.root.wait_window(win)
        return win

    def _preview_apply_never_minimize(self):
        """Persist the checked exemptions into `never_minimize`. Keys are the same
        lowercased char keys used everywhere in the preview subsystem; characters
        not shown in the dialog keep their existing membership untouched."""
        vars_ = getattr(self, "_preview_never_vars", None)
        if vars_ is None:
            return
        cfg = self._preview_cfg()
        existing = {str(k).strip().lower()
                    for k in (cfg.get("never_minimize", []) or [])}
        shown = set(vars_)
        checked = set()
        for key, v in vars_.items():
            try:
                if v.get():
                    checked.add(key)
            except tk.TclError:
                checked.add(key)          # widget gone → keep it exempt (fail safe)
        # Preserve keys not shown in this dialog; replace membership for shown keys.
        cfg["never_minimize"] = sorted((existing - shown) | checked)
        self._save_config()

    def _add_section(self, parent, title):
        tk.Label(parent, text=f"── {title} ──",
                 font=("Consolas", 12, "bold"), fg=FG_ACCENT, bg=BG_DARK
                 ).pack(anchor=tk.W, padx=10, pady=(15, 5))

    def _add_setting(self, parent, label, key, default="", tooltip=None):
        frame = tk.Frame(parent, bg=BG_DARK)
        frame.pack(fill=tk.X, padx=20, pady=2)
        lbl = tk.Label(frame, text=f"{label}:", font=("Consolas", 10),
                       fg=FG_TEXT, bg=BG_DARK, width=28, anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        var = tk.StringVar(value=default)
        entry = tk.Entry(frame, textvariable=var, font=("Consolas", 10),
                         bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                         width=40, borderwidth=1, relief=tk.RIDGE)
        entry.pack(side=tk.LEFT, padx=5)
        self._setting_entries[key] = var
        if tooltip:
            for _w in (lbl, entry):
                _w.bind("<Enter>",
                        lambda e, t=tooltip: self._show_tooltip(e, t))
                _w.bind("<Leave>", lambda e: self._hide_tooltip())

    def _browse_logs(self):
        path = filedialog.askdirectory(title="Select EVE Chat Logs Folder")
        if path:
            self._logs_path_var.set(path)

    def _on_tracked_character_change(self):
        """Apply tracked character change instantly — restart chat monitor
        so the new listener_filter takes effect without a full settings save."""
        new_tracked = self._char_var.get().strip()
        old_tracked = self.config.get("tracked_character", "")
        if new_tracked == old_tracked:
            return

        self.config["tracked_character"] = new_tracked
        self._save_config()

        # Recreate the chat monitor with the new listener_filter
        logs_path = self.config.get("eve_logs_path", "")
        if logs_path and os.path.isdir(logs_path):
            channel = self.config.get("xup", {}).get("channel_name", "Fleet")
            tracked_char = new_tracked or None
            # Replace existing chat monitor in-place so _chat_poll_loop keeps working
            self.chat_monitor = ChatMonitor(
                logs_path=logs_path,
                poll_interval=self.config.get("poll_interval_seconds", 1.0),
                channel_filter=channel,
                listener_filter=tracked_char,
            )
            self.chat_monitor.on_message(self._on_chat_message)
            char_label = f" ({tracked_char})" if tracked_char else ""
            self._chat_status.config(text=f"CHAT: ON{char_label}", fg=FG_GREEN)

    def _scan_characters(self):
        """Scan log files to find all character names that have fleet channels.
        Results are cached in config.json so the dropdown stays populated
        across app restarts without requiring another scan."""
        logs_path = self._logs_path_var.get()
        if not logs_path or not os.path.isdir(logs_path):
            return
        channel = self.config.get("xup", {}).get("channel_name", "Fleet")
        temp_monitor = ChatMonitor(logs_path, channel_filter=channel)
        listeners = temp_monitor.get_available_listeners()
        if listeners:
            self._char_combo["values"] = [""] + listeners
            # Cache for next startup
            self.config["cached_tracked_characters"] = listeners
            self._save_config()
        else:
            self._char_combo["values"] = [""]

    # ── Intel Channels settings (curation UI) ──────────────────────────────

    def _build_intel_channels_settings(self, parent):
        """Build the Settings-tab 'Intel Channels' curation section.

        Lets the user view/add/remove the tracked intel channels (shared with
        the web UI via config["intel_channels"]["tracked"]). The AutocompleteEntry
        suggests discovered channel names (noise-filtered); the user may still
        free-type any name. 'Scan Channels' discovers channels from the logs dir
        and caches them into config["intel_channels"]["cached_discovered"].
        """
        self._add_section(parent, "Intel Channels")
        tk.Label(parent,
                 text="Channels tracked for Intelligence Fusion "
                      "(shared with the web UI).",
                 font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK
                 ).pack(anchor=tk.W, padx=20)

        # Seed the picker's suggestion pool from cached discovery (noise-filtered).
        ic_cfg = self.config.get("intel_channels", {})
        cached = ic_cfg.get("cached_discovered", []) or []
        self._intel_channel_suggestions: list[str] = filter_suggestion_channels(cached)
        # Seed the Suggested-panel pool (intel/intelligence-named, not tracked)
        # from the same cached discovery so the panel is useful before a scan.
        self._intel_suggested_channels: list[str] = compute_intel_channel_suggestions(
            cached, self._tracked_intel_channels)

        # Split the curation area: existing add/scan/list controls on the left,
        # the 1-click Suggested panel on the right.
        body = tk.Frame(parent, bg=BG_DARK)
        body.pack(fill=tk.X, padx=20, pady=2)
        left_col = tk.Frame(body, bg=BG_DARK)
        left_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right_col = tk.Frame(body, bg=BG_DARK)
        right_col.pack(side=tk.LEFT, fill=tk.Y, padx=(16, 0))

        # Add row: searchable entry + Add + Scan Channels + status.
        add_row = tk.Frame(left_col, bg=BG_DARK)
        add_row.pack(fill=tk.X, pady=2)
        tk.Label(add_row, text="Add channel:", font=("Consolas", 10),
                 fg=FG_TEXT, bg=BG_DARK).pack(side=tk.LEFT, padx=(0, 5))
        self._intel_channel_entry = AutocompleteEntry(
            add_row, self._intel_channel_suggestions,
            font=("Consolas", 10), bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, width=28,
            borderwidth=1, relief=tk.RIDGE,
        )
        self._intel_channel_entry.pack(side=tk.LEFT, padx=5)
        self._intel_channel_entry.bind("<Return>",
                                       lambda e: self._add_intel_channel())
        ttk.Button(add_row, text="Add", style="Green.TButton",
                   command=self._add_intel_channel).pack(side=tk.LEFT, padx=5)
        ttk.Button(add_row, text="Scan Channels", style="Dark.TButton",
                   command=self._scan_intel_channels).pack(side=tk.LEFT, padx=5)
        self._intel_channels_status = tk.Label(
            add_row, text="", font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK,
        )
        self._intel_channels_status.pack(side=tk.LEFT, padx=10)

        # Tracked-channel listbox (dark look, matches staging manager).
        list_row = tk.Frame(left_col, bg=BG_DARK)
        list_row.pack(fill=tk.X, pady=2)
        self._intel_channels_listbox = tk.Listbox(
            list_row, height=5, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_MAGENTA, selectbackground="#1a5a90",
            selectforeground=FG_WHITE, highlightthickness=1,
            highlightbackground=BORDER_COLOR, borderwidth=1, relief=tk.RIDGE,
            activestyle="none", exportselection=False,
        )
        self._intel_channels_listbox.pack(side=tk.LEFT, fill=tk.BOTH,
                                          expand=True, pady=(2, 2))
        self._intel_channels_listbox.bind(
            "<Double-Button-1>", lambda e: self._remove_intel_channel())
        ttk.Button(list_row, text="Remove Selected", style="Dark.TButton",
                   command=self._remove_intel_channel
                   ).pack(side=tk.LEFT, padx=(6, 0), anchor=tk.N)

        # Suggested panel: discovered intel-named channels not yet tracked,
        # each addable in one click. Populated/rebuilt by
        # _refresh_intel_channel_suggestions().
        tk.Label(right_col, text="Suggested (intel channels):",
                 font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK,
                 ).pack(anchor=tk.W)
        self._intel_suggested_frame = tk.Frame(
            right_col, bg=BG_DARK, highlightthickness=1,
            highlightbackground=BORDER_COLOR, borderwidth=0,
        )
        self._intel_suggested_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        self._refresh_intel_channels_listbox()
        self._refresh_intel_channel_suggestions()

    def _refresh_intel_channel_suggestions(self, discovered=None):
        """(Re)draw the Suggested intel-channels panel.

        Recomputes the suggestion list (intel/intelligence-named, minus the
        currently tracked set, deduped) and rebuilds one row per suggestion with
        a 1-click '+ Add' button. When ``discovered`` is provided it becomes the
        new candidate pool (e.g. a fresh scan); otherwise the cached
        ``self._intel_suggested_channels`` pool is recomputed against the
        current tracked list. Shows a dim placeholder when empty. Safe to call
        before the panel exists (no-op) and idempotent."""
        if discovered is not None:
            pool = list(discovered)
        else:
            # Recompute from the last known pool so newly-tracked channels drop
            # out without needing a rescan.
            pool = list(getattr(self, "_intel_suggested_channels", []))
        self._intel_suggested_channels = compute_intel_channel_suggestions(
            pool, self._tracked_intel_channels)

        frame = getattr(self, "_intel_suggested_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()

        if not self._intel_suggested_channels:
            tk.Label(frame, text="(none — Scan to refresh)",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK,
                     ).pack(anchor=tk.W, padx=6, pady=4)
            return

        for name in self._intel_suggested_channels:
            row = tk.Frame(frame, bg=BG_DARK)
            row.pack(fill=tk.X, padx=4, pady=1)
            ttk.Button(row, text="+ Add", style="Green.TButton", width=6,
                       command=lambda n=name: self._add_intel_channel_by_name(n)
                       ).pack(side=tk.LEFT)
            tk.Label(row, text=name, font=("Consolas", 10),
                     fg=FG_MAGENTA, bg=BG_DARK, anchor=tk.W,
                     ).pack(side=tk.LEFT, padx=(6, 0))

    def _refresh_intel_channels_listbox(self):
        """Redraw the tracked-channel listbox from self._tracked_intel_channels."""
        box = getattr(self, "_intel_channels_listbox", None)
        if box is None:
            return
        box.delete(0, tk.END)
        for name in self._tracked_intel_channels:
            box.insert(tk.END, name)

    def _add_intel_channel(self):
        """Add the channel typed/selected in the entry to the tracked list.

        Thin wrapper over :meth:`_add_intel_channel_by_name`; clears the entry
        on a successful (non-blank) add. Free-typed names are accepted (a
        channel may have no recent log)."""
        name = self._intel_channel_entry.get().strip()
        if not name:
            return
        self._intel_channel_entry.delete(0, tk.END)
        self._add_intel_channel_by_name(name)

    def _add_intel_channel_by_name(self, name: str):
        """Core add path shared by the Add button and the Suggested chips.

        Normalizes (de-dupes case-insensitively) the tracked list with ``name``
        appended. If the list is unchanged (already tracked, or blank), it is a
        no-op with an inline notice. Otherwise it persists, refreshes the
        listbox + Intel-panel checkboxes, refreshes the Suggested panel (so the
        just-added channel drops out of suggestions), and sets a status."""
        name = (name or "").strip()
        if not name:
            return
        before = len(self._tracked_intel_channels)
        self._tracked_intel_channels = normalize_tracked_channels(
            self._tracked_intel_channels + [name]
        )
        if len(self._tracked_intel_channels) == before:
            self._intel_channels_status.config(
                text=f"{name} already tracked", fg=FG_YELLOW)
            return
        self._save_tracked_intel_channels()
        self._refresh_intel_channels_listbox()
        self._rebuild_intel_channel_checkboxes()
        self._refresh_intel_channel_suggestions()
        self._intel_channels_status.config(text=f"Added {name}", fg=FG_GREEN)

    def _remove_intel_channel(self):
        """Remove the selected channel from the tracked list, persist, and
        refresh the listbox + Intel-panel checkboxes."""
        box = getattr(self, "_intel_channels_listbox", None)
        if box is None:
            return
        sel = box.curselection()
        if not sel:
            return
        name = box.get(sel[0])
        self._tracked_intel_channels = [
            c for c in self._tracked_intel_channels
            if c.strip().lower() != name.strip().lower()
        ]
        self._save_tracked_intel_channels()
        self._refresh_intel_channels_listbox()
        self._rebuild_intel_channel_checkboxes()
        # If the removed channel is an intel-named one still in the discovered
        # pool, it re-appears as a suggestion.
        self._refresh_intel_channel_suggestions()
        self._intel_channels_status.config(text=f"Removed {name}", fg=FG_DIM)

    def _scan_intel_channels(self):
        """Discover channels from the logs dir (off the Tk main thread) to
        populate the picker's suggestion pool, the Suggested panel, and the
        cache.

        discover_channels() does directory + header I/O, so it runs on a worker
        thread; results are applied back on the Tk main thread via root.after.

        The scan deliberately passes ``tracked_character=None`` (every character)
        rather than the currently-selected character: intel channels are often
        logged under an alt, so narrowing to the selected character would miss
        them. The Suggested panel then surfaces any intel-named channel found in
        the logs regardless of which alt opened it."""
        logs_path = self._logs_path_var.get()
        if not logs_path or not os.path.isdir(logs_path):
            self._intel_channels_status.config(
                text="Set a valid EVE Chat Logs path first", fg=FG_ORANGE)
            return
        self._intel_channels_status.config(text="Scanning...", fg=FG_ACCENT)

        def worker():
            try:
                # tracked_character=None -> scan across ALL characters so
                # channels logged under an alt are still surfaced.
                found = discover_channels(logs_path, tracked_character=None,
                                          max_age_days=30)
                names = [d["name"] for d in found]
            except Exception:
                names = []
            self.root.after(0, self._apply_scanned_intel_channels, names)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_scanned_intel_channels(self, discovered_names):
        """Apply discovery results on the Tk main thread: cache ALL discovered
        names, refresh the (noise-filtered) autocomplete pool, and refresh the
        Suggested panel from the fresh discovered set."""
        # Cache the full discovery set (unfiltered — anything can be added).
        ic = self.config.setdefault("intel_channels", {})
        ic["cached_discovered"] = list(discovered_names)
        self._save_config()
        # Autocomplete suggestions hide obvious non-intel system channels.
        self._intel_channel_suggestions = filter_suggestion_channels(
            discovered_names)
        entry = getattr(self, "_intel_channel_entry", None)
        if entry is not None:
            entry.update_completions(self._intel_channel_suggestions)
        # Suggested panel uses the intel/intelligence predicate (not the
        # autocomplete denylist) against the fresh discovered set, minus tracked.
        self._refresh_intel_channel_suggestions(discovered=discovered_names)
        self._intel_channels_status.config(
            text=f"Found {len(self._intel_channel_suggestions)} channel(s), "
                 f"{len(self._intel_suggested_channels)} suggested",
            fg=FG_GREEN)

    def _set_autostart(self, enabled: bool):
        """Add or remove FCTool from Windows startup via Start Menu shortcut."""
        try:
            import subprocess
            startup_dir = os.path.join(
                os.environ.get("APPDATA", ""),
                "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
            )
            shortcut_path = os.path.join(startup_dir, "FCTool.lnk")

            if enabled:
                # When frozen as EXE, link directly to the executable
                if getattr(sys, "frozen", False):
                    target_path = sys.executable
                else:
                    target_path = os.path.join(app_dir(), "FCTool.bat")
                if not os.path.exists(target_path):
                    print(f"[FCTool] {target_path} not found, cannot create startup shortcut")
                    return

                # Create shortcut via PowerShell
                ps_cmd = (
                    f'$ws = New-Object -ComObject WScript.Shell; '
                    f'$s = $ws.CreateShortcut("{shortcut_path}"); '
                    f'$s.TargetPath = "{target_path}"; '
                    f'$s.WorkingDirectory = "{os.path.dirname(target_path)}"; '
                    f'$s.WindowStyle = 7; '
                    f'$s.Save()'
                )
                subprocess.run(["powershell", "-Command", ps_cmd],
                               capture_output=True, timeout=10)
                print(f"[FCTool] Startup shortcut created: {shortcut_path}")
            else:
                if os.path.exists(shortcut_path):
                    os.remove(shortcut_path)
                    print("[FCTool] Startup shortcut removed")
        except Exception as e:
            print(f"[FCTool] Error setting autostart: {e}")

    def _save_settings(self):
        """Gather all settings and save to config.json."""
        self.config["eve_logs_path"] = self._logs_path_var.get()
        self.config["tracked_character"] = self._char_var.get().strip()

        # Staging system
        staging = self._staging_entry.get().strip()
        self.config.setdefault("zkillboard", {})["staging_system"] = staging
        self._staging_display.config(
            text=f"Staging: {staging}" if staging else "Staging: --"
        )

        # Handle autostart toggle
        new_autostart = self._autostart_var.get()
        old_autostart = self.config.get("autostart", False)
        self.config["autostart"] = new_autostart
        if new_autostart != old_autostart:
            self._set_autostart(new_autostart)

        # Sound setting
        self.config["sound_on_ready"] = self._sound_var.get()
        self._sound_enabled = self._sound_var.get()

        # Ansiblex connections
        ansiblex_lines = self._ansiblex_text.get("1.0", tk.END).strip().split("\n")
        ansiblex_pairs = []
        for line in ansiblex_lines:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2 and parts[0] and parts[1]:
                ansiblex_pairs.append(parts)
        self.config["ansiblex_connections"] = ansiblex_pairs
        # Re-resolve Ansiblex IDs
        self._resolve_ansiblex_async()

        # Intel channels — gather the tracked-channel listbox (mirrors the
        # ansiblex parse-back). The listbox is kept in sync with
        # self._tracked_intel_channels on every add/remove, but reading it here
        # guards against drift. cached_discovered is preserved as-is.
        ic_box = getattr(self, "_intel_channels_listbox", None)
        if ic_box is not None:
            listed = [ic_box.get(i) for i in range(ic_box.size())]
            self._tracked_intel_channels = normalize_tracked_channels(listed)
        ic_cfg = self.config.setdefault("intel_channels", {})
        ic_cfg["tracked"] = list(self._tracked_intel_channels)

        self.config.setdefault("xup", {})
        self.config["xup"]["trigger_word"] = self._setting_entries["xup_trigger"].get()
        self.config["xup"]["fire_word"] = self._setting_entries["xup_fire"].get()
        self.config["xup"]["channel_name"] = self._setting_entries["xup_channel"].get()
        # Threshold is controlled from the Fleet Management tab spinbox only
        self.config["xup"]["threshold"] = self.config.get("xup", {}).get("threshold", 50)

        self.config.setdefault("zkillboard", {})
        self.config["zkillboard"]["enabled"] = self._zkill_enabled_var.get()
        # min_pilots_involved is no longer edited here; the Intelligence-tab
        # spinbox (config["intel_filter"]["min_pilots"]) is the single source of
        # truth. The key is preserved as-is so the one-time migration seed and
        # any existing readers keep working.

        self._save_config()
        self._save_status.config(text="Saved!", fg=FG_GREEN)

        # Restart modules
        self._stop_monitoring()
        self.config = self._load_config()
        # Re-sync tracked intel channels from the reloaded config and rebuild
        # the Intel-panel checkboxes so the panel reflects edits without an app
        # restart. (If intel fusion is currently running, the new channel_filters
        # apply on the next fusion toggle; the checkboxes/tracked set update now.)
        self._tracked_intel_channels = normalize_tracked_channels(
            self.config.get("intel_channels", {}).get("tracked"),
            seed=sorted(INTEL_CHANNELS),
        )
        self._rebuild_intel_channel_checkboxes()
        self._setup_modules()
        self._start_monitoring()
        self._save_status.config(text="Saved!", fg=FG_GREEN)

    def _autosave_staging_system(self, *args):
        val = self._staging_entry.get().strip()
        self.config.setdefault("zkillboard", {})["staging_system"] = val
        self._save_config()

    # ── Module Setup ──────────────────────────────────────────────────────────

    def _setup_modules(self):
        # Fleet Management
        xup_cfg = self.config.get("xup", {})
        self.xup_counter = XUpCounter(
            trigger_word=xup_cfg.get("trigger_word", "x"),
            fire_word=xup_cfg.get("fire_word", "FIRE"),
            threshold=xup_cfg.get("threshold", 50),
            case_sensitive=xup_cfg.get("case_sensitive", False),
            on_ready=self._on_xup_ready,
            on_fire=self._on_xup_fire,
            on_update=self._on_xup_update,
        )

        # Chat Monitor
        logs_path = self.config.get("eve_logs_path", "")
        if logs_path and os.path.isdir(logs_path):
            channel = xup_cfg.get("channel_name", "Fleet")
            tracked_char = self.config.get("tracked_character", "") or None
            self.chat_monitor = ChatMonitor(
                logs_path=logs_path,
                poll_interval=self.config.get("poll_interval_seconds", 1.0),
                channel_filter=channel,
                listener_filter=tracked_char,
            )
            self.chat_monitor.on_message(self._on_chat_message)
            char_label = f" ({tracked_char})" if tracked_char else ""
            self._chat_status.config(text=f"CHAT: ON{char_label}", fg=FG_GREEN)
        else:
            self.chat_monitor = None
            self._chat_status.config(text="CHAT: NO PATH", fg=FG_ORANGE)

        # zKillboard
        zk_cfg = self.config.get("zkillboard", {})
        if zk_cfg.get("enabled"):
            # Monitor runs with watch_all=True and min_pilots=1 to capture
            # everything; GUI-only filters (All K-Space, min pilots, max jumps)
            # are applied at display time so they don't affect what the
            # monitor captures.
            self.zkill_monitor = ZKillMonitor(
                watch_regions=zk_cfg.get("watch_regions", []),
                watch_alliances=zk_cfg.get("watch_alliances", []),
                watch_systems=zk_cfg.get("watch_systems", []),
                min_kill_value_millions=zk_cfg.get("min_kill_value_millions", 0),
                min_pilots_involved=1,
                alert_window_seconds=zk_cfg.get("alert_window_seconds", 300),
                on_alert=self._on_zkill_alert,
                watch_all=True,
                friendly_ids=set(self._standings_cache.friendly_ids),
            )
            self._zkill_status.config(text="ZKILL: ON", fg=FG_GREEN)
        else:
            self.zkill_monitor = None
            self._zkill_status.config(text="ZKILL: OFF", fg=FG_DIM)

        # Jump Range
        jr_cfg = self.config.get("jump_range", {})
        self.jump_checker = JumpRangeChecker(
            ship_type=jr_cfg.get("ship_type", "Dreadnought"),
            jdc_level=jr_cfg.get("jump_drive_calibration_level", 5),
            custom_ranges=jr_cfg.get("ranges_ly"),
        )

    # ── Monitoring ────────────────────────────────────────────────────────────

    def _start_monitoring(self):
        self._running = True
        if self.chat_monitor:
            self._chat_thread = threading.Thread(target=self._chat_poll_loop, daemon=True)
            self._chat_thread.start()
        if self.zkill_monitor:
            self.zkill_monitor.start()

    def _stop_monitoring(self):
        self._running = False
        if self.zkill_monitor:
            self.zkill_monitor.stop()
        if hasattr(self, '_intel_monitor') and self._intel_monitor:
            self._intel_monitor.stop()

    def _chat_poll_loop(self):
        while self._running:
            try:
                if self.chat_monitor:
                    self.chat_monitor.poll()
            except Exception:
                pass
            time.sleep(self.config.get("poll_interval_seconds", 1.0))

    # ── Callbacks (threadsafe via root.after) ─────────────────────────────────

    def _on_chat_message(self, msg: ChatMessage):
        if self.xup_counter:
            self.xup_counter.process_message(msg)
        # Record command-burst "charge up" calls. This callback is only ever
        # registered on self.chat_monitor, which is constructed with
        # channel_filter=<fleet channel name>; the monitor only tails log files
        # whose basename starts with that channel (chat_monitor._discover_files),
        # so every msg reaching here is already a fleet-channel message — the
        # same structural filter the x-up counter above relies on. There is no
        # per-message channel predicate to reuse, so we record unconditionally.
        if self.charge_tracker.record(msg.sender, msg.message):
            self._schedule_booster_refresh()
        # Check role tracker letters (must run on main thread for UI updates)
        self.root.after(0, self._check_role_letters, msg)

    def _on_xup_update(self, state: XUpState):
        self.root.after(0, self._update_xup_display, state)

    def _on_xup_ready(self, state: XUpState):
        self.root.after(0, self._flash_ready, state)

    def _on_xup_fire(self, state: XUpState):
        self.root.after(0, self._show_fire, state)

    # ── Command-burst / charge tracking ───────────────────────────────────────

    def _load_burst_icons(self):
        """Load discipline icons once. self._burst_icons holds the full 64px
        PhotoImages; self._burst_icons_small holds crisp ~21px copies used by
        both the top coverage strip and the per-pilot Links rows (33% larger
        than the former 16px). The small set is a pre-rendered LANCZOS downscale
        of the 64px master (assets/bursts/<disc>_21.png) loaded natively to
        avoid runtime scaling artifacts; if that asset is absent we fall back to
        an integer subsample of the master. Glyph fallback on any failure.
        Called during UI build, after self.root exists."""
        from app_path import bundle_dir
        files = {
            command_bursts.SHIELD: "shield",
            command_bursts.ARMOR: "armor",
            command_bursts.SKIRMISH: "skirmish",
            command_bursts.INFORMATION: "info",
        }
        base = os.path.join(bundle_dir(), "assets", "bursts")
        for disc, stem in files.items():
            try:
                full = tk.PhotoImage(file=os.path.join(base, f"{stem}.png"))
                self._burst_icons[disc] = full
                # Crisp ~21px copy for the inline top strip and per-pilot Links
                # rows. Prefer the pre-rendered downscale; fall back to an
                # integer subsample of the 64px master (no upscaling). References
                # are retained in the dict so Tk won't GC them.
                small_path = os.path.join(base, f"{stem}_21.png")
                if os.path.exists(small_path):
                    self._burst_icons_small[disc] = tk.PhotoImage(file=small_path)
                else:
                    self._burst_icons_small[disc] = full.subsample(3, 3)
            except Exception:
                self._burst_icons[disc] = None  # fall back to Unicode glyph
                self._burst_icons_small[disc] = None

    def _schedule_booster_refresh(self):
        """Coalesce refresh requests onto the Tk loop, then compute off-thread."""
        if self._booster_refresh_pending:
            return
        self._booster_refresh_pending = True
        self.root.after(250, self._run_booster_refresh)

    def _run_booster_refresh(self):
        self._booster_refresh_pending = False
        snapshot = self.charge_tracker.snapshot()
        coverage = self.charge_tracker.coverage()
        roster = dict(self._booster_roster)

        def work():
            from zkill_monitor import resolve_name
            rows = command_bursts.build_pilot_rows(
                snapshot, roster, self._group_of_safe)
            # Resolve hull names here (off the Tk thread) — resolve_name can
            # block on a synchronous network call on a cache miss, so it must
            # not run inside the Tk-thread render path.
            ship_names = {}
            for row in rows:
                tid = row.ship_type_id
                if tid is not None and tid not in ship_names:
                    try:
                        ship_names[tid] = resolve_name(tid, "type")
                    except Exception:
                        ship_names[tid] = None
            # Pre-index rows by lowercased pilot name (off-thread) so the Tk-thread
            # Links render does pure dict lookups when matching pilots to charges.
            rows_by_name = {r.name.lower(): r for r in rows}
            self.root.after(
                0, lambda: self._apply_booster_compute(rows_by_name, coverage, ship_names))

        threading.Thread(target=work, daemon=True).start()

    def _group_of_safe(self, type_id):
        """group_id resolver that never raises (network failure -> None)."""
        try:
            return ship_classes.get_group_id(type_id)
        except Exception:
            return None

    def _apply_booster_compute(self, rows_by_name, coverage, ship_names):
        """Apply off-thread booster compute results, then render (Tk thread only).

        Stores the pre-indexed rows / hull names / boss flag on self (read only on
        the Tk thread, so no lock is needed) and drives the three render steps:
        the always-visible top coverage strip, the non-boss banner, and the Links
        section (per-pilot charges + off-hull posters). Always invoked via
        root.after. Must NOT trigger another refresh / role update (no re-entrancy).
        """
        self._booster_rows_by_name = rows_by_name
        self._booster_ship_names = ship_names
        # Roster is only populated when we can read fleet member ships (fleet
        # boss). Empty roster => hulls can't be verified, so we are treated as
        # non-boss. NOTE: best-effort heuristic — an empty roster also occurs when
        # we ARE the boss but the fleet is empty, or the ESI member fetch failed,
        # so this can be a false positive.
        self._booster_is_boss = bool(self._booster_roster)
        self._render_coverage_strip(coverage)
        self._render_boss_banner()
        self._render_links_section()

    def _render_coverage_strip(self, coverage):
        """Render the always-visible fleet-aggregate coverage strip (Tk thread).

        Clears and rebuilds the children of the persistent _booster_strip, one
        cell per discipline showing the small icon + a ✓/✗ full/missing glyph
        with a tooltip. Runs regardless of boss status."""
        # Drop any tooltip bound to a strip cell about to be destroyed (its
        # <Leave> never fires once destroyed, which would orphan the tooltip).
        self._hide_tooltip()
        for w in self._booster_strip.winfo_children():
            w.destroy()
        for disc in command_bursts.DISCIPLINES:
            status = coverage[disc]
            cell = tk.Frame(self._booster_strip, bg=BG_PANEL)
            cell.pack(side=tk.LEFT, padx=(0, 10))
            icon = self._burst_icons_small.get(disc)
            if icon is not None:
                lbl = tk.Label(cell, image=icon, bg=BG_PANEL)
            else:
                lbl = tk.Label(cell, text=command_bursts.DISCIPLINE_LABEL[disc][:2],
                               bg=BG_PANEL, fg=FG_TEXT, font=("Consolas", 9, "bold"))
            lbl.pack(side=tk.LEFT)
            glyph = "✓" if status.full else "✗"
            color = FG_GREEN if status.full else FG_RED
            mark = tk.Label(cell, text=glyph, bg=BG_PANEL, fg=color,
                            font=("Consolas", 10, "bold"))
            mark.pack(side=tk.LEFT)
            if status.full and status.redundancy >= 2:
                tk.Label(cell, text=f"{status.redundancy}x", bg=BG_PANEL,
                         fg=FG_ACCENT, font=("Consolas", 8, "bold")).pack(side=tk.LEFT)
            if status.full:
                tip = f"{command_bursts.DISCIPLINE_LABEL[disc]} links full (all 3 charges)"
                if status.redundancy >= 2:
                    tip += f" — covered {status.redundancy}x"
            else:
                tip = f"{command_bursts.DISCIPLINE_LABEL[disc]} missing: " + ", ".join(status.missing)
            for wdg in (lbl, mark):
                wdg.bind("<Enter>", lambda e, t=tip: self._show_tooltip(e, t))
                wdg.bind("<Leave>", lambda e: self._hide_tooltip())

    def _render_boss_banner(self):
        """Show/hide the non-boss warning banner (Tk thread).

        Packed on _spec_roles_frame just before the Links container (so it stays
        visible even when the Links section is collapsed); forgotten when boss."""
        if not self._booster_is_boss:
            self._booster_banner.config(
                text="⚠ Ship verification unavailable (not fleet boss) — "
                     "charges shown, hulls not checked")
            self._booster_banner.pack(fill=tk.X, padx=4, pady=(0, 2),
                                      before=self._links_container)
        else:
            self._booster_banner.pack_forget()

    def _render_links_section(self):
        """Render the Links / Command Ships section (Tk thread; pure lookups).

        Single owner of _links_content. When fleet boss, the per-pilot rows are
        decorated with inline booster charges (rows_by_name) and off-hull
        charge-posters are appended flagged; when not boss, charges are hidden and
        only the plain ship-type/pilot listing renders. Uses pre-computed dict
        state only (no network). Must NOT trigger a refresh / role update."""
        rows_by_name = self._booster_rows_by_name if self._booster_is_boss else None
        # Apply doctrine guidance to the links count badge if present. Computed
        # here (not just in the synchronous section loop) because this method also
        # re-renders asynchronously from the booster refresh; recomputing keeps the
        # guided badge after that async render instead of reverting to "(N)".
        # _role_guidance_badge returns None when links are suppressed (high
        # command-ship fraction) or no doctrine is active -> fixed-threshold badge.
        status_override = self._role_guidance_badge("links")
        self._populate_role_section(self._links_content, self._links_count,
                                    self._links_categories,
                                    threshold=self._links_threshold,
                                    rows_by_name=rows_by_name,
                                    status_override=status_override)
        if self._booster_is_boss:
            self._append_offhull_rows()

    def _append_offhull_rows(self):
        """Append off-hull charge-posters into _links_content, flagged (Tk thread).

        Surfaces pilots who posted charges but are NOT in the command-ship
        listing (off-hull / non-command hull). Appended inside _links_content so
        they collapse with the section. The ``listed`` (command-ship) and
        ``offhull`` sets are disjoint, so no pilot is double-rendered."""
        listed = {name.lower()
                  for pilots in self._links_categories.values()
                  for (name, _cid) in pilots}
        offhull = sorted(
            [prow for lname, prow in self._booster_rows_by_name.items()
             if lname not in listed and prow.cells],
            key=lambda r: r.name.lower())
        if not offhull:
            return
        tk.Label(self._links_content, text="  Off-hull charge posters:",
                 font=("Consolas", 8, "bold"), fg=FG_YELLOW, bg=BG_PANEL,
                 anchor=tk.W).pack(anchor=tk.W)
        for prow in offhull:
            self._build_decorated_pilot_row(self._links_content, prow.name, "", prow)

    def _clear_booster_state(self):
        """Reset charge tracking + roster when the fleet/auth context goes away.
        Safe to call from any thread: it only touches plain attributes and
        schedules the refresh via root.after (Tk-thread-safe)."""
        self.charge_tracker.clear()
        self._booster_roster = {}
        self._schedule_booster_refresh()

    def _open_remove_charge_dialog(self):
        """Modal listing pilots with a tracked command-burst/charge record, each
        with an X to remove just that one. Additive to the fleet-wide clear —
        uses the backend ChargeTracker.remove_pilot and mirrors the add-path
        refresh (_schedule_booster_refresh)."""
        pilots = sorted(name for (name, _charges) in self.charge_tracker.snapshot())
        if not pilots:
            messagebox.showinfo(
                "Remove link",
                "There are no tracked command-burst links to remove.",
                parent=self.root)
            return

        win = tk.Toplevel(self.root)
        win.title("Remove command-burst link")
        win.configure(bg=BG_DARK)
        win.geometry("340x420")
        win.minsize(320, 320)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass

        tk.Label(win,
                 text="Remove a single pilot's tracked command-burst charges. "
                      "This does not clear the whole fleet.",
                 font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                 anchor=tk.W, justify=tk.LEFT, wraplength=310).pack(
                     anchor=tk.W, padx=12, pady=(12, 4))

        list_wrap = tk.Frame(win, bg=BG_PANEL, bd=1, relief=tk.RIDGE)
        canvas = tk.Canvas(list_wrap, bg=BG_PANEL, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.configure(yscrollcommand=sb.set)
        inner = tk.Frame(canvas, bg=BG_PANEL)
        _win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(_win, width=e.width))

        def _rebuild():
            for w in inner.winfo_children():
                w.destroy()
            current = {name: ch for (name, ch) in self.charge_tracker.snapshot()}
            if not current:
                tk.Label(inner, text="(no tracked links)", font=("Consolas", 9),
                         fg=FG_DIM, bg=BG_PANEL, anchor=tk.W).pack(
                             anchor=tk.W, padx=4, pady=2)
                return
            for name in sorted(current):
                row = tk.Frame(inner, bg=BG_PANEL)
                row.pack(fill=tk.X, anchor=tk.W, padx=4, pady=1)
                label = f"{name}  ({len(current[name])})"
                tk.Label(row, text=label, font=("Consolas", 9), fg=FG_TEXT,
                         bg=BG_PANEL, anchor=tk.W).pack(
                             side=tk.LEFT, fill=tk.X, expand=True)
                ttk.Button(row, text="X", style="Red.TButton", width=2,
                           command=lambda n=name: _remove(n)).pack(side=tk.RIGHT)

        def _remove(name):
            try:
                removed = self.charge_tracker.remove_pilot(name)
                if removed:
                    self._schedule_booster_refresh()
                    _rebuild()
            except Exception:
                log.exception(
                    "Failed to remove command-burst link for pilot %r", name)

        ttk.Button(win, text="Close", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.BOTTOM, padx=12, pady=12)
        list_wrap.pack(fill=tk.BOTH, expand=True, padx=12)
        win.bind("<Escape>", lambda e: win.destroy())
        _rebuild()

    def _on_zkill_alert(self, alert: KillAlert):
        # Get route from staging
        staging = self.config.get("zkillboard", {}).get("staging_system", "")
        route_info = ""
        if staging:
            try:
                from jump_range import search_system as _ss, get_stargate_route as _gr
                o = _ss(staging)
                d = _ss(alert.system_name)
                conns = self._get_ansiblex_connections()
                if o and d:
                    r = _gr(o, d, connections=conns)
                    if r:
                        route_info = f"{staging} -> {alert.system_name}: **{len(r)-1} jumps**"
            except Exception:
                pass
            alert.route_from_staging = route_info

        self.root.after(0, self._show_zkill_alert, alert)

    # ── UI Update Methods ────────────────────────────────────────────────────

    def _update_xup_display(self, state: XUpState):
        threshold = self.config.get("xup", {}).get("threshold", 50)
        self._xup_count_label.config(text=str(state.count))

        # Update progress bar
        self._xup_canvas.delete("all")
        w = self._xup_canvas.winfo_width()
        if w < 10:
            w = 500
        h = 12
        ratio = min(1.0, state.count / max(threshold, 1))
        fill_w = int(w * ratio)

        # Background
        self._xup_canvas.create_rectangle(0, 0, w, h, fill=BG_DARK, outline="")
        # Fill
        color = FG_GREEN if state.is_ready else FG_ACCENT
        if fill_w > 0:
            self._xup_canvas.create_rectangle(0, 0, fill_w, h, fill=color, outline="")

        # Status
        if state.is_ready:
            self._xup_status.config(text="READY TO FIRE!", fg=FG_GREEN)
            self._xup_count_label.config(fg=FG_GREEN)
        else:
            self._xup_status.config(text="Forming...", fg=FG_ACCENT)
            self._xup_count_label.config(fg=FG_ACCENT)

        # Log only new unique x-ups (skip duplicate x's from the same pilot)
        if state.last_xup_sender and state.last_xup_was_new:
            ts = state.xups[state.last_xup_sender]
            self._append_xup_log(
                f"[{ts.strftime('%H:%M:%S')}] {state.last_xup_sender} x'd up  "
                f"({state.count}/{threshold})\n", "xup"
            )

    def _flash_ready(self, state: XUpState):
        threshold = self.config.get("xup", {}).get("threshold", 50)
        self._xup_status.config(text="READY TO FIRE!", fg=FG_GREEN)
        self._append_xup_log(
            f"\n>>> FLEET READY! {state.count}/{threshold} x-ups <<<\n\n", "ready"
        )
        # Flash the window
        self._flash_title(0)
        # Play fire alert sound (replaces system bell)
        if self._sound_enabled:
            self._play_fire_alert()

    def _play_fire_alert(self):
        """Play the fire alert sound (fire_alert.mp3) using pygame."""
        def _play():
            try:
                import pygame
                from app_path import bundle_dir
                alert_path = os.path.join(app_dir(), "fire_alert.mp3")
                if not os.path.exists(alert_path):
                    alert_path = os.path.join(bundle_dir(), "fire_alert.mp3")
                if not os.path.exists(alert_path):
                    # Fallback to system beep
                    if HAS_WINSOUND:
                        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                    return
                if not pygame.mixer.get_init():
                    pygame.mixer.init()
                pygame.mixer.music.load(alert_path)
                pygame.mixer.music.play()
            except Exception:
                if HAS_WINSOUND:
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        threading.Thread(target=_play, daemon=True).start()

    def _flash_title(self, count):
        if count >= 10:
            self.root.title("FCTool - Fleet Commander Assistant")
            return
        if count % 2 == 0:
            self.root.title(">>> FLEET READY! <<<")
        else:
            self.root.title("FCTool - Fleet Commander Assistant")
        self.root.after(500, self._flash_title, count + 1)

    def _show_fire(self, state: XUpState):
        self._xup_count_label.config(text="0", fg=FG_RED)
        self._xup_status.config(text=f"FIRE #{state.fire_count} - Counter Reset", fg=FG_RED)
        self._append_xup_log(
            f"\n>>> FIRE #{state.fire_count} CALLED - COUNTER RESET <<<\n\n", "fire"
        )
        # Reset color after 2 seconds
        self.root.after(2000, lambda: self._xup_count_label.config(fg=FG_ACCENT))

    def _on_threshold_change(self):
        """Update the x-up threshold live."""
        try:
            new_val = int(self._threshold_var.get())
            if new_val < 1:
                new_val = 1
        except ValueError:
            return
        self.config.setdefault("xup", {})["threshold"] = new_val
        if self.xup_counter:
            self.xup_counter.threshold = new_val
            # Re-evaluate ready state
            self.xup_counter.state.is_ready = self.xup_counter.state.count >= new_val
            self._update_xup_display(self.xup_counter.state)

    def _reset_xup(self):
        if self.xup_counter:
            self.xup_counter.reset()
            self._xup_count_label.config(text="0", fg=FG_ACCENT)
            self._xup_status.config(text="Counter Reset", fg=FG_DIM)
            self._update_xup_display(self.xup_counter.state)
            self._append_xup_log("[Manual Reset]\n", "dim")

    def _open_remove_xup_dialog(self):
        """Modal listing current x-up pilots, each with an X to remove just that
        one. Additive to the full Reset control — uses the backend
        XUpCounter.remove_pilot and mirrors the add-path display refresh."""
        if not self.xup_counter or not self.xup_counter.state.xups:
            messagebox.showinfo(
                "Remove X-up",
                "There are no x-ups to remove.",
                parent=self.root)
            return

        win = tk.Toplevel(self.root)
        win.title("Remove X-up")
        win.configure(bg=BG_DARK)
        win.geometry("320x420")
        win.minsize(300, 320)
        try:
            win.transient(self.root)
            win.grab_set()
        except tk.TclError:
            pass

        tk.Label(win,
                 text="Remove a single pilot's x-up. This does not reset the "
                      "counter.",
                 font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                 anchor=tk.W, justify=tk.LEFT, wraplength=290).pack(
                     anchor=tk.W, padx=12, pady=(12, 4))

        list_wrap = tk.Frame(win, bg=BG_PANEL, bd=1, relief=tk.RIDGE)
        canvas = tk.Canvas(list_wrap, bg=BG_PANEL, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.configure(yscrollcommand=sb.set)
        inner = tk.Frame(canvas, bg=BG_PANEL)
        _win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(_win, width=e.width))

        def _rebuild():
            for w in inner.winfo_children():
                w.destroy()
            if not self.xup_counter or not self.xup_counter.state.xups:
                tk.Label(inner, text="(no x-ups)", font=("Consolas", 9),
                         fg=FG_DIM, bg=BG_PANEL, anchor=tk.W).pack(
                             anchor=tk.W, padx=4, pady=2)
                return
            for name in sorted(self.xup_counter.state.xups):
                row = tk.Frame(inner, bg=BG_PANEL)
                row.pack(fill=tk.X, anchor=tk.W, padx=4, pady=1)
                tk.Label(row, text=name, font=("Consolas", 9), fg=FG_TEXT,
                         bg=BG_PANEL, anchor=tk.W).pack(
                             side=tk.LEFT, fill=tk.X, expand=True)
                ttk.Button(row, text="X", style="Red.TButton", width=2,
                           command=lambda n=name: _remove(n)).pack(side=tk.RIGHT)

        def _remove(name):
            try:
                if not self.xup_counter:
                    return
                removed = self.xup_counter.remove_pilot(name)
                if removed:
                    self._update_xup_display(self.xup_counter.state)
                    self._append_xup_log(f"[Removed {name}]\n", "dim")
                    _rebuild()
            except Exception:
                log.exception("Failed to remove x-up for pilot %r", name)

        ttk.Button(win, text="Close", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.BOTTOM, padx=12, pady=12)
        list_wrap.pack(fill=tk.BOTH, expand=True, padx=12)
        win.bind("<Escape>", lambda e: win.destroy())
        _rebuild()

    # ── Role Tracker Methods ──────────────────────────────────────────────────

    def _rebuild_preset_buttons(self):
        """Rebuild preset buttons from defaults + saved custom presets."""
        # Clear existing buttons from row 1 (keep the "Presets:" label)
        for w in list(self._preset_frame.winfo_children()):
            if isinstance(w, ttk.Button):
                w.destroy()

        # Clear row 2 entirely
        for w in list(self._preset_row2.winfo_children()):
            w.destroy()
        self._preset_row2.pack_forget()

        # Row 1: Default presets
        for label, letter, title, cap in self._default_presets:
            ttk.Button(self._preset_frame, text=label, style="Dark.TButton",
                       command=lambda l=letter, t=title, c=cap: self._add_role_preset(l, t, c)
                       ).pack(side=tk.LEFT, padx=2)

        # Row 2: Custom presets + Clear button
        custom = self.config.get("custom_role_presets", [])
        if custom:
            self._preset_row2.pack(fill=tk.X)
            tk.Label(self._preset_row2, text="Custom:", font=("Consolas", 8),
                     fg=FG_DIM, bg=BG_DARK, width=8, anchor=tk.W
                     ).pack(side=tk.LEFT, padx=(0, 4))
            for p in custom:
                label = p.get("label", "")
                letter = p.get("letter", "")
                title = p.get("title", "")
                cap = p.get("cap")
                # Each custom preset is a group: apply button + rename + delete.
                group = tk.Frame(self._preset_row2, bg=BG_DARK)
                group.pack(side=tk.LEFT, padx=2)
                ttk.Button(group, text=label, style="Dark.TButton",
                           command=lambda l=letter, t=title, c=cap: self._add_role_preset(l, t, c)
                           ).pack(side=tk.LEFT)
                ttk.Button(group, text="R", style="Dark.TButton", width=2,
                           command=lambda lbl=label: self._rename_custom_preset(lbl)
                           ).pack(side=tk.LEFT, padx=(2, 0))
                ttk.Button(group, text="X", style="Red.TButton", width=2,
                           command=lambda lbl=label: self._delete_custom_preset(lbl)
                           ).pack(side=tk.LEFT, padx=(1, 0))
            ttk.Button(self._preset_row2, text="Clear Custom", style="Dark.TButton",
                       command=self._clear_custom_presets
                       ).pack(side=tk.RIGHT, padx=2)

    def _save_role_as_preset(self, slot):
        """Save the current role slot's configuration as a custom preset."""
        letter = slot["letter_var"].get().strip()
        title = slot["title_var"].get().strip()
        if not letter or not title:
            messagebox.showwarning(
                "Save preset",
                "Both a key and a role name are required to save a preset.",
                parent=self.root)
            return
        cap_str = slot["cap_var"].get().strip()
        cap = int(cap_str) if cap_str.isdigit() else None

        # Build label: letter-Title or letter-Title-cap
        label = f"{letter.upper()}-{title}"
        if cap is not None:
            label += f"-{cap}"

        # Check for duplicates and limit
        custom = self.config.get("custom_role_presets", [])
        if len(custom) >= self._MAX_CUSTOM_PRESETS:
            messagebox.showwarning(
                "Save preset",
                f"You already have the maximum of {self._MAX_CUSTOM_PRESETS} "
                "custom presets. Delete one before saving a new preset.",
                parent=self.root)
            return  # At limit
        for p in custom:
            if p.get("label") == label:
                messagebox.showwarning(
                    "Save preset",
                    f"A custom preset named '{label}' already exists.",
                    parent=self.root)
                return  # Already exists
        for dlabel, _, _, _ in self._default_presets:
            if dlabel == label:
                messagebox.showwarning(
                    "Save preset",
                    f"'{label}' matches a built-in preset and cannot be saved "
                    "as a custom preset.",
                    parent=self.root)
                return  # Matches a default

        custom.append({"label": label, "letter": letter, "title": title, "cap": cap})
        self.config["custom_role_presets"] = custom
        self._save_config()
        self._rebuild_preset_buttons()

    def _clear_custom_presets(self):
        """Remove all custom presets."""
        if not messagebox.askyesno(
                "Clear custom presets",
                "Remove ALL custom role presets? This cannot be undone.",
                parent=self.root):
            return
        self.config["custom_role_presets"] = []
        self._save_config()
        self._rebuild_preset_buttons()

    def _delete_custom_preset(self, label):
        """Delete a single custom preset identified by its label.

        Only custom presets are deletable; built-in/default presets are never
        passed here (no delete control is attached to them)."""
        try:
            custom = self.config.get("custom_role_presets", [])
            if not any(p.get("label") == label for p in custom):
                return  # Already gone (stale button) — nothing to do.
            if not messagebox.askyesno(
                    "Delete preset",
                    f"Delete the custom preset '{label}'?",
                    parent=self.root):
                return
            custom = [p for p in custom if p.get("label") != label]
            self.config["custom_role_presets"] = custom
            self._save_config()
            self._rebuild_preset_buttons()
        except Exception:
            log.exception("Failed to delete custom preset %r", label)
            messagebox.showerror(
                "Delete preset",
                "Could not delete the preset. See the log for details.",
                parent=self.root)

    def _rename_custom_preset(self, label):
        """Rename a single custom preset identified by its current label.

        Prompts for a new role name, validates it (non-blank, no collision with
        an existing custom or built-in preset), then rebuilds the label from the
        new name while preserving the preset's letter and cap."""
        try:
            custom = self.config.get("custom_role_presets", [])
            target = next((p for p in custom if p.get("label") == label), None)
            if target is None:
                return  # Stale button — preset no longer exists.

            letter = target.get("letter", "")
            cap = target.get("cap")
            current_title = target.get("title", "")

            new_title = simpledialog.askstring(
                "Rename preset",
                "New role name for this preset:",
                initialvalue=current_title, parent=self.root)
            if new_title is None:
                return  # Cancelled.
            new_title = new_title.strip()
            if not new_title:
                messagebox.showwarning(
                    "Rename preset",
                    "The role name cannot be blank.",
                    parent=self.root)
                return

            # Rebuild label the same way _save_role_as_preset does.
            new_label = f"{letter.upper()}-{new_title}"
            if cap is not None:
                new_label += f"-{cap}"

            if new_label == label:
                return  # No change.

            for p in custom:
                if p is target:
                    continue
                if p.get("label") == new_label:
                    messagebox.showwarning(
                        "Rename preset",
                        f"A custom preset named '{new_label}' already exists.",
                        parent=self.root)
                    return
            for dlabel, _, _, _ in self._default_presets:
                if dlabel == new_label:
                    messagebox.showwarning(
                        "Rename preset",
                        f"'{new_label}' matches a built-in preset and cannot "
                        "be used.",
                        parent=self.root)
                    return

            target["title"] = new_title
            target["label"] = new_label
            self.config["custom_role_presets"] = custom
            self._save_config()
            self._rebuild_preset_buttons()
        except Exception:
            log.exception("Failed to rename custom preset %r", label)
            messagebox.showerror(
                "Rename preset",
                "Could not rename the preset. See the log for details.",
                parent=self.root)

    def _add_role_preset(self, letter: str, title: str, cap: int | None):
        """Add a pre-configured role slot, auto-numbering if duplicates exist."""
        # Count existing slots with the same base title
        base = title.rstrip("0123456789 ")
        count = 0
        for slot in self._role_slots:
            existing = slot["title_var"].get().strip()
            existing_base = existing.rstrip("0123456789 ")
            if existing_base.lower() == base.lower():
                count += 1
        if count > 0:
            # Rename the first one to "Title 1" if it isn't numbered yet
            if count == 1:
                for slot in self._role_slots:
                    existing = slot["title_var"].get().strip()
                    existing_base = existing.rstrip("0123456789 ")
                    if existing_base.lower() == base.lower():
                        slot["title_var"].set(f"{base} 1")
                        break
            numbered_title = f"{base} {count + 1}"
        else:
            numbered_title = title
        self._add_role_slot(letter=letter, title=numbered_title, cap=cap)

    def _add_role_slot(self, letter: str = "", title: str = "", cap: int | None = None):
        """Add a new role tracking slot (letter + title + optional cap + per-person notes)."""
        slot_frame = tk.Frame(self._role_container, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                               highlightbackground=BORDER_COLOR, highlightthickness=1)
        # Grid position is assigned by _relayout_role_slots (called at end)

        top_row = tk.Frame(slot_frame, bg=BG_PANEL)
        top_row.pack(fill=tk.X, padx=6, pady=(4, 1))

        # Collapse toggle
        expanded_var = tk.BooleanVar(value=True)
        arrow_label = tk.Label(top_row, text="\u25BC", font=("Consolas", 10),
                                fg=FG_ACCENT, bg=BG_PANEL, cursor="hand2")
        arrow_label.pack(side=tk.LEFT, padx=(0, 4))

        tk.Label(top_row, text="Key:", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(0, 2))
        letter_var = tk.StringVar(value=letter)
        tk.Entry(top_row, textvariable=letter_var,
                 font=("Consolas", 11, "bold"), width=2,
                 bg=BG_ENTRY, fg=FG_YELLOW,
                 insertbackground=FG_WHITE,
                 borderwidth=1, relief=tk.RIDGE).pack(side=tk.LEFT, padx=(0, 6))

        tk.Label(top_row, text="Role:", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(0, 2))
        title_var = tk.StringVar(value=title)
        tk.Entry(top_row, textvariable=title_var,
                 font=("Consolas", 10), width=14,
                 bg=BG_ENTRY, fg=FG_WHITE,
                 insertbackground=FG_WHITE,
                 borderwidth=1, relief=tk.RIDGE).pack(
                 side=tk.LEFT, padx=(0, 6), fill=tk.X, expand=True)

        count_label = tk.Label(top_row, text="0", font=("Consolas", 10, "bold"),
                                fg=FG_ACCENT, bg=BG_PANEL)
        count_label.pack(side=tk.LEFT, padx=(0, 4))

        # Optional cap (max people for this role)
        cap_var = tk.StringVar(value="")
        cap_label = tk.Label(top_row, text="/", font=("Consolas", 10),
                              fg=FG_DIM, bg=BG_PANEL)
        cap_entry = tk.Entry(top_row, textvariable=cap_var,
                              font=("Consolas", 10), width=3,
                              bg=BG_ENTRY, fg=FG_YELLOW,
                              insertbackground=FG_WHITE,
                              borderwidth=1, relief=tk.RIDGE)
        # Cap is hidden by default; toggled by checkbox or preset
        has_preset_cap = cap is not None
        cap_enabled_var = tk.BooleanVar(value=has_preset_cap)
        if has_preset_cap:
            cap_var.set(str(cap))

        def toggle_cap():
            if cap_enabled_var.get():
                cap_label.pack(side=tk.LEFT)
                cap_entry.pack(side=tk.LEFT, padx=(0, 4))
            else:
                cap_label.pack_forget()
                cap_entry.pack_forget()
                cap_var.set("")

        cap_cb = tk.Checkbutton(top_row, text="Cap",
                                 variable=cap_enabled_var,
                                 font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                                 selectcolor=BG_ENTRY, activebackground=BG_PANEL,
                                 activeforeground=FG_DIM,
                                 command=toggle_cap)
        cap_cb.pack(side=tk.LEFT, padx=(0, 4))

        # Show cap widgets if preset provided a cap value
        if has_preset_cap:
            toggle_cap()

        def remove_slot():
            slot_frame.destroy()
            self._role_slots = [s for s in self._role_slots if s["frame"] is not slot_frame]
            self._relayout_role_slots()

        def copy_people():
            title_txt = slot["title_var"].get() or slot["letter_var"].get().upper()
            names = "\n".join(sorted(slot["people"].keys()))
            if names:
                self.root.clipboard_clear()
                self.root.clipboard_append(f"{title_txt}:\n{names}")
                self.root.update()

        # Button row — on its own line so buttons stay visible/readable
        # regardless of whether the card is in 1- or 2-column layout.
        button_row = tk.Frame(slot_frame, bg=BG_PANEL)
        button_row.pack(fill=tk.X, padx=6, pady=(0, 3))

        # Delete button packed FIRST on the right so it always claims its
        # space even when the card is narrow (2-column layout).
        ttk.Button(button_row, text="X", style="Red.TButton", width=2,
                   command=remove_slot).pack(side=tk.RIGHT, padx=1)
        ttk.Button(button_row, text="Copy", style="Dark.TButton", width=5,
                   command=copy_people).pack(side=tk.LEFT, padx=1)
        ttk.Button(button_row, text="Save", style="Dark.TButton", width=5,
                   command=lambda: self._save_role_as_preset(slot)
                   ).pack(side=tk.LEFT, padx=1)
        ttk.Button(button_row, text="Clear", style="Dark.TButton", width=5,
                   command=lambda: self._clear_role_slot(slot)
                   ).pack(side=tk.LEFT, padx=1)

        # Per-person list: each person gets a row with name + note field
        people_frame = tk.Frame(slot_frame, bg=BG_PANEL)
        people_frame.pack(fill=tk.X, padx=8, pady=(0, 3))

        def toggle_collapse(event=None):
            if expanded_var.get():
                people_frame.pack_forget()
                arrow_label.config(text="\u25B6")
                expanded_var.set(False)
            else:
                people_frame.pack(fill=tk.X, padx=8, pady=(0, 3))
                arrow_label.config(text="\u25BC")
                expanded_var.set(True)

        arrow_label.bind("<Button-1>", toggle_collapse)

        slot = {
            "frame": slot_frame,
            "letter_var": letter_var,
            "title_var": title_var,
            "cap_var": cap_var,
            "cap_enabled_var": cap_enabled_var,
            "people_frame": people_frame,
            "expanded_var": expanded_var,
            "arrow_label": arrow_label,
            "people": {},  # sender -> {"timestamp": dt, "note_var": StringVar, "row": Frame}
            "count_label": count_label,
        }
        self._role_slots.append(slot)
        self._relayout_role_slots()

    def _relayout_role_slots(self):
        """Arrange role slots in the grid — 1 column when few slots, 2 when many."""
        count = len(self._role_slots)
        use_two_cols = count >= self._ROLE_2COL_THRESHOLD

        for i, slot in enumerate(self._role_slots):
            frame = slot.get("frame")
            if not frame:
                continue
            if use_two_cols:
                row, col = divmod(i, 2)
                # Span 2 columns if last odd slot has no neighbor
                if i == count - 1 and count % 2 == 1:
                    frame.grid(row=row, column=0, columnspan=2,
                               sticky="nsew", padx=2, pady=2)
                else:
                    frame.grid(row=row, column=col,
                               sticky="nsew", padx=2, pady=2)
            else:
                frame.grid(row=i, column=0, columnspan=2,
                           sticky="nsew", padx=2, pady=2)

    def _add_person_to_slot(self, slot, sender, timestamp):
        """Add a person row to a role slot with their own note field."""
        if sender in slot["people"]:
            return  # Already listed

        # Check cap if enabled
        if slot["cap_enabled_var"].get():
            try:
                cap = int(slot["cap_var"].get())
                if cap > 0 and len(slot["people"]) >= cap:
                    # Cap reached — log it but don't add
                    title = slot["title_var"].get() or slot["letter_var"].get().upper()
                    self._append_xup_log(
                        f"  {sender} -> {title} (FULL - {cap} cap reached)\n", "dim"
                    )
                    return
            except ValueError:
                pass

        row = tk.Frame(slot["people_frame"], bg=BG_PANEL)
        row.pack(fill=tk.X, pady=1)

        tk.Label(row, text=sender, font=("Consolas", 9, "bold"),
                 fg=FG_GREEN, bg=BG_PANEL, width=20, anchor=tk.W).pack(side=tk.LEFT)

        location_label = tk.Label(row, text="", font=("Consolas", 8),
                                   fg=FG_DIM, bg=BG_PANEL, anchor=tk.W)
        location_label.pack(side=tk.LEFT, padx=(2, 0))

        # Per-person remove control. Pack RIGHT first so it reserves its
        # space at the right edge before the note entry is packed LEFT.
        def remove_person(_sender=sender, _slot=slot, _row=row):
            try:
                _row.destroy()
                if _sender in _slot["people"]:
                    del _slot["people"][_sender]
                self._update_role_count_label(_slot)
            except Exception:
                log.exception("Failed to remove person %r from role slot", _sender)
        ttk.Button(row, text="X", style="Red.TButton", width=2,
                   command=remove_person).pack(side=tk.RIGHT, padx=(4, 0))

        note_var = tk.StringVar()
        note_entry = tk.Entry(row, textvariable=note_var,
                               font=("Consolas", 9), width=30,
                               bg=BG_ENTRY, fg=FG_ORANGE,
                               insertbackground=FG_WHITE,
                               borderwidth=1, relief=tk.RIDGE)
        note_entry.pack(side=tk.LEFT, padx=(4, 0))

        # Placeholder hint so the inline-note field reads as an intentional,
        # editable affordance. The placeholder string is greyed (FG_DIM) and is
        # never treated as a real note: limit_note ignores it, and the field is
        # cleared on focus-in / restored on focus-out only while empty.
        NOTE_PLACEHOLDER = "note..."

        def show_placeholder():
            note_var.set(NOTE_PLACEHOLDER)
            note_entry.config(fg=FG_DIM)

        def clear_placeholder():
            note_var.set("")
            note_entry.config(fg=FG_ORANGE)

        def on_note_focus_in(_=None):
            if note_var.get() == NOTE_PLACEHOLDER:
                clear_placeholder()

        def on_note_focus_out(_=None):
            if not note_var.get():
                show_placeholder()

        # Limit real note input to 30 characters (placeholder is exempt).
        def limit_note(*_):
            val = note_var.get()
            if val == NOTE_PLACEHOLDER:
                return
            if len(val) > 30:
                note_var.set(val[:30])
        note_var.trace_add("write", limit_note)

        note_entry.bind("<FocusIn>", on_note_focus_in)
        note_entry.bind("<FocusOut>", on_note_focus_out)
        show_placeholder()  # start in placeholder state (field is empty)

        slot["people"][sender] = {
            "timestamp": timestamp,
            "note_var": note_var,
            "row": row,
            "location_label": location_label,
        }
        self._update_role_count_label(slot)

    def _update_role_count_label(self, slot):
        """Update a role slot's count label, showing cap if enabled."""
        count = len(slot["people"])
        if slot["cap_enabled_var"].get():
            try:
                cap = int(slot["cap_var"].get())
                if cap > 0:
                    color = FG_GREEN if count >= cap else FG_ACCENT
                    slot["count_label"].config(text=f"{count}/{cap}", fg=color)
                    return
            except ValueError:
                pass
        slot["count_label"].config(text=str(count), fg=FG_ACCENT)

    def _refresh_fleet_locations(self):
        """Periodically fetch fleet member data and update role tracker + composition."""
        if not self.esi_auth or not self.esi_auth.is_authenticated:
            # Clear composition display when not authenticated
            self.root.after(0, self._update_fleet_composition, {}, 0)
            self._clear_booster_state()
            self.root.after(30000, self._refresh_fleet_locations)
            return

        # Track consecutive "no fleet" results so transient ESI errors don't
        # wipe state. Only clear after N consecutive misses (~3 min at 60s backoff).
        if not hasattr(self, "_no_fleet_misses"):
            self._no_fleet_misses = 0
        NO_FLEET_GRACE = 3

        def do_fetch():
            try:
                info = self.esi_auth.get_fleet_info()
                fleet_id = info["fleet_id"] if info else None
                # Only the fleet boss may read /fleets/{id}/members/ (others get
                # a guaranteed 403); non-boss falls into the existing back-off.
                polled_is_boss = bool(
                    info and self.esi_auth.is_boss(
                        info, self.esi_auth.character_id))
                # Cache the primary character's fleet/boss state so the MOTD tab
                # can give instant Set-button feedback without re-querying ESI
                # (see _motd_refresh_fleet_status).
                self._last_polled_fleet_id = fleet_id if info else None
                self._last_polled_fleet_is_boss = polled_is_boss
                if polled_is_boss:
                    members = self.esi_auth.get_fleet_members(fleet_id=fleet_id)
                else:
                    members = None
                if members:
                    # Got a real fleet — reset miss counter
                    self._no_fleet_misses = 0
                    # Derive locations (pass pre-fetched members to avoid duplicate call)
                    locations = self.esi_auth.get_fleet_member_locations(members=members)
                    if locations:
                        self.root.after(0, self._apply_fleet_locations, locations)

                    # Derive fleet composition
                    ship_counts: dict[int, int] = {}
                    for m in members:
                        stid = m.get("ship_type_id", 0)
                        if stid:
                            ship_counts[stid] = ship_counts.get(stid, 0) + 1
                    total = len(members)

                    self.root.after(0, self._update_fleet_composition, ship_counts, total)
                    self.root.after(0, self._update_specialized_roles, members, ship_counts, total)

                    # Enrich member records with names for loss tracker.
                    # Pre-compute is_tackle here (BG thread) to avoid ESI calls
                    # blocking the main thread in loss_tracker.update().
                    from zkill_monitor import resolve_name
                    from ship_classes import is_tackle
                    enriched = []
                    for m in members:
                        char_id = m.get("character_id", 0)
                        stid = m.get("ship_type_id", 0)
                        sys_id = m.get("solar_system_id", 0)
                        enriched.append({
                            "character_id": char_id,
                            "character_name": resolve_name(char_id, "character") if char_id else "",
                            "ship_type_id": stid,
                            "ship_name": resolve_name(stid, "type") if stid else "",
                            "solar_system_id": sys_id,
                            "system_name": resolve_name(sys_id, "solar_system") if sys_id else "",
                            "station_id": m.get("station_id"),
                            "structure_id": m.get("structure_id"),
                            "role": m.get("role", ""),
                            "is_tackle": is_tackle(stid) if stid else False,
                        })
                    self.root.after(0, self._process_loss_tracking, fleet_id, enriched)
                elif fleet_id:
                    # In a fleet but not boss (can't read members → members is None).
                    # Keep chat-fed command-burst charges; only the hull roster is
                    # unknown. Do NOT run the grace/clear path (that wipes charges).
                    self._no_fleet_misses = 0
                    self._booster_roster = {}        # hulls unverified; charges kept
                    self._schedule_booster_refresh()
                    self.root.after(60000, self._refresh_fleet_locations)
                    return
                else:
                    # Genuinely not in a fleet (no fleet_id) — could also be a
                    # transient ESI error. Only clear state after NO_FLEET_GRACE
                    # consecutive misses to avoid flicker.
                    self._no_fleet_misses += 1
                    if self._no_fleet_misses >= NO_FLEET_GRACE:
                        self.root.after(0, self._update_fleet_composition, {}, 0)
                        self.root.after(0, self._process_loss_tracking, None, [])
                        self._clear_booster_state()
                    else:
                        print(f"[Fleet] No fleet data (miss {self._no_fleet_misses}/"
                              f"{NO_FLEET_GRACE}) — keeping previous state")
                    # Use longer backoff to reduce ESI 404 spam
                    self.root.after(60000, self._refresh_fleet_locations)
                    return
            except Exception as e:
                print(f"[Fleet] Location/composition fetch error: {e}")
            self.root.after(15000, self._refresh_fleet_locations)

        threading.Thread(target=do_fetch, daemon=True).start()

    def _auto_refresh_character_tab(self):
        """Periodically refresh the character tab (location + ship type).
        Runs every 5 minutes.  Uses force=False so asset data is served
        from cache unless the 10-minute asset TTL has also expired."""
        if self.esi_accounts and hasattr(self, '_char_tab_content'):
            try:
                self._refresh_character_tab(force=False)
            except Exception:
                log.exception("Auto-refresh of character tab failed; will retry next cycle")
        self.root.after(300_000, self._auto_refresh_character_tab)

    def _refresh_current_system(self):
        """Periodically fetch the tracked character's current system from ESI."""
        if not self.esi_auth or not self.esi_auth.is_authenticated:
            self.root.after(30000, self._refresh_current_system)
            return

        def do_fetch():
            try:
                loc = self.esi_auth.get_location()
                if loc:
                    sys_id = loc.get("solar_system_id")
                    if sys_id:
                        sys_info = get_system_info(sys_id)
                        sys_name = sys_info.get("name", "???") if sys_info else "???"
                        region_name = ""
                        if sys_info:
                            region_name = self.esi_auth._get_region_name(sys_info)
                        self._current_system_name = sys_name
                        self._current_system_region = region_name
                        region_str = f" ({region_name})" if region_name else ""
                        self.root.after(
                            0,
                            self._current_system_display.config,
                            {"text": f"System: {sys_name}{region_str}"},
                        )
            except Exception as e:
                print(f"[Location] Current system fetch error: {e}")
            self.root.after(15000, self._refresh_current_system)

        threading.Thread(target=do_fetch, daemon=True).start()

    def _build_status_bar_menu(self, parent):
        """Build the overflow settings gear for the status bar."""
        gear_btn = tk.Label(parent, text="\u2699", font=("Consolas", 14),
                             fg=FG_DIM, bg=BG_PANEL, cursor="hand2", padx=10, pady=4)
        gear_btn.pack(side=tk.RIGHT)

        def show_menu(event=None):
            menu = tk.Menu(self.root, tearoff=0, bg=BG_PANEL, fg=FG_TEXT,
                           activebackground=FG_ACCENT, activeforeground=BG_DARK)
            menu.add_command(label="Reset Losses", command=self._reset_loss_tracker)
            menu.add_separator()
            menu.add_command(label="Test Audio Alert",
                             command=lambda: tts_helper.speak(
                                 "Ten percent of fleet lost"))
            try:
                x = gear_btn.winfo_rootx()
                y = gear_btn.winfo_rooty() + gear_btn.winfo_height()
                menu.tk_popup(x, y)
            finally:
                menu.grab_release()

        gear_btn.bind("<Button-1>", show_menu)

    def _reset_loss_tracker(self):
        """Manually reset the fleet loss tracker."""
        self._loss_tracker.reset()
        if hasattr(self, "_loss_status_label"):
            self._loss_status_label.config(
                text="Losses: (reset — waiting for next poll)", fg=FG_DIM
            )
        self._append_xup_log("[Loss Tracker] Reset\n", "info")

    def _process_loss_tracking(self, fleet_id, members: list[dict]):
        """Update the loss tracker and fire UI + audio alerts on threshold cross."""
        # Reset on fleet change
        self._loss_tracker.set_fleet_id(fleet_id)

        new_deaths, highest_threshold, fc_docked = self._loss_tracker.update(members)

        # Update UI display
        if hasattr(self, "_loss_status_label"):
            pct = self._loss_tracker.loss_percentage
            relevant = self._loss_tracker.relevant_deaths_count
            total_deaths = self._loss_tracker.deaths_count
            baseline = self._loss_tracker.initial_size
            mode = self._loss_tracker.mode
            mode_label = "Mainline Fleet" if mode == "main" else "Support Fleet"

            if baseline > 0:
                color = FG_GREEN
                if pct >= 50:
                    color = FG_RED
                elif pct >= 25:
                    color = FG_ORANGE
                elif pct >= 10:
                    color = FG_YELLOW
                # In main mode: show relevant (major) deaths, plus tackle deaths as extra
                if mode == "main" and total_deaths > relevant:
                    extra = total_deaths - relevant
                    text = (f"Losses: {relevant} / {baseline} ({pct:.1f}%) "
                            f"[{mode_label}] (+{extra} tackle)")
                else:
                    text = f"Losses: {relevant} / {baseline} ({pct:.1f}%) [{mode_label}]"
                self._loss_status_label.config(text=text, fg=color)
            else:
                self._loss_status_label.config(
                    text="Losses: (waiting for fleet)", fg=FG_DIM
                )

        # Log individual deaths (tackle deaths tagged differently)
        for death in new_deaths:
            loc = f" in {death.system_name}" if death.system_name else ""
            ship = f" ({death.ship_name})" if death.ship_name else ""
            was_tackle = getattr(death, "_was_tackle", False)
            if was_tackle and self._loss_tracker.mode == "main":
                self._append_xup_log(
                    f"[LOSS-tackle] {death.character_name}{ship}{loc}\n", "dim"
                )
            else:
                self._append_xup_log(
                    f"[LOSS] {death.character_name}{ship}{loc}\n", "fire"
                )

        # Fire notification for the highest threshold crossed (if any)
        if highest_threshold is not None:
            pct_int = int(highest_threshold * 100)
            lost = self._loss_tracker.deaths_count
            total = self._loss_tracker.initial_size
            suffix = " (FC docked — suppressed)" if fc_docked else ""
            self._append_xup_log(
                f"\n>>> FLEET LOSS: {pct_int}% ({lost}/{total}){suffix} <<<\n\n",
                "fire",
            )
            # Flash window and play audio only if FC is NOT docked
            # (FC docked = fleet likely over, pod transitions are ship swaps)
            if not fc_docked:
                self._flash_title(0)
                if self._loss_audio_enabled:
                    if highest_threshold == 0.10:
                        phrase = "Ten percent of fleet lost"
                    elif highest_threshold == 0.25:
                        phrase = "Twenty five percent of fleet lost"
                    elif highest_threshold == 0.50:
                        phrase = "Fifty percent of fleet lost"
                    else:
                        phrase = f"{pct_int} percent of fleet lost"
                    tts_helper.speak(phrase)

    def _apply_fleet_locations(self, locations: dict[str, tuple[str, str, str]]):
        """Update location labels for all role tracker members."""
        self._fleet_locations_cache = locations
        for slot in self._role_slots:
            for sender, info in slot["people"].items():
                loc_label = info.get("location_label")
                if loc_label:
                    loc_data = locations.get(sender)
                    if loc_data:
                        sys_name, region_name, ship_name = loc_data
                        parts = [sys_name]
                        if region_name:
                            parts.append(region_name)
                        display = f"({' - '.join(parts)})"
                        if ship_name:
                            display += f" [{ship_name}]"
                        loc_label.config(
                            text=display,
                            fg=FG_ACCENT, cursor="hand2"
                        )
                        loc_label.unbind("<Button-1>")
                        loc_label.bind("<Button-1>",
                            lambda e, s=sys_name: webbrowser.open(
                                f"https://evemaps.dotlan.net/system/{s.replace(' ', '_')}"
                            ))
                    else:
                        loc_label.config(text="", cursor="")

    def _clear_role_slot(self, slot):
        """Clear all people from a role slot."""
        for person_data in slot["people"].values():
            person_data["row"].destroy()
        slot["people"].clear()
        self._update_role_count_label(slot)

    def _reset_all_roles(self):
        """Clear all role slot player lists."""
        for slot in self._role_slots:
            self._clear_role_slot(slot)

    def _themed_modal(self, title, message, *,
                      buttons=(("OK", "Dark.TButton", None),), accent=FG_ACCENT,
                      detail_items=None):
        """A dark-themed modal dialog matching the app (replaces tkinter.messagebox).

        `buttons` is a sequence of (label, ttk_style, result); they are packed
        right-to-left, so the FIRST entry is the rightmost (primary) button.
        Returns the chosen result, or None if the dialog is closed/escaped.
        Blocks until dismissed (modal), like messagebox."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=BG_DARK)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        result = {"value": None}

        def _choose(val):
            result["value"] = val
            try:
                dlg.destroy()
            except tk.TclError:
                pass

        body = tk.Frame(dlg, bg=BG_DARK, padx=18, pady=16,
                        highlightbackground=BORDER_COLOR, highlightthickness=1)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(body, text=title, font=("Consolas", 12, "bold"),
                 fg=accent, bg=BG_DARK, anchor=tk.W).pack(fill=tk.X, pady=(0, 8))
        tk.Label(body, text=message, font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                 justify=tk.LEFT, wraplength=440, anchor=tk.W).pack(fill=tk.X)
        if detail_items:
            list_wrap = tk.Frame(body, bg=BG_DARK)
            list_wrap.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
            lb = tk.Listbox(
                list_wrap, height=min(10, max(3, len(detail_items))),
                bg=BG_ENTRY, fg=FG_TEXT, font=("Consolas", 9),
                selectbackground=BG_PANEL, selectforeground=FG_TEXT,
                highlightthickness=1, highlightbackground=BORDER_COLOR,
                activestyle="none", borderwidth=0)
            for it in detail_items:
                lb.insert(tk.END, it)
            if len(detail_items) > 10:
                sb = ttk.Scrollbar(list_wrap, orient=tk.VERTICAL, command=lb.yview)
                lb.configure(yscrollcommand=sb.set)
                sb.pack(side=tk.RIGHT, fill=tk.Y)
            lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        btn_row = tk.Frame(body, bg=BG_DARK)
        btn_row.pack(fill=tk.X, pady=(16, 0))
        for label, style, val in buttons:
            ttk.Button(btn_row, text=label, style=style,
                       command=lambda v=val: _choose(v)).pack(side=tk.RIGHT, padx=(6, 0))

        dlg.protocol("WM_DELETE_WINDOW", lambda: _choose(None))
        dlg.bind("<Escape>", lambda e: _choose(None))
        dlg.update_idletasks()
        try:
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            w, h = dlg.winfo_width(), dlg.winfo_height()
            dlg.geometry(f"+{rx + max(0, (rw - w) // 2)}+{ry + max(0, (rh - h) // 3)}")
        except Exception:
            pass
        dlg.grab_set()
        dlg.wait_window()
        return result["value"]

    def _kick_pods_from_fleet(self):
        """Kick every fleet member currently in a Capsule (pod). Boss-only, confirmed."""
        import ship_classes
        fleet_id = getattr(self, "_last_polled_fleet_id", None)
        is_boss = bool(getattr(self, "_last_polled_fleet_is_boss", False))
        if not fleet_id or not is_boss:
            self._themed_modal(
                "Kick Pods",
                "You must be the current fleet boss to kick pods.\n"
                "Use 'Refresh fleet' after forming or joining a fleet, then try again.",
                buttons=[("OK", "Dark.TButton", None)], accent=FG_YELLOW)
            return
        auth = self.esi_auth
        if auth is None or not auth.is_authenticated:
            self._themed_modal("Kick Pods", "No authenticated character.",
                               buttons=[("OK", "Dark.TButton", None)], accent=FG_YELLOW)
            return

        def fetch():
            pods = []   # list of (character_id, name)
            err = None
            try:
                from zkill_monitor import resolve_name
                members = auth.get_fleet_members(fleet_id=fleet_id) or []
                for m in members:
                    if m.get("ship_type_id") in ship_classes.CAPSULE_TYPE_IDS:
                        cid = m.get("character_id")
                        if cid:
                            pods.append((cid, resolve_name(cid, "character") or str(cid)))
            except Exception as e:
                err = str(e)
            self.root.after(0, _confirm, pods, err)

        def _confirm(pods, err):
            if err:
                self._themed_modal("Kick Pods",
                                   f"Could not read fleet members:\n{err}",
                                   buttons=[("OK", "Dark.TButton", None)], accent=FG_RED)
                return
            if not pods:
                self._themed_modal("Kick Pods", "No one in the fleet is in a pod.",
                                   buttons=[("OK", "Dark.TButton", None)], accent=FG_ACCENT)
                return
            confirmed = self._themed_modal(
                "Kick Pods",
                f"Kick {len(pods)} pilot(s) currently in a Capsule from the fleet?\n"
                f"This cannot be undone.",
                detail_items=[n for _cid, n in pods],
                buttons=[(f"Kick {len(pods)} Pods", "Red.TButton", True),
                         ("Cancel", "Dark.TButton", False)],
                accent=FG_RED)
            if not confirmed:
                return
            _do_kick(pods)

        def _do_kick(pods):
            def worker():
                kicked = failed = 0
                for cid, _name in pods:
                    if auth.esi_delete(f"/fleets/{fleet_id}/members/{cid}/"):
                        kicked += 1
                    else:
                        failed += 1
                self.root.after(0, _done, kicked, failed)
            threading.Thread(target=worker, daemon=True).start()

        def _done(kicked, failed):
            msg = f"Kicked {kicked} pod(s) from the fleet."
            accent = FG_GREEN
            if failed:
                msg += (f"\n{failed} could not be kicked (they may have left, "
                        "swapped ship, or you lost the boss role).")
                accent = FG_YELLOW
            self._themed_modal("Kick Pods", msg,
                               buttons=[("OK", "Dark.TButton", None)], accent=accent)

        threading.Thread(target=fetch, daemon=True).start()

    def _set_all_roles_collapsed(self, collapsed: bool):
        """Expand or collapse all role slots at once."""
        for slot in self._role_slots:
            expanded = slot.get("expanded_var")
            people_frame = slot.get("people_frame")
            arrow = slot.get("arrow_label")
            if expanded is None or people_frame is None or arrow is None:
                continue
            if collapsed and expanded.get():
                people_frame.pack_forget()
                arrow.config(text="\u25B6")
                expanded.set(False)
            elif not collapsed and not expanded.get():
                people_frame.pack(fill=tk.X, padx=8, pady=(0, 3))
                arrow.config(text="\u25BC")
                expanded.set(True)

    def _take_screenshot(self):
        """Capture window screenshot and save to clipboard."""
        self._screenshot_link.config(text="Capturing...", fg=FG_DIM)
        self.root.update_idletasks()

        def do_capture():
            try:
                # Capture the window using the window's geometry
                x = self.root.winfo_rootx()
                y = self.root.winfo_rooty()
                w = self.root.winfo_width()
                h = self.root.winfo_height()

                if sys.platform == "win32":
                    ok, msg = self._capture_screenshot_windows(x, y, w, h)
                elif sys.platform.startswith("linux"):
                    ok, msg = self._capture_screenshot_linux(x, y, w, h)
                else:
                    ok, msg = False, "Screenshot not supported on this platform"
                color = FG_GREEN if ok else FG_RED
                self.root.after(0, self._screenshot_link.config, {"text": msg, "fg": color})
            except Exception as e:
                self.root.after(0, self._screenshot_link.config,
                               {"text": f"Error: {e}", "fg": FG_RED})

        threading.Thread(target=do_capture, daemon=True).start()

    def _capture_screenshot_windows(self, x, y, w, h) -> tuple[bool, str]:
        """Capture the screen region to the Windows clipboard via PowerShell."""
        import subprocess

        # Use PowerShell to capture screen region and copy to clipboard
        ps_script = f'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap({w}, {h})
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen({x}, {y}, 0, 0, $bmp.Size)
$g.Dispose()
[System.Windows.Forms.Clipboard]::SetImage($bmp)
$bmp.Dispose()
'''
        result = subprocess.run(
            ["powershell", "-STA", "-Command", ps_script],
            capture_output=True, timeout=10,
        )

        if result.returncode == 0:
            return True, "Saved to clipboard!"
        err = result.stderr.decode(errors='replace').strip()[:80]
        return False, f"Capture failed: {err}"

    def _capture_screenshot_linux(self, x, y, w, h) -> tuple[bool, str]:
        """Capture the screen region on Linux: clipboard first, file-save fallback."""
        import subprocess

        wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or \
            os.environ.get("XDG_SESSION_TYPE") == "wayland"
        tools = ("grim", "maim", "scrot", "import", "xclip", "wl-copy")
        available = {t for t in tools if shutil.which(t)}

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        out_path = tmp.name
        tmp.close()

        capture_cmd, clipboard_cmd, error = build_linux_screenshot_cmds(
            wayland, available, x, y, w, h, out_path)
        try:
            if error:
                return False, error
            result = subprocess.run(capture_cmd, capture_output=True, timeout=10)
            if result.returncode != 0 or os.path.getsize(out_path) == 0:
                err = result.stderr.decode(errors='replace').strip()[:80]
                return False, "Capture failed: " + err
            if clipboard_cmd is not None:
                with open(out_path, "rb") as f:
                    png = f.read()
                r = subprocess.run(clipboard_cmd, input=png, capture_output=True, timeout=10)
                if r.returncode == 0:
                    return True, "Saved to clipboard!"
                # else fall through to file-save
            # No clipboard tool, or clipboard failed -> save the file:
            pictures = os.path.expanduser("~/Pictures")
            dest_dir = pictures if os.path.isdir(pictures) else app_dir()
            dest = os.path.join(
                dest_dir,
                f"fctool_screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            )
            shutil.copyfile(out_path, dest)
            return True, f"Saved to {dest}"
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

    def _check_role_letters(self, msg):
        """Check if a chat message matches any role tracker letter.
        Fills slots sequentially — if a slot has a cap and is full,
        the person overflows into the next slot with the same letter.
        """
        msg_text = msg.message.strip().lower()
        for slot in self._role_slots:
            letter = slot["letter_var"].get().strip()
            if not letter:
                continue
            letter_lower = letter.lower()
            if msg_text == letter_lower or msg_text.startswith(letter_lower + " "):
                # Check if this person is already in ANY slot with this letter
                already_placed = False
                for s in self._role_slots:
                    if s["letter_var"].get().strip().lower() == letter_lower:
                        if msg.sender in s["people"]:
                            already_placed = True
                            break
                if already_placed:
                    return
                # Find first matching slot that isn't full
                target = None
                for s in self._role_slots:
                    if s["letter_var"].get().strip().lower() != letter_lower:
                        continue
                    if s["cap_enabled_var"].get():
                        try:
                            cap = int(s["cap_var"].get())
                            if cap > 0 and len(s["people"]) >= cap:
                                continue  # This slot is full, try next
                        except ValueError:
                            pass
                    target = s
                    break
                if target:
                    self._add_person_to_slot(target, msg.sender, msg.timestamp)
                    title = target["title_var"].get() or letter.upper()
                    self._append_xup_log(
                        f"[{msg.timestamp.strftime('%H:%M:%S')}] {msg.sender} -> {title}\n", "role"
                    )
                else:
                    # All slots full
                    title = slot["title_var"].get() or letter.upper()
                    self._append_xup_log(
                        f"[{msg.timestamp.strftime('%H:%M:%S')}] {msg.sender} -> {title} (ALL FULL)\n", "dim"
                    )
                return  # Only match one letter per message

    def _append_xup_log(self, text, tag=None):
        self._xup_log.config(state=tk.NORMAL)
        if tag:
            self._xup_log.insert(tk.END, text, tag)
        else:
            self._xup_log.insert(tk.END, text)
        self._xup_log.see(tk.END)
        self._xup_log.config(state=tk.DISABLED)

    def _open_url(self, url: str):
        """Open a URL in the default browser."""
        webbrowser.open(url)

    def _theme_scrolledtext_bar(self, st):
        """Dark-theme the classic tk.Scrollbar inside a scrolledtext.ScrolledText.

        ScrolledText builds a CLASSIC tk.Scrollbar (reachable as st.vbar) that
        ttk styles do NOT reach, so it must be coloured directly to match the
        ttk scrollbars themed in the style block above.
        """
        st.vbar.configure(
            bg=BG_ENTRY, troughcolor=BG_DARK, activebackground="#1a5a90",
            highlightbackground=BG_DARK, highlightcolor=BG_DARK,
            highlightthickness=0, borderwidth=0, relief="flat",
            elementborderwidth=0,
        )

    def _show_tooltip(self, event, text):
        """Show a tooltip near the mouse cursor. Long text wraps to multiple
        rows and the popup is clamped to stay fully on-screen."""
        self._hide_tooltip()  # destroy any existing tooltip first (idempotent)
        tip = self._tooltip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        label = tk.Label(tip, text=text,
                         font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                         borderwidth=1, relief=tk.SOLID, padx=4, pady=2,
                         wraplength=420, justify=tk.LEFT)
        label.pack()
        # Measure the laid-out popup, then clamp its top-left to the full
        # VIRTUAL desktop (all monitors), not just the primary screen —
        # winfo_screenwidth/height report only the primary monitor, which would
        # yank a tooltip back onto screen 1 when the app is on screen 2.
        tip.update_idletasks()
        x0, y0, x1, y1 = self._virtual_screen_bounds()
        x = max(x0, min(event.x_root + 12, x1 - tip.winfo_width() - 8))
        y = max(y0, min(event.y_root + 12, y1 - tip.winfo_height() - 8))
        tip.wm_geometry(f"+{x}+{y}")

    def _virtual_screen_bounds(self):
        """Return (x0, y0, x1, y1) of the full virtual desktop spanning every
        monitor. On Windows this uses the SM_*VIRTUALSCREEN metrics so popups
        can sit on a secondary screen; elsewhere (or on failure) it falls back
        to the primary screen reported by Tk."""
        try:
            if sys.platform == "win32":
                import ctypes
                gsm = ctypes.windll.user32.GetSystemMetrics
                x0, y0 = gsm(76), gsm(77)          # SM_X/Y VIRTUALSCREEN
                x1, y1 = x0 + gsm(78), y0 + gsm(79)  # + SM_CX/CY VIRTUALSCREEN
                if x1 > x0 and y1 > y0:
                    return x0, y0, x1, y1
        except Exception:
            pass
        return (0, 0, self.root.winfo_screenwidth(),
                self.root.winfo_screenheight())

    def _hide_tooltip(self):
        """Hide the current tooltip."""
        if hasattr(self, '_tooltip') and self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None

    def _show_zkill_alert(self, alert: KillAlert):
        # ── GUI-only filters (applied at display time only) ──
        # These read the panel vars, which mirror config["intel_filter"]; the
        # criteria (location/parties) come straight from the live config so the
        # display filter always reflects the current panel state.
        #
        # Capital-alert settings (read live). A hostile capital is a cap whose
        # corp AND alliance are both outside the standings friendly set, so
        # alert.capitals_involved is already hostile-only.
        caps_cfg = self.config.get("intel_filter", {}).get("capitals", {})
        cap_alert = caps_cfg.get("alert", True)
        cap_bypass = caps_cfg.get("bypass_filter", False)
        #
        # (a) Min-pilots gate — hostile capitals bypass it only when
        # "Alert on hostile capitals" is on.
        try:
            gui_min_pilots = int(self._zkill_min_pilots_var.get())
        except (ValueError, AttributeError):
            gui_min_pilots = 1
        if (alert.pilots_on_field < gui_min_pilots
                and not (alert.capitals_involved and cap_alert)):
            return  # Below GUI min pilot threshold

        # (b) Criteria gate — location + parties combined per config["combine"].
        # A hostile-cap alert skips this location/parties filter only when BOTH
        # the capital alert and its bypass toggle are on.
        ok = intel_filter.matches(
            alert.system_id,
            alert.region_id,
            alert.alliances_involved,
            alert.corps_involved,
            self.config.get("intel_filter", {}),
            self.config.get("coalitions", {}),
        )
        if not ok and not (alert.capitals_involved and cap_alert and cap_bypass):
            return  # Does not match the configured location/parties criteria

        # (c) Max-jumps gate — drop if route distance exceeds the limit.
        try:
            max_jumps = int(self._zkill_max_jumps_var.get())
        except (ValueError, AttributeError):
            max_jumps = 0
        if max_jumps > 0 and alert.route_from_staging:
            m = re.search(r"\*\*(\d+) jumps\*\*", alert.route_from_staging)
            if m and int(m.group(1)) > max_jumps:
                return  # Too far, skip this alert

        ts = alert.timestamp.strftime("%H:%M:%S")
        caps_tag = " [CAPITALS]" if alert.capitals_involved else ""

        # Track for fusion detection
        if alert.system_name:
            self._recent_zkill_systems[alert.system_name] = datetime.now()

        # Check for fusion with recent intel reports
        is_fused = False
        if alert.system_name and alert.system_name in self._recent_intel_systems:
            delta = datetime.now() - self._recent_intel_systems[alert.system_name]
            if delta.total_seconds() <= 600:  # 10 minutes
                is_fused = True

        fused_prefix = "[FUSED] " if is_fused else ""
        if alert.is_update:
            header = f"[{ts}] {fused_prefix}FIGHT GROWING{caps_tag}"
        else:
            header = f"[{ts}] {fused_prefix}FIGHT DETECTED{caps_tag}"

        header_tag = "fused" if is_fused else "fight"

        self._begin_alert_block()
        self._append_zkill_log(header + "\n", header_tag)
        # System as clickable button (set destination + copy) with region
        region_str = f" ({alert.region_name})" if alert.region_name else ""
        self._append_zkill_log("  System:  ", "info")
        sys_btn = tk.Button(
            self._zkill_log, text=f"{alert.system_name}{region_str}",
            font=("Consolas", 10, "bold"), fg=FG_ACCENT, bg=BG_ENTRY,
            activebackground="#1a5a90", activeforeground=FG_WHITE,
            borderwidth=1, relief=tk.RIDGE, cursor="hand2",
            command=lambda s=alert.system_name: self._set_destination_or_copy(s),
        )
        self._zkill_log.window_create("alert_ins", window=sys_btn)
        self._append_zkill_log("\n")
        self._append_zkill_log(
            f"  Pilots:  {alert.pilots_on_field}  |  "
            f"Kills: {alert.kill_count}  |  "
            f"Value: {alert.total_value_millions:.0f}M ISK\n", "value"
        )
        if alert.capitals_involved:
            cap_detail = ""
            if alert.capital_breakdown:
                parts = [f"{count} {cls}" for cls, count in
                         sorted(alert.capital_breakdown.items(),
                                key=lambda x: x[1], reverse=True)]
                cap_detail = f" ({', '.join(parts)})"
            self._append_zkill_log(
                f"  ** CAPITAL SHIPS ON FIELD **{cap_detail}\n", "fight"
            )
        if alert.top_alliances:
            alliance_str = ", ".join(f"{name} ({count})" for name, count in alert.top_alliances)
            self._append_zkill_log(
                f"  Alliances: {alliance_str}\n", "info"
            )
        if alert.route_from_staging:
            self._append_zkill_log(
                f"  Route:   {alert.route_from_staging}\n", "info"
            )

        # Insert clickable link buttons and action buttons inline
        self._zkill_log.insert("alert_ins", "  ")

        # zKillboard link button
        zkill_btn = tk.Button(
            self._zkill_log, text="zKillboard", font=("Consolas", 8, "bold"),
            fg=FG_ACCENT, bg=BG_ENTRY, activebackground="#1a5a90",
            activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
            cursor="hand2",
            command=lambda u=alert.zkill_url: self._open_url(u),
        )
        self._zkill_log.window_create("alert_ins", window=zkill_btn)
        self._zkill_log.insert("alert_ins", "  ")

        # Dotlan link button
        if alert.dotlan_url:
            dotlan_btn = tk.Button(
                self._zkill_log, text="Dotlan", font=("Consolas", 8, "bold"),
                fg=FG_ORANGE, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda u=alert.dotlan_url: self._open_url(u),
            )
            self._zkill_log.window_create("alert_ins", window=dotlan_btn)
            self._zkill_log.insert("alert_ins", "  ")

        # zKillboard Related Kills button
        if alert.zkill_related_url:
            related_btn = tk.Button(
                self._zkill_log, text="Related Kills", font=("Consolas", 8, "bold"),
                fg=FG_RED, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda u=alert.zkill_related_url: self._open_url(u),
            )
            self._zkill_log.window_create("alert_ins", window=related_btn)
            self._zkill_log.insert("alert_ins", "  ")

        # WarBeacon Battle Report button
        if alert.warbeacon_url:
            wb_btn = tk.Button(
                self._zkill_log, text="WarBeacon BR", font=("Consolas", 8, "bold"),
                fg="#e040fb", bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda u=alert.warbeacon_url: self._open_url(u),
            )
            self._zkill_log.window_create("alert_ins", window=wb_btn)
            self._zkill_log.insert("alert_ins", "  ")

        # "WH Route" button -> fills WH Route tab and searches
        staging = self._get_staging_system()
        if staging:
            dest_btn = tk.Button(
                self._zkill_log, text="Navigate", font=("Consolas", 8, "bold"),
                fg=FG_YELLOW, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda s=alert.system_name: self._navigate_wh_route(s),
            )
            self._zkill_log.window_create("alert_ins", window=dest_btn)
            self._zkill_log.insert("alert_ins", "  ")

            # "Titan Bridge?" button -> fills Jump Range tab
            range_btn = tk.Button(
                self._zkill_log, text="Titan Bridge?", font=("Consolas", 8, "bold"),
                fg=FG_GREEN, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda s=alert.system_name: self._navigate_jump_range(s),
            )
            self._zkill_log.window_create("alert_ins", window=range_btn)

        self._zkill_log.insert("alert_ins", "\n\n")
        self._end_alert_block()

        if not self._intel_mute_var.get():
            self.root.bell()
        self._notify_zkill_tab()

    # ── Paste Intel drawer ─────────────────────────────────────────────────

    def _toggle_paste_drawer(self):
        self._paste_drawer_expanded = not self._paste_drawer_expanded
        if self._paste_drawer_expanded:
            self._paste_toggle_btn.config(text="▼ Paste Intel")
            self._paste_body.pack(fill=tk.X, padx=0, pady=0)
        else:
            self._paste_toggle_btn.config(text="▶ Paste Intel")
            self._paste_body.pack_forget()

    # ── Cyno Check drawer ──────────────────────────────────────────────────

    def _build_cyno_check_drawer(self, tab):
        """Build the collapsible 'Cyno Check' drawer on the Intel tab.

        Baseline footprint is a single header row (▶ Cyno Check); the body is
        pack()/pack_forget()'d on toggle and starts COLLAPSED. The body holds a
        one-line hint, a character AutocompleteEntry (debounced ESI search), a
        Check button, a status line, and a DISABLED ScrolledText result area.
        """
        # Session-only state (mirrors the Paste Intel drawer pattern).
        self._cyno_drawer_expanded = False
        self._cyno_typeahead_after = None
        self._cyno_name_to_id: dict[str, int] = {}  # name(lower) -> character_id
        self._cyno_busy = False
        self._cyno_latest_url: str | None = None

        self._cyno_drawer_frame = tk.Frame(
            tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
            highlightbackground=BORDER_COLOR, highlightthickness=1)
        self._cyno_drawer_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

        cyno_header = tk.Frame(self._cyno_drawer_frame, bg=BG_PANEL)
        cyno_header.pack(fill=tk.X, padx=10, pady=4)

        self._cyno_toggle_btn = tk.Label(
            cyno_header, text="▶ Cyno Check",
            font=("Consolas", 10, "bold"), fg=FG_ACCENT, bg=BG_PANEL,
            cursor="hand2",
        )
        self._cyno_toggle_btn.pack(side=tk.LEFT)
        self._cyno_toggle_btn.bind(
            "<Button-1>", lambda e: self._toggle_cyno_drawer())

        # Body (hidden by default)
        self._cyno_body = tk.Frame(self._cyno_drawer_frame, bg=BG_PANEL)

        tk.Label(
            self._cyno_body,
            text=("Find a character's cyno-ship LOSSES over the last 6 months "
                  "(public zKillboard data; no login required)."),
            font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL,
            justify=tk.LEFT, anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(2, 4))

        cyno_input_row = tk.Frame(self._cyno_body, bg=BG_PANEL)
        cyno_input_row.pack(fill=tk.X, padx=10, pady=(0, 4))

        tk.Label(cyno_input_row, text="Character:", font=("Consolas", 9),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT, padx=(0, 4))

        self._cyno_entry = AutocompleteEntry(
            cyno_input_row, completions=[], max_shown=12,
            font=("Consolas", 10), width=28,
            bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
            borderwidth=1, relief=tk.RIDGE,
        )
        self._cyno_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # Debounced ESI type-ahead (only when logged in); Return submits.
        self._cyno_entry.bind("<KeyRelease>", self._cyno_on_typeahead, add="+")
        self._cyno_entry.bind("<Return>", lambda e: self._cyno_submit())

        self._cyno_check_btn = ttk.Button(
            cyno_input_row, text="Check", style="Dark.TButton",
            command=self._cyno_submit)
        self._cyno_check_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Hint shown only when login-gated autocomplete is unavailable.
        if not (self.esi_auth and self.esi_auth.is_authenticated):
            tk.Label(
                self._cyno_body,
                text="Log in (Settings) to enable name autocomplete.",
                font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL,
                justify=tk.LEFT, anchor="w",
            ).pack(fill=tk.X, padx=10, pady=(0, 2))

        self._cyno_status = tk.Label(
            self._cyno_body, text="", font=("Consolas", 9),
            fg=FG_DIM, bg=BG_PANEL, justify=tk.LEFT, anchor="w",
        )
        self._cyno_status.pack(fill=tk.X, padx=10, pady=(0, 2))

        self._cyno_result = scrolledtext.ScrolledText(
            self._cyno_body, height=8, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
            selectbackground="#1a5a90", wrap=tk.WORD, state=tk.DISABLED,
            borderwidth=1, relief=tk.RIDGE,
        )
        self._cyno_result.pack(fill=tk.X, padx=10, pady=(0, 6))
        self._theme_scrolledtext_bar(self._cyno_result)
        # Clickable link tag for the latest-loss zKill URL.
        self._cyno_result.tag_configure(
            "cyno_link", foreground=FG_ACCENT, underline=True)
        self._cyno_result.tag_bind(
            "cyno_link", "<Enter>",
            lambda e: self._cyno_result.config(cursor="hand2"))
        self._cyno_result.tag_bind(
            "cyno_link", "<Leave>",
            lambda e: self._cyno_result.config(cursor=""))
        self._cyno_result.tag_bind(
            "cyno_link", "<Button-1>", self._cyno_open_latest)

    def _toggle_cyno_drawer(self):
        self._cyno_drawer_expanded = not self._cyno_drawer_expanded
        if self._cyno_drawer_expanded:
            self._cyno_toggle_btn.config(text="▼ Cyno Check")
            self._cyno_body.pack(fill=tk.X, padx=0, pady=0)
        else:
            self._cyno_toggle_btn.config(text="▶ Cyno Check")
            self._cyno_body.pack_forget()

    def _toggle_xup_log(self):
        self._xup_log_expanded = not self._xup_log_expanded
        if self._xup_log_expanded:
            self._xup_log_toggle_btn.config(text="▼ X-Up Log")
            self._xup_log_body.pack(fill=tk.X, padx=6, pady=(0, 6))
        else:
            self._xup_log_toggle_btn.config(text="▶ X-Up Log")
            self._xup_log_body.pack_forget()

    def _cyno_on_typeahead(self, event=None):
        """Best-effort debounced ESI character type-ahead.

        Mirrors _cm_on_typeahead: ignore nav keys, require >= 3 chars, 300ms
        debounce. Skipped entirely when not logged in (search needs auth)."""
        if event is not None and event.keysym in (
                "Return", "Up", "Down", "Tab", "Escape",
                "Shift_L", "Shift_R", "Control_L", "Control_R",
                "Alt_L", "Alt_R"):
            return
        if not (self.esi_auth and self.esi_auth.is_authenticated):
            return
        query = self._cyno_entry.get().strip()
        if len(query) < 3:
            return
        if self._cyno_typeahead_after is not None:
            try:
                self.root.after_cancel(self._cyno_typeahead_after)
            except Exception:
                pass
        self._cyno_typeahead_after = self.root.after(
            300, lambda: self._cyno_do_typeahead(query))

    def _cyno_do_typeahead(self, query: str):
        self._cyno_typeahead_after = None

        def worker():
            results = []
            try:
                if self.esi_auth:
                    results = self.esi_auth.search_entities(query, ["character"])
            except Exception:
                results = []
            names = []
            name_map: dict[str, int] = {}
            for r in results:
                if not isinstance(r, dict):
                    continue
                nm = r.get("name")
                rid = r.get("id")
                if nm and isinstance(rid, int):
                    names.append(nm)
                    name_map[nm.lower()] = rid
            if getattr(self, "_cyno_entry", None) is not None:
                self.root.after(0, self._cyno_apply_typeahead, names, name_map)

        threading.Thread(target=worker, daemon=True).start()

    def _cyno_apply_typeahead(self, names, name_map):
        """Marshal search results back onto the Tk thread."""
        # Merge into the name->id map so a later submit can resolve without a
        # second network round-trip.
        self._cyno_name_to_id.update(name_map)
        self._cyno_entry.update_completions(names)

    def _cyno_submit(self):
        """Resolve the typed character name -> id and kick off the analysis."""
        if self._cyno_busy:
            return
        name = self._cyno_entry.get().strip()
        if not name:
            self._cyno_set_status("Enter a character name.", FG_DIM)
            return

        self._cyno_set_status(f"Resolving '{name}' ...", FG_DIM)
        self._cyno_set_result("")
        self._cyno_latest_url = None
        self._cyno_busy = True
        self._cyno_check_btn.config(state=tk.DISABLED)

        def worker():
            cid = self._cyno_resolve_character_id(name)
            if not cid:
                self.root.after(
                    0, self._cyno_resolve_failed, name)
                return
            self.root.after(
                0, self._cyno_set_status,
                f"Checking {name} ...", FG_DIM)

            def progress(msg):
                # Marshal each backend status line to the UI thread.
                self.root.after(0, self._cyno_set_status, msg, FG_DIM)

            try:
                result = cyno_analyze_character(cid, progress=progress)
            except Exception as exc:
                self.root.after(0, self._cyno_render_error, str(exc))
                return
            self.root.after(0, self._cyno_render_result, name, result)

        threading.Thread(target=worker, daemon=True).start()

    def _cyno_resolve_character_id(self, name: str):
        """Resolve a character name to an id (worker-thread only, no Tk).

        Tries the cached type-ahead map first, then a public exact-name resolve
        via resolve_ids()'s 'characters' bucket (case-insensitive)."""
        cached = self._cyno_name_to_id.get(name.lower())
        if cached:
            return cached
        try:
            if self.esi_auth:
                resolved = self.esi_auth.resolve_ids([name])
                for entry in resolved.get("characters", []) or []:
                    nm = entry.get("name")
                    rid = entry.get("id")
                    if nm and isinstance(rid, int) and nm.lower() == name.lower():
                        return rid
        except Exception:
            pass
        return None

    def _cyno_resolve_failed(self, name: str):
        self._cyno_busy = False
        self._cyno_check_btn.config(state=tk.NORMAL)
        self._cyno_set_status(f"No character found for '{name}'.", FG_RED)

    def _cyno_render_error(self, message: str):
        self._cyno_busy = False
        self._cyno_check_btn.config(state=tk.NORMAL)
        self._cyno_set_status(f"Error: {message}", FG_RED)

    def _cyno_render_result(self, name: str, result: dict):
        """Render a completed analysis into the result area (Tk thread)."""
        try:
            total = int(result.get("total", 0) or 0)
            breakdown = result.get("breakdown") or {}
            latest = result.get("latest")
            association = result.get("association") or {"kind": "unknown"}
            status = result.get("status") or ""

            lines = [f"{name}", ""]
            if total == 0:
                lines.append("No cyno-ship losses in the last 6 months.")
            else:
                lines.append(
                    f"{total} cyno-ship loss"
                    f"{'es' if total != 1 else ''} in the last 6 months")
                if breakdown:
                    parts = [f"{hull} {cnt}"
                             for hull, cnt in sorted(breakdown.items())]
                    lines.append("  " + " · ".join(parts))

            # Association line. `basis` distinguishes the battle-inferred signal
            # (who the pilot's blues were in the fights around recent losses)
            # from the all-time stats fallback.
            if isinstance(association, dict) and \
                    association.get("kind") != "unknown" and \
                    association.get("name"):
                kind = association.get("kind", "")
                basis = association.get("basis")
                aname = association["name"]
                if basis == "battles":
                    bc = association.get("battle_count")
                    sample = association.get("sample_total")
                    confident = association.get("confident", True)
                    if bc and sample:
                        ratio = (f"{bc} of {sample} "
                                 f"battle{'s' if sample != 1 else ''}")
                    elif sample:
                        ratio = (f"from {sample} "
                                 f"battle{'s' if sample != 1 else ''}")
                    else:
                        ratio = "battles"
                    if confident:
                        lines.append(
                            f"Flies with: {aname} ({kind} · {ratio})")
                    else:
                        lines.append(
                            f"No clear bloc · top: {aname} ({kind} · {ratio})")
                    runners = association.get("runners_up") or []
                    extra = " · ".join(
                        f"{r.get('name')} {r.get('battle_count')}"
                        for r in runners
                        if isinstance(r, dict) and r.get("name"))
                    if extra:
                        lines.append(f"  also: {extra}")
                elif basis == "stats":
                    lines.append(
                        f"Flies with: {aname} ({kind} · all-time)")
                else:
                    lines.append(f"Flies with: {aname} ({kind})")
            else:
                lines.append("Association: unknown")

            # Surface a partial/error note from the backend status if present.
            low = status.lower()
            if "partial" in low or "unavailable" in low:
                lines.append(f"Note: {status}")

            self._cyno_set_result("\n".join(lines) + "\n")

            # Append the latest-loss link as a clickable tagged run.
            if isinstance(latest, dict) and latest.get("url"):
                self._cyno_latest_url = latest["url"]
                time_txt = latest.get("time") or ""
                self._cyno_result.config(state=tk.NORMAL)
                self._cyno_result.insert(tk.END, "Latest loss: ")
                label = f"open on zKillboard{(' (' + time_txt + ')') if time_txt else ''}"
                self._cyno_result.insert(tk.END, label, "cyno_link")
                self._cyno_result.insert(tk.END, "\n")
                self._cyno_result.config(state=tk.DISABLED)

            self._cyno_set_status(status or "Done", FG_GREEN)
        finally:
            self._cyno_busy = False
            self._cyno_check_btn.config(state=tk.NORMAL)

    def _cyno_open_latest(self, event=None):
        if self._cyno_latest_url:
            try:
                webbrowser.open(self._cyno_latest_url)
            except Exception:
                pass

    def _cyno_set_status(self, text: str, color: str = FG_DIM):
        self._cyno_status.config(text=text, fg=color)

    def _cyno_set_result(self, text: str):
        """Replace the DISABLED ScrolledText body with ``text``."""
        self._cyno_result.config(state=tk.NORMAL)
        self._cyno_result.delete("1.0", tk.END)
        if text:
            self._cyno_result.insert("1.0", text)
        self._cyno_result.config(state=tk.DISABLED)

    def _toggle_intel_filter_panel(self):
        """Collapse/expand the fight-alert filter body to free up feed space.

        Session-only — the collapse state is intentionally not persisted, so the
        panel starts expanded on every launch. The header stays visible when
        collapsed so the user can re-expand.
        """
        self._intel_filter_expanded = not self._intel_filter_expanded
        if self._intel_filter_expanded:
            self._intel_filter_header.config(text="▼ Filters")
            self._intel_filter_body.pack(fill=tk.X)
        else:
            self._intel_filter_header.config(text="▶ Filters")
            self._intel_filter_body.pack_forget()

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
            self._append_intel_summary_line(
                f"Fleet {kind} stored ({len(rows)} entries)"
            )
            return

        if isinstance(parsed, LocalScan):
            if auth is None:
                self._set_paste_result("No active SSO character; cannot resolve names.")
                return
            prior = self._intel_session.prior_local_scan(system, window_minutes=15)
            self._intel_session.add_local_scan(system, parsed)
            self._set_paste_result(
                f"Analyzing local scan ({len(parsed.pilot_names)} pilots)…"
            )

            # Snapshot what the worker needs so it doesn't touch self.* state
            # from the daemon thread.
            friendly_ids = set(self._standings_cache.friendly_ids)
            prior_count = len(prior.parsed.pilot_names) if prior else None
            prior_timestamp = prior.timestamp if prior else None

            def _worker():
                try:
                    result = intel_analyzer.analyze_local_scan(
                        parsed, auth=auth,
                        friendly_ids=friendly_ids,
                        own_character_ids=own_chars,
                    )
                # Broad catch: surface any failure to the user as a friendly
                # message rather than crashing the Tk mainloop.
                except Exception as exc:
                    self.root.after(
                        0,
                        lambda exc=exc: self._set_paste_result(
                            f"Local-scan analysis failed: {exc}"
                        ),
                    )
                    return

                trend = None
                if prior_count is not None and prior_timestamp is not None:
                    minutes_ago = max(0, int(
                        (datetime.now(timezone.utc) - prior_timestamp)
                        .total_seconds() / 60
                    ))
                    trend = intel_analyzer.compute_local_scan_trend(
                        current_count=result.total,
                        prior_count=prior_count,
                        minutes_ago=minutes_ago,
                    )

                from zkill_monitor import resolve_name as _resolve_name
                text_out = intel_analyzer.format_local_scan_result(
                    result, trend=trend, resolve_name=_resolve_name,
                )

                def _finish():
                    self._set_paste_result(text_out)
                    delta_str = ""
                    if trend and trend.delta:
                        sign = "+" if trend.delta > 0 else ""
                        delta_str = (
                            f" ({sign}{trend.delta} vs scan "
                            f"{trend.minutes_ago}m ago)"
                        )
                    effective_hostile = (
                        result.hostile_count + len(result.unresolved_names)
                    )
                    self._append_intel_summary_line(
                        f"Local {system} — {result.friendly_count} friendly, "
                        f"{effective_hostile} hostile{delta_str}"
                    )

                self.root.after(0, _finish)

            threading.Thread(
                target=_worker, daemon=True, name="LocalScanAnalyze",
            ).start()
            return

        if isinstance(parsed, DScan):
            latest_fleet = self._intel_session.latest_fleet_paste()
            if latest_fleet is not None:
                friendly_source = intel_analyzer.DScanSource.PASTED
                roster = latest_fleet.parsed
                roster_age_min = max(0, int(
                    (datetime.now(timezone.utc) - latest_fleet.timestamp).total_seconds() / 60
                ))
            elif auth is not None:
                try:
                    if auth.is_fleet_boss():
                        members = auth.get_fleet_members() or []
                        from collections import Counter
                        counts = Counter(
                            m.get("ship_type_id") for m in members if m.get("ship_type_id")
                        )
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
                except Exception as exc:
                    self._set_paste_result(f"Fleet roster fetch failed: {exc}")
                    return
            else:
                friendly_source = None
                roster = None
                roster_age_min = None

            prior = self._intel_session.prior_dscan(system, window_minutes=15)
            try:
                result = intel_analyzer.analyze_dscan(
                    parsed, friendly_source=friendly_source, fleet_roster=roster,
                )
                trend = None
                if prior is not None:
                    prior_result = intel_analyzer.analyze_dscan(
                        prior.parsed, friendly_source=friendly_source, fleet_roster=roster,
                    )
                    minutes_ago = max(0, int(
                        (datetime.now(timezone.utc) - prior.timestamp).total_seconds() / 60
                    ))
                    trend = intel_analyzer.compute_dscan_trend(
                        current_result=result, prior_result=prior_result,
                        minutes_ago=minutes_ago,
                    )
            except Exception as exc:
                self._set_paste_result(f"D-scan analysis failed: {exc}")
                return
            self._intel_session.add_dscan(system, parsed)
            text_out = intel_analyzer.format_dscan_result(
                result, trend=trend, roster_age_minutes=roster_age_min,
            )
            self._set_paste_result(text_out)
            delta_str = ""
            if trend and trend.hostile_delta:
                sign = "+" if trend.hostile_delta > 0 else ""
                delta_str = (
                    f" ({sign}{trend.hostile_delta} hostile vs scan "
                    f"{trend.minutes_ago}m ago)"
                )
            h_str = (str(result.hostile_count)
                     if result.hostile_count is not None else "?")
            f_str = (str(result.friendly_count)
                     if result.friendly_count is not None else "?")
            self._append_intel_summary_line(
                f"D-Scan {system} — {h_str} hostile, {f_str} friendly{delta_str}"
            )

    def _clear_paste(self):
        self._paste_text.delete("1.0", tk.END)
        self._paste_result.config(state=tk.NORMAL)
        self._paste_result.delete("1.0", tk.END)
        self._paste_result.config(state=tk.DISABLED)
        self._paste_format_chip.config(text="", fg=FG_DIM)
        self._intel_session.clear()

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
        # Push the refreshed friendly set into the live monitor (no rebuild;
        # thread-safe set swap). Guarded — monitor is None when zkill is off.
        if self.zkill_monitor:
            self.zkill_monitor.set_friendly_ids(
                set(self._standings_cache.friendly_ids))
        self._update_standings_label()
        msg = (
            f"Standings refreshed. {len(self._standings_cache.friendly_ids)} "
            f"friendly, {len(self._standings_cache.hostile_ids)} hostile."
        )
        # If friendly is very small, the user's token probably predates the
        # corp/alliance contact scopes — nudge them to re-add their character.
        if len(self._standings_cache.friendly_ids) < 5:
            msg += (
                "\n\nIf you expected more friendly entries, your token may "
                "predate recent scope additions. Remove and re-add your "
                "character in Settings → EVE SSO Characters to pick up the new "
                "corp/alliance contact scopes."
            )
        self._set_paste_result(msg)

    def _schedule_background_standings_refresh(self):
        """Spawn a daemon thread that refreshes standings if an auth becomes available.

        Retries every 30s for up to 5 minutes so authentication completed after
        startup still triggers the refresh.
        """
        import threading
        import time

        def _runner():
            for _ in range(10):  # 10 × 30s = 5 min
                auth = self._active_auth_for_intel()
                if auth is not None:
                    try:
                        self._standings_cache.refresh(auth)
                    except Exception:
                        return
                    # Push the refreshed friendly set into the live monitor.
                    # set_friendly_ids only swaps a set reference (no Tk, no
                    # rebuild) so it is safe to call directly off-thread. Guard
                    # — monitor is None when zkill is disabled.
                    if self.zkill_monitor:
                        self.zkill_monitor.set_friendly_ids(
                            set(self._standings_cache.friendly_ids))
                    # Update label on the Tk thread (FCToolGUI uses self.root,
                    # not subclass-style self).
                    if hasattr(self, "_paste_standings_age"):
                        try:
                            self.root.after(0, self._update_standings_label)
                        except Exception:
                            pass
                    return
                time.sleep(30)

        threading.Thread(target=_runner, daemon=True, name="StandingsRefresh").start()

    # ── Paste Intel helpers ────────────────────────────────────────────────

    def _active_auth_for_intel(self):
        """Return the ESIAuth instance for the user's active character.

        FCToolGUI stores the primary character on self.esi_auth (set in __init__
        from primary_character_id / tracked_character / first account).
        """
        auth = getattr(self, "esi_auth", None)
        if auth is not None and getattr(auth, "character_id", None):
            return auth
        accounts = getattr(self, "esi_accounts", None)
        if accounts:
            for acct in accounts:
                if getattr(acct, "character_id", None):
                    return acct
        return None

    def _update_standings_label(self):
        if hasattr(self, "_paste_standings_age"):
            self._paste_standings_age.config(
                text=f"Standings: {self._standings_cache.age_string()}"
            )

    def _own_character_ids(self) -> set[int]:
        """Return character IDs of all logged-in SSO characters."""
        accounts = getattr(self, "esi_accounts", None) or []
        ids: set[int] = set()
        for acct in accounts:
            cid = getattr(acct, "character_id", None)
            if cid:
                ids.add(cid)
        if not ids:
            auth = self._active_auth_for_intel()
            if auth is not None and getattr(auth, "character_id", None):
                ids.add(auth.character_id)
        return ids

    def _infer_intel_system(self) -> str:
        """Best-effort guess of the system the active character is currently in.

        Order: ESI location → most recent local-scan system (≤ 60s old) → unknown.
        """
        auth = self._active_auth_for_intel()
        if auth is not None:
            try:
                loc = auth.get_location()
            except Exception:  # broad: any failure → fall through to local-scan
                loc = None
            if loc:
                sid = loc.get("solar_system_id")
                if sid:
                    from zkill_monitor import resolve_name
                    return resolve_name(sid, "solar_system")

        from intel_session import find_recent_system
        recent = find_recent_system(
            self._intel_session.local_scans,
            now=datetime.now(timezone.utc),
            window_seconds=60,
        )
        return recent or "unknown"

    def _set_paste_result(self, text: str):
        self._paste_result.config(state=tk.NORMAL)
        self._paste_result.delete("1.0", tk.END)
        self._paste_result.insert(tk.END, text)
        self._paste_result.config(state=tk.DISABLED)

    def _append_intel_summary_line(self, message: str):
        """Append a one-line summary to the right-pane intel log."""
        if not hasattr(self, "_intel_log"):
            return
        stamp = datetime.now().strftime("%H:%M")
        self._intel_log.config(state=tk.NORMAL)
        self._intel_log.insert(tk.END, f"[{stamp}] {message}\n")
        self._intel_log.see(tk.END)
        self._intel_log.config(state=tk.DISABLED)

    # ── Intelligence Fusion ────────────────────────────────────────────────

    def _toggle_intel_fusion(self):
        """Enable or disable intelligence fusion."""
        enabled = self._intel_fusion_var.get()
        if enabled:
            self._start_intel_monitor()
        else:
            self._stop_intel_monitor()

    def _rebuild_intel_channel_checkboxes(self):
        """(Re)build the Intel-panel channel checkboxes from the tracked list.

        Sourced from ``self._tracked_intel_channels``. Safe to call at build
        time and again after a Settings save so the panel reflects the new
        tracked set without an app restart. Existing per-session enabled state
        is preserved for channels that still exist; channels removed from the
        tracked list drop out of both the panel and the enabled set. Checkboxes
        start DISABLED (greyed) until a fusion session activates them; a channel
        that is already enabled (running session) is re-shown checked + active.
        """
        frame = getattr(self, "_intel_channels_frame", None)
        if frame is None:
            return

        # Preserve which channels are currently enabled so a live fusion session
        # keeps its toggles across the rebuild.
        previously_enabled = set(getattr(self, "_intel_channels_enabled", set()))
        running = self._intel_monitor is not None

        for child in frame.winfo_children():
            child.destroy()
        self._intel_channel_vars = {}

        new_enabled: set[str] = set()
        for ch_name in self._tracked_intel_channels:
            still_on = running and ch_name in previously_enabled
            var = tk.BooleanVar(value=still_on)
            self._intel_channel_vars[ch_name] = var
            if still_on:
                new_enabled.add(ch_name)
            cb = tk.Checkbutton(
                frame, text=ch_name,
                variable=var, font=("Consolas", 8),
                fg=FG_MAGENTA if still_on else FG_DIM, bg=BG_PANEL,
                selectcolor=BG_ENTRY, activebackground=BG_PANEL,
                activeforeground=FG_MAGENTA,
                state=tk.NORMAL if running else tk.DISABLED,
                command=self._on_intel_channel_change,
            )
            cb.pack(side=tk.LEFT, padx=4)

        # Drop enabled entries for channels that no longer exist.
        if hasattr(self, "_intel_channels_enabled"):
            self._intel_channels_enabled.clear()
            self._intel_channels_enabled.update(new_enabled)

    def _start_intel_monitor(self):
        """Start the intel channel ChatMonitor."""
        # Load standings (friendly/hostile) in background, then build and start
        # the async name->standing resolver for the firehose stream.
        def _load_and_init_resolver():
            friendly, hostile = (set(), set())
            if self.esi_auth and self.esi_auth.is_authenticated:
                try:
                    friendly, hostile = load_standings(self.esi_auth)
                except Exception:
                    log.exception("intel: load_standings failed")
            self._intel_resolver = IntelResolver(
                friendly=friendly, hostile=hostile)
            self._intel_resolver.start()
        threading.Thread(target=_load_and_init_resolver, daemon=True).start()

        logs_path = self.config.get("eve_logs_path", "")
        if not logs_path or not os.path.isdir(logs_path):
            self._append_zkill_log(
                "\n[Intel] Cannot start: eve_logs_path not configured\n", "dim"
            )
            self._intel_fusion_var.set(False)
            return

        tracked_char = self.config.get("tracked_character", "") or None

        # Scan the tracked channels for which ones are currently active (have a
        # log modified today). Active channels are auto-enabled; inactive ones
        # stay toggleable (clickable but dimmed) so the FC can opt in manually.
        channels = scan_available_channels(
            logs_path, tracked_char, self._tracked_intel_channels
        )
        active_names = {ch["name"] for ch in channels if ch["active"]}
        self._intel_channels_enabled.clear()
        for name, var in self._intel_channel_vars.items():
            is_active = name in active_names
            var.set(is_active)
            if is_active:
                self._intel_channels_enabled.add(name)
            # Keep every tracked checkbox clickable; colour signals active state.
            for w in self._intel_channels_frame.winfo_children():
                if isinstance(w, tk.Checkbutton) and w.cget("text") == name:
                    w.config(state=tk.NORMAL,
                             fg=FG_MAGENTA if is_active else FG_DIM)

        self._intel_monitor = ChatMonitor(
            logs_path=logs_path,
            poll_interval=self.config.get("poll_interval_seconds", 1.0),
            listener_filter=tracked_char,
            channel_filters=list(self._tracked_intel_channels),
        )
        self._intel_monitor.on_message(self._on_intel_message)
        self._intel_thread = threading.Thread(
            target=self._intel_poll_loop, daemon=True
        )
        self._intel_thread.start()

        active = len(self._intel_channels_enabled)

        # Show the right pane
        try:
            self._paned.add(self._intel_right_frame, stretch="always")
        except tk.TclError:
            pass  # Already added

        self._log_prepend(self._intel_log,
            f"[Intel] Intelligence Fusion ACTIVE — {active} channel(s) streaming\n",
            "intel")

    def _stop_intel_monitor(self):
        """Stop the intel channel ChatMonitor."""
        if self._intel_monitor:
            self._intel_monitor.stop()
            self._intel_monitor = None
        if self._intel_resolver is not None:
            try:
                self._intel_resolver.stop()
            except Exception:
                pass
            self._intel_resolver = None
        self._intel_channels_enabled.clear()
        # Disable all checkboxes
        for var in self._intel_channel_vars.values():
            var.set(False)
        for w in self._intel_channels_frame.winfo_children():
            if isinstance(w, tk.Checkbutton):
                w.config(state=tk.DISABLED, fg=FG_DIM)
        # Hide the right pane
        try:
            self._paned.forget(self._intel_right_frame)
        except tk.TclError:
            pass

    def _intel_poll_loop(self):
        """Background polling loop for intel channels."""
        while self._intel_monitor and self._intel_fusion_var.get():
            try:
                self._intel_monitor.poll()
            except Exception:
                log.exception("Intel poll loop iteration failed; continuing to poll")
            time.sleep(self.config.get("poll_interval_seconds", 1.0))

    def _on_intel_channel_change(self):
        """Called when a channel checkbox is toggled."""
        self._intel_channels_enabled.clear()
        for name, var in self._intel_channel_vars.items():
            if var.get():
                self._intel_channels_enabled.add(name)
        if hasattr(self, "_intel_buffer"):
            self._intel_rerender_from_buffer()

    def _intel_rerender_from_buffer(self):
        """Clear the Text and re-render every buffered entry that passes the
        current view filter. The deque is never mutated -- no data is lost."""
        log = self._intel_log
        log.config(state=tk.NORMAL)
        log.delete("1.0", tk.END)
        log.config(state=tk.DISABLED)
        for entry in list(self._intel_buffer):
            msg = entry[0]
            if self._passes_view_filter(msg):
                self._render_line(entry)
        self._intel_new_count = 0
        self._intel_update_new_button()

    _CHANNEL_PALETTE = ("#ff66ff", "#00d4ff", "#00ff88", "#ff8c00",
                        "#ffdd00", "#9b87ff", "#ff6b9d", "#5fd3bc")

    def _channel_color(self, channel: str) -> str:
        c = self._intel_channel_colors.get(channel)
        if c is None:
            c = self._CHANNEL_PALETTE[len(self._intel_channel_colors)
                                      % len(self._CHANNEL_PALETTE)]
            self._intel_channel_colors[channel] = c
        return c

    def _on_intel_sound_toggle(self):
        self.config["intel_sound_enabled"] = bool(self._intel_sound_var.get())
        try:
            self._save_config()
        except Exception:
            pass

    def _on_intel_message(self, msg: ChatMessage):
        """Worker-thread callback: annotate verbatim, enrich for priority only,
        then marshal to the main thread. Nothing is dropped."""
        if msg.channel not in self._intel_channels_enabled:
            return
        try:
            spans = intel_stream.annotate(msg.message)
        except Exception:
            spans = []
        report = None
        try:
            report = parse_intel_message(msg, search_system)
        except Exception:
            report = None
        # Enrich the report for priority/route/system_id (never gates).
        if report is not None and report.system_id:
            staging = self._get_staging_system()
            if staging:
                try:
                    from jump_range import get_stargate_route as _gr
                    o = search_system(staging)
                    if o:
                        conns = self._get_ansiblex_connections()
                        r = _gr(o, report.system_id, connections=conns)
                        if r:
                            report.route_from_staging = (
                                f"{staging} -> {report.system_name}: "
                                f"**{len(r)-1} jumps**")
                except Exception:
                    pass
            try:
                report.has_cyno_beacon = self._check_cyno_beacon(report.system_id)
            except Exception:
                pass
        try:
            min_rep = int(self._intel_min_reported_var.get())
        except (ValueError, AttributeError):
            min_rep = 0
        priority = high_priority(report, min_rep) if report else False
        self.root.after(0, self._intel_stream_ingest, msg, spans, report, priority)

    def _passes_view_filter(self, msg) -> bool:
        if msg.channel not in self._intel_channels_enabled:
            return False
        needle = self._intel_find_var.get().strip().lower()
        if needle:
            hay = f"{msg.channel} {msg.sender} {msg.message}".lower()
            if needle not in hay:
                return False
        return True

    def _intel_stream_ingest(self, msg, spans, report, priority):
        """Main thread: append to the ring buffer, render if visible, fire the
        async resolver, and ping on priority lines."""
        self._intel_buffer.append((msg, spans, report, priority))
        # B3: feed the native-preview intel index (own chat logs only — spec §4).
        # This runs on the Tk thread (root.after-marshalled), so it is a plain
        # write to the single-owner _preview_intel dict.
        if report is not None:
            try:
                self._preview_intel_note(self._preview_intel, report,
                                         time.monotonic())
            except Exception:
                pass
        if self._passes_view_filter(msg):
            self._render_line((msg, spans, report, priority))
        # async name resolution
        if self._intel_resolver is not None:
            try:
                names = intel_stream.candidate_names(msg.message)
                if names:
                    self._intel_resolver.request(
                        names,
                        lambda d: self.root.after(
                            0, self._intel_apply_resolutions, d),
                    )
            except Exception:
                pass
        if priority and self._intel_sound_var.get():
            try:
                self._play_fire_alert()
            except Exception:
                pass

    def _render_line(self, entry):
        """Append one verbatim line at the BOTTOM (deliberate change from the
        old newest-at-top prepend) and apply span tags. Autoscroll only when at
        the bottom; otherwise hold and bump the '▼ N new' counter."""
        msg, spans, report, priority = entry
        log = self._intel_log
        log.config(state=tk.NORMAL)
        ts = msg.timestamp.strftime("%H:%M:%S") if msg.timestamp else "??:??:??"
        at_bottom = log.yview()[1] >= 0.999

        start_index = log.index("end-1c")
        prefix = f"[{ts}] "
        log.insert("end", prefix)
        ch_start = log.index("end-1c")
        log.insert("end", msg.channel)
        ch_end = log.index("end-1c")
        ch_tag = f"chan_{abs(hash(msg.channel)) & 0xffffff}"
        log.tag_config(ch_tag, foreground=self._channel_color(msg.channel))
        log.tag_add(ch_tag, ch_start, ch_end)

        log.insert("end", f"  {msg.sender} > ")
        body_start = log.index("end-1c")
        log.insert("end", msg.message)

        # Apply structural span tags relative to body_start.
        kind_to_tag = {
            "system": "intel_system", "count": "intel_count",
            "clear": "intel_clear", "camp": "intel_camp",
            "spike": "intel_spike", "cyno": "intel_cyno",
            "dscan_url": "intel_dscan",
        }
        for sp in spans:
            tag = kind_to_tag.get(sp.kind)
            if not tag:
                continue
            s = log.index(f"{body_start}+{sp.start}c")
            e = log.index(f"{body_start}+{sp.end}c")
            log.tag_add(tag, s, e)
            if sp.kind == "system":
                self._bind_system_span(log, tag, s, e, sp.value)
            elif sp.kind == "dscan_url":
                self._bind_dscan_span(log, s, e, sp.payload.get("url", ""))

        log.insert("end", "\n")
        end_index = log.index("end-1c")
        if priority:
            log.tag_add("intel_priority", start_index, end_index)

        # Bounded trim: keep at most maxlen lines in the widget.
        cap = self._intel_buffer.maxlen or 2000
        line_count = int(log.index("end-1c").split(".")[0])
        if line_count > cap:
            log.delete("1.0", f"{line_count - cap + 1}.0")

        log.config(state=tk.DISABLED)
        if at_bottom and not self._intel_autoscroll_paused:
            log.see("end")
        else:
            self._intel_new_count += 1
            self._intel_update_new_button()

    def _bind_system_span(self, log, base_tag, s, e, system_name):
        """Per-instance click + right-click bindings for a system span."""
        click_tag = f"sysclick_{abs(hash((system_name, s))) & 0xffffff}"
        log.tag_add(click_tag, s, e)
        log.tag_bind(click_tag, "<Button-1>",
                     lambda ev, n=system_name: self._set_destination_or_copy(n))
        log.tag_bind(click_tag, "<Button-3>",
                     lambda ev, n=system_name: self._intel_system_menu(ev, n))
        log.tag_bind(click_tag, "<Enter>",
                     lambda ev: log.config(cursor="hand2"))
        log.tag_bind(click_tag, "<Leave>",
                     lambda ev: log.config(cursor=""))

    def _bind_dscan_span(self, log, s, e, url):
        """Per-instance click binding for a D-Scan URL span. Left-click opens
        the URL with the SAME call the legacy 'D-Scan' card button used
        (self._open_url(report.dscan_url), fc_gui.py:14334)."""
        if not url:
            return
        click_tag = f"dscan_{abs(hash((url, s))) & 0xffffff}"
        log.tag_add(click_tag, s, e)
        log.tag_bind(click_tag, "<Button-1>",
                     lambda ev, u=url: self._open_url(u))
        log.tag_bind(click_tag, "<Enter>",
                     lambda ev: log.config(cursor="hand2"))
        log.tag_bind(click_tag, "<Leave>",
                     lambda ev: log.config(cursor=""))

    def _intel_update_new_button(self):
        if self._intel_new_btn is not None:
            try:
                self._intel_new_btn.config(text=f"▼ {self._intel_new_count} new")
                self._intel_new_btn.pack(side=tk.RIGHT, padx=2) \
                    if self._intel_new_count else self._intel_new_btn.pack_forget()
            except Exception:
                pass

    _STANDING_TAG = {
        "friendly": "name_friendly", "hostile": "name_hostile",
        "neutral": "name_neutral", "unknown": "name_unknown",
    }

    def _intel_apply_resolutions(self, resolutions: dict):
        """Main thread: for each resolved name found in the visible text, add a
        standing-coloured 'name' tag + corp/alliance tooltip. No-op for names
        that have scrolled out (search returns nothing)."""
        log = self._intel_log
        log.config(state=tk.NORMAL)
        try:
            for name, res in resolutions.items():
                tag = self._STANDING_TAG.get(res.standing, "name_unknown")
                tip = res.alliance or res.corporation or ""
                idx = "1.0"
                while True:
                    pos = log.search(name, idx, stopindex="end", nocase=False)
                    if not pos:
                        break
                    end = f"{pos}+{len(name)}c"
                    log.tag_add(tag, pos, end)
                    if tip:
                        bind_tag = f"nametip_{abs(hash((name, pos))) & 0xffffff}"
                        log.tag_add(bind_tag, pos, end)
                        log.tag_bind(bind_tag, "<Enter>",
                                     lambda e, t=f"[{tip}]": self._show_tooltip(e, t))
                        log.tag_bind(bind_tag, "<Leave>",
                                     lambda e: self._hide_tooltip())
                    idx = end
        finally:
            log.config(state=tk.DISABLED)

    def _intel_system_menu(self, event, system_name: str):
        """Right-click context menu on a system span; reuses the EXACT legacy
        card actions. Items: Set destination (boss) / Copy name / Open in
        Dotlan / Navigate WH route / Titan bridge. D-scan is NOT here -- the
        legacy 'D-Scan' action (self._open_url(report.dscan_url)) is
        per-message, so it is exposed as a clickable dscan span instead (see
        _bind_dscan_span), not as a system-menu item."""
        menu = tk.Menu(self.root, tearoff=0, bg=BG_PANEL, fg=FG_TEXT)
        # Set destination — boss-gated copy/destination helper.
        menu.add_command(
            label="Set destination (boss)",
            command=lambda: self._set_destination_or_copy(system_name))
        menu.add_command(
            label="Copy name",
            command=lambda: (self.root.clipboard_clear(),
                             self.root.clipboard_append(system_name)))
        # Open in Dotlan — same URL + self._open_url as the legacy 'Dotlan' button.
        dotlan = (f"https://evemaps.dotlan.net/system/"
                  f"{system_name.replace(' ', '_')}")
        menu.add_command(label="Open in Dotlan",
                         command=lambda: self._open_url(dotlan))
        # Navigate WH route — same call as the legacy 'Navigate' button.
        menu.add_command(
            label="Navigate WH route",
            command=lambda: self._navigate_wh_route(system_name))
        # Titan bridge — same call as the legacy 'Titan Bridge?' button.
        menu.add_command(
            label="Titan bridge",
            command=lambda: self._navigate_jump_range(system_name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _intel_jump_to_bottom(self):
        self._intel_log.see("end")
        self._intel_new_count = 0
        self._intel_autoscroll_paused = False
        if hasattr(self, "_intel_pause_var"):
            self._intel_pause_var.set(False)
        self._intel_update_new_button()

    def _check_cyno_beacon(self, system_id: int) -> bool:
        """Check if a Pharolux Cynosural Beacon exists in the given system via ESI."""
        if not self.esi_auth or not self.esi_auth.is_authenticated:
            return False
        try:
            # Search for structures with "Pharolux" in name in the system
            # ESI: /characters/{character_id}/search/?categories=structure&search=Pharolux
            char_id = self.esi_auth._character_id
            if not char_id:
                return False
            data = self.esi_auth.esi_get(
                f"/characters/{char_id}/search/",
                params={"categories": "structure", "search": "Pharolux", "strict": "false"},
            )
            if not data or "structure" not in data:
                return False
            # Check each structure to see if it's in our target system
            for struct_id in data["structure"][:20]:  # Check up to 20 results
                info = self.esi_auth.esi_get(f"/universe/structures/{struct_id}/")
                if info and info.get("solar_system_id") == system_id:
                    return True
        except Exception:
            pass
        return False

    # ── Log helpers (parameterized for split pane) ─────────────────────────

    def _begin_alert_block(self, log_widget=None):
        """Begin a new alert block at the top of the log (newest-first)."""
        w = log_widget or self._zkill_log
        w.config(state=tk.NORMAL)
        w.mark_set("alert_ins", "1.0")
        w.mark_gravity("alert_ins", tk.RIGHT)
        self._current_log = w

    def _end_alert_block(self, log_widget=None):
        """Finish an alert block and scroll to show it at the top."""
        w = log_widget or self._current_log or self._zkill_log
        w.see("1.0")
        w.config(state=tk.DISABLED)

    def _clear_zkill_log(self):
        """Clear the zKillboard intel pane."""
        self._zkill_log.config(state=tk.NORMAL)
        self._zkill_log.delete("1.0", tk.END)
        self._zkill_log.config(state=tk.DISABLED)

    def _clear_intel_log(self):
        """Clear the Intel Channels pane."""
        self._intel_log.config(state=tk.NORMAL)
        self._intel_log.delete("1.0", tk.END)
        self._intel_log.config(state=tk.DISABLED)
        if hasattr(self, "_intel_buffer"):
            self._intel_buffer.clear()
        self._intel_new_count = 0
        self._intel_update_new_button()

    def _append_zkill_log(self, text, tag=None):
        w = self._current_log or self._zkill_log
        w.config(state=tk.NORMAL)
        # Ensure the insertion mark exists
        try:
            w.index("alert_ins")
        except tk.TclError:
            w.mark_set("alert_ins", "1.0")
            w.mark_gravity("alert_ins", tk.RIGHT)
        if tag:
            w.insert("alert_ins", text, tag)
        else:
            w.insert("alert_ins", text)
        # Don't disable yet — caller may add more content

    def _log_prepend(self, log_widget, text, tag=None):
        """Prepend a single line to the given log widget."""
        log_widget.config(state=tk.NORMAL)
        log_widget.insert("1.0", text + "\n", tag)
        log_widget.see("1.0")
        log_widget.config(state=tk.DISABLED)

    # ── Jump Range ────────────────────────────────────────────────────────────

    def _do_range_check(self):
        origin = self._range_origin.get().strip()
        dest = self._range_dest.get().strip()
        if not origin or not dest:
            self._range_result_label.config(text="Enter both systems", fg=FG_ORANGE)
            return

        ship = self._ship_type_var.get()
        self._range_result_label.config(text="Checking...", fg=FG_DIM)
        self._range_detail_label.config(text="")
        self.root.update_idletasks()

        # Staging systems come from the user-editable, persisted lists.
        # Dedupe case-insensitively and exclude any system equal to the
        # destination (the dest-vs-dest check is meaningless).
        dest_lower = dest.lower()

        def _build_list(source):
            seen = set()
            out = []
            for s in source:
                s_lower = s.strip().lower()
                if not s_lower or s_lower in seen or s_lower == dest_lower:
                    continue
                seen.add(s_lower)
                out.append(s)
            return out

        friendly_systems = _build_list(self._friendly_staging)
        hostile_systems = _build_list(self._hostile_staging)
        no_staging = not self._friendly_staging and not self._hostile_staging

        def do_check():
            try:
                from jump_range import save_route_cache, calculate_ly_distance
                checker = JumpRangeChecker(ship, jdc_level=5)
                conns = self._get_ansiblex_connections()
                result = checker.check_range(origin, dest, connections=conns)
                # Add region names (local lookup; the region map is cached in
                # regions_cache.json and prewarmed at startup — no per-check ESI).
                try:
                    region_map = system_cache.get_region_map()
                    result["origin_region"] = region_map.get(str(result.get("origin_id", 0)), "")
                    result["dest_region"] = region_map.get(str(result.get("destination_id", 0)), "")
                except Exception:
                    result["origin_region"] = ""
                    result["dest_region"] = ""

                # Range thresholds at JDC 5
                titan_range = JumpRangeChecker("Titan", jdc_level=5).jump_range
                capital_range = JumpRangeChecker("Dreadnought", jdc_level=5).jump_range
                blops_range = JumpRangeChecker("Black Ops", jdc_level=5).jump_range
                dest_id = result.get("destination_id")

                def _compute_range_list(systems_list):
                    """Compute range results for a list of systems."""
                    results_list = []
                    if not dest_id:
                        return results_list
                    sec_ids = {}
                    for sys_name in systems_list:
                        sid = search_system(sys_name)
                        if sid:
                            sec_ids[sys_name] = sid
                    staging = self._get_staging_system()
                    staging_id = search_system(staging) if staging else None
                    for sys_name in systems_list:
                        sid = sec_ids.get(sys_name)
                        if sid:
                            dist = calculate_ly_distance(sid, dest_id)
                            gate_jumps = None
                            if staging_id and conns:
                                from jump_range import get_stargate_route
                                route = get_stargate_route(staging_id, sid, connections=conns)
                                if route:
                                    gate_jumps = len(route) - 1
                            if dist is not None:
                                results_list.append({
                                    "system": sys_name,
                                    "in_titan_range": dist <= titan_range,
                                    "in_capital_range": dist <= capital_range,
                                    "in_blops_range": dist <= blops_range,
                                    "distance_ly": round(dist, 2),
                                    "jumps_from_staging": gate_jumps,
                                })
                    return results_list

                result["secondary"] = _compute_range_list(friendly_systems)
                result["hostile"] = _compute_range_list(hostile_systems)
                result["no_staging"] = no_staging

                save_route_cache()
                self.root.after(0, self._show_range_result, result)
            except Exception as e:
                print(f"[JumpRange] Error in range check: {e}")
                import traceback
                traceback.print_exc()
                self.root.after(0, self._range_result_label.config,
                                {"text": f"Error: {e}", "fg": FG_RED})

        threading.Thread(target=do_check, daemon=True).start()

    def _place_range_cell(self, table, row: int, col: int,
                          in_range: bool, chars: list[str],
                          padx=(0, 6)):
        """Place a YES/NO cell in the range check table.
        If >3 characters match, show 'YES (3 chars)' with hover to expand."""
        MAX_INLINE = 3
        color = FG_GREEN if in_range else FG_RED

        if not in_range or not chars:
            text = "YES" if in_range else "NO"
            tk.Label(table, text=text, font=("Consolas", 10, "bold"),
                     fg=color, bg=BG_DARK, anchor=tk.W
                     ).grid(row=row, column=col, padx=padx, sticky=tk.W)
            return

        if len(chars) <= MAX_INLINE:
            text = f"YES ({', '.join(chars)})"
            tk.Label(table, text=text, font=("Consolas", 10, "bold"),
                     fg=color, bg=BG_DARK, anchor=tk.W
                     ).grid(row=row, column=col, padx=padx, sticky=tk.W)
        else:
            # Compact: show count, hover for full list
            text = f"YES ({len(chars)} chars)"
            lbl = tk.Label(table, text=text, font=("Consolas", 10, "bold"),
                           fg=color, bg=BG_DARK, anchor=tk.W, cursor="hand2")
            lbl.grid(row=row, column=col, padx=padx, sticky=tk.W)
            full_list = ", ".join(chars)
            lbl.bind("<Enter>",
                     lambda e, t=full_list: self._show_tooltip(e, t))
            lbl.bind("<Leave>",
                     lambda e: self._hide_tooltip())

    def _find_chars_with_cap_at(self, cap_key: str, system_name: str) -> list[str]:
        """Find connected characters who have a given capability at a system.
        cap_key: 'titans', 'dreads', 'fax', 'blops'
        Returns list of character names."""
        names = []
        if not hasattr(self, '_char_panels'):
            return names
        for panel in self._char_panels:
            info = getattr(panel, '_info', None)
            if not info:
                continue
            acct = panel._acct
            char_name = acct.character_name or "Unknown"
            for entry in info.get(cap_key, []):
                if isinstance(entry, dict) and entry.get("location") == system_name:
                    if char_name not in names:
                        names.append(char_name)
                    break
        return names

    def _show_range_result(self, result):
        # Clear secondary table
        for w in self._range_secondary_frame.winfo_children():
            w.destroy()

        if "error" in result:
            self._range_result_label.config(text=result["error"], fg=FG_RED)
            return

        if result["in_range"]:
            self._range_result_label.config(text="IN RANGE", fg=FG_GREEN)
        else:
            self._range_result_label.config(text="OUT OF RANGE", fg=FG_RED)

        o_region = f" ({result['origin_region']})" if result.get("origin_region") else ""
        d_region = f" ({result['dest_region']})" if result.get("dest_region") else ""
        details = (
            f"{result['origin']}{o_region}  -->  {result['destination']}{d_region}\n"
            f"Distance: {result['distance_ly']} LY   |   "
            f"{result['ship_type']} range: {result['jump_range_ly']} LY"
        )
        if result.get("gate_jumps") is not None:
            details += f"   |   Gate jumps: {result['gate_jumps']}"
        self._range_detail_label.config(text=details, fg=FG_TEXT)

        # Show friendly range check table
        friendly = result.get("secondary", [])
        if friendly:
            self._render_range_table(
                result["destination"], friendly,
                title_suffix="(Friendly)", title_color=FG_GREEN,
                cross_ref=True,
            )

        # Show hostile range check table
        hostile = result.get("hostile", [])
        if hostile:
            self._render_range_table(
                result["destination"], hostile,
                title_suffix="(Hostile)", title_color=FG_RED,
                cross_ref=False, show_jumps=False,
            )

        # No staging systems configured yet: the origin->dest headline above
        # still shows, but prompt the user to populate the lists below.
        if result.get("no_staging"):
            tk.Label(
                self._range_secondary_frame,
                text="No staging systems yet — add friendly/hostile staging "
                     "systems below to check them against this destination.",
                font=("Consolas", 10), fg=FG_DIM, bg=BG_DARK,
                justify=tk.LEFT, wraplength=900,
            ).pack(anchor=tk.W, pady=(6, 3))

    def _render_range_table(self, destination: str, entries: list[dict],
                            title_suffix: str = "", title_color: str = FG_ACCENT,
                            cross_ref: bool = False, show_jumps: bool = True):
        """Render a secondary range check table into the secondary frame."""
        parent = self._range_secondary_frame

        title = f"Range Check to {destination}"
        if title_suffix:
            title += f" {title_suffix}"
        tk.Label(parent, text=title,
                 font=("Consolas", 11, "bold"), fg=title_color, bg=BG_DARK,
                 ).pack(anchor=tk.W, pady=(5, 3))

        table = tk.Frame(parent, bg=BG_DARK)
        table.pack(anchor=tk.W, padx=10)

        # Header row
        col = 0
        tk.Label(table, text="System", font=("Consolas", 10, "bold"),
                 fg=FG_DIM, bg=BG_DARK, width=14, anchor=tk.W
                 ).grid(row=0, column=col, padx=(0, 10)); col += 1
        if show_jumps:
            tk.Label(table, text="Jumps from Staging", font=("Consolas", 10, "bold"),
                     fg=FG_DIM, bg=BG_DARK, width=18, anchor=tk.W
                     ).grid(row=0, column=col, padx=(0, 10)); col += 1
        tk.Label(table, text="Super (6 LY)", font=("Consolas", 10, "bold"),
                 fg=FG_DIM, bg=BG_DARK, anchor=tk.W
                 ).grid(row=0, column=col, padx=(0, 10), sticky=tk.W); col += 1
        tk.Label(table, text="Dread (7 LY)", font=("Consolas", 10, "bold"),
                 fg=FG_DIM, bg=BG_DARK, anchor=tk.W
                 ).grid(row=0, column=col, padx=(0, 10), sticky=tk.W); col += 1
        tk.Label(table, text="Blops (8 LY)", font=("Consolas", 10, "bold"),
                 fg=FG_DIM, bg=BG_DARK, anchor=tk.W
                 ).grid(row=0, column=col, padx=(0, 10), sticky=tk.W); col += 1
        tk.Label(table, text="Distance", font=("Consolas", 10, "bold"),
                 fg=FG_DIM, bg=BG_DARK, width=10, anchor=tk.W
                 ).grid(row=0, column=col)

        for i, sr in enumerate(entries, 1):
            col = 0
            sys_label = tk.Label(table, text=sr["system"],
                     font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                     width=14, anchor=tk.W, cursor="hand2")
            sys_label.grid(row=i, column=col, padx=(0, 10)); col += 1
            sys_name = sr["system"]
            menu = tk.Menu(sys_label, tearoff=0, bg=BG_PANEL, fg=FG_TEXT,
                           activebackground=FG_ACCENT, activeforeground=BG_DARK)
            menu.add_command(label=f"Set destination: {sys_name}",
                             command=lambda n=sys_name: self._set_destination_or_copy(n))
            menu.add_command(label=f"Copy \"{sys_name}\"",
                             command=lambda n=sys_name: (self.root.clipboard_clear(), self.root.clipboard_append(n)))
            sys_label.bind("<Button-3>", lambda e, m=menu: m.tk_popup(e.x_root, e.y_root))
            sys_label.bind("<Button-1>", lambda e, n=sys_name: self._set_destination_or_copy(n))

            # Jumps from staging (friendly only)
            if show_jumps:
                jumps = sr.get("jumps_from_staging")
                jumps_text = str(jumps) if jumps is not None else "?"
                tk.Label(table, text=jumps_text,
                         font=("Consolas", 10), fg=FG_ACCENT, bg=BG_DARK,
                         width=6, anchor=tk.W
                         ).grid(row=i, column=col, padx=(0, 10)); col += 1

            # Titan range — cross-ref with character capabilities (friendly only)
            titan_chars = (self._find_chars_with_cap_at("titans", sys_name)
                           if cross_ref and sr["in_titan_range"] else [])
            self._place_range_cell(
                table, i, col, sr["in_titan_range"], titan_chars, padx=(0, 6)
            ); col += 1

            # Capital range — cross-ref dreads + fax
            cap_caps = []
            if cross_ref and sr["in_capital_range"]:
                cap_caps = (self._find_chars_with_cap_at("dreads", sys_name)
                            + self._find_chars_with_cap_at("fax", sys_name))
                cap_caps = list(dict.fromkeys(cap_caps))
            self._place_range_cell(
                table, i, col, sr["in_capital_range"], cap_caps, padx=(0, 6)
            ); col += 1

            # Blops range — cross-ref blops
            blops_chars = (self._find_chars_with_cap_at("blops", sys_name)
                           if cross_ref and sr["in_blops_range"] else [])
            self._place_range_cell(
                table, i, col, sr["in_blops_range"], blops_chars, padx=(0, 10)
            ); col += 1

            dist = sr["distance_ly"]
            dist_text = f"{dist:.1f} LY" if dist is not None else "N/A"
            tk.Label(table, text=dist_text,
                     font=("Consolas", 10), fg=FG_DIM, bg=BG_DARK,
                     width=10, anchor=tk.W
                     ).grid(row=i, column=col)


    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _on_close(self):
        self._running = False
        self._stop_monitoring()
        # Persist route cache to disk
        try:
            from jump_range import save_route_cache
            save_route_cache()
        except Exception:
            pass
        # Tear down the Eve-O overlay (cancel its after-loop, stop the poller,
        # withdraw the window) before destroying root. Guarded: the attribute /
        # method may be absent if overlay init never ran.
        try:
            teardown = getattr(self, "_overlay_teardown", None)
            if callable(teardown):
                teardown()
        except Exception:
            pass
        # Tear down the native-preview controller too (cancel its after-loop,
        # retire tiles, stop the hotkey worker thread). Guarded like the overlay.
        try:
            preview_teardown = getattr(self, "_preview_teardown", None)
            if callable(preview_teardown):
                preview_teardown()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    app = FCToolGUI()
    app.run()


if __name__ == "__main__":
    main()
