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
