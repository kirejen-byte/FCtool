"""
FCTool GUI - Fleet Commander Assistant Frontend
Tkinter-based GUI that wraps all FCTool modules.
"""

import json
import os
import sys
import threading
import time
import tkinter as tk
import webbrowser
import requests
from tkinter import ttk, scrolledtext, messagebox, filedialog
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

from chat_monitor import ChatMonitor, ChatMessage
from xup_counter import XUpCounter, XUpState
from zkill_monitor import ZKillMonitor, KillAlert
from discord_notify import DiscordNotifier
from jump_range import JumpRangeChecker, search_system, get_stargate_route, get_system_info
from wh_route import find_wh_route, fetch_connections, WHRoute
from autocomplete import AutocompleteEntry
from system_cache import get_sorted_names, get_system_names, get_region_map
from esi_auth import ESIAuth
from app_path import app_dir


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
        self.discord: DiscordNotifier | None = None
        self.chat_monitor: ChatMonitor | None = None
        self.xup_counter: XUpCounter | None = None
        self.zkill_monitor: ZKillMonitor | None = None
        self.jump_checker: JumpRangeChecker | None = None
        self._running = False
        self._chat_thread: threading.Thread | None = None
        self._is_discord_primary = self.config.get("discord", {}).get("role", "primary") == "primary"
        self._sound_enabled = self.config.get("sound_on_ready", False)
        self._ansiblex_connections: list[str] = []  # "id1|id2" strings for ESI route
        # Maps (id1, id2) -> (name1, name2) for identifying Ansiblex jumps in routes
        self._ansiblex_id_pairs: dict[tuple[int, int], tuple[str, str]] = {}

        # ESI SSO auth
        esi_cfg = self.config.get("esi", {})
        if esi_cfg.get("client_id"):
            self.esi_auth = ESIAuth(
                client_id=esi_cfg["client_id"],
                client_secret=esi_cfg.get("client_secret", ""),
                callback_url=esi_cfg.get("callback_url", "http://localhost:8834/callback"),
            )
        else:
            self.esi_auth = None

        # Discover ansiblex from ESI if authenticated, else fall back to config
        self._refresh_ansiblex_from_esi()
        self._prewarm_cache_async()

        # Start fleet location refresh loop (updates role tracker locations)
        self.root.after(5000, self._refresh_fleet_locations)

        # Load system names for autocomplete (runs in background if cache miss)
        self._system_names: list[str] = []
        self._system_labels: dict[str, str] = {}
        self._load_system_names_async()

        self._build_ui()
        self._setup_modules()
        self._start_monitoring()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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

        for attr in ['_range_origin', '_range_dest', '_wh_origin', '_wh_dest', '_staging_entry']:
            widget = getattr(self, attr, None)
            if widget and hasattr(widget, 'update_completions'):
                widget.update_completions(all_names, labels)

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
        """Pre-resolve commonly used systems in background so first check is fast."""
        def prewarm():
            from jump_range import get_system_info, save_route_cache
            systems = [
                "C-N4OD", "6RCQ-V", "F7C-H0", "CL6-ZG", "HPS5-C",
                "Korasen", "Y-2ANO", "NOL-M9",
            ]
            # Add staging system if configured
            staging = self.config.get("zkillboard", {}).get("staging_system", "")
            if staging and staging not in systems:
                systems.insert(0, staging)
            for name in systems:
                sid = search_system(name)
                if sid:
                    get_system_info(sid)
            save_route_cache()
            print(f"[Cache] Pre-warmed {len(systems)} system(s)")

        threading.Thread(target=prewarm, daemon=True).start()

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        return {}

    def _save_config(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.config, f, indent=4)

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

        # Status indicators on right
        self._status_frame = tk.Frame(title_frame, bg=BG_DARK)
        self._status_frame.pack(side=tk.RIGHT, padx=15)
        self._chat_status = tk.Label(self._status_frame, text="CHAT: --",
                                      font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK)
        self._chat_status.pack(side=tk.LEFT, padx=8)
        self._zkill_status = tk.Label(self._status_frame, text="ZKILL: --",
                                       font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK)
        self._zkill_status.pack(side=tk.LEFT, padx=8)
        self._discord_status = tk.Label(self._status_frame, text="DISCORD: OFF",
                                         font=("Consolas", 9), fg=FG_DIM, bg=BG_DARK)
        self._discord_status.pack(side=tk.LEFT, padx=8)

        # ── Notebook (Tabs) ──────────────────────────────────────────────────
        self.notebook = ttk.Notebook(self.root, style="Dark.TNotebook")
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self._build_xup_tab()
        self._build_zkill_tab()
        self._build_range_tab()
        self._build_wh_route_tab()
        self._build_settings_tab()

        # Track zkill alert notifications
        self._zkill_has_unread = False
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ── X-Up Tab ──────────────────────────────────────────────────────────────

    def _build_xup_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Fleet Management  ")

        # Top section: compact counter + controls in one row
        counter_frame = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                  highlightbackground=BORDER_COLOR, highlightthickness=1)
        counter_frame.pack(fill=tk.X, padx=10, pady=(8, 4))

        # Counter and status side by side
        counter_left = tk.Frame(counter_frame, bg=BG_PANEL)
        counter_left.pack(side=tk.LEFT, padx=15, pady=8)

        self._xup_count_label = tk.Label(counter_left, text="0",
                                          font=("Consolas", 48, "bold"),
                                          fg=FG_ACCENT, bg=BG_PANEL)
        self._xup_count_label.pack(side=tk.LEFT)

        threshold = self.config.get("xup", {}).get("threshold", 50)
        self._xup_threshold_label = tk.Label(
            counter_left,
            text=f"/ {threshold}",
            font=("Consolas", 20), fg=FG_DIM, bg=BG_PANEL
        )
        self._xup_threshold_label.pack(side=tk.LEFT, padx=(5, 0))

        # Progress bar and status in center
        counter_center = tk.Frame(counter_frame, bg=BG_PANEL)
        counter_center.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, pady=8)

        self._xup_canvas = tk.Canvas(counter_center, height=22,
                                      bg=BG_DARK, highlightthickness=0)
        self._xup_canvas.pack(fill=tk.X)

        self._xup_status = tk.Label(counter_center, text="Waiting for fleet chat...",
                                     font=("Consolas", 11, "bold"),
                                     fg=FG_DIM, bg=BG_PANEL)
        self._xup_status.pack(pady=(4, 0))

        # Controls on the right
        counter_right = tk.Frame(counter_frame, bg=BG_PANEL)
        counter_right.pack(side=tk.RIGHT, padx=10, pady=8)

        ctrl_row = tk.Frame(counter_right, bg=BG_PANEL)
        ctrl_row.pack()
        tk.Label(ctrl_row, text="Threshold:", font=("Consolas", 9),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT, padx=(0, 3))
        self._threshold_var = tk.StringVar(value=str(threshold))
        self._threshold_spin = tk.Spinbox(
            ctrl_row, from_=1, to=500, textvariable=self._threshold_var,
            font=("Consolas", 11), width=4, bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, buttonbackground=BG_PANEL,
            borderwidth=1, relief=tk.RIDGE, command=self._on_threshold_change
        )
        self._threshold_spin.pack(side=tk.LEFT)
        self._threshold_spin.bind("<Return>", lambda e: self._on_threshold_change())
        self._threshold_spin.bind("<FocusOut>", lambda e: self._on_threshold_change())

        ttk.Button(counter_right, text="Reset Counter", style="Red.TButton",
                   command=self._reset_xup).pack(pady=(4, 0))

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

        # Preset role buttons
        preset_frame = tk.Frame(tab, bg=BG_DARK)
        preset_frame.pack(fill=tk.X, padx=10, pady=(0, 2))
        tk.Label(preset_frame, text="Presets:", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_DARK).pack(side=tk.LEFT, padx=(0, 4))
        presets = [
            ("C-Cyno", "c", "Cyno", None),
            ("D-Dictors", "d", "Dictors", None),
            ("F-Fax 3", "f", "FAX", 3),
            ("1-Dreads-10", "1", "Dreads", 10),
        ]
        for label, letter, title, cap in presets:
            ttk.Button(preset_frame, text=label, style="Dark.TButton",
                       command=lambda l=letter, t=title, c=cap: self._add_role_preset(l, t, c)
                       ).pack(side=tk.LEFT, padx=2)
        ttk.Button(role_header, text="Screenshot", style="Dark.TButton",
                   command=self._take_screenshot).pack(side=tk.RIGHT, padx=3)
        self._screenshot_link = tk.Label(role_header, text="", font=("Consolas", 9),
                                          fg=FG_ACCENT, bg=BG_DARK, cursor="hand2")
        self._screenshot_link.pack(side=tk.RIGHT, padx=5)
        self._screenshot_link.bind("<Button-1>", self._open_screenshot_link)

        # Role tracker container (scrollable)
        self._role_container = tk.Frame(tab, bg=BG_DARK)
        self._role_container.pack(fill=tk.X, padx=10, pady=2)
        self._role_slots: list[dict] = []

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

        # Bind mousewheel to scroll
        def _on_spec_mousewheel(event):
            spec_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        spec_canvas.bind("<MouseWheel>", _on_spec_mousewheel)
        self._spec_roles_frame.bind("<MouseWheel>", _on_spec_mousewheel)

        # Create the three collapsible sections
        self._links_container, self._links_content, self._links_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Links / Command Ships")
        self._defenders_container, self._defenders_content, self._defenders_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Defenders")
        self._logi_container, self._logi_content, self._logi_count = \
            self._create_collapsible_section(self._spec_roles_frame, "Logistics")

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
        self._xup_log.tag_config("xup", foreground=FG_GREEN)
        self._xup_log.tag_config("fire", foreground=FG_RED, font=("Consolas", 10, "bold"))
        self._xup_log.tag_config("ready", foreground=FG_YELLOW, font=("Consolas", 10, "bold"))
        self._xup_log.tag_config("dim", foreground=FG_DIM)
        self._xup_log.tag_config("role", foreground=FG_MAGENTA)

    # ── zKillboard Tab ────────────────────────────────────────────────────────

    def _build_zkill_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  zKillboard Intel  ")

        header = tk.Frame(tab, bg=BG_DARK)
        header.pack(fill=tk.X, padx=10, pady=(10, 2))
        tk.Label(header, text="Live Engagement Feed",
                 font=("Consolas", 13, "bold"), fg=FG_ACCENT, bg=BG_DARK
                 ).pack(side=tk.LEFT)

        self._zkill_indicator = tk.Label(header, text="  LIVE",
                                          font=("Consolas", 10, "bold"),
                                          fg=FG_GREEN, bg=BG_DARK)
        self._zkill_indicator.pack(side=tk.LEFT, padx=10)

        # All K-Space toggle
        self._zkill_watch_all_var = tk.BooleanVar(value=False)
        tk.Checkbutton(header, text="All K-Space",
                       variable=self._zkill_watch_all_var,
                       font=("Consolas", 9), fg=FG_ORANGE, bg=BG_DARK,
                       selectcolor=BG_ENTRY, activebackground=BG_DARK,
                       activeforeground=FG_ORANGE,
                       command=self._toggle_watch_all).pack(side=tk.RIGHT, padx=10)
        tk.Label(header, text="(any fight, any alliance)",
                 font=("Consolas", 8), fg=FG_DIM, bg=BG_DARK
                 ).pack(side=tk.RIGHT)

        # ── Inline filter controls ─────────────────────────────────────────
        filter_frame = tk.Frame(tab, bg=BG_PANEL, bd=1, relief=tk.RIDGE,
                                 highlightbackground=BORDER_COLOR, highlightthickness=1)
        filter_frame.pack(fill=tk.X, padx=10, pady=(2, 5))

        filter_row = tk.Frame(filter_frame, bg=BG_PANEL)
        filter_row.pack(fill=tk.X, padx=10, pady=5)

        tk.Label(filter_row, text="Min Pilots:", font=("Consolas", 9),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        zk_cfg = self.config.get("zkillboard", {})
        self._zkill_min_pilots_var = tk.StringVar(
            value=str(zk_cfg.get("min_pilots_involved", 25)))
        self._zkill_min_pilots_spin = tk.Spinbox(
            filter_row, from_=1, to=500, textvariable=self._zkill_min_pilots_var,
            font=("Consolas", 10), width=4, bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, buttonbackground=BG_PANEL,
            borderwidth=1, relief=tk.RIDGE,
            command=self._on_zkill_filter_change,
        )
        self._zkill_min_pilots_spin.pack(side=tk.LEFT, padx=(4, 15))
        self._zkill_min_pilots_spin.bind("<Return>", lambda e: self._on_zkill_filter_change())

        tk.Label(filter_row, text="Max Jumps from Staging:", font=("Consolas", 9),
                 fg=FG_TEXT, bg=BG_PANEL).pack(side=tk.LEFT)
        self._zkill_max_jumps_var = tk.StringVar(value="0")
        self._zkill_max_jumps_spin = tk.Spinbox(
            filter_row, from_=0, to=200, textvariable=self._zkill_max_jumps_var,
            font=("Consolas", 10), width=4, bg=BG_ENTRY, fg=FG_WHITE,
            insertbackground=FG_WHITE, buttonbackground=BG_PANEL,
            borderwidth=1, relief=tk.RIDGE,
        )
        self._zkill_max_jumps_spin.pack(side=tk.LEFT, padx=(4, 5))
        tk.Label(filter_row, text="(0 = no limit)",
                 font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT)

        self._zkill_log = scrolledtext.ScrolledText(
            tab, height=30, font=("Consolas", 10),
            bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
            selectbackground="#1a5a90", wrap=tk.WORD, state=tk.DISABLED,
            borderwidth=1, relief=tk.RIDGE
        )
        self._zkill_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self._zkill_log.tag_config("fight", foreground=FG_RED,
                                    font=("Consolas", 11, "bold"))
        self._zkill_log.tag_config("info", foreground=FG_ACCENT)
        self._zkill_log.tag_config("value", foreground=FG_ORANGE)
        self._zkill_log.tag_config("dim", foreground=FG_DIM)

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
        self._range_origin.pack(side=tk.LEFT, padx=(10, 20))

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

        # Secondary Titan range table (shown when out of range)
        self._range_secondary_frame = tk.Frame(tab, bg=BG_DARK)
        self._range_secondary_frame.pack(fill=tk.X, padx=10, pady=(0, 5))


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
        self._wh_origin.pack(side=tk.LEFT, padx=(10, 20))

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
            lbl = tk.Label(self._fleet_comp_frame,
                           text=f"  {ship_name}: {count}",
                           font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W)
            lbl.pack(anchor=tk.W)
            self._fleet_comp_labels.append(lbl)

        if not new_data:
            tk.Label(self._fleet_comp_frame, text="  No fleet data",
                     font=("Consolas", 9), fg=FG_DIM, bg=BG_PANEL, anchor=tk.W
                     ).pack(anchor=tk.W)

    def _update_specialized_roles(self, members: list[dict], ship_counts: dict[int, int], total: int):
        """Update the three collapsible specialized role sections."""
        from ship_classes import (
            ALL_LINKS_COMMAND, ALL_LOGISTICS, TACTICAL_DESTROYERS,
            INTERDICTORS, is_defender
        )
        from zkill_monitor import resolve_name

        # Check >50% command ship rule
        command_count = sum(ship_counts.get(tid, 0) for tid in ALL_LINKS_COMMAND)
        skip_links = total > 0 and (command_count / total) > 0.5

        # Categorize members
        links_members: dict[int, list[tuple[str, str]]] = {}  # type_id -> [(char_name, char_id_str)]
        defenders_members: dict[int, list[tuple[str, str]]] = {}
        logi_members: dict[int, list[tuple[str, str]]] = {}

        for m in members:
            stid = m.get("ship_type_id", 0)
            char_id = m.get("character_id", 0)
            if not stid or not char_id:
                continue

            char_name = resolve_name(char_id, "character")
            entry = (char_name, str(char_id))

            if not skip_links and stid in ALL_LINKS_COMMAND:
                links_members.setdefault(stid, []).append(entry)
            elif stid in ALL_LOGISTICS:
                logi_members.setdefault(stid, []).append(entry)
            elif stid in TACTICAL_DESTROYERS or is_defender(stid):
                defenders_members.setdefault(stid, []).append(entry)

        # Update each section
        self._populate_role_section(self._links_content, self._links_count, links_members)
        self._populate_role_section(self._defenders_content, self._defenders_count, defenders_members)
        self._populate_role_section(self._logi_content, self._logi_count, logi_members)

    def _populate_role_section(self, content_frame, count_label, ship_members):
        """Populate a collapsible section with ship type counts and pilot details."""
        from zkill_monitor import resolve_name

        for widget in content_frame.winfo_children():
            widget.destroy()

        total = sum(len(pilots) for pilots in ship_members.values())
        count_label.config(text=f"({total})")

        if not ship_members:
            tk.Label(content_frame, text="  None detected",
                     font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL, anchor=tk.W
                     ).pack(anchor=tk.W)
            return

        # Sort by count descending
        for type_id, pilots in sorted(ship_members.items(),
                                       key=lambda x: len(x[1]), reverse=True):
            ship_name = resolve_name(type_id, "type")
            tk.Label(content_frame,
                     text=f"{ship_name} - {len(pilots)}",
                     font=("Consolas", 9, "bold"), fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W
                     ).pack(anchor=tk.W, pady=(2, 0))

            for char_name, _char_id in pilots:
                loc = self._fleet_locations_cache.get(char_name)
                if loc:
                    sys_name, region_name, _ship = loc
                    loc_text = f"({sys_name} - {region_name})" if region_name else f"({sys_name})"
                else:
                    loc_text = ""
                tk.Label(content_frame,
                         text=f"    {char_name} {loc_text}",
                         font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL, anchor=tk.W
                         ).pack(anchor=tk.W)

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

    def _toggle_watch_all(self):
        """Toggle All K-Space display filter (GUI-only, does not affect Discord)."""
        watch_all = self._zkill_watch_all_var.get()
        mode = "ALL K-SPACE" if watch_all else "FILTERED"
        self._append_zkill_log(f"\n[Mode] Display switched to {mode}\n", "info")
        if watch_all:
            self._zkill_indicator.config(text="  LIVE (ALL)", fg=FG_ORANGE)
        else:
            self._zkill_indicator.config(text="  LIVE", fg=FG_GREEN)

    def _on_tab_changed(self, event=None):
        """Clear zkill alert indicator when switching to the zkill tab."""
        current = self.notebook.index(self.notebook.select())
        if current == 1 and self._zkill_has_unread:
            # Switched to zKill tab — clear notification
            self._zkill_has_unread = False
            self.notebook.tab(1, text="  zKillboard Intel  ")
            self._zkill_status.config(bg=BG_DARK)

    def _notify_zkill_tab(self):
        """Flash the zKill tab to indicate a new alert if not currently viewing it."""
        current = self.notebook.index(self.notebook.select())
        if current != 1:
            self._zkill_has_unread = True
            self.notebook.tab(1, text="  ** zKill ALERT **  ")
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
        if not self.esi_auth:
            self._esi_status_label.config(text="ESI not configured", fg=FG_DIM)
            return
        if self.esi_auth.is_authenticated:
            name = self.esi_auth.character_name or "Unknown"
            self._esi_status_label.config(
                text=f"Logged in: {name}", fg=FG_GREEN
            )
        else:
            self._esi_status_label.config(text="Not logged in", fg=FG_DIM)

    def _esi_login(self):
        """Start EVE SSO login flow."""
        if not self.esi_auth:
            return
        self._esi_status_label.config(text="Opening browser...", fg=FG_YELLOW)
        self._esi_login_btn.config(state=tk.DISABLED)

        def on_complete(success, info):
            self.root.after(0, self._esi_login_complete, success, info)

        self.esi_auth.login(on_complete=on_complete)

    def _esi_login_complete(self, success: bool, info: str):
        """Handle ESI login completion (runs on main thread)."""
        self._esi_login_btn.config(state=tk.NORMAL)
        if success:
            self._esi_status_label.config(
                text=f"Logged in: {info}", fg=FG_GREEN
            )
            # Update tracked character if empty
            if not self._char_var.get() and self.esi_auth.character_name:
                self._char_var.set(self.esi_auth.character_name)
        else:
            self._esi_status_label.config(
                text=f"Login failed: {info}", fg=FG_RED
            )

    def _esi_logout(self):
        """Log out of EVE SSO."""
        if self.esi_auth:
            self.esi_auth.logout()
            self._update_esi_status()

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

    def _on_zkill_filter_change(self):
        """Update min pilots display filter (GUI-only, does not affect Discord)."""
        pass  # Value is read from _zkill_min_pilots_var at display time

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

    def _navigate_jump_range(self, destination: str):
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

    # ── Settings Tab ──────────────────────────────────────────────────────────

    def _build_settings_tab(self):
        tab = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab, text="  Settings  ")

        canvas = tk.Canvas(tab, bg=BG_DARK, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=BG_DARK)

        scroll_frame.bind("<Configure>",
                          lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

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
        self._char_combo.pack(side=tk.LEFT, padx=5)
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

        # ── ESI SSO Auth ────────────────────────────────────────────────
        self._add_section(scroll_frame, "EVE SSO Authentication")
        esi_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        esi_frame.pack(fill=tk.X, padx=20, pady=2)

        self._esi_status_label = tk.Label(
            esi_frame, text="Not logged in", font=("Consolas", 10),
            fg=FG_DIM, bg=BG_DARK, width=35, anchor=tk.W,
        )
        self._esi_status_label.pack(side=tk.LEFT)

        self._esi_login_btn = ttk.Button(
            esi_frame, text="Login with EVE", style="Dark.TButton",
            command=self._esi_login,
        )
        self._esi_login_btn.pack(side=tk.LEFT, padx=5)

        self._esi_logout_btn = ttk.Button(
            esi_frame, text="Logout", style="Dark.TButton",
            command=self._esi_logout,
        )
        self._esi_logout_btn.pack(side=tk.LEFT, padx=5)

        self._esi_discover_btn = ttk.Button(
            esi_frame, text="Discover Ansiblex Gates", style="Dark.TButton",
            command=self._esi_discover_ansiblex,
        )
        self._esi_discover_btn.pack(side=tk.LEFT, padx=5)

        # Update ESI status display
        self._update_esi_status()

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

        # ── X-Up Settings ────────────────────────────────────────────────────
        self._add_section(scroll_frame, "Fleet Management")
        xup = self.config.get("xup", {})

        self._setting_entries = {}
        self._add_setting(scroll_frame, "Trigger Word", "xup_trigger",
                          xup.get("trigger_word", "x"))
        self._add_setting(scroll_frame, "Fire Word", "xup_fire",
                          xup.get("fire_word", "FIRE"))
        self._add_setting(scroll_frame, "Channel Name", "xup_channel",
                          xup.get("channel_name", "Fleet"))

        # ── Discord Settings ─────────────────────────────────────────────────
        self._add_section(scroll_frame, "Discord")
        dc = self.config.get("discord", {})

        self._discord_enabled_var = tk.BooleanVar(value=dc.get("enabled", False))
        cb_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        cb_frame.pack(fill=tk.X, padx=20, pady=2)
        tk.Checkbutton(cb_frame, text="Enable Discord Notifications",
                       variable=self._discord_enabled_var,
                       font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                       selectcolor=BG_ENTRY, activebackground=BG_DARK,
                       activeforeground=FG_TEXT).pack(anchor=tk.W)

        self._add_setting(scroll_frame, "Webhook URL", "discord_webhook",
                          dc.get("webhook_url", ""))
        self._add_setting(scroll_frame, "Role (primary/listener)", "discord_role",
                          dc.get("role", "primary"))

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

        self._add_setting(scroll_frame, "Min Pilots Involved", "zkill_min_pilots",
                          str(zk.get("min_pilots_involved", 10)))
        self._add_setting(scroll_frame, "Watch Alliance IDs (comma-sep)",
                          "zkill_alliances",
                          ",".join(str(x) for x in zk.get("watch_alliances", [])))
        self._add_setting(scroll_frame, "Watch Region IDs (comma-sep)",
                          "zkill_regions",
                          ",".join(str(x) for x in zk.get("watch_regions", [])))

        # ── Save Button ──────────────────────────────────────────────────────
        save_frame = tk.Frame(scroll_frame, bg=BG_DARK)
        save_frame.pack(fill=tk.X, padx=20, pady=20)
        ttk.Button(save_frame, text="Save Settings & Restart",
                   style="Green.TButton",
                   command=self._save_settings).pack(side=tk.LEFT)
        self._save_status = tk.Label(save_frame, text="",
                                      font=("Consolas", 10), fg=FG_GREEN, bg=BG_DARK)
        self._save_status.pack(side=tk.LEFT, padx=15)

    def _add_section(self, parent, title):
        tk.Label(parent, text=f"── {title} ──",
                 font=("Consolas", 12, "bold"), fg=FG_ACCENT, bg=BG_DARK
                 ).pack(anchor=tk.W, padx=10, pady=(15, 5))

    def _add_setting(self, parent, label, key, default=""):
        frame = tk.Frame(parent, bg=BG_DARK)
        frame.pack(fill=tk.X, padx=20, pady=2)
        tk.Label(frame, text=f"{label}:", font=("Consolas", 10),
                 fg=FG_TEXT, bg=BG_DARK, width=28, anchor=tk.W).pack(side=tk.LEFT)
        var = tk.StringVar(value=default)
        entry = tk.Entry(frame, textvariable=var, font=("Consolas", 10),
                         bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                         width=40, borderwidth=1, relief=tk.RIDGE)
        entry.pack(side=tk.LEFT, padx=5)
        self._setting_entries[key] = var

    def _browse_logs(self):
        path = filedialog.askdirectory(title="Select EVE Chat Logs Folder")
        if path:
            self._logs_path_var.set(path)

    def _scan_characters(self):
        """Scan log files to find all character names that have fleet channels."""
        logs_path = self._logs_path_var.get()
        if not logs_path or not os.path.isdir(logs_path):
            return
        channel = self.config.get("xup", {}).get("channel_name", "Fleet")
        temp_monitor = ChatMonitor(logs_path, channel_filter=channel)
        listeners = temp_monitor.get_available_listeners()
        if listeners:
            self._char_combo["values"] = [""] + listeners
        else:
            self._char_combo["values"] = [""]

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

        self.config.setdefault("xup", {})
        self.config["xup"]["trigger_word"] = self._setting_entries["xup_trigger"].get()
        self.config["xup"]["fire_word"] = self._setting_entries["xup_fire"].get()
        self.config["xup"]["channel_name"] = self._setting_entries["xup_channel"].get()
        # Threshold is controlled from the Fleet Management tab spinbox only
        self.config["xup"]["threshold"] = self.config.get("xup", {}).get("threshold", 50)

        self.config.setdefault("discord", {})
        self.config["discord"]["enabled"] = self._discord_enabled_var.get()
        self.config["discord"]["webhook_url"] = self._setting_entries["discord_webhook"].get()
        self.config["discord"]["role"] = self._setting_entries["discord_role"].get().strip() or "primary"
        self._is_discord_primary = self.config["discord"]["role"] == "primary"

        self.config.setdefault("zkillboard", {})
        self.config["zkillboard"]["enabled"] = self._zkill_enabled_var.get()
        self.config["zkillboard"]["min_pilots_involved"] = int(
            self._setting_entries["zkill_min_pilots"].get() or 10)

        alliances_str = self._setting_entries["zkill_alliances"].get()
        self.config["zkillboard"]["watch_alliances"] = [
            int(x.strip()) for x in alliances_str.split(",") if x.strip().isdigit()
        ]
        regions_str = self._setting_entries["zkill_regions"].get()
        self.config["zkillboard"]["watch_regions"] = [
            int(x.strip()) for x in regions_str.split(",") if x.strip().isdigit()
        ]

        self._save_config()
        self._save_status.config(text="Saved! Restart to apply.", fg=FG_GREEN)

        # Restart modules
        self._stop_monitoring()
        self.config = self._load_config()
        self._setup_modules()
        self._start_monitoring()
        self._save_status.config(text="Saved & Restarted!", fg=FG_GREEN)

    # ── Module Setup ──────────────────────────────────────────────────────────

    def _setup_modules(self):
        # Discord
        dc = self.config.get("discord", {})
        if dc.get("enabled") and dc.get("webhook_url"):
            self.discord = DiscordNotifier(dc["webhook_url"])
            self._discord_status.config(text="DISCORD: ON", fg=FG_GREEN)
        else:
            self.discord = None
            self._discord_status.config(text="DISCORD: OFF", fg=FG_DIM)

        # Fleet Management
        xup_cfg = self.config.get("xup", {})
        self.xup_counter = XUpCounter(
            trigger_word=xup_cfg.get("trigger_word", "x"),
            fire_word=xup_cfg.get("fire_word", "FIRE"),
            threshold=xup_cfg.get("threshold", 30),
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
            # are applied at display time so they don't affect Discord/server.
            self.zkill_monitor = ZKillMonitor(
                watch_regions=zk_cfg.get("watch_regions", []),
                watch_alliances=zk_cfg.get("watch_alliances", []),
                watch_systems=zk_cfg.get("watch_systems", []),
                min_kill_value_millions=zk_cfg.get("min_kill_value_millions", 0),
                min_pilots_involved=1,
                alert_window_seconds=zk_cfg.get("alert_window_seconds", 300),
                on_alert=self._on_zkill_alert,
                watch_all=True,
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
        # Check role tracker letters (must run on main thread for UI updates)
        self.root.after(0, self._check_role_letters, msg)

    def _on_xup_update(self, state: XUpState):
        self.root.after(0, self._update_xup_display, state)

    def _on_xup_ready(self, state: XUpState):
        self.root.after(0, self._flash_ready, state)

    def _on_xup_fire(self, state: XUpState):
        self.root.after(0, self._show_fire, state)

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
        if self.discord and self._is_discord_primary and self.config.get("discord", {}).get("notify_zkill_alerts"):
            self.discord.notify_zkill_alert(
                alert.system_name, alert.region_name,
                alert.kill_count, alert.pilots_on_field,
                alert.total_value_millions, alert.zkill_url,
                alert.capitals_involved,
                alert.dotlan_url,
                route_info,
                alert.capital_breakdown,
                alert.is_update,
                zkill_related_url=alert.zkill_related_url,
                warbeacon_url=alert.warbeacon_url,
                top_alliances=alert.top_alliances,
            )

    # ── UI Update Methods ────────────────────────────────────────────────────

    def _update_xup_display(self, state: XUpState):
        threshold = self.config.get("xup", {}).get("threshold", 30)
        self._xup_count_label.config(text=str(state.count))
        self._xup_threshold_label.config(text=f"/ {threshold}")

        # Update progress bar
        self._xup_canvas.delete("all")
        w = self._xup_canvas.winfo_width()
        if w < 10:
            w = 500
        h = 22
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

        # Log the latest x-up
        if state.xups:
            latest = max(state.xups.items(), key=lambda x: x[1])
            self._append_xup_log(
                f"[{latest[1].strftime('%H:%M:%S')}] {latest[0]} x'd up  "
                f"({state.count}/{threshold})\n", "xup"
            )

    def _flash_ready(self, state: XUpState):
        threshold = self.config.get("xup", {}).get("threshold", 30)
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
        slot_frame.pack(fill=tk.X, pady=2)

        top_row = tk.Frame(slot_frame, bg=BG_PANEL)
        top_row.pack(fill=tk.X, padx=6, pady=(4, 1))

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
                 font=("Consolas", 10), width=18,
                 bg=BG_ENTRY, fg=FG_WHITE,
                 insertbackground=FG_WHITE,
                 borderwidth=1, relief=tk.RIDGE).pack(side=tk.LEFT, padx=(0, 6))

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

        ttk.Button(top_row, text="X", style="Red.TButton",
                   command=remove_slot).pack(side=tk.RIGHT, padx=1)
        ttk.Button(top_row, text="Clear", style="Dark.TButton",
                   command=lambda: self._clear_role_slot(slot)).pack(side=tk.RIGHT, padx=1)

        def copy_people():
            title = slot["title_var"].get() or slot["letter_var"].get().upper()
            names = "\n".join(sorted(slot["people"].keys()))
            if names:
                self.root.clipboard_clear()
                self.root.clipboard_append(f"{title}:\n{names}")
                self.root.update()

        ttk.Button(top_row, text="Copy", style="Dark.TButton",
                   command=copy_people).pack(side=tk.RIGHT, padx=1)

        # Per-person list: each person gets a row with name + note field
        people_frame = tk.Frame(slot_frame, bg=BG_PANEL)
        people_frame.pack(fill=tk.X, padx=8, pady=(0, 3))

        slot = {
            "frame": slot_frame,
            "letter_var": letter_var,
            "title_var": title_var,
            "cap_var": cap_var,
            "cap_enabled_var": cap_enabled_var,
            "people_frame": people_frame,
            "people": {},  # sender -> {"timestamp": dt, "note_var": StringVar, "row": Frame}
            "count_label": count_label,
        }
        self._role_slots.append(slot)

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
            self.root.after(30000, self._refresh_fleet_locations)
            return

        def do_fetch():
            try:
                # Single ESI call for fleet members
                members = self.esi_auth.get_fleet_members()
                if members:
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
                else:
                    self.root.after(0, self._update_fleet_composition, {}, 0)
            except Exception as e:
                print(f"[Fleet] Location/composition fetch error: {e}")
            self.root.after(15000, self._refresh_fleet_locations)

        threading.Thread(target=do_fetch, daemon=True).start()

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

    def _take_screenshot(self):
        """Capture window screenshot and upload to imgur."""
        self._screenshot_link.config(text="Capturing...", fg=FG_DIM)
        self.root.update_idletasks()

        def do_upload():
            try:
                import subprocess
                import tempfile
                import base64

                # Capture the window using the window's geometry
                x = self.root.winfo_rootx()
                y = self.root.winfo_rooty()
                w = self.root.winfo_width()
                h = self.root.winfo_height()

                tmp = os.path.join(tempfile.gettempdir(), "fctool_screenshot.png")

                # Use PowerShell to capture screen region
                ps_script = f'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap({w}, {h})
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen({x}, {y}, 0, 0, $bmp.Size)
$g.Dispose()
$bmp.Save("{tmp.replace(chr(92), '/')}")
$bmp.Dispose()
'''
                subprocess.run(["powershell", "-Command", ps_script],
                               capture_output=True, timeout=10)

                if not os.path.exists(tmp):
                    self.root.after(0, self._screenshot_link.config,
                                   {"text": "Capture failed", "fg": FG_RED})
                    return

                # Read and base64-encode for imgur
                with open(tmp, "rb") as f:
                    img_data = base64.b64encode(f.read()).decode("utf-8")

                # Upload to imgur (anonymous, no API key needed for client_id)
                resp = requests.post(
                    "https://api.imgur.com/3/image",
                    headers={"Authorization": "Client-ID 546c25a59c58ad7"},
                    data={"image": img_data, "type": "base64"},
                    timeout=30,
                )

                os.remove(tmp)

                if resp.ok:
                    link = resp.json().get("data", {}).get("link", "")
                    self._screenshot_url = link
                    self.root.after(0, self._screenshot_link.config,
                                   {"text": link, "fg": FG_ACCENT})
                else:
                    self.root.after(0, self._screenshot_link.config,
                                   {"text": f"Upload failed ({resp.status_code})", "fg": FG_RED})
            except Exception as e:
                self.root.after(0, self._screenshot_link.config,
                               {"text": f"Error: {e}", "fg": FG_RED})

        threading.Thread(target=do_upload, daemon=True).start()

    def _open_screenshot_link(self, event=None):
        """Open the screenshot URL in browser, or clear it."""
        url = getattr(self, '_screenshot_url', '')
        if url:
            import webbrowser
            webbrowser.open(url)

    def _clear_screenshot(self):
        """Clear the screenshot link."""
        self._screenshot_url = ""
        self._screenshot_link.config(text="")

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

    def _show_zkill_alert(self, alert: KillAlert):
        # ── GUI-only filters (do not affect Discord/server) ──
        # Min pilots filter
        try:
            gui_min_pilots = int(self._zkill_min_pilots_var.get())
        except ValueError:
            gui_min_pilots = 1
        if alert.pilots_on_field < gui_min_pilots and not alert.capitals_involved:
            return  # Below GUI min pilot threshold

        # All K-Space vs filtered mode
        if not self._zkill_watch_all_var.get():
            # Filtered mode: only show alerts matching config regions+alliances
            zk_cfg = self.config.get("zkillboard", {})
            watch_regions = set(zk_cfg.get("watch_regions", []))
            watch_alliances = set(zk_cfg.get("watch_alliances", []))
            watch_systems = set(zk_cfg.get("watch_systems", []))
            # Check system filter
            if watch_systems and alert.system_id in watch_systems:
                pass  # Explicitly watched system, always show
            elif watch_regions or watch_alliances:
                region_ok = not watch_regions or (alert.region_id in watch_regions)
                alliance_ok = not watch_alliances or bool(
                    alert.alliances_involved & watch_alliances)
                if watch_regions and watch_alliances:
                    if not (region_ok and alliance_ok):
                        return
                elif not region_ok and not alliance_ok:
                    return

        # Proximity filter
        try:
            max_jumps = int(self._zkill_max_jumps_var.get())
        except ValueError:
            max_jumps = 0
        if max_jumps > 0 and alert.route_from_staging:
            import re
            m = re.search(r"\*\*(\d+) jumps\*\*", alert.route_from_staging)
            if m and int(m.group(1)) > max_jumps:
                return  # Too far, skip this alert

        ts = alert.timestamp.strftime("%H:%M:%S")
        caps_tag = " [CAPITALS]" if alert.capitals_involved else ""
        if alert.is_update:
            header = f"\n[{ts}] FIGHT GROWING{caps_tag}"
        else:
            header = f"\n[{ts}] FIGHT DETECTED{caps_tag}"
        self._append_zkill_log(header + "\n", "fight")
        self._append_zkill_log(
            f"  System:  {alert.system_name} ({alert.region_name})\n", "info"
        )
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
        self._zkill_log.config(state=tk.NORMAL)
        self._zkill_log.insert(tk.END, "  ")

        # zKillboard link button
        zkill_btn = tk.Button(
            self._zkill_log, text="zKillboard", font=("Consolas", 8, "bold"),
            fg=FG_ACCENT, bg=BG_ENTRY, activebackground="#1a5a90",
            activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
            cursor="hand2",
            command=lambda u=alert.zkill_url: self._open_url(u),
        )
        self._zkill_log.window_create(tk.END, window=zkill_btn)
        self._zkill_log.insert(tk.END, "  ")

        # Dotlan link button
        if alert.dotlan_url:
            dotlan_btn = tk.Button(
                self._zkill_log, text="Dotlan", font=("Consolas", 8, "bold"),
                fg=FG_ORANGE, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda u=alert.dotlan_url: self._open_url(u),
            )
            self._zkill_log.window_create(tk.END, window=dotlan_btn)
            self._zkill_log.insert(tk.END, "  ")

        # zKillboard Related Kills button
        if alert.zkill_related_url:
            related_btn = tk.Button(
                self._zkill_log, text="Related Kills", font=("Consolas", 8, "bold"),
                fg=FG_RED, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda u=alert.zkill_related_url: self._open_url(u),
            )
            self._zkill_log.window_create(tk.END, window=related_btn)
            self._zkill_log.insert(tk.END, "  ")

        # WarBeacon Battle Report button
        if alert.warbeacon_url:
            wb_btn = tk.Button(
                self._zkill_log, text="WarBeacon BR", font=("Consolas", 8, "bold"),
                fg="#e040fb", bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda u=alert.warbeacon_url: self._open_url(u),
            )
            self._zkill_log.window_create(tk.END, window=wb_btn)
            self._zkill_log.insert(tk.END, "  ")

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
            self._zkill_log.window_create(tk.END, window=dest_btn)
            self._zkill_log.insert(tk.END, "  ")

            # "Titan Bridge?" button -> fills Jump Range tab
            range_btn = tk.Button(
                self._zkill_log, text="Titan Bridge?", font=("Consolas", 8, "bold"),
                fg=FG_GREEN, bg=BG_ENTRY, activebackground="#1a5a90",
                activeforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
                cursor="hand2",
                command=lambda s=alert.system_name: self._navigate_jump_range(s),
            )
            self._zkill_log.window_create(tk.END, window=range_btn)

        self._zkill_log.insert(tk.END, "\n")
        self._zkill_log.see(tk.END)
        self._zkill_log.config(state=tk.DISABLED)

        self.root.bell()
        self._notify_zkill_tab()

    def _append_zkill_log(self, text, tag=None):
        self._zkill_log.config(state=tk.NORMAL)
        if tag:
            self._zkill_log.insert(tk.END, text, tag)
        else:
            self._zkill_log.insert(tk.END, text)
        self._zkill_log.see(tk.END)
        self._zkill_log.config(state=tk.DISABLED)

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

        # Secondary check systems for Titan bridge
        secondary_systems = [
            "6RCQ-V", "F7C-H0", "CL6-ZG", "HPS5-C",
            "Korasen", "Y-2ANO", "NOL-M9",
        ]

        def do_check():
            try:
                from zkill_monitor import get_region_for_system, resolve_name
                from jump_range import is_in_jump_range, save_route_cache
                from jump_range import calculate_ly_distance, get_system_info
                checker = JumpRangeChecker(ship, jdc_level=5)
                conns = self._get_ansiblex_connections()
                result = checker.check_range(origin, dest, connections=conns)
                # Add region names
                try:
                    r1 = get_region_for_system(result.get("origin_id", 0))
                    r2 = get_region_for_system(result.get("destination_id", 0))
                    result["origin_region"] = resolve_name(r1, "region") if r1 else ""
                    result["dest_region"] = resolve_name(r2, "region") if r2 else ""
                except Exception:
                    result["origin_region"] = ""
                    result["dest_region"] = ""

                # Always check secondary systems for Titan bridge range
                titan_checker = JumpRangeChecker("Titan", jdc_level=5)
                titan_range = titan_checker.jump_range
                dest_id = result.get("destination_id")
                secondary_results = []
                print(f"[JumpRange] Secondary check: dest_id={dest_id}, titan_range={titan_range}")
                if dest_id:
                    # Pre-resolve all system IDs sequentially (disk cache after first run)
                    sec_ids = {}
                    for sys_name in secondary_systems:
                        sid = search_system(sys_name)
                        print(f"[JumpRange]   Resolved {sys_name} -> {sid}")
                        if sid:
                            sec_ids[sys_name] = sid
                            get_system_info(sid)
                    dest_info = get_system_info(dest_id)
                    print(f"[JumpRange]   Dest info has position: {bool(dest_info and dest_info.get('position'))}")
                    save_route_cache()

                    # Calculate distances (pure math, no API calls)
                    # Also compute ansiblex jump distance from staging
                    staging = self._get_staging_system()
                    staging_id = search_system(staging) if staging else None
                    for sys_name in secondary_systems:
                        sid = sec_ids.get(sys_name)
                        if sid:
                            dist = calculate_ly_distance(sid, dest_id)
                            print(f"[JumpRange]   {sys_name} ({sid}) -> dest: {dist} LY")
                            # Gate jumps from staging (with ansiblex)
                            gate_jumps = None
                            if staging_id and conns:
                                from jump_range import get_stargate_route
                                route = get_stargate_route(staging_id, sid, connections=conns)
                                if route:
                                    gate_jumps = len(route) - 1
                            if dist is not None:
                                secondary_results.append({
                                    "system": sys_name,
                                    "in_range": dist <= titan_range,
                                    "distance_ly": round(dist, 2),
                                    "range_ly": round(titan_range, 2),
                                    "jumps_from_staging": gate_jumps,
                                })
                print(f"[JumpRange] Secondary results: {len(secondary_results)} entries")
                result["secondary"] = secondary_results

                save_route_cache()
                self.root.after(0, self._show_range_result, result)
            except Exception as e:
                print(f"[JumpRange] Error in range check: {e}")
                import traceback
                traceback.print_exc()
                self.root.after(0, self._range_result_label.config,
                                {"text": f"Error: {e}", "fg": FG_RED})

        threading.Thread(target=do_check, daemon=True).start()

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

        # Show secondary Titan bridge range table
        secondary = result.get("secondary", [])
        if secondary:
            tk.Label(
                self._range_secondary_frame,
                text=f"Titan Bridge Range to {result['destination']}:",
                font=("Consolas", 11, "bold"), fg=FG_ACCENT, bg=BG_DARK,
            ).pack(anchor=tk.W, pady=(5, 3))

            table = tk.Frame(self._range_secondary_frame, bg=BG_DARK)
            table.pack(anchor=tk.W, padx=10)

            # Header row
            tk.Label(table, text="System", font=("Consolas", 10, "bold"),
                     fg=FG_DIM, bg=BG_DARK, width=14, anchor=tk.W
                     ).grid(row=0, column=0, padx=(0, 10))
            tk.Label(table, text="Jumps", font=("Consolas", 10, "bold"),
                     fg=FG_DIM, bg=BG_DARK, width=6, anchor=tk.W
                     ).grid(row=0, column=1, padx=(0, 10))
            tk.Label(table, text="In Range?", font=("Consolas", 10, "bold"),
                     fg=FG_DIM, bg=BG_DARK, width=10, anchor=tk.W
                     ).grid(row=0, column=2, padx=(0, 10))
            tk.Label(table, text="Distance", font=("Consolas", 10, "bold"),
                     fg=FG_DIM, bg=BG_DARK, width=12, anchor=tk.W
                     ).grid(row=0, column=3)

            for i, sr in enumerate(secondary, 1):
                sys_label = tk.Label(table, text=sr["system"],
                         font=("Consolas", 10), fg=FG_TEXT, bg=BG_DARK,
                         width=14, anchor=tk.W, cursor="hand2")
                sys_label.grid(row=i, column=0, padx=(0, 10))
                # Right-click context menu
                sys_name = sr["system"]
                menu = tk.Menu(sys_label, tearoff=0, bg=BG_PANEL, fg=FG_TEXT,
                               activebackground=FG_ACCENT, activeforeground=BG_DARK)
                menu.add_command(label=f"Set destination: {sys_name}",
                                 command=lambda n=sys_name: self._set_destination_or_copy(n))
                menu.add_command(label=f"Copy \"{sys_name}\"",
                                 command=lambda n=sys_name: (self.root.clipboard_clear(), self.root.clipboard_append(n)))
                sys_label.bind("<Button-3>", lambda e, m=menu: m.tk_popup(e.x_root, e.y_root))
                # Left-click sets destination or copies
                sys_label.bind("<Button-1>", lambda e, n=sys_name: self._set_destination_or_copy(n))

                # Ansiblex jumps from staging
                jumps = sr.get("jumps_from_staging")
                jumps_text = str(jumps) if jumps is not None else "?"
                tk.Label(table, text=jumps_text,
                         font=("Consolas", 10), fg=FG_ACCENT, bg=BG_DARK,
                         width=6, anchor=tk.W
                         ).grid(row=i, column=1, padx=(0, 10))

                in_range_text = "YES" if sr["in_range"] else "NO"
                in_range_color = FG_GREEN if sr["in_range"] else FG_RED
                tk.Label(table, text=in_range_text,
                         font=("Consolas", 10, "bold"), fg=in_range_color,
                         bg=BG_DARK, width=10, anchor=tk.W
                         ).grid(row=i, column=2, padx=(0, 10))

                dist = sr["distance_ly"]
                dist_text = f"{dist:.1f} LY" if dist is not None else "N/A"
                tk.Label(table, text=dist_text,
                         font=("Consolas", 10), fg=FG_DIM, bg=BG_DARK,
                         width=12, anchor=tk.W
                         ).grid(row=i, column=3)


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
