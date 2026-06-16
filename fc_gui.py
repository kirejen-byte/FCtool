"""
FCTool GUI - Fleet Commander Assistant Frontend
Tkinter-based GUI that wraps all FCTool modules.
"""

import json
import os
import sys
import shutil
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
import requests
from tkinter import ttk, scrolledtext, messagebox, filedialog
from datetime import datetime, timedelta

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
from chat_monitor import ChatMonitor, ChatMessage
from intel_monitor import (
    IntelReport, parse_intel_message, scan_available_channels,
    discover_channels, INTEL_CHANNELS, parse_dscan_text, make_dscan_summary,
    resolve_characters, coalesce_report, load_standings_whitelist,
)
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
from loss_tracker import FleetLossTracker, DeathEvent
from cyno_check import analyze_character as cyno_analyze_character
from eve_paths import resolve_eve_logs_path
from default_config import DEFAULT_CONFIG
import tts_helper
from app_path import app_dir
import ship_classes
import charge_tracker
import command_bursts


CONFIG_PATH = os.path.join(app_dir(), "config.json")

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
BORDER_COLOR = "#2a2a4a"

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


class FCToolGUI:
    def __init__(self):
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

        for attr in ['_range_origin', '_range_dest', '_wh_origin', '_wh_dest', '_staging_entry', '_range_add_entry']:
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
                        self.config["ansiblex_connections"] = gates
                        self._save_config()
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
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
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
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.config, f, indent=4)

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
        style.configure("Dark.TButton", background=BG_ENTRY, foreground=FG_TEXT,
                         font=("Consolas", 10), borderwidth=1)
        style.map("Dark.TButton",
                  background=[("active", "#1a5a90")],
                  foreground=[("active", FG_WHITE)])
        style.configure("Green.TButton", background="#006644", foreground=FG_WHITE,
                         font=("Consolas", 10, "bold"))
        style.map("Green.TButton",
                  background=[("active", "#008855")])
        style.configure("Red.TButton", background="#660022", foreground=FG_WHITE,
                         font=("Consolas", 10, "bold"))
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
        self._build_settings_tab()

        # Track zkill alert notifications
        self._zkill_has_unread = False
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

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
        # Kept for compatibility with existing update code (no-op display)
        self._xup_threshold_label = tk.Label(
            xup_label_row, text="", font=("Consolas", 1), bg=BG_PANEL,
        )

        ttk.Button(xup_label_row, text="Reset", style="Red.TButton",
                   command=self._reset_xup).pack(side=tk.LEFT, padx=(8, 10))

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

        comp_header = tk.Frame(comp_left, bg=BG_PANEL)
        comp_header.pack(fill=tk.X, padx=8)
        tk.Label(comp_header, text="DPS", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_PANEL, width=4).pack(side=tk.LEFT)
        tk.Label(comp_header, text="Ship Type", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT)

        self._fleet_comp_frame = tk.Frame(comp_left, bg=BG_PANEL)
        self._fleet_comp_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        self._fleet_comp_labels: list[tk.Label] = []
        self._fleet_comp_prev: list[tuple[str, int]] = []  # for flicker prevention
        self._dps_designated: set[str] = set()  # ship names marked as DPS
        self._dps_ratio_label = tk.Label(comp_left, text="", font=("Consolas", 9, "bold"),
                                          fg=FG_DIM, bg=BG_PANEL)
        self._dps_ratio_label.pack(anchor=tk.W, padx=8, pady=(0, 4))
        self._fleet_total = 0
        self._fleet_ship_counts: dict[str, int] = {}  # ship_name -> count

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

        # Create collapsible sections (order matters for display)
        self._links_container, self._links_content, self._links_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Links / Command Ships")
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
        log_label = tk.Label(tab, text="X-Up Log", font=("Consolas", 10, "bold"),
                              fg=FG_ACCENT, bg=BG_DARK)
        log_label.pack(anchor=tk.W, padx=15, pady=(4, 2))

        self._xup_log = scrolledtext.ScrolledText(
            tab, height=4, font=("Consolas", 9),
            bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
            selectbackground="#1a5a90", wrap=tk.WORD, state=tk.DISABLED,
            borderwidth=1, relief=tk.RIDGE
        )
        self._xup_log.pack(fill=tk.X, padx=10, pady=(0, 10))
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

        self._zkill_indicator = tk.Label(header, text="  LIVE",
                                          font=("Consolas", 10, "bold"),
                                          fg=FG_GREEN, bg=BG_DARK)
        self._zkill_indicator.pack(side=tk.LEFT, padx=10)

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

        self._intel_channels_frame = tk.Frame(intel_row, bg=BG_PANEL)
        self._intel_channels_frame.pack(side=tk.LEFT, padx=(15, 0))

        # Intel-monitor state must exist before building the checkboxes, since
        # the (re)build inspects _intel_monitor / _intel_channels_enabled.
        self._intel_monitor: ChatMonitor | None = None
        self._intel_thread: threading.Thread | None = None
        self._intel_channels_enabled: set[str] = set()

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
            log.tag_config("intel", foreground=FG_MAGENTA,
                           font=("Consolas", 11, "bold"))
            log.tag_config("intel_clear", foreground=FG_GREEN,
                           font=("Consolas", 11, "bold"))
            log.tag_config("intel_system", foreground=FG_ACCENT)
            log.tag_config("intel_meta", foreground=FG_DIM)
            log.tag_config("info", foreground=FG_ACCENT)
            log.tag_config("value", foreground=FG_ORANGE)
            log.tag_config("dim", foreground=FG_DIM)
            log.tag_config("fused", foreground=FG_YELLOW,
                           font=("Consolas", 11, "bold"))
            log.tag_config("hostile_char", foreground=FG_RED,
                           font=("Consolas", 10, "bold"))

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
        ship_types = ["Dreadnought", "Carrier", "Force Auxiliary", "Supercarrier",
                      "Titan", "Black Ops", "Jump Freighter", "Rorqual"]
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

    def _update_dps_ratio(self):
        """Recalculate and display DPS ratio based on checked ship types."""
        if not self._dps_designated or self._fleet_total == 0:
            self._dps_ratio_label.config(text="")
            return
        dps_count = sum(self._fleet_ship_counts.get(name, 0)
                        for name in self._dps_designated)
        ratio = dps_count / self._fleet_total
        pct = int(ratio * 100)
        if ratio >= 0.6:
            self._dps_ratio_label.config(
                text=f"\u2705 Good Ratio ({dps_count}/{self._fleet_total}, {pct}%)",
                fg=FG_GREEN)
        else:
            self._dps_ratio_label.config(
                text=f"\u274c Need more DPS ({dps_count}/{self._fleet_total}, {pct}%)",
                fg="#ff6666")

    def _update_fleet_composition(self, ship_counts: dict[int, int], total: int):
        """Update the Top 10 fleet composition display."""
        from zkill_monitor import resolve_name

        self._fleet_size_label.config(text=f"Fleet Size: {total}")
        self._fleet_total = total

        # Build top 10 list
        sorted_ships = sorted(ship_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        new_data = [(resolve_name(tid, "type"), count) for tid, count in sorted_ships]

        # Update name->count mapping for DPS ratio calculation
        self._fleet_ship_counts = {name: count for name, count in new_data}

        # Flicker prevention: only rebuild if data changed
        if new_data == self._fleet_comp_prev:
            self._update_dps_ratio()
            return
        self._fleet_comp_prev = new_data

        # Prune DPS designations for ship types no longer in fleet
        current_names = {name for name, _ in new_data}
        self._dps_designated &= current_names

        # Clear and rebuild
        for widget in self._fleet_comp_frame.winfo_children():
            widget.destroy()
        self._fleet_comp_labels = []

        for ship_name, count in new_data:
            row = tk.Frame(self._fleet_comp_frame, bg=BG_PANEL)
            row.pack(fill=tk.X)

            is_dps = tk.BooleanVar(value=ship_name in self._dps_designated)

            def on_toggle(name=ship_name, var=is_dps):
                if var.get():
                    self._dps_designated.add(name)
                else:
                    self._dps_designated.discard(name)
                self._update_dps_ratio()

            cb = tk.Checkbutton(row, variable=is_dps, command=on_toggle,
                                 bg=BG_PANEL, fg=FG_TEXT, selectcolor="#2a2a3a",
                                 activebackground=BG_PANEL, activeforeground=FG_TEXT,
                                 highlightthickness=0, bd=1, relief=tk.FLAT)
            cb.pack(side=tk.LEFT)

            lbl = tk.Label(row, text=f"{ship_name}: {count}",
                           font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W)
            lbl.pack(side=tk.LEFT)
            self._fleet_comp_labels.append(lbl)

        if not new_data:
            tk.Label(self._fleet_comp_frame, text="  No fleet data",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL, anchor=tk.W
                     ).pack(anchor=tk.W)

        self._update_dps_ratio()

    def _update_specialized_roles(self, members: list[dict], ship_counts: dict[int, int], total: int):
        """Update all collapsible specialized role sections."""
        from ship_classes import (
            ALL_LINKS_COMMAND, ALL_LOGISTICS, ALL_CYNO, ALL_WEBS,
            ALL_HICS, ALL_BRIDGE, ALL_FAX, ALL_DREADS,
            TITANS, BLACK_OPS, TACTICAL_DESTROYERS, is_defender
        )
        from zkill_monitor import resolve_name

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
            if cat_key == "links":
                # Links is owned by _render_links_section so per-pilot booster
                # charges + off-hull posters render (and collapse) inside it. It
                # reads the _links_categories/_links_threshold cached just above.
                self._render_links_section()
            else:
                self._populate_role_section(content, count_lbl, members_dict,
                                            threshold=threshold, sort_override=sort_override)

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

    def _populate_role_section(self, content_frame, count_label, ship_members,
                                threshold: int | None = None,
                                sort_override: dict | None = None,
                                rows_by_name: dict | None = None):
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
        """Rebuild the ESI character list in Settings."""
        for w in self._esi_chars_frame.winfo_children():
            w.destroy()
        if not self.esi_accounts:
            tk.Label(self._esi_chars_frame, text="No characters connected",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK
                     ).pack(anchor=tk.W)
            return
        for acct in self.esi_accounts:
            row = tk.Frame(self._esi_chars_frame, bg=BG_DARK)
            row.pack(fill=tk.X, pady=1)
            is_primary = (acct is self.esi_auth)
            name = acct.character_name or "Unknown"
            prefix = "[PRIMARY] " if is_primary else ""
            fg = FG_GREEN if acct.is_authenticated else FG_DIM
            lbl = tk.Label(row, text=f"{prefix}{name}",
                           font=("Consolas", 10), fg=fg, bg=BG_DARK, anchor=tk.W)
            lbl.pack(side=tk.LEFT, padx=(0, 10))
            if not is_primary:
                ttk.Button(row, text="Set Primary", style="Dark.TButton",
                           command=lambda a=acct: self._esi_set_primary(a)
                           ).pack(side=tk.LEFT, padx=2)
            ttk.Button(row, text="Disconnect", style="Dark.TButton",
                       command=lambda a=acct: self._esi_disconnect(a)
                       ).pack(side=tk.LEFT, padx=2)

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
            with open(self._CHAR_CACHE_FILE, "w") as f:
                json.dump(data, f)
        except Exception as e:
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
                resp = _req.get(
                    f"https://esi.evetech.net/latest/universe/stations/{location_id}/",
                    headers={"User-Agent": "FCTool/1.0"}, timeout=8,
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

            # Structure lookup failed (403) — try /assets/locations/ as last resort
            token = acct.access_token
            if token:
                try:
                    # Find any asset at this location to use for the locations endpoint
                    # We need item_ids of assets AT this location
                    resp = acct._session.post(
                        f"https://esi.evetech.net/latest/characters/{acct.character_id}/assets/locations/",
                        headers={"Authorization": f"Bearer {token}"},
                        json=[location_id],
                        timeout=10,
                    )
                    if resp.ok:
                        locs = resp.json()
                        # This returns positions — not directly useful for system name
                        # But we can try /assets/names/ to at least get a readable name
                except Exception:
                    pass

                # Try /assets/names/ for a human-readable location name
                try:
                    resp = acct._session.post(
                        f"https://esi.evetech.net/latest/characters/{acct.character_id}/assets/names/",
                        headers={"Authorization": f"Bearer {token}"},
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

    def _render_capabilities(self, panel, info: dict, only_cap: str = ""):
        """Render capability rows in a panel's cap_frame.
        If only_cap is set (e.g. 'fax'), only show that capability."""
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
        if info.get("cyno") and (not only_cap or only_cap == "cyno"):
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
        if info.get("dictor") and (not only_cap or only_cap == "hic/dictor"):
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
                                       only_cap=cap_key)

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

    # ── Settings Tab ──────────────────────────────────────────────────────────

    def _build_settings_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Settings  ")

        # ── Save bar at top (always visible) ─────────────────────────────
        save_bar = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                            highlightbackground=BORDER_COLOR, highlightthickness=1)
        save_bar.pack(fill=tk.X, padx=10, pady=(5, 0))
        ttk.Button(save_bar, text="Save Settings & Restart",
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
        )
        self._staging_entry.pack(side=tk.LEFT, padx=5)
        # Pre-fill with saved staging system
        saved_staging = self.config.get("zkillboard", {}).get("staging_system", "")
        if saved_staging:
            self._staging_entry.insert(0, saved_staging)
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
        self._save_status.config(text="Saved! Restart to apply.", fg=FG_GREEN)

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
        self._save_status.config(text="Saved & Restarted!", fg=FG_GREEN)

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
        PhotoImages; self._burst_icons_small holds ~16px subsample(4,4) copies.
        Both the top coverage strip and the per-pilot Links rows render the
        small (~16px) set; the full-size set is kept only as the source for the
        subsample copies. Glyph fallback on any failure. Called during UI build,
        after self.root exists."""
        from app_path import bundle_dir
        files = {
            command_bursts.SHIELD: "shield.png",
            command_bursts.ARMOR: "armor.png",
            command_bursts.SKIRMISH: "skirmish.png",
            command_bursts.INFORMATION: "info.png",
        }
        for disc, fname in files.items():
            try:
                path = os.path.join(bundle_dir(), "assets", "bursts", fname)
                full = tk.PhotoImage(file=path)
                self._burst_icons[disc] = full
                # Small copy (64px -> ~16px) for the inline top strip and the
                # per-pilot Links rows; the reference is retained in the dict so
                # Tk won't GC it.
                self._burst_icons_small[disc] = full.subsample(4, 4)
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
        self._populate_role_section(self._links_content, self._links_count,
                                    self._links_categories,
                                    threshold=self._links_threshold,
                                    rows_by_name=rows_by_name)
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
        self._xup_threshold_label.config(text=f"/ {threshold}")

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
                ttk.Button(self._preset_row2, text=label, style="Dark.TButton",
                           command=lambda l=letter, t=title, c=cap: self._add_role_preset(l, t, c)
                           ).pack(side=tk.LEFT, padx=2)
            ttk.Button(self._preset_row2, text="Clear Custom", style="Dark.TButton",
                       command=self._clear_custom_presets
                       ).pack(side=tk.RIGHT, padx=2)

    def _save_role_as_preset(self, slot):
        """Save the current role slot's configuration as a custom preset."""
        letter = slot["letter_var"].get().strip()
        title = slot["title_var"].get().strip()
        if not letter or not title:
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
            return  # At limit
        for p in custom:
            if p.get("label") == label:
                return  # Already exists
        for dlabel, _, _, _ in self._default_presets:
            if dlabel == label:
                return  # Matches a default

        custom.append({"label": label, "letter": letter, "title": title, "cap": cap})
        self.config["custom_role_presets"] = custom
        self._save_config()
        self._rebuild_preset_buttons()

    def _clear_custom_presets(self):
        """Remove all custom presets."""
        self.config["custom_role_presets"] = []
        self._save_config()
        self._rebuild_preset_buttons()

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

        note_var = tk.StringVar()
        note_entry = tk.Entry(row, textvariable=note_var,
                               font=("Consolas", 9), width=30,
                               bg=BG_ENTRY, fg=FG_ORANGE,
                               insertbackground=FG_WHITE,
                               borderwidth=1, relief=tk.RIDGE)
        note_entry.pack(side=tk.LEFT, padx=(4, 0))

        # Limit note to 30 characters
        def limit_note(*_):
            val = note_var.get()
            if len(val) > 30:
                note_var.set(val[:30])
        note_var.trace_add("write", limit_note)

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
                if info and self.esi_auth.is_boss(info, self.esi_auth.character_id):
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
                else:
                    # No fleet data this poll — could be genuine (not in fleet)
                    # OR a transient ESI error. Only clear state after
                    # NO_FLEET_GRACE consecutive misses to avoid flicker.
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
            self._refresh_character_tab(force=False)
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
        import webbrowser
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
        """Show a tooltip near the mouse cursor."""
        self._hide_tooltip()  # destroy any existing tooltip first (idempotent)
        self._tooltip = tk.Toplevel(self.root)
        self._tooltip.wm_overrideredirect(True)
        self._tooltip.wm_geometry(f"+{event.x_root + 10}+{event.y_root + 10}")
        label = tk.Label(self._tooltip, text=text,
                         font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
                         borderwidth=1, relief=tk.SOLID, padx=4, pady=2)
        label.pack()

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
            import re
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
        # Load standings whitelist in background (for hostile detection)
        if self.esi_auth and self.esi_auth.is_authenticated:
            threading.Thread(
                target=load_standings_whitelist, args=(self.esi_auth,), daemon=True
            ).start()

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
                pass
            time.sleep(self.config.get("poll_interval_seconds", 1.0))

    def _on_intel_channel_change(self):
        """Called when a channel checkbox is toggled."""
        self._intel_channels_enabled.clear()
        for name, var in self._intel_channel_vars.items():
            if var.get():
                self._intel_channels_enabled.add(name)

    def _on_intel_message(self, msg: ChatMessage):
        """Callback for intel channel messages."""
        if msg.channel not in self._intel_channels_enabled:
            return

        report = parse_intel_message(msg, search_system)
        if report is None:
            return

        # Skip clear reports entirely
        if report.report_type == "clear":
            return

        # Skip pure info with no system
        if report.report_type == "info" and not report.system_name:
            return

        # Coalesce rapid-fire posts from same reporter
        report, is_new = coalesce_report(report)
        if not is_new:
            # Merged into existing — no new card needed
            return

        # Resolve region name for the system
        if report.system_id and not report.region_name:
            try:
                sys_info = get_system_info(report.system_id)
                if sys_info:
                    if self.esi_auth and self.esi_auth.is_authenticated:
                        report.region_name = self.esi_auth._get_region_name(sys_info)
                    else:
                        # Fallback: resolve via public ESI
                        import requests as _rq
                        cid = sys_info.get("constellation_id")
                        if cid:
                            cr = _rq.get(f"https://esi.evetech.net/latest/universe/constellations/{cid}/", timeout=5)
                            if cr.ok:
                                rid = cr.json().get("region_id")
                                if rid:
                                    rr = _rq.get(f"https://esi.evetech.net/latest/universe/regions/{rid}/", timeout=5)
                                    if rr.ok:
                                        report.region_name = rr.json().get("name", "")
            except Exception:
                pass

        # Calculate route from staging
        staging = self._get_staging_system()
        if staging and report.system_id:
            try:
                from jump_range import get_stargate_route as _gr
                o = search_system(staging)
                if o:
                    conns = self._get_ansiblex_connections()
                    r = _gr(o, report.system_id, connections=conns)
                    if r:
                        report.route_from_staging = (
                            f"{staging} -> {report.system_name}: **{len(r)-1} jumps**"
                        )
            except Exception:
                pass

        # Check for Pharolux cyno beacon via ESI (non-blocking, no failure if ESI unavailable)
        if report.system_id:
            try:
                report.has_cyno_beacon = self._check_cyno_beacon(report.system_id)
            except Exception:
                pass

        # Resolve character names in message (public ESI, no auth needed)
        try:
            chars = resolve_characters(report.raw_message, report.system_name, search_system)
            if chars:
                report.characters = chars
        except Exception:
            pass

        # Fetch dscan data if URL present (populate summary before display)
        if report.dscan_url:
            try:
                import requests as _req
                resp = _req.get(report.dscan_url, timeout=10)
                if resp.ok:
                    from intel_monitor import make_dscan_summary
                    result = parse_dscan_text(resp.text)
                    if result["total"] > 0:
                        report.dscan_ships = result["ships"]
                        report.dscan_total = result["total"]
                        report.dscan_summary = make_dscan_summary(result["ships"], result["total"])
            except Exception:
                pass

        self.root.after(0, self._show_intel_report, report)

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

    def _show_intel_report(self, report: IntelReport):
        """Display an intel report in the intel log pane (newest at top)."""
        ts = report.timestamp.strftime("%H:%M:%S") if report.timestamp else "??:??:??"
        log = self._intel_log

        # Track for fusion detection
        if report.system_name:
            self._recent_intel_systems[report.system_name] = datetime.now()

        # Check for fusion with recent zkill alerts
        is_fused = False
        if report.system_name and report.system_name in self._recent_zkill_systems:
            delta = datetime.now() - self._recent_zkill_systems[report.system_name]
            if delta.total_seconds() <= 600:  # 10 minutes
                is_fused = True

        if report.report_type == "clear":
            header_tag = "intel_clear"
            header = f"[{ts}] INTEL — CLEAR"
        else:
            header_tag = "intel"
            header = f"[{ts}] INTEL"
            if is_fused:
                header = f"[{ts}] [FUSED] INTEL"
                header_tag = "fused"
            if report.has_camp:
                header += " [CAMP]"
            if report.has_spike:
                header += " [SPIKE]"

        self._begin_alert_block(log)
        self._append_zkill_log(header + "\n", header_tag)

        # Determine highlight threshold
        try:
            min_rep = int(self._intel_min_reported_var.get())
        except ValueError:
            min_rep = 0
        report_count = report.pilot_count or report.dscan_total or 0
        is_above_threshold = min_rep > 0 and report_count >= min_rep

        if report.system_name:
            region_str = f" ({report.region_name})" if report.region_name else ""
            # System name as clickable button (sets destination + copies)
            self._append_zkill_log("  System:  ", "info")
            sys_btn = tk.Button(
                log, text=f"{report.system_name}{region_str}",
                font=("Consolas", 10, "bold"), fg=FG_ACCENT, bg=BG_ENTRY,
                activebackground="#1a5a90", activeforeground=FG_WHITE,
                borderwidth=1, relief=tk.RIDGE, cursor="hand2",
                command=lambda s=report.system_name: self._set_destination_or_copy(s),
            )
            log.window_create("alert_ins", window=sys_btn)
            self._append_zkill_log("\n")

        if report.pilot_count:
            count_tag = "fight" if is_above_threshold else "value"
            prefix = "  *** " if is_above_threshold else "  "
            self._append_zkill_log(
                f"{prefix}Reported: {report.pilot_count}+ hostiles\n", count_tag
            )

        if report.dscan_summary:
            scan_tag = "fight" if is_above_threshold else "info"
            self._append_zkill_log(
                f"  Scan: {report.dscan_summary}\n", scan_tag
            )

        if report.has_cyno_beacon:
            self._append_zkill_log(
                "  ** PHAROLUX CYNO BEACON IN SYSTEM **\n", "fused"
            )

        if report.route_from_staging:
            self._append_zkill_log(
                f"  Route:   {report.route_from_staging}\n", "info"
            )

        self._append_zkill_log(
            f"  Channel: {report.channel}  |  Reporter: {report.reporter}\n",
            "intel_meta",
        )

        # Render message with character names highlighted in red
        self._append_zkill_log("  Message: ", "dim")
        if report.characters:
            char_names = {c["name"] for c in report.characters}
            char_lookup = {c["name"]: c for c in report.characters}
            msg_text = report.raw_message
            # Split message around character names and render with tags
            remaining = msg_text
            for cname in sorted(char_names, key=len, reverse=True):
                if cname in remaining:
                    parts = remaining.split(cname, 1)
                    if parts[0]:
                        self._append_zkill_log(parts[0], "dim")
                    # Create a unique tag for tooltip
                    ci = char_lookup[cname]
                    tooltip = ci.get("alliance") or ci.get("corporation") or ""
                    hostile = ci.get("hostile", True)
                    tag_id = f"char_{ci['character_id']}"
                    char_color = FG_RED if hostile else FG_GREEN
                    log.tag_config(tag_id, foreground=char_color,
                                   font=("Consolas", 10, "bold"))
                    self._append_zkill_log(cname, tag_id)
                    # Bind tooltip on hover
                    if tooltip:
                        log.tag_bind(tag_id, "<Enter>",
                            lambda e, t=f"[{tooltip}]": self._show_tooltip(e, t))
                        log.tag_bind(tag_id, "<Leave>",
                            lambda e: self._hide_tooltip())
                    remaining = parts[1] if len(parts) > 1 else ""
            if remaining:
                self._append_zkill_log(remaining, "dim")
            self._append_zkill_log("\n")
        else:
            self._append_zkill_log(f"{report.raw_message}\n", "dim")

        # Insert action buttons
        log.insert("alert_ins", "  ")

        # Dotlan link
        if report.system_name:
            dotlan_url = f"https://evemaps.dotlan.net/system/{report.system_name.replace(' ', '_')}"
            dotlan_btn = tk.Button(
                log, text="Dotlan", font=("Consolas", 8, "bold"),
                fg=FG_ORANGE, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda u=dotlan_url: self._open_url(u),
            )
            log.window_create("alert_ins", window=dotlan_btn)
            log.insert("alert_ins", "  ")

        # D-Scan link
        if report.dscan_url:
            dscan_btn = tk.Button(
                log, text="D-Scan", font=("Consolas", 8, "bold"),
                fg=FG_ACCENT, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda u=report.dscan_url: self._open_url(u),
            )
            log.window_create("alert_ins", window=dscan_btn)
            log.insert("alert_ins", "  ")

        # Navigate button
        staging = self._get_staging_system()
        if staging and report.system_name:
            nav_btn = tk.Button(
                log, text="Navigate", font=("Consolas", 8, "bold"),
                fg=FG_YELLOW, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda s=report.system_name: self._navigate_wh_route(s),
            )
            log.window_create("alert_ins", window=nav_btn)
            log.insert("alert_ins", "  ")

            # Titan Bridge button — highlight if cyno beacon present
            bridge_label = "Titan Bridge? [CYNO]" if report.has_cyno_beacon else "Titan Bridge?"
            bridge_color = FG_YELLOW if report.has_cyno_beacon else FG_GREEN
            bridge_btn = tk.Button(
                log, text=bridge_label, font=("Consolas", 8, "bold"),
                fg=bridge_color, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda s=report.system_name, cyno=report.has_cyno_beacon: self._navigate_jump_range(s, has_cyno=cyno),
            )
            log.window_create("alert_ins", window=bridge_btn)

        log.insert("alert_ins", "\n\n")
        self._end_alert_block(log)

        self._notify_zkill_tab()

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
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    app = FCToolGUI()
    app.run()


if __name__ == "__main__":
    main()
