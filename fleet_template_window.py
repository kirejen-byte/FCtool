# fleet_template_window.py
"""Fleet Templates window — Tkinter view over the pure fleet_* modules.

Owns a Toplevel with a Template/Live mode toggle, a wing/squad/slot tree, a
right-hand Members/Rules/Settings notebook, a hybrid apply flow, and a
compose-driven Auto-sort loop. All matching/persistence/ESI logic lives in
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
from collections import deque
from datetime import datetime
from tkinter import ttk, messagebox, simpledialog

import fleet_composer
import fleet_esi
from fleet_executor import MoveJob
from fleet_template_store import (
    Wing, Squad, Slot, RuleCondition, RuleAction, AssignmentRule, validate_template,
)

# House dark palette — imports the shared ui_theme palette (a stdlib-only,
# containment-safe leaf; importing fc_gui here would be circular). This window
# previously carried a divergent gray palette; it now uses the canonical navy
# scheme like the rest of the app (OPTIMIZATION_REVIEW.md D1 reconciliation).
from ui_theme import (
    BG_DARK, BG_PANEL, BG_ENTRY,
    FG_TEXT, FG_DIM, FG_ACCENT, FG_GREEN, FG_YELLOW, FG_RED,
    BORDER_COLOR,
)
# Shared house dialog + tooltip helpers (both stdlib-only leaves): make_modal
# wires the guarded transient/grab + Escape→cancel + base bg once (D2/D6), and
# attach_tooltip is the single hover-tooltip impl with the <Destroy> leak fix (D9).
from ui_helpers import make_modal, attach_tooltip

ROLE_VALUES = ["squad_member", "squad_commander", "wing_commander", "fleet_commander"]
ROLE_ABBR = {"squad_member": "", "squad_commander": "SC",
             "wing_commander": "WC", "fleet_commander": "FC"}
CONDITION_TYPES = ["ship_type", "ship_class", "character", "doctrine_tag",
                   "capital", "subcap", "default"]

# Condition types whose value is meaningless (the value widget is hidden).
VALUELESS_CONDITIONS = {"capital", "subcap", "default"}

# Static common ship-class labels for the ship_class dropdown (augmented at
# runtime by classes present in the live fleet).
COMMON_SHIP_CLASSES = [
    "Titan", "Supercarrier", "Carrier", "Dreadnought", "Force Auxiliary",
    "Command Ship", "Command Destroyer", "Logistics Cruiser",
    "Logistics Frigate", "Strategic Cruiser", "Heavy Assault Cruiser",
    "Interdictor", "Heavy Interdiction Cruiser", "Interceptor", "Battleship",
    "Battlecruiser", "Cruiser", "Frigate", "Destroyer",
]


def parse_pilot_lines(text: str) -> list[str]:
    """Parse a multiline bulk-add blob into a clean ordered name list.

    One name per line: strip surrounding whitespace, drop empty lines, and
    de-dupe case-insensitively (first-seen casing wins). Pure — no Tk."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        name = raw.strip()
        if not name:
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(name)
    return out


def inline_rename_counter(kind: str, text: str) -> str:
    """Live counter for the inline rename Entry. Wing/squad names are clamped to
    10 chars on ESI, so show 'N/10' (with ⚠ when over). Slots aren't clamped."""
    if kind not in ("wing", "squad"):
        return ""
    n = len(text or "")
    return f"{n}/10 ⚠" if n > 10 else f"{n}/10"


def mode_banner_text(mode: str) -> str:
    if mode == "live":
        return "LIVE — changes affect the real fleet"
    return "TEMPLATE — sandbox (changes saved to the template only)"


def build_apply_preview_rows(res, members) -> dict:
    """Turn a ComposeResult + the live member dicts into display rows for the
    Apply preview dialog.

    moves      -> 'Pilot — Ship → Wing/Squad [role]'.  Move has no ship name, so
                  the ship is looked up in `members` by character_id (== pilot_id);
                  a missing pilot/ship drops the ship segment (segments are
                  ' — '.joined so there is no doubled ' — ' artifact).
    unassigned -> 'Name — reason'.  res.unassigned are enriched member dicts;
                  the reason is joined from res.unassigned_reasons by
                  character_id, falling back to 'no matching rule' only when the
                  id has no entry.
    warnings   -> passed through.
    Pure — no Tk."""
    members_by_id = {m.get("character_id"): m for m in (members or [])}
    moves = []
    for mv in res.executable:
        role = getattr(mv, "target_role", None) or "squad_member"
        wing = getattr(mv, "target_wing_name", "") or ""
        squad = getattr(mv, "target_squad_name", "") or ""
        member = members_by_id.get(getattr(mv, "pilot_id", None))
        ship = (member.get("ship_type_name") or "") if member else ""
        # Build the leading segments and join the non-empty ones so a missing
        # ship yields 'Pilot → ...' rather than 'Pilot —  → ...'.
        lead = [seg for seg in (mv.pilot_name, ship) if seg]
        moves.append(f"{' — '.join(lead)} → {wing}/{squad} [{role}]")
    reasons = getattr(res, "unassigned_reasons", None) or {}
    unassigned = []
    for u in getattr(res, "unassigned", []) or []:
        if isinstance(u, dict):
            cid = u.get("character_id")
            name = u.get("name") or u.get("pilot_name") or "?"
        else:
            cid = getattr(u, "character_id", None)
            name = getattr(u, "name", None) or getattr(u, "pilot_name", "?")
        reason = reasons.get(cid) or "no matching rule"
        unassigned.append(f"{name} — {reason}")
    return {"moves": moves, "unassigned": unassigned,
            "warnings": list(getattr(res, "warnings", []) or [])}


def member_matches_filter(member: dict, query: str) -> bool:
    """Substring match (case-insensitive) of `query` against a member's name,
    ship type, and ship class. Empty query matches all. Pure — no Tk."""
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = " ".join(str(member.get(k, "") or "") for k in
                   ("name", "ship_type_name", "ship_class")).lower()
    return q in hay


class FleetTemplateWindow:
    def __init__(self, root, *, store, fittings, config, esi_session_provider,
                 fleet_info_provider, doctrine_provider, character_names_provider,
                 resolve_names_provider=None, host=None):
        self.store = store
        self.fittings = fittings
        self.config = config
        self._esi_session_provider = esi_session_provider
        self._fleet_info_provider = fleet_info_provider
        self._doctrine_provider = doctrine_provider
        self._character_names_provider = character_names_provider
        # names -> {name_lower: character_id}; None/absent = no-auth (returns {}).
        self._resolve_names_provider = resolve_names_provider or (lambda names: {})
        # Host GUI exposing the shared main-thread UI dispatcher (_post_ui). When
        # present (normal in-app use) worker→UI marshalling routes through its
        # queue so no Tcl call runs off the main thread; tests construct the
        # window with a bare Tk root (no host), and _post falls back to win.after.
        self._host = host

        self.mode = "template"
        self._current_template_id = (store.templates[0].id if store.templates else None)
        self._auto_sort_on = False
        self._sync_after_id = None
        self._executor = None
        # True while an async fleet-boss check is in flight (set_mode → live).
        # Guards against stacking workers on rapid double-clicks of the toggle.
        self._entering_live = False
        self._last_write_wall = 0.0
        self._pins: dict[int, int] = {}          # pilot_id -> ship_type_id at pin time
        self._prev_members: list[dict] = []      # previous sync snapshot (for diffing)
        self._sync_generation = 0                # bumped per sync; guards stale worker results
        self._live_members: list[dict] = []      # enriched dicts (Task D5)
        self._live_structure: dict = {"wings": []}
        self._last_preview = None                 # cached ComposeResult (Tasks D5/D10)
        self._undo_stack: list[dict] = []         # template-mode undo (Task D9)
        self._log_buffer: deque = deque(maxlen=500)   # internal executor/sort log

        self.win = tk.Toplevel(root)
        self.win.title("Fleet Templates")
        self.win.configure(bg=BG_DARK)
        geom = self.store.ui.get("geometry") if hasattr(self.store, "ui") else None
        self.win.geometry(geom if isinstance(geom, str) and geom else "980x640")
        self.win.minsize(900, 560)
        self.win.protocol("WM_DELETE_WINDOW", self.destroy)

        self._build_header()
        self._build_body()
        self._build_footer()
        self._refresh_template_selector()
        self.set_mode("template")
        self.win.bind("<Control-z>", self._undo)

    # ── construction ─────────────────────────────────────────────────────────
    def _build_header(self):
        bar = tk.Frame(self.win, bg=BG_PANEL)
        bar.pack(fill=tk.X, side=tk.TOP)
        tk.Label(bar, text="Fleet Templates", font=("Consolas", 12, "bold"),
                 fg=FG_ACCENT, bg=BG_PANEL).pack(side=tk.LEFT, padx=10, pady=6)

        self._mode_btn = ttk.Button(bar, text="Mode: Template",
                                    style="Dark.TButton", command=self._toggle_mode)
        self._mode_btn.pack(side=tk.RIGHT, padx=10)

        self._import_btn = ttk.Button(bar, text="Import live fleet",
                                      style="Dark.TButton",
                                      command=self._import_live_as_template)
        self._import_btn.pack(side=tk.RIGHT, padx=4)

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
        ttk.Button(sel, text="Duplicate", style="Dark.TButton",
                   command=self._duplicate_template).pack(side=tk.LEFT, padx=2)

        self._banner = tk.Label(self.win, text="", font=("Consolas", 9, "bold"),
                                anchor="w", padx=10, pady=3)
        self._banner.pack(fill=tk.X, side=tk.TOP)

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
        self._log_tab = tk.Frame(self._panel, bg=BG_PANEL)
        self._panel.add(self._members_tab, text="Members")
        self._panel.add(self._rules_tab, text="Rules")
        self._panel.add(self._settings_tab, text="Settings")
        self._panel.add(self._log_tab, text="Log")
        self._build_tree()         # Task D2
        self._build_rules_tab()    # Task D3
        self._build_settings_tab() # Task D4
        self._build_members_tab()  # Task D6
        self._build_log_tab()      # Task D6

    def _build_footer(self):
        bar = tk.Frame(self.win, bg=BG_PANEL)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self._save_btn = ttk.Button(bar, text="Save Template",
                                    style="Dark.TButton", command=self._save)
        self._save_btn.pack(side=tk.LEFT, padx=8, pady=6)
        self._auto_sort_btn = ttk.Button(bar, text="Auto-move: OFF",
                                         style="Dark.TButton",
                                         command=self._toggle_auto_sort)
        self._auto_sort_btn.pack(side=tk.LEFT, padx=8)
        self._attach_tooltip(self._auto_sort_btn,
                             "When ON, continuously moves fleet members into the "
                             "positions your template's rules dictate as the fleet "
                             "changes (joins, ship swaps). Manually dragged pilots "
                             "stay where you put them. OFF by default; only active "
                             "in live-fleet mode.")
        self._status = tk.Label(bar, text="", font=("Consolas", 9),
                                fg=FG_DIM, bg=BG_PANEL)
        self._status.pack(side=tk.LEFT, padx=10)
        self._clear_pins_btn = ttk.Button(bar, text="Clear pins (0)",
                                          style="Dark.TButton", command=self._clear_pins)
        # packed/unpacked by _refresh_pins_button (hidden when 0)
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

    def _duplicate_template(self):
        t = self.current_template()
        if t is None:
            return
        copy = self.store.duplicate_template(t.id)
        if copy is None:
            return
        self._current_template_id = copy.id
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
        if mode == "live":
            # Entering live requires a fleet-boss check, which is a SYNCHRONOUS
            # ESI round-trip. Do it off the Tk thread and only flip the UI to
            # live once it succeeds — keep mode='template' until confirmed so a
            # failed check needs no visible revert. See _enter_live_mode.
            self._enter_live_mode()
            return
        # Template mode is local-only → apply immediately.
        self._apply_mode_ui("template")
        self._exit_live_mode()         # Task D5
        self._refresh_add_buttons()

    def _apply_mode_ui(self, mode: str):
        """Apply all mode-dependent widget state (banner, button enablement).
        Shared by the synchronous template path and the async live-entry
        completion so both leave the window in a consistent state."""
        self.mode = mode
        self._banner.config(
            text=mode_banner_text(mode),
            fg=(FG_YELLOW if mode == "live" else FG_DIM),
            bg=(BG_DARK if mode == "live" else BG_PANEL))
        self._mode_btn.config(text=f"Mode: {mode.capitalize()}")
        live = (mode == "live")
        self._apply_btn.config(state="normal" if live else "disabled")
        self._import_btn.config(state="normal" if live else "disabled")
        self._auto_sort_btn.config(state="normal" if live else "disabled")
        self._save_btn.config(state="disabled" if live else "normal")
        if not live:
            self._auto_sort_on = False
            self._auto_sort_btn.config(text="Auto-move: OFF")

    def _save(self):
        t = self.current_template()
        if t is not None:
            validate_template(t)
            self.store.save()
            self._status.config(text="Saved.", fg=FG_GREEN)

    # ── undo (template mode) ─────────────────────────────────────────────────
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

    # ── lifecycle ────────────────────────────────────────────────────────────
    def destroy(self):
        try:
            if hasattr(self.store, "ui"):
                self.store.ui["geometry"] = self.win.geometry()
                self.store.save()
        except Exception:
            pass
        try:
            self._destroy_drag_ghost()
        except Exception:
            pass
        if self._sync_after_id:
            try:
                self.win.after_cancel(self._sync_after_id)
            except Exception:
                pass
        if getattr(self, "_executor", None) is not None:
            self._executor.stop()   # ends the persistent worker (None sentinel)
        try:
            self.win.destroy()
        except Exception:
            pass

    def _build_tree(self):
        wrap = tk.Frame(self._tree_frame, bg=BG_PANEL)
        wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._tree = ttk.Treeview(wrap, show="tree", selectmode="extended")
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.tag_configure("inpos", foreground=FG_GREEN)   # in correct position
        self._tree.tag_configure("moveme", foreground=FG_YELLOW)  # present, needs moving
        self._tree.tag_configure("empty", foreground=FG_DIM)      # unfilled slot
        # node-id → ("wing"|"squad"|"slot"|"unassigned", path tuple)
        self._node_meta: dict[str, tuple] = {}
        self._tree.bind("<Button-3>", self._on_tree_right_click)
        self._tree.bind("<F2>", lambda e: self._begin_inline_rename())
        self._tree.bind("<Double-Button-1>", self._on_tree_double_click)
        self._tree.bind("<Delete>", lambda e: self._delete_selected())
        # Drag-drop bindings (Task D8).
        self._tree.bind("<ButtonPress-1>", self._on_drag_start)
        self._tree.bind("<B1-Motion>", self._on_drag_motion)
        self._tree.bind("<ButtonRelease-1>", self._on_drag_drop)
        self._drag_pending = None   # item ids captured at ButtonPress, dragged on motion
        self._drag_ghost = None     # floating Toplevel that follows the cursor

        add = tk.Frame(self._tree_frame, bg=BG_PANEL)
        add.pack(fill=tk.X)
        ttk.Button(add, text="+ Add Wing", style="Dark.TButton",
                   command=self._add_wing).pack(side=tk.LEFT, padx=4, pady=2)
        self._add_squad_btn = ttk.Button(add, text="+ Squad", style="Dark.TButton",
                                         command=self._add_squad_from_selection)
        self._add_squad_btn.pack(side=tk.LEFT, padx=4, pady=2)
        self._add_slot_btn = ttk.Button(add, text="+ Slot", style="Dark.TButton",
                                        command=self._add_slot_from_selection)
        self._add_slot_btn.pack(side=tk.LEFT, padx=4, pady=2)
        self._tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_add_buttons())
        self._reload_tree()

    def _slot_label(self, slot: Slot) -> str:
        abbr = ROLE_ABBR.get(slot.role, "")
        suffix = f" [{abbr}]" if abbr else ""
        if slot.character:
            mark = "" if slot.character_id else " ⚠"
            return f"● {slot.character}{mark}{suffix}"
        if slot.tag:
            return f"◈ {slot.tag}{suffix}"
        return f"○ (empty){suffix}"

    def _reload_tree(self):
        self._tree.delete(*self._tree.get_children())
        self._node_meta.clear()
        t = self.current_template()
        if t is None:
            self._last_preview = None
            return
        if self.mode == "live":
            self._reload_live_tree()
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
                    tags = () if (slot.character is None or slot.character_id
                                  or slot.tag) else ("empty",)
                    nid = self._tree.insert(sid, "end",
                                            text=self._slot_label(slot), tags=tags)
                    self._node_meta[nid] = ("slot", (wi, si, li))
        self._refresh_add_buttons()

    def _reload_live_tree(self):
        # One compose() to know which currently-placed pilots the template would move.
        self._last_preview = self._compose_preview()
        move_ids = set()
        if self._last_preview is not None:
            move_ids = {mv.pilot_id for mv in self._last_preview.executable}
        layout = fleet_composer.live_layout(self._live_members, self._live_structure)
        fc = layout["fc"]
        if fc is not None:
            fid = self._tree.insert(
                "", "end", open=True,
                text=f"★ FC: {fc['name']} — {fc.get('ship_type_name', '')}")
            self._node_meta[fid] = ("livepilot", fc["character_id"])
        for w in layout["wings"]:
            wc = w["wc"]
            wctxt = f"   ◄ WC: {wc['name']}" if wc else ""
            wid = self._tree.insert("", "end", open=True,
                                    text=f"▼ {w['name']}{wctxt}")
            self._node_meta[wid] = ("livewing", w["id"])
            for s in w["squads"]:
                sc = s["sc"]
                sctxt = f"   ◄ SC: {sc['name']}" if sc else ""
                sid = self._tree.insert(wid, "end", open=True,
                                        text=f"▼ {s['name']}{sctxt}")
                self._node_meta[sid] = ("livesquad", (w["id"], s["id"]))
                for m in s["members"]:
                    mark = ""
                    if m.get("role") == "wing_commander":
                        mark = " [WC]"
                    elif m.get("role") == "squad_commander":
                        mark = " [SC]"
                    tag = "moveme" if m["character_id"] in move_ids else "inpos"
                    pin = "📌 " if m["character_id"] in self._pins else ""
                    nid = self._tree.insert(
                        sid, "end", tags=(tag,),
                        text=f"{pin}• {m['name']} — {m.get('ship_type_name', '')}{mark}")
                    self._node_meta[nid] = ("livepilot", m["character_id"])
        if layout["unplaced"]:
            head = self._tree.insert("", "end", open=True, text="── Unassigned ──")
            self._node_meta[head] = ("unassigned_header", ())
            for m in layout["unplaced"]:
                nid = self._tree.insert(
                    head, "end",
                    text=f"· {m['name']} — {m.get('ship_type_name', '')}")
                self._node_meta[nid] = ("unassigned", (m["character_id"],))
        self._refresh_add_buttons()

    # ── pins ─────────────────────────────────────────────────────────────────
    def _refresh_pins_button(self):
        n = len(self._pins)
        if n:
            self._clear_pins_btn.config(text=f"Clear pins ({n})")
            if not self._clear_pins_btn.winfo_ismapped():
                self._clear_pins_btn.pack(side=tk.LEFT, padx=6)
        else:
            self._clear_pins_btn.pack_forget()

    def _clear_pins(self):
        self._pins.clear()
        self._refresh_pins_button()
        if self.mode == "live":
            self._reload_tree()

    # ── sync member diff ───────────────────────────────────────────────────────
    def _diff_members(self, prev, new):
        prev_by = {m["character_id"]: m for m in prev}
        new_by = {m["character_id"]: m for m in new}
        joined = [cid for cid in new_by if cid not in prev_by]
        left = [cid for cid in prev_by if cid not in new_by]
        ship_changed = [cid for cid in new_by
                        if cid in prev_by
                        and new_by[cid].get("ship_type_id")
                        != prev_by[cid].get("ship_type_id")]
        return {"joined": joined, "left": left, "ship_changed": ship_changed}

    def _apply_member_diff(self, events):
        for cid in events["ship_changed"]:
            self._pins.pop(cid, None)
        for cid in events["left"]:
            self._pins.pop(cid, None)
        self._refresh_pins_button()

    # ── structural edits ─────────────────────────────────────────────────────
    def _add_wing(self):
        self._push_undo()
        t = self.current_template()
        if t is None:
            return
        t.wings.append(Wing(name=f"Wing {len(t.wings) + 1}", max_size=None, squads=[]))
        self._after_structure_change()

    def _add_squad(self, wi):
        self._push_undo()
        t = self.current_template()
        t.wings[wi].squads.append(
            Squad(name=f"Squad {len(t.wings[wi].squads) + 1}", max_size=None, slots=[]))
        self._after_structure_change()

    def _add_slot(self, wi, si):
        self._push_undo()
        t = self.current_template()
        t.wings[wi].squads[si].slots.append(
            Slot(character=None, tag=None, role="squad_member"))
        self._after_structure_change()

    def _refresh_add_buttons(self):
        """+ Squad enabled when a wing/squad/slot is selected; + Slot enabled
        when a squad or slot is selected. Template mode only (live tree has no
        editable structure)."""
        squad_ok = slot_ok = False
        if self.mode == "template":
            _item, meta = self._selected_meta()
            if meta is not None:
                kind = meta[0]
                squad_ok = kind in ("wing", "squad", "slot")
                slot_ok = kind in ("squad", "slot")
        self._add_squad_btn.config(state="normal" if squad_ok else "disabled")
        self._add_slot_btn.config(state="normal" if slot_ok else "disabled")

    def _add_squad_from_selection(self):
        _item, meta = self._selected_meta()
        if not meta:
            return
        wi = meta[1][0]            # wing/squad/slot paths all start with wi
        self._add_squad(wi)

    def _add_slot_from_selection(self):
        _item, meta = self._selected_meta()
        if not meta:
            return
        kind, path = meta
        if kind in ("squad", "slot"):
            self._add_slot(path[0], path[1])

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

    def _on_tree_double_click(self, event):
        if self.mode != "template":
            return
        item = self._tree.identify_row(event.y)
        if not item:
            return
        meta = self._node_meta.get(item)
        if meta and meta[0] in ("wing", "squad", "slot"):
            self._tree.selection_set(item)
            self._begin_inline_rename(item)
            return "break"

    def _on_tree_right_click(self, event):
        item = self._tree.identify_row(event.y)
        if item:
            self._tree.selection_set(item)
        meta = self._node_meta.get(item)
        if self.mode == "live":
            self._live_right_click_menu(event, item, meta)
            return
        menu = tk.Menu(self.win, tearoff=0, bg=BG_PANEL, fg=FG_TEXT)
        if meta is None:
            menu.add_command(label="Add Wing", command=self._add_wing)
        else:
            kind, path = meta
            if kind == "wing":
                wi = path[0]
                menu.add_command(label="Rename", command=lambda: self._rename_selected())
                menu.add_command(label="Add Squad", command=lambda: self._add_squad(wi))
                menu.add_separator()
                menu.add_command(label="Delete Wing",
                                 command=lambda: self._delete_selected())
            elif kind == "squad":
                wi, si = path
                menu.add_command(label="Rename", command=lambda: self._rename_selected())
                menu.add_command(label="Add Slot",
                                 command=lambda: self._add_slot(wi, si))
                menu.add_command(label="Add pilots from list…",
                                 command=lambda: self._open_bulk_add(wi, si))
                menu.add_command(label="Add my characters…",
                                 command=lambda: self._open_add_my_chars(wi, si))
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
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _live_right_click_menu(self, event, item, meta):
        if not meta:
            return
        kind, val = meta
        char_id = None
        if kind == "livepilot":
            char_id = val
        elif kind == "unassigned":
            char_id = val[0]
        if char_id is None:
            return
        menu = tk.Menu(self.win, tearoff=0, bg=BG_PANEL, fg=FG_TEXT)
        self._add_move_to_squad_cascade(menu, char_id)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _add_move_to_squad_cascade(self, menu, char_id):
        """Attach a 'Move to squad…' cascade (wing → squad) that routes the
        pilot through _menu_move_pilot."""
        cascade = tk.Menu(menu, tearoff=0, bg=BG_PANEL, fg=FG_TEXT)
        for w in self._live_structure.get("wings", []):
            wsub = tk.Menu(cascade, tearoff=0, bg=BG_PANEL, fg=FG_TEXT)
            for s in w.get("squads", []):
                wid, sid = w["id"], s["id"]
                wsub.add_command(
                    label=s.get("name", ""),
                    command=lambda cid=char_id, wi=wid, si=sid:
                    self._menu_move_pilot(cid, wi, si))
            cascade.add_cascade(label=w.get("name", ""), menu=wsub)
        menu.add_cascade(label="Move to squad…", menu=cascade)

    def _rename_selected(self):
        self._begin_inline_rename()

    def _commit_inline_rename(self, kind, path, new_name):
        """Model write for an inline rename. Wing/squad: set .name; slot: set the
        named character (blank clears to generic). Blank wing/squad names are
        ignored. Pure of Tk overlay concerns so it is unit-testable."""
        name = (new_name or "").strip()
        t = self.current_template()
        if t is None:
            return
        if kind == "wing":
            if not name:
                return
            self._push_undo()
            t.wings[path[0]].name = name
        elif kind == "squad":
            if not name:
                return
            self._push_undo()
            t.wings[path[0]].squads[path[1]].name = name
        elif kind == "slot":
            self._push_undo()
            slot = t.wings[path[0]].squads[path[1]].slots[path[2]]
            slot.character = name or None
            if not name:
                slot.character_id = None
            else:
                slot.character_id = self.store.cached_id(name)
        else:
            return
        self._after_structure_change()

    def _begin_inline_rename(self, item=None):
        """Overlay a tk.Entry on the selected (or given) tree row for in-place
        rename. Return/FocusOut commits, Escape cancels. Template mode only;
        live rows are not renameable."""
        if self.mode != "template":
            return
        if item is None:
            item, meta = self._selected_meta()
        else:
            meta = self._node_meta.get(item)
        if not item or not meta:
            return
        kind, path = meta
        if kind not in ("wing", "squad", "slot"):
            return
        bbox = self._tree.bbox(item)
        if not bbox:                 # row not visible
            return
        x, y, w, h = bbox
        t = self.current_template()
        if kind == "wing":
            initial = t.wings[path[0]].name
        elif kind == "squad":
            initial = t.wings[path[0]].squads[path[1]].name
        else:
            slot = t.wings[path[0]].squads[path[1]].slots[path[2]]
            initial = slot.character or ""

        overlay = tk.Frame(self._tree, bg=BG_PANEL)
        entry = tk.Entry(overlay, bg=BG_ENTRY, fg=FG_TEXT,
                         insertbackground=FG_TEXT, font=("Consolas", 9),
                         relief=tk.SOLID, borderwidth=1)
        entry.insert(0, initial)
        entry.select_range(0, tk.END)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        counter = tk.Label(overlay, text=inline_rename_counter(kind, initial),
                           bg=BG_PANEL, fg=FG_DIM, font=("Consolas", 8))
        counter.pack(side=tk.LEFT, padx=2)
        overlay.place(x=x, y=y, width=max(w, 160), height=h)

        state = {"done": False}

        def finish(commit):
            if state["done"]:
                return
            state["done"] = True
            value = entry.get()
            try:
                overlay.destroy()
            except tk.TclError:
                pass
            if commit:
                self._commit_inline_rename(kind, path, value)

        entry.bind("<KeyRelease>",
                   lambda e: counter.config(
                       text=inline_rename_counter(kind, entry.get())))
        entry.bind("<Return>", lambda e: finish(True))
        entry.bind("<FocusOut>", lambda e: finish(True))
        entry.bind("<Escape>", lambda e: finish(False))
        entry.focus_set()

    def _set_max_size(self, kind, path):
        self._push_undo()
        t = self.current_template()
        obj = t.wings[path[0]] if kind == "wing" else t.wings[path[0]].squads[path[1]]
        val = simpledialog.askinteger("Max size",
                                      "Max members (blank/0 = no cap):",
                                      initialvalue=obj.max_size or 0,
                                      minvalue=0, parent=self.win)
        obj.max_size = val if val else None
        self._after_structure_change()

    def _edit_slot(self, path):
        self._push_undo()
        wi, si, li = path
        slot = self.current_template().wings[wi].squads[si].slots[li]
        SlotEditor(self.win, slot, self.fittings, self._character_names_provider(),
                   on_ok=lambda: self._after_structure_change(),
                   resolve_names=self._resolve_names_provider,
                   store=self.store, post=self._post)

    def _open_bulk_add(self, wi, si):
        _BulkAddDialog(self.win, on_ok=lambda text: self._bulk_add_pilots_names(
            wi, si, parse_pilot_lines(text)))

    def _bulk_add_pilots_names(self, wi, si, names):
        """Resolve `names` on a worker, then create pinned named slots on the Tk
        thread. Unresolved names are routed to the Add-anyway/Skip dialog."""
        if not names:
            return
        self._status.config(text=f"Resolving {len(names)} name(s)…", fg=FG_DIM)

        def worker():
            resolved = self._resolve_names_provider(names) or {}
            self._post(self._apply_bulk_resolution, wi, si, names, resolved)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_bulk_resolution(self, wi, si, names, resolved):
        """Tk thread: add a pinned named slot per resolved name; cache the pair;
        collect unresolved names for the decision dialog."""
        found_pairs = []      # (display_name, character_id)
        unresolved = []
        for name in names:
            cid = resolved.get(name.strip().lower())
            if isinstance(cid, int):
                found_pairs.append((name, cid))
            else:
                unresolved.append(name)
        if found_pairs:
            self._add_named_slots(wi, si, found_pairs)
        if unresolved:
            self._show_unresolved_dialog(wi, si, unresolved)
        else:
            self._status.config(text=f"Added {len(found_pairs)} pilot(s).",
                                fg=FG_GREEN)

    def _add_named_slots(self, wi, si, pairs):
        """Append one pinned named slot per (name, character_id) pair, cache the
        pairs, and persist via the standard structure-change path."""
        self._push_undo()
        t = self.current_template()
        if t is None:
            return
        squad = t.wings[wi].squads[si]
        for name, cid in pairs:
            squad.slots.append(Slot(character=name, tag=None,
                                    role="squad_member",
                                    character_id=cid if isinstance(cid, int) else None))
            self.store.cache_character(name, cid if isinstance(cid, int) else None)
        self._after_structure_change()

    def _show_unresolved_dialog(self, wi, si, unresolved):
        _UnresolvedDialog(
            self.win, unresolved,
            on_add_anyway=lambda: self._add_named_slots(
                wi, si, [(n, None) for n in unresolved]),
            on_skip=lambda: self._status.config(
                text=f"Skipped {len(unresolved)} unresolved name(s).", fg=FG_YELLOW))

    def _open_add_my_chars(self, wi, si):
        names = sorted(self._character_names_provider() or [])
        if not names:
            self._status.config(text="No authed characters to add.", fg=FG_YELLOW)
            return
        _AddMyCharsDialog(self.win, names,
                          on_ok=lambda chosen: self._add_my_chars_names(wi, si, chosen))

    def _add_my_chars_names(self, wi, si, chosen):
        """Resolve the checked SSO names (should resolve trivially) and pin them
        as named slots. Reuses the bulk resolution path."""
        self._bulk_add_pilots_names(wi, si, list(chosen))

    def _delete_selected(self):
        self._push_undo()
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

    def _on_drag_start(self, event):
        self._destroy_drag_ghost()
        self._drag_pending = None
        # Ctrl/Shift clicks build the selection — don't start a drag for those.
        if event.state & 0x0004 or event.state & 0x0001:   # Control or Shift
            return
        item = self._tree.identify_row(event.y)
        if not item:
            return
        sel = self._tree.selection()
        if item in sel and len(sel) > 1:
            # Pressing an already-multi-selected row → drag the whole selection,
            # and "break" so Tk's default doesn't collapse it to one row.
            self._drag_pending = list(sel)
            return "break"
        # Plain press on a single row: record it and let Tk select it normally.
        self._drag_pending = [item]

    def _on_drag_motion(self, event):
        if not self._drag_pending:
            return
        kind, ids = self._draggable_drag_set(self._drag_pending)
        if not ids:
            return
        if self._drag_ghost is None:
            self._create_drag_ghost(kind, ids)
        self._move_drag_ghost(event.x_root, event.y_root)

    def _on_drag_drop(self, event):
        pending = self._drag_pending
        self._drag_pending = None
        self._destroy_drag_ghost()
        if not pending:
            return
        kind, ids = self._draggable_drag_set(pending)
        if not ids:
            return
        dst = self._tree.identify_row(event.y)
        dst_meta = self._node_meta.get(dst)
        if not dst_meta:
            return
        if self.mode == "live":
            if kind not in ("livepilot", "unassigned"):
                return
            char_ids = []
            for i in ids:
                meta = self._node_meta.get(i)
                if not meta:
                    continue
                if meta[0] == "livepilot":
                    char_ids.append(meta[1])
                elif meta[0] == "unassigned":
                    char_ids.append(meta[1][0])
            dk, dval = dst_meta
            wing_id = squad_id = None
            if dk == "livesquad":
                wing_id, squad_id = dval
            elif dk == "livewing":
                wing_id = dval
            elif dk == "livepilot":
                tgt = next((m for m in self._live_members
                            if m["character_id"] == dval), None)
                if tgt is not None:
                    wing_id, squad_id = tgt.get("wing_id"), tgt.get("squad_id")
            if wing_id is None and squad_id is None:
                self._flash_reject(dst)
                return
            self._live_drop_pilots(char_ids, wing_id=wing_id, squad_id=squad_id)
            return
        if kind == "squad":
            dst_wing = self._wing_path_of(dst_meta)
            if dst_wing is None:
                self._flash_reject(dst)
                return
            self._drop_squads_into_wing([self._node_meta[i][1] for i in ids], dst_wing)
            return
        dst_squad = self._squad_path_of(dst_meta)
        if dst_squad is None:
            self._flash_reject(dst)
            return
        if kind == "slot":
            self._drop_slots_into_squad([self._node_meta[i][1] for i in ids], dst_squad)
        elif kind == "unassigned" and self.mode == "live":
            char_ids = [self._node_meta[i][1][0] for i in ids]
            self._drop_pilots_into_squad(char_ids, dst_squad)

    def _draggable_drag_set(self, pending):
        """(kind, [item_ids]) of the homogeneous draggable items in `pending`,
        or (None, []). Kind is taken from the first item; only same-kind,
        currently-draggable items are kept. Squads drag in template mode only."""
        if not pending:
            return None, []
        metas = [(i, self._node_meta.get(i)) for i in pending]
        metas = [(i, m) for i, m in metas if m]
        if not metas:
            return None, []
        kind = metas[0][1][0]
        if self.mode == "live":
            draggable = {"livepilot", "unassigned"}
        else:
            draggable = {"slot", "unassigned", "squad"}
        if kind not in draggable:
            return None, []
        return kind, [i for i, m in metas if m[0] == kind]

    def _create_drag_ghost(self, kind, ids):
        n = len(ids)
        if n == 1:
            label = self._tree.item(ids[0], "text")
        else:
            noun = {"slot": "slots", "unassigned": "pilots",
                    "squad": "squads"}.get(kind, "items")
            label = f"{n} {noun}"
        self._drag_ghost = tk.Toplevel(self.win)
        self._drag_ghost.overrideredirect(True)
        try:
            self._drag_ghost.attributes("-topmost", True)
            self._drag_ghost.attributes("-alpha", 0.85)
        except tk.TclError:
            pass
        tk.Label(self._drag_ghost, text=label, bg=FG_ACCENT, fg=BG_DARK,
                 font=("Consolas", 9), padx=6, pady=2,
                 relief=tk.SOLID, borderwidth=1).pack()

    def _move_drag_ghost(self, x_root, y_root):
        if self._drag_ghost is not None:
            try:
                self._drag_ghost.geometry(f"+{x_root + 12}+{y_root + 10}")
            except tk.TclError:
                pass

    def _destroy_drag_ghost(self):
        if getattr(self, "_drag_ghost", None) is not None:
            try:
                self._drag_ghost.destroy()
            except tk.TclError:
                pass
            self._drag_ghost = None

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

    def _drop_squads_into_wing(self, squad_paths, wing_path):
        twi = wing_path[0]
        paths = [p for p in squad_paths if p[0] != twi]   # skip squads already there
        if not paths:
            return
        self._push_undo()
        t = self.current_template()
        squads = [t.wings[wi].squads[si] for (wi, si) in paths]   # capture by ref first
        for (wi, si) in sorted(paths, reverse=True):              # delete high→low
            del t.wings[wi].squads[si]
        t.wings[twi].squads.extend(squads)
        self._after_structure_change()

    def _drop_slots_into_squad(self, slot_paths, squad_path):
        if not slot_paths:
            return
        self._push_undo()
        t = self.current_template()
        slots = [t.wings[wi].squads[si].slots[li] for (wi, si, li) in slot_paths]
        for (wi, si, li) in sorted(slot_paths, reverse=True):
            del t.wings[wi].squads[si].slots[li]
        tgt = t.wings[squad_path[0]].squads[squad_path[1]]
        tgt.slots.extend(slots)
        self._after_structure_change()

    def _drop_pilots_into_squad(self, char_ids, squad_path):
        self._ensure_executor()
        t = self.current_template()
        wing = t.wings[squad_path[0]]
        squad = wing.squads[squad_path[1]]
        # Build live id maps (drag targets concrete existing wings/squads).
        wing_ids = {fleet_esi.clamp_name(w["name"]): w["id"]
                    for w in self._live_structure.get("wings", [])}
        squad_ids = {(fleet_esi.clamp_name(w["name"]),
                      fleet_esi.clamp_name(s["name"])): s["id"]
                     for w in self._live_structure.get("wings", [])
                     for s in w.get("squads", [])}
        moves = [fleet_composer.Move(pilot_id=cid,
                                     pilot_name=self._name_of(cid),
                                     target_wing_name=wing.name,
                                     target_squad_name=squad.name,
                                     target_role=self._current_role_of(cid))
                 for cid in char_ids]
        self._pin_dragged(char_ids)
        self._enqueue_moves_sync(moves, wing_ids, squad_ids, "drag")

    def _name_of(self, cid):
        return next((m["name"] for m in self._live_members
                     if m["character_id"] == cid), str(cid))

    def _live_drop_pilots(self, char_ids, *, wing_id, squad_id):
        self._ensure_executor()
        # The drop target ids are already concrete; build a trivial id map so
        # _enqueue_moves_sync resolves them by the live wing/squad names.
        wname = next((w["name"] for w in self._live_structure.get("wings", [])
                      if w["id"] == wing_id), None)
        sname = next((s["name"]
                      for w in self._live_structure.get("wings", [])
                      if w["id"] == wing_id
                      for s in w.get("squads", [])
                      if s["id"] == squad_id), None)
        wkey = fleet_esi.clamp_name(wname) if wname is not None else None
        skey = fleet_esi.clamp_name(sname) if sname is not None else None
        wing_ids = {wkey: wing_id} if wkey is not None else {}
        squad_ids = {(wkey, skey): squad_id} if skey is not None else {}
        moves = [fleet_composer.Move(pilot_id=cid,
                                     pilot_name=self._name_of(cid),
                                     target_wing_name=wname,
                                     target_squad_name=sname,
                                     target_role=self._current_role_of(cid))
                 for cid in char_ids]
        self._pin_dragged(char_ids)
        self._enqueue_moves_sync(moves, wing_ids, squad_ids, "drag")

    def _pin_dragged(self, char_ids):
        """Record each dragged pilot's current ship as a pin (pilot -> ship_type_id)
        so sync-time ship changes / leaves can clear it. Spec §5 pins."""
        for cid in char_ids:
            m = next((x for x in self._live_members
                      if x["character_id"] == cid), None)
            if m is not None and m.get("ship_type_id"):
                self._pins[cid] = m["ship_type_id"]
        self._refresh_pins_button()

    def _flash_reject(self, item):
        if not item:
            return
        self._status.config(text="Invalid drop target.", fg=FG_YELLOW)

    def _build_rules_tab(self):
        top = tk.Frame(self._rules_tab, bg=BG_PANEL)
        top.pack(fill=tk.X)
        ttk.Button(top, text="+ Add Rule", style="Dark.TButton",
                   command=self._add_rule).pack(side=tk.LEFT, padx=6, pady=4)
        ttk.Button(top, text="Test Rules", style="Dark.TButton",
                   command=self._test_rules).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Capitals →", style="Dark.TButton",
                   command=lambda: self._open_quick_add("capital")).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Subcaps →", style="Dark.TButton",
                   command=lambda: self._open_quick_add("subcap")).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Class… →", style="Dark.TButton",
                   command=lambda: self._open_quick_add("ship_class")).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Tag… →", style="Dark.TButton",
                   command=lambda: self._open_quick_add("doctrine_tag")).pack(side=tk.LEFT, padx=2)
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

        cval = ttk.Combobox(row, width=16,
                            values=self._condition_values(rule.condition.type))
        cval.set(rule.condition.value)
        cval.pack(side=tk.LEFT, padx=1)

        def _apply_value_state():
            if ctype.get() in VALUELESS_CONDITIONS:
                cval.set("")
                cval.configure(state="disabled")
            else:
                cval.configure(state="normal")
        _apply_value_state()

        def _on_ctype(_e):
            self._update_rule(idx, ctype=ctype.get())
            cval.configure(values=self._condition_values(ctype.get()))
            _apply_value_state()
        ctype.bind("<<ComboboxSelected>>", _on_ctype)

        def _on_cval_key(_e):
            if ctype.get() == "ship_type":
                cval.configure(values=self._ship_type_suggestions(cval.get()))
        cval.bind("<KeyRelease>", _on_cval_key)
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
        if ctype == "ship_class":
            present = sorted({c for c in self._live_ship_classes() if c})
            merged = list(dict.fromkeys(present + COMMON_SHIP_CLASSES))
            return merged
        return []   # ship_type (autocomplete via <KeyRelease>); valueless types

    def _live_ship_classes(self):
        """Ship-class labels present among the last-synced live members."""
        members = getattr(self, "_live_members", None) or []
        return [m.get("ship_class") for m in members]

    def _ship_type_suggestions(self, prefix):
        catalog = getattr(self.fittings, "catalog", None)
        if catalog is None:
            return []
        try:
            return catalog.search_prefix(prefix, limit=20)
        except Exception:
            return []

    def _add_rule(self):
        self._push_undo()
        t = self.current_template()
        if t is None:
            return
        t.rules.append(AssignmentRule(
            priority=len(t.rules),
            condition=RuleCondition("ship_type", ""),
            action=RuleAction("squad_member", None, None)))
        self._renumber_and_save()

    def _quick_add_rule(self, cond_type, cond_value, wing, squad, role):
        """Append a single-squad routing rule and reload. Pure enough to unit-test
        without driving the picker widgets."""
        self._push_undo()
        t = self.current_template()
        if t is None:
            return
        value = "" if cond_type in VALUELESS_CONDITIONS else (cond_value or "")
        t.rules.append(AssignmentRule(
            priority=len(t.rules),
            condition=RuleCondition(cond_type, value),
            action=RuleAction(role or "squad_member", wing or None, squad or None)))
        self._renumber_and_save()

    def _open_quick_add(self, mode):
        """Open the compact routing picker for one of capital/subcap/ship_class/
        doctrine_tag. Class/Tag modes show a value picker; capital/subcap don't."""
        t = self.current_template()
        if t is None:
            return
        _QuickAddPicker(self.win, mode=mode, window=self)

    def _update_rule(self, idx, *, ctype=None, cval=None, role=None, wing=None, squad=None):
        self._push_undo()
        t = self.current_template()
        if t is None or idx >= len(t.rules):
            return
        r = t.rules[idx]
        if ctype is not None:
            r.condition.type = ctype
            if ctype in VALUELESS_CONDITIONS:
                r.condition.value = ""      # value meaningless for these types
        if cval is not None and r.condition.type not in VALUELESS_CONDITIONS:
            r.condition.value = cval
        if role is not None:
            r.action.role = role
        if wing is not None:
            r.action.wing_name = wing or None
        if squad is not None:
            r.action.squad_name = squad or None
        validate_template(t)
        self.store.save()
        # NOTE: no _reload_rules() here — that rebuilt every row and stole focus.
        # add/delete/reorder still call _reload_rules via their own paths.

    def _move_rule(self, idx, delta):
        self._push_undo()
        t = self.current_template()
        j = idx + delta
        if t is None or not (0 <= j < len(t.rules)):
            return
        t.rules[idx], t.rules[j] = t.rules[j], t.rules[idx]
        self._renumber_and_save()

    def _delete_rule(self, idx):
        self._push_undo()
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

    def _build_settings_tab(self):
        self._settings_vars = {}
        self._settings_spinboxes = {}
        fields = [
            ("sync_active_s", "Active sync interval (s)", 5, 120),
            ("sync_idle_s", "Idle sync interval (s)", 5, 300),
            ("move_spacing_ms", "Spacing between moves (ms)", 100, 5000),
            ("burst_cap", "Moves per burst", 1, 100),
            ("settle_s", "Settle pause after a burst (s)", 1, 30),
            ("bulk_apply_threshold", "Bulk apply warning threshold", 1, 100),
        ]
        for i, (key, label, lo, hi) in enumerate(fields):
            tk.Label(self._settings_tab, text=label, bg=BG_PANEL, fg=FG_TEXT,
                     font=("Consolas", 9)).grid(row=i, column=0, sticky="w",
                                                padx=8, pady=6)
            var = tk.IntVar()
            self._settings_vars[key] = (var, lo, hi)
            sb = tk.Spinbox(self._settings_tab, from_=lo, to=hi, textvariable=var,
                            width=8, bg=BG_ENTRY, fg=FG_TEXT,
                            command=self._on_settings_changed)
            sb.grid(row=i, column=1, padx=8, pady=6)
            sb.bind("<FocusOut>", lambda e: self._on_settings_changed())
            sb.bind("<Return>", lambda e: self._on_settings_changed())
            self._settings_spinboxes[key] = sb
        tk.Label(self._settings_tab,
                 text="Fast bursts are ESI-verified: after each burst the tool "
                      "pauses and re-checks the fleet before continuing.",
                 bg=BG_PANEL, fg=FG_DIM, font=("Consolas", 8),
                 justify=tk.LEFT, wraplength=320).grid(
                     row=len(fields), column=0, columnspan=2,
                     sticky="w", padx=8, pady=8)
        self._reload_settings()

    def _build_members_tab(self):
        top = tk.Frame(self._members_tab, bg=BG_PANEL)
        top.pack(fill=tk.X)
        tk.Label(top, text="Filter:", bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=(6, 2), pady=4)
        self._members_filter_var = tk.StringVar()
        ent = tk.Entry(top, textvariable=self._members_filter_var, width=22,
                       bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_TEXT,
                       font=("Consolas", 9), relief=tk.FLAT)
        ent.pack(side=tk.LEFT, padx=2)
        ent.bind("<KeyRelease>", lambda e: self._refresh_members_tab())

        legend = tk.Frame(self._members_tab, bg=BG_PANEL)
        legend.pack(fill=tk.X, padx=6)
        tk.Label(legend, text="● in position", bg=BG_PANEL, fg=FG_GREEN,
                 font=("Consolas", 8)).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(legend, text="● needs moving", bg=BG_PANEL, fg=FG_YELLOW,
                 font=("Consolas", 8)).pack(side=tk.LEFT)

        wrap = tk.Frame(self._members_tab, bg=BG_PANEL)
        wrap.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        cols = ("pin", "name", "ship", "class", "squad")
        self._members_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                          selectmode="browse")
        for col, head, w in (("pin", "📌", 28), ("name", "Name", 130),
                             ("ship", "Ship", 120), ("class", "Class", 110),
                             ("squad", "Squad", 90)):
            self._members_tree.heading(col, text=head)
            self._members_tree.column(col, width=w,
                                      anchor="center" if col == "pin" else "w")
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL,
                            command=self._members_tree.yview)
        self._members_tree.configure(yscrollcommand=vsb.set)
        self._members_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._members_tree.tag_configure("inpos", foreground=FG_GREEN)
        self._members_tree.tag_configure("moveme", foreground=FG_YELLOW)
        # character_id -> iid so a double-click can select in the left tree.
        self._members_row_cid: dict = {}
        self._members_tree.bind("<Double-Button-1>",
                                self._on_members_double_click)

    def _refresh_members_tab(self):
        tree = getattr(self, "_members_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        self._members_row_cid.clear()
        query = self._members_filter_var.get()
        move_ids = set()
        if self._last_preview is not None:
            move_ids = {mv.pilot_id for mv in self._last_preview.executable}
        # squad_id -> "Wing/Squad" label for the Squad column.
        squad_label = {}
        for w in self._live_structure.get("wings", []):
            for s in w.get("squads", []):
                squad_label[s.get("id")] = f"{w.get('name', '')}/{s.get('name', '')}"
        for m in self._live_members:
            if not member_matches_filter(m, query):
                continue
            cid = m.get("character_id")
            pin = "📌" if cid in self._pins else ""
            tag = "moveme" if cid in move_ids else "inpos"
            iid = tree.insert(
                "", "end", tags=(tag,),
                values=(pin, m.get("name", ""), m.get("ship_type_name", ""),
                        m.get("ship_class") or "",
                        squad_label.get(m.get("squad_id"), "")))
            self._members_row_cid[iid] = cid

    def _on_members_double_click(self, event):
        iid = self._members_tree.identify_row(event.y)
        cid = self._members_row_cid.get(iid)
        if cid is None:
            return
        for node, meta in self._node_meta.items():
            if meta[0] == "livepilot" and meta[1] == cid:
                self._tree.selection_set(node)
                self._tree.see(node)
                break

    def _build_log_tab(self):
        wrap = tk.Frame(self._log_tab, bg=BG_PANEL)
        wrap.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self._log_text = tk.Text(wrap, bg=BG_ENTRY, fg=FG_TEXT,
                                 font=("Consolas", 9), relief=tk.FLAT,
                                 wrap="word", state="disabled")
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=vsb.set)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        btns = tk.Frame(self._log_tab, bg=BG_PANEL)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Clear", style="Dark.TButton",
                   command=self._clear_log).pack(side=tk.LEFT, padx=6, pady=4)

    def _clear_log(self):
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", tk.END)
        self._log_text.configure(state="disabled")

    def _reload_settings(self):
        t = self.current_template()
        if t is None:
            return
        s = t.settings
        for key, (var, _lo, _hi) in self._settings_vars.items():
            var.set(getattr(s, key))

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

    def ship_class_label(self, type_id):
        """Human group name for a hull (for ship_class rule conditions)."""
        if not type_id:
            return None
        try:
            import ship_classes
            return ship_classes.get_group_name(type_id)
        except Exception:
            return None

    def _enrich_members(self, raw_members):
        """ESI member dicts → composer-shaped dicts. Runs on the background sync
        worker (see _sync_live), so the name/ship-class resolution here may hit
        ESI without blocking the UI thread."""
        from zkill_monitor import resolve_name
        import ship_classes
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
                "is_capital": ship_classes.is_capital(tid) if tid else False,
                "role": m.get("role", "squad_member"),
                "wing_id": m.get("wing_id"),
                "squad_id": m.get("squad_id"),
                "join_time": m.get("join_time") or "",
            })
        return out

    def _enter_live_mode(self):
        """Begin the switch to live mode. The fleet-boss check
        (_fleet_info_provider) is a SYNCHRONOUS ESI round-trip, so run it on a
        daemon worker and finish the switch from the _post()'d continuation.
        Mode stays 'template' until the check confirms boss status, so a failed
        check requires no visible revert. Re-entrancy-guarded so rapid
        double-clicks of the toggle don't stack workers."""
        if self._entering_live:
            return
        self._entering_live = True
        self._status.config(text="Checking fleet…", fg=FG_DIM)

        def worker():
            # _fleet_info_provider() does real ESI I/O and can raise (network
            # errors are the normal environment here). If it does, marshal the
            # failure back so the guard is always cleared and the UI reverts —
            # otherwise _entering_live would leak True forever and lock out every
            # future live-mode entry.
            try:
                info = self._fleet_info_provider()
            except Exception as e:
                self._post(self._enter_live_failed, e)
                return
            self._post(self._finish_enter_live, info)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_enter_live(self, info):
        """Tk-thread continuation of _enter_live_mode once fleet info is known."""
        # Clear the re-entrancy guard FIRST so that even if the continuation below
        # raises, a leaked guard can never permanently block live-mode entry.
        self._entering_live = False
        if self.mode != "template":
            # Something else changed the mode while the check was in flight;
            # abandon this (stale) entry attempt.
            return
        if not info or not info.get("is_boss"):
            messagebox.showwarning(
                "Live mode unavailable",
                "The selected FC character must be the current fleet boss to "
                "read and manage fleet structure.", parent=self.win)
            # Mode was never flipped, so just restore the resting template UI.
            self._apply_mode_ui("template")
            self._refresh_add_buttons()
            return
        self._apply_mode_ui("live")
        self._fleet_id = info["fleet_id"]
        self._ensure_executor()
        self._sync_live(initial=True)
        self._schedule_sync()
        self._refresh_add_buttons()

    def _enter_live_failed(self, exc):
        """Tk-thread continuation when the async fleet-boss check RAISED (real ESI
        I/O — network errors are the normal environment). Clears the re-entrancy
        guard so live mode can be retried, reverts to the resting template UI (the
        same path the not-boss branch uses), and surfaces the failure instead of
        leaving the status stuck on 'Checking fleet…'."""
        self._entering_live = False
        # Log first, then set the red status — _log_line writes a FG_DIM status as
        # a side effect, so setting the red status afterward keeps it visible.
        msg = f"Fleet check failed: {exc}"
        if len(msg) > 120:
            msg = msg[:117] + "…"
        try:
            self._log_line(msg)
        except Exception:
            pass
        if self.mode == "template":
            # Never flipped to live; restore the resting template UI (same revert
            # the not-boss branch performs).
            self._apply_mode_ui("template")
            self._refresh_add_buttons()
        try:
            self._status.config(text=msg, fg=FG_RED)
        except tk.TclError:
            pass

    def _exit_live_mode(self):
        if self._sync_after_id:
            try:
                self.win.after_cancel(self._sync_after_id)
            except Exception:
                pass
            self._sync_after_id = None
        self._reload_tree()   # revert to stored template

    def _sync_delay_ms(self):
        import time
        t = self.current_template()
        active = t.settings.sync_active_s if t else 10
        idle = t.settings.sync_idle_s if t else 30
        recent = (time.time() - self._last_write_wall) < 60
        return (active if (self._auto_sort_on or recent) else idle) * 1000

    def _schedule_sync(self):
        # Active cadence (sync_active_s) when auto-sort is on or a write is
        # recent (<60s); otherwise the idle cadence (sync_idle_s). Spec §5.
        self._sync_after_id = self.win.after(self._sync_delay_ms(), self._sync_tick)

    def _post(self, fn, *args):
        """Schedule fn on the Tk thread from a worker; ignore the teardown race
        when a daemon worker outlives the GUI. Tk raises TclError (widget gone)
        or RuntimeError ('main thread is not in main loop') during shutdown —
        both mean 'the window is gone', so both are safe to swallow here.

        Prefers the host GUI's shared main-thread UI dispatcher (``_post_ui``)
        when available so workers never touch Tcl off the main thread (safe even
        under an update()-pumped host). Falls back to the hardened ``win.after``
        schedule when constructed without a host (tests use a bare Tk root)."""
        post = getattr(self._host, "_post_ui", None) if self._host is not None else None
        if callable(post):
            post(fn, *args)
            return
        try:
            self.win.after(0, fn, *args)
        except (tk.TclError, RuntimeError):
            pass

    def _sync_tick(self):
        if self.mode != "live":
            return
        self._sync_live(initial=False)
        self._schedule_sync()

    def _sync_live(self, *, initial):
        self._sync_generation += 1
        generation = self._sync_generation

        def worker():
            session = self._esi_session_provider()   # blocking get_fleet_info — off the Tk thread
            if session is None:
                self._post(lambda: self._status.config(
                    text="No fleet-boss session.", fg=FG_RED))
                return
            err = None
            structure, members = {"wings": []}, []
            try:
                structure = {"wings": fleet_esi.get_wings(session, self._fleet_id)}
                # Enrich (name + ship_class) here, off the Tk thread —
                # ship_class_label may hit ESI on cache-miss.
                members = self._enrich_members(self._read_members(session))
            except fleet_esi.FleetESIError as e:
                err = e
            self._post(self._apply_sync_result, generation, structure, members, err)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_sync_result(self, generation, structure, members, err):
        """Tk-thread-only snapshot swap, guarded by the generation counter.
        Returns True if applied, False if a newer sync superseded this one."""
        if generation != self._sync_generation:
            return False
        if err is not None:
            self._status.config(text=f"Sync failed: {err.reason}", fg=FG_RED)
            return False
        events = self._diff_members(self._prev_members, members)
        self._apply_member_diff(events)
        self._live_structure = structure
        self._live_members = members          # already enriched on the worker
        self._prev_members = members
        self._status.config(text="● Synced", fg=FG_GREEN)
        self._reload_tree()
        self._refresh_members_tab()
        self._on_sync_complete(events)   # Task 7: auto-sort tick hook
        return True

    def _on_sync_complete(self, events):
        if self._auto_sort_on and self.mode == "live":
            self._auto_sort_tick()

    def _attach_tooltip(self, widget, text):
        """Hover tooltip via the shared ui_helpers helper (D9 dedupe — the shared
        impl carries the <Destroy> cleanup this window's bespoke copy lacked)."""
        return attach_tooltip(widget, text)

    def _toggle_auto_sort(self):
        if self.mode != "live":
            return
        self._auto_sort_on = not self._auto_sort_on
        self._auto_sort_btn.config(
            text=f"Auto-move: {'ON' if self._auto_sort_on else 'OFF'}")
        if self._auto_sort_on:
            # Re-cadence to active immediately.
            self._sync_tick_now()

    def _auto_sort_tick(self):
        t = self.current_template()
        if t is None:
            return
        res = fleet_composer.compose(
            t, self._live_members, self._live_structure,
            doctrine=self._doctrine_provider(), fittings=self.fittings,
            passes=(1, 2))
        self._ensure_executor()
        # id -> (wing, squad) live-name lookup for concrete ids.
        wing_ids = {fleet_esi.clamp_name(w["name"]): w["id"]
                    for w in self._live_structure.get("wings", [])}
        squad_ids = {(fleet_esi.clamp_name(w["name"]),
                      fleet_esi.clamp_name(s["name"])): s["id"]
                     for w in self._live_structure.get("wings", [])
                     for s in w.get("squads", [])}
        import time
        enqueued = 0
        for mv in res.executable:              # already-correct already filtered
            if mv.pilot_id in self._pins:      # never move a pinned pilot
                continue
            # C3-04 dedup (auto-sort only): a pilot whose previous auto-sort job
            # is still queued or in flight must NOT be re-enqueued this tick — the
            # burst-boundary re-verify used to be the only dedup, so a still-queued
            # pilot got a fresh duplicate ESI write billed against the token budget
            # on every ~10s sync tick. Manual drag/menu/apply moves bypass this
            # entirely (they call submit() directly); a re-drag to a NEW squad must
            # still supersede. Self-healing: once the pending job clears, a
            # still-misplaced pilot is re-enqueued on the next tick — no starvation.
            if self._executor.has_pending(mv.pilot_id):
                continue
            wkey = (fleet_esi.clamp_name(mv.target_wing_name)
                    if mv.target_wing_name else None)
            wid = wing_ids.get(wkey) if wkey else None
            sid = squad_ids.get((wkey, fleet_esi.clamp_name(mv.target_squad_name))) \
                if mv.target_squad_name and wid is not None else None
            self._executor.submit(MoveJob(
                pilot_id=mv.pilot_id, pilot_name=mv.pilot_name,
                wing_id=wid, squad_id=sid, role=mv.target_role, source="autosort"))
            self._log_line(f"[autosort] queue {mv.pilot_name} → "
                           f"{mv.target_wing_name}/{mv.target_squad_name}")
            enqueued += 1
        if enqueued:
            self._last_write_wall = time.time()

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

    def _menu_move_pilot(self, char_id, wing_id, squad_id):
        """Live-mode right-click move: enqueue a source='menu' job and pin the
        pilot (drag-wins). Ids are already concrete (from the fleet snapshot)."""
        import time
        self._ensure_executor()
        role = self._current_role_of(char_id)
        self._executor.submit(MoveJob(
            pilot_id=char_id, pilot_name=self._name_of(char_id),
            wing_id=wing_id, squad_id=squad_id, role=role, source="menu"))
        self._pin_dragged([char_id])
        self._log_line(f"[menu] queue {self._name_of(char_id)} → "
                       f"wing {wing_id}/squad {squad_id}")
        self._last_write_wall = time.time()
        self._schedule_reconcile_poll()

    def _apply(self):
        if self.mode != "live":
            return
        # _esi_session_provider() does a SYNCHRONOUS ESI round-trip (get_fleet_info
        # + a possible token refresh), so fetch it off the Tk thread and open the
        # preview from the _post()'d continuation. Keeps the UI responsive.
        self._status.config(text="Checking fleet…", fg=FG_DIM)

        def worker():
            # _esi_session_provider() does real ESI I/O and can raise; surface the
            # failure rather than swallowing it and leaving the status stuck on
            # "Checking fleet…" with no preview and no error.
            try:
                session = self._esi_session_provider()
            except Exception as e:
                self._post(self._apply_failed, e)
                return
            self._post(self._apply_with_session, session)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_failed(self, exc):
        """Tk-thread continuation when _apply's async session fetch RAISED.
        Surfaces the error in the status line (no dialog, no state to unwind)."""
        msg = f"Fleet check failed: {exc}"
        if len(msg) > 120:
            msg = msg[:117] + "…"
        try:
            self._log_line(msg)
        except Exception:
            pass
        try:
            self._status.config(text=msg, fg=FG_RED)
        except tk.TclError:
            pass

    def _apply_with_session(self, session):
        """Tk-thread continuation of _apply once the boss session is known.
        Identical to the old inline post-fetch logic."""
        if self.mode != "live":
            return
        if session is None:
            messagebox.showwarning(
                "Apply", "No fleet-boss session — you must be the current fleet "
                "boss with the fleet-write scope (re-authenticate if your token "
                "is old).", parent=self.win)
            self._status.config(text="No fleet-boss session.", fg=FG_RED)
            return
        res = self._compose_preview()
        if res is None:
            return
        rows = build_apply_preview_rows(res, self._live_members)
        t = self.current_template()
        threshold = t.settings.bulk_apply_threshold
        big_warning = ""
        if len(rows["moves"]) > threshold:
            big_warning = (f"Large apply: {len(rows['moves'])} moves "
                           f"(> {threshold}). This will take "
                           f"~{len(rows['moves']) * 0.5:.0f}s.")

        def _confirm():
            self._execute_moves(session, res.executable, materialize_template=True)
            self._pins.clear()
            self._refresh_pins_button()

        _ApplyPreviewDialog(self.win, rows, big_warning, on_confirm=_confirm)

    def _import_live_as_template(self):
        """Build a NEW template from the current live fleet, pin every member,
        select it, and switch to template mode (spec ask 4). Requires a live
        snapshot (live mode with members synced)."""
        if self.mode != "live" or not self._live_structure.get("wings"):
            self._status.config(text="Import needs a synced live fleet.",
                                fg=FG_YELLOW)
            return
        from fleet_template_store import build_template_from_live
        t = build_template_from_live(self._live_members, self._live_structure,
                                     now=datetime.now())
        self.store.templates.append(t)
        # Pin every member that became a slot (id -> current ship_type_id).
        for m in self._live_members:
            cid = m.get("character_id")
            tid = m.get("ship_type_id")
            if isinstance(cid, int) and tid:
                self._pins[cid] = tid
        self.store.save()
        self._current_template_id = t.id
        self._refresh_template_selector()
        self.set_mode("template")            # exits live, reloads the stored tree
        self._refresh_pins_button()
        self._status.config(text=f"Imported '{t.name}'.", fg=FG_GREEN)

    def _ensure_executor(self):
        if self._executor is not None:
            return
        import time
        from fleet_executor import FleetExecutor, FleetTokenLedger
        t = self.current_template()
        s = t.settings if t is not None else None
        self._ledger = FleetTokenLedger()
        # autostart defaults True → the single persistent worker thread is
        # launched here (parks on a blocking queue.get until submit()/stop()).
        # session=None on purpose: _esi_session_provider() does a SYNCHRONOUS
        # ESI round-trip and _ensure_executor runs on the Tk thread. The
        # executor never needs a session before the first move — _executor_on_move
        # fetches a fresh one on the worker and re-points self._executor.session
        # for every write, and FleetExecutor's header reads use
        # `getattr(self.session, "last_headers", {}) or {}`, which is None-safe.
        self._executor = FleetExecutor(
            session=None,
            on_move=self._executor_on_move,
            post=self._post,
            sleep=time.sleep,
            ledger=self._ledger,
            move_spacing_ms=(s.move_spacing_ms if s else 400),
            burst_cap=(s.burst_cap if s else 25),
            settle_s=(s.settle_s if s else 3),
            on_log=self._log_line,
            on_repoll=lambda: self._post(self._sync_tick_now),
            remaining_needed=self._job_still_needed,
            on_boss_lost=lambda: self._post(self._on_boss_lost))

    def _executor_on_move(self, job):
        session = self._esi_session_provider()
        if session is None:
            raise fleet_esi.FleetESIError("no_token")
        # Point the executor at the freshest session each write.
        self._executor.session = session
        fleet_esi.move_member(session, self._fleet_id, job.pilot_id,
                              wing_id=job.wing_id, squad_id=job.squad_id,
                              role=job.role)
        return 204

    def _job_still_needed(self, job):
        """After a burst re-poll, is this pilot still not where the job wants?"""
        m = next((x for x in self._live_members
                  if x["character_id"] == job.pilot_id), None)
        if m is None:
            return False
        return not (m.get("wing_id") == job.wing_id
                    and m.get("squad_id") == job.squad_id
                    and m.get("role") == job.role)

    def _sync_tick_now(self):
        if self.mode == "live":
            self._sync_live(initial=False)

    def _on_boss_lost(self):
        self._status.config(text="Boss lost — fleet ops aborted.", fg=FG_RED)

    def _log_line(self, line):
        import time
        stamped = f"{time.strftime('%H:%M:%S')} {line}"
        self._log_buffer.append(stamped)
        try:
            self._status.config(text=line, fg=FG_DIM)
        except tk.TclError:
            pass
        log_text = getattr(self, "_log_text", None)
        if log_text is not None:
            try:
                log_text.configure(state="normal")
                log_text.insert(tk.END, stamped + "\n")
                # Bound to the deque size (drop oldest visible lines).
                excess = int(log_text.index("end-1c").split(".")[0]) - \
                    self._log_buffer.maxlen
                if excess > 0:
                    log_text.delete("1.0", f"{excess + 1}.0")
                log_text.see(tk.END)
                log_text.configure(state="disabled")
            except tk.TclError:
                pass

    def _current_role_of(self, cid):
        m = next((x for x in self._live_members if x["character_id"] == cid), None)
        return (m.get("role") if m else None) or "squad_member"

    def _execute_moves(self, session, moves, *, materialize_template=True):
        """Apply path: create structure on a worker, then enqueue MoveJobs.

        (Drag/drop does NOT use this — it enqueues synchronously, see
        _enqueue_moves_sync — because its target ids are already known.)"""
        self._ensure_executor()
        fleet_id = self._fleet_id
        t = self.current_template()

        # Structure creation (Apply only) still happens up front on a worker so
        # the executor jobs carry concrete wing/squad ids.
        def prep():
            try:
                if materialize_template and t is not None:
                    wanted = [(w.name, [s.name for s in w.squads]) for w in t.wings]
                    wing_ids, squad_ids = fleet_esi.ensure_structure(
                        session, fleet_id, wanted,
                        self._live_structure.get("wings", []))
                else:
                    wing_ids = {fleet_esi.clamp_name(w["name"]): w["id"]
                                for w in self._live_structure.get("wings", [])}
                    squad_ids = {(fleet_esi.clamp_name(w["name"]),
                                  fleet_esi.clamp_name(s["name"])): s["id"]
                                 for w in self._live_structure.get("wings", [])
                                 for s in w.get("squads", [])}
            except fleet_esi.FleetESIError as e:
                self._post(lambda: self._status.config(
                    text=f"Apply error: {e.reason}", fg=FG_RED))
                return
            self._post(self._enqueue_moves_sync, moves, wing_ids, squad_ids, "apply")

        threading.Thread(target=prep, daemon=True).start()

    def _enqueue_moves_sync(self, moves, wing_ids, squad_ids, source):
        """Build + submit MoveJobs on the Tk thread. Used directly by drag/drop
        (ids known up front) and by the Apply worker's _post callback."""
        import time
        for mv in moves:
            wkey = (fleet_esi.clamp_name(mv.target_wing_name)
                    if mv.target_wing_name is not None else None)
            wing_id = wing_ids.get(wkey) if wkey is not None else None
            squad_id = None
            if mv.target_squad_name is not None and wing_id is not None:
                squad_id = squad_ids.get(
                    (wkey, fleet_esi.clamp_name(mv.target_squad_name)))
            self._executor.submit(MoveJob(
                pilot_id=mv.pilot_id, pilot_name=mv.pilot_name,
                wing_id=wing_id, squad_id=squad_id, role=mv.target_role,
                source=source))
        self._last_write_wall = time.time()   # go active-cadence after a write
        self._schedule_reconcile_poll()

    def _schedule_reconcile_poll(self):
        """Fire one sync ~(settle_s + 3)s after enqueue so the tree reflects the
        drained queue (replaces the old 45s move cooldown)."""
        t = self.current_template()
        settle = t.settings.settle_s if t is not None else 3
        delay_ms = (settle + 3) * 1000
        try:
            self.win.after(delay_ms, self._sync_tick_now)
        except tk.TclError:
            pass

class SlotEditor:
    """Modal: edit a slot's type (named/role/generic), tag, and role."""
    def __init__(self, parent, slot, fittings, character_names, *, on_ok,
                 resolve_names=None, store=None, post=None):
        self.slot = slot
        self.on_ok = on_ok
        self._resolve_names = resolve_names
        self._store = store
        self._post = post or (lambda fn, *a: fn(*a))
        self._warning_shown = False
        self._pending_char = None
        self.win = tk.Toplevel(parent)
        self.win.title("Edit Slot")
        make_modal(self.win, parent, base_bg=BG_PANEL)

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

        self._warning = tk.Label(self.win, text="", bg=BG_PANEL, fg=FG_YELLOW,
                                 font=("Consolas", 8))
        self._warning.grid(row=4, column=0, columnspan=2, sticky="w", padx=6)

        btns = tk.Frame(self.win, bg=BG_PANEL)
        btns.grid(row=5, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="OK", style="Dark.TButton",
                   command=self._ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Save anyway", style="Dark.TButton",
                   command=self._save_anyway).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=self.win.destroy).pack(side=tk.LEFT, padx=4)

    def _ok(self):
        char = self._char.get().strip()
        tag = self._tag.get().strip()
        self.slot.tag = (tag or None) if not char else None
        self.slot.role = self._role.get() or "squad_member"
        # Generic slot or a name that already carries an id → commit immediately.
        if not char:
            self.slot.character = None
            self.slot.character_id = None
            self._commit()
            return
        if self.slot.character and self.slot.character.strip().lower() == char.lower() \
                and self.slot.character_id:
            self.slot.character = char
            self._commit()
            return
        # Already-cached id?
        cached = self._store.cached_id(char) if self._store is not None else None
        if isinstance(cached, int):
            self.slot.character = char
            self.slot.character_id = cached
            self._commit()
            return
        # Otherwise resolve in the background; do NOT block saving.
        if self._resolve_names is None:
            self.slot.character = char
            self.slot.character_id = None
            self._commit()
            return
        self._pending_char = char
        self._warning.config(text="Resolving…", fg=FG_DIM)

        def worker():
            found = (self._resolve_names([char]) or {}).get(char.lower())
            self._post(self._after_resolve, char, found)

        threading.Thread(target=worker, daemon=True).start()

    def _after_resolve(self, char, cid):
        if isinstance(cid, int):
            self.slot.character = char
            self.slot.character_id = cid
            if self._store is not None:
                self._store.cache_character(char, cid)
            self._commit()
            return
        # Not found — inline warning; the FC decides Save anyway / Cancel.
        self._warning_shown = True
        self._warning.config(text="Name not found on ESI.", fg=FG_YELLOW)

    def _save_anyway(self):
        char = (self._pending_char or self._char.get().strip())
        self.slot.character = char or None
        self.slot.character_id = None
        if char and self._store is not None:
            self._store.cache_character(char, None)   # cache name for autocomplete
        self._commit()

    def _commit(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.on_ok()


class _AddMyCharsDialog:
    """Themed checkbox list of authed character names -> on_ok([checked names])."""
    def __init__(self, parent, names, *, on_ok):
        self.on_ok = on_ok
        self.win = tk.Toplevel(parent)
        self.win.title("Add my characters")
        make_modal(self.win, parent, base_bg=BG_PANEL)
        tk.Label(self.win, text="Select characters to add:", bg=BG_PANEL,
                 fg=FG_TEXT, font=("Consolas", 9)).pack(anchor="w", padx=8, pady=(8, 2))
        self._vars = {}
        body = tk.Frame(self.win, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=2)
        for name in names:
            v = tk.BooleanVar(value=False)
            self._vars[name] = v
            tk.Checkbutton(body, text=name, variable=v, bg=BG_PANEL, fg=FG_TEXT,
                           selectcolor=BG_ENTRY, activebackground=BG_PANEL,
                           activeforeground=FG_TEXT, font=("Consolas", 9),
                           anchor="w").pack(fill=tk.X, anchor="w")
        btns = tk.Frame(self.win, bg=BG_PANEL)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="Add", style="Dark.TButton",
                   command=self._ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=self.win.destroy).pack(side=tk.LEFT, padx=2)

    def _ok(self):
        chosen = [n for n, v in self._vars.items() if v.get()]
        self.win.destroy()
        self.on_ok(chosen)


class _BulkAddDialog:
    """Themed multiline dialog: paste one pilot name per line, OK -> on_ok(text)."""
    def __init__(self, parent, *, on_ok):
        self.on_ok = on_ok
        self.win = tk.Toplevel(parent)
        self.win.title("Add pilots from list")
        make_modal(self.win, parent, base_bg=BG_PANEL)
        tk.Label(self.win, text="One pilot name per line:", bg=BG_PANEL,
                 fg=FG_TEXT, font=("Consolas", 9)).pack(anchor="w", padx=8, pady=(8, 2))
        self._text = tk.Text(self.win, width=36, height=12, bg=BG_ENTRY,
                             fg=FG_TEXT, insertbackground=FG_TEXT,
                             font=("Consolas", 9), relief=tk.FLAT)
        self._text.pack(fill=tk.BOTH, expand=True, padx=8, pady=2)
        btns = tk.Frame(self.win, bg=BG_PANEL)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="OK", style="Dark.TButton",
                   command=self._ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=self.win.destroy).pack(side=tk.LEFT, padx=2)
        self._text.focus_set()

    def _ok(self):
        text = self._text.get("1.0", tk.END)
        self.win.destroy()
        self.on_ok(text)


class _UnresolvedDialog:
    """Themed result dialog listing names ESI could not resolve, with
    [Add anyway] (create unvalidated slots) / [Skip]."""
    def __init__(self, parent, unresolved, *, on_add_anyway, on_skip):
        self.on_add_anyway = on_add_anyway
        self.on_skip = on_skip
        self.win = tk.Toplevel(parent)
        self.win.title("Unresolved names")
        # Escape == Skip (the non-destructive dismissal; runs on_skip like Skip).
        make_modal(self.win, parent, on_cancel=self._skip, base_bg=BG_PANEL)
        tk.Label(self.win,
                 text=f"{len(unresolved)} name(s) not found on ESI:",
                 bg=BG_PANEL, fg=FG_YELLOW, font=("Consolas", 9)).pack(
                     anchor="w", padx=8, pady=(8, 2))
        box = tk.Text(self.win, width=32, height=min(10, max(3, len(unresolved))),
                     bg=BG_ENTRY, fg=FG_TEXT, font=("Consolas", 9), relief=tk.FLAT)
        box.pack(fill=tk.BOTH, expand=True, padx=8, pady=2)
        box.insert("1.0", "\n".join(unresolved))
        box.configure(state="disabled")
        btns = tk.Frame(self.win, bg=BG_PANEL)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="Add anyway", style="Dark.TButton",
                   command=self._add).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Skip", style="Dark.TButton",
                   command=self._skip).pack(side=tk.LEFT, padx=2)

    def _add(self):
        self.win.destroy()
        self.on_add_anyway()

    def _skip(self):
        self.win.destroy()
        self.on_skip()


class _QuickAddPicker:
    """Compact modal: choose destination wing+squad+role (and, for class/tag
    modes, the class or tag), then create one routing rule via the window's
    _quick_add_rule."""

    _MODE_LABEL = {"capital": "Route Capitals", "subcap": "Route Subcaps",
                   "ship_class": "Route Ship Class", "doctrine_tag": "Route Tag"}

    def __init__(self, parent, *, mode, window):
        self.mode = mode
        self.window = window
        self.win = tk.Toplevel(parent)
        self.win.title(self._MODE_LABEL.get(mode, "Route"))
        make_modal(self.win, parent, base_bg=BG_PANEL)

        row = 0
        self._value = None
        if mode in ("ship_class", "doctrine_tag"):
            label = "Ship class:" if mode == "ship_class" else "Doctrine tag:"
            tk.Label(self.win, text=label, bg=BG_PANEL, fg=FG_TEXT,
                     font=("Consolas", 9)).grid(row=row, column=0, sticky="w",
                                                padx=6, pady=4)
            values = (window._condition_values("ship_class") if mode == "ship_class"
                      else list(getattr(window.fittings, "tags", [])))
            self._value = ttk.Combobox(self.win, values=values, width=24)
            self._value.grid(row=row, column=1, padx=6, pady=4)
            row += 1

        tk.Label(self.win, text="Wing:", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).grid(row=row, column=0, sticky="w", padx=6, pady=4)
        self._wing = ttk.Combobox(self.win, values=window._wing_names(), width=24,
                                  state="readonly")
        self._wing.grid(row=row, column=1, padx=6, pady=4)
        self._wing.bind("<<ComboboxSelected>>", lambda e: self._sync_squads())
        row += 1

        tk.Label(self.win, text="Squad:", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).grid(row=row, column=0, sticky="w", padx=6, pady=4)
        self._squad = ttk.Combobox(self.win, values=window._squad_names(), width=24,
                                   state="readonly")
        self._squad.grid(row=row, column=1, padx=6, pady=4)
        row += 1

        tk.Label(self.win, text="Role:", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).grid(row=row, column=0, sticky="w", padx=6, pady=4)
        self._role = ttk.Combobox(self.win, values=ROLE_VALUES, width=24,
                                  state="readonly")
        self._role.set("squad_member")
        self._role.grid(row=row, column=1, padx=6, pady=4)
        row += 1

        btns = tk.Frame(self.win, bg=BG_PANEL)
        btns.grid(row=row, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="Create rule", style="Dark.TButton",
                   command=self._ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=self.win.destroy).pack(side=tk.LEFT, padx=4)

    def _sync_squads(self):
        # Restrict the squad list to the chosen wing's squads.
        t = self.window.current_template()
        wname = self._wing.get()
        squads = [""]
        for w in (t.wings if t else []):
            if w.name == wname:
                squads += [s.name for s in w.squads]
        self._squad.configure(values=squads)

    def _ok(self):
        value = self._value.get().strip() if self._value is not None else ""
        wing = self._wing.get().strip()
        squad = self._squad.get().strip()
        role = self._role.get() or "squad_member"
        if not wing or not squad:
            messagebox.showwarning("Route", "Pick a wing and a squad.",
                                   parent=self.win)
            return
        cond_type = self.mode
        self.window._quick_add_rule(cond_type, value, wing, squad, role)
        self.win.destroy()


class _ApplyPreviewDialog:
    """Themed modal listing every move + unassigned + warnings; Confirm/Cancel.
    Confirm runs on_confirm(); the itemized list is bounded and scrollable."""

    def __init__(self, parent, rows, big_warning, *, on_confirm):
        self.on_confirm = on_confirm
        self.win = tk.Toplevel(parent)
        self.win.title("Apply preview")
        make_modal(self.win, parent, base_bg=BG_PANEL)

        moves = rows.get("moves", [])
        tk.Label(self.win, text=f"{len(moves)} move(s):", bg=BG_PANEL,
                 fg=FG_TEXT, font=("Consolas", 9, "bold")).pack(
                     anchor="w", padx=8, pady=(8, 2))

        wrap = tk.Frame(self.win, bg=BG_PANEL)
        wrap.pack(fill=tk.BOTH, expand=True, padx=8)
        box = tk.Text(wrap, width=54, height=min(14, max(3, len(moves) or 3)),
                      bg=BG_ENTRY, fg=FG_TEXT, font=("Consolas", 9), relief=tk.FLAT,
                      wrap="none")
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=box.yview)
        box.configure(yscrollcommand=vsb.set)
        box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        box.insert("1.0", "\n".join(moves) if moves else "(no moves needed)")
        box.configure(state="disabled")

        for label, items, colour in (
                ("Unassigned / unrouted:", rows.get("unassigned", []), FG_YELLOW),
                ("Warnings:", rows.get("warnings", []), FG_YELLOW)):
            if items:
                tk.Label(self.win, text=label, bg=BG_PANEL, fg=colour,
                         font=("Consolas", 8, "bold")).pack(anchor="w", padx=8,
                                                            pady=(6, 0))
                tk.Label(self.win, text="\n".join(items), bg=BG_PANEL, fg=colour,
                         font=("Consolas", 8), justify=tk.LEFT).pack(
                             anchor="w", padx=12)

        if big_warning:
            tk.Label(self.win, text=big_warning, bg=BG_PANEL, fg=FG_RED,
                     font=("Consolas", 8, "bold"), justify=tk.LEFT,
                     wraplength=380).pack(anchor="w", padx=8, pady=(6, 0))

        btns = tk.Frame(self.win, bg=BG_PANEL)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="Confirm", style="Dark.TButton",
                   command=self._confirm).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=self.win.destroy).pack(side=tk.LEFT, padx=2)

    def _confirm(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.on_confirm()
