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

# group_id → ship_class rule label (covers the doctrine-relevant hull groups).
_GROUP_LABELS = {
    540: "Command Ship", 1534: "Command Destroyer", 832: "Logistics Cruiser",
    1527: "Logistics Frigate", 963: "Strategic Cruiser", 547: "Carrier",
    1538: "Force Auxiliary", 485: "Dreadnought", 30: "Titan", 659: "Supercarrier",
    1201: "Combat Battlecruiser", 419: "Combat Battlecruiser",
}


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

    def _on_drag_start(self, event): self._drag_item = self._tree.identify_row(event.y)
    def _on_drag_motion(self, event): pass            # Task D8
    def _on_drag_drop(self, event): self._drag_item = None   # Task D8

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
        # UI sync read every ~30 s (spec §9 budget).
        self._sync_after_id = self.win.after(30_000, self._sync_tick)

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
                from esi_auth import ESIAuth  # only for type clarity; not required
                members = self._read_members(session)
            except fleet_esi.FleetESIError as e:
                err = e
            self.win.after(0, _done, structure, members, err)

        def _done(structure, members, err):
            if err is not None:
                self._status.config(text=f"Sync failed: {err.reason}", fg=FG_RED)
                return
            self._live_structure = structure
            self._live_members = self._enrich_members(members)
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

    def _apply(self): pass                       # Task D6
    def _toggle_rebalance(self): pass            # Task D7


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
