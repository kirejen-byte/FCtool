"""Overview pack editor — a Toplevel that edits one :class:`overview_store.PackRecord`.

Wave D2 of the Overview Manager. Fully synchronous UI (no worker threads — no
``_post_ui`` needed). Constructed by the tab module via an injected callable so
this file never imports ``overview_manager_ui`` or ``fc_gui``:

    PackEditorWindow(master, record, groups, categories, on_save)

``record``      an :class:`overview_store.PackRecord`; the editor works on a
                deep copy of ``record.pack`` and NEVER mutates the record.
``groups``      parsed ``inv_groups.json``  (``{"<groupID>": {name, cat, pub}}``).
``categories``  parsed ``inv_categories.json`` (``{"<categoryID>": "<name>"}``).
``on_save``     ``on_save(pack_id, pack)`` — invoked on Save (the caller persists).

The window is a ttk.Notebook of six sub-tabs mirroring the wire model
(overview_schema): Presets, Tabs, Appearance, Columns, Ship labels, Misc.
Sections that are ``None`` in the model are "off" and show an *Enable this
section* button (partial packs are first-class — spec §4.1). Save runs
``overview_schema.validate`` and shows any warnings as an advisory dialog
(Save-anyway / Cancel) — warnings never hard-block, because the client is the
final validator.

Design facts: docs/superpowers/specs/2026-07-12-overview-manager-design.md §4.4,
§4.1; research §A.2. Semantics: alwaysShown > filtered > groups.
"""
from __future__ import annotations

import copy
import re
import tkinter as tk
from tkinter import ttk, messagebox, colorchooser

import overview_markup as om
import overview_schema as osch

# ── House dark palette — imports the shared ui_theme palette (a stdlib-only,
# containment-safe leaf; importing fc_gui would be circular and drag in the whole
# app). One source of truth for the app's navy scheme. ──
from ui_theme import (
    BG_DARK, BG_PANEL, BG_ENTRY,
    FG_TEXT, FG_DIM, FG_ACCENT, FG_GREEN, FG_RED,
    FG_YELLOW, FG_WHITE,
    BORDER_COLOR,
)
# Shared house modal-dialog contract (guarded transient/grab + Escape→cancel +
# base bg) — one wiring for every dialog (OPTIMIZATION_REVIEW.md D2/D6).
from ui_helpers import make_modal

# The 7 ship-label component types the client understands (research §A.2). "None"
# is the editor's sentinel for an unused slot (emits nothing to the wire).
# Unknown type tokens found in ingested packs are PRESERVED (never coerced) and
# appended to the dropdown at runtime — same pattern as unknown color names.
SHIP_LABEL_TYPES = ["None", "ship type", "alliance", "corporation", "ship name",
                    "pilot name", "linebreak"]

# Ship-label pair keys the editor edits directly. Every OTHER pair key found in
# a label (bold/color/fontsize/italic/underline/anything future — the golden
# export carries styling keys on 5 of 7 entries, C2 verifier 2026-07-13) is
# preserved verbatim per-label and re-emitted (losslessness, review M1).
_EDITED_LABEL_KEYS = ("post", "pre", "state", "type")

# Named-color palette floor for stateColorsNameList (research U6 / §A.2 golden):
# a confirmed floor, not an exhaustive enum — any name already in the pack is
# added to the dropdown so ingested packs never lose an unknown color name.
BASE_COLOR_NAMES = ["blue", "darkBlue", "orange", "red", "white"]

_APPEARANCE_FIELDS = ("flag_order", "flag_states", "background_order",
                      "background_states", "state_blinks", "state_colors")

# The overview/bracket comboboxes show display_text (markup rendered/stripped),
# but the bracket "show all" sentinel has no name to render — it displays as
# this friendly string and stores osch.BRACKET_SHOW_ALL.
_BRACKET_SHOW_ALL_DISPLAY = "(show all brackets)"


def wrap_color_markup(name: str, rgb) -> str:
    """Wrap ``name`` in an EVE ``<color=0xAARRGGBB>…</color>`` span (alpha FF).

    ``rgb`` is a 3-tuple of 0..255 ints (the shape ``colorchooser.askcolor``
    returns as its first element). Pure — unit-testable without Tk."""
    r, g, b = (int(round(c)) for c in rgb)
    return f"<color=0xFF{r:02X}{g:02X}{b:02X}>{name}</color>"


_COLOR_WRAP_RE = re.compile(r"^<color=0x[0-9A-Fa-f]{8}>(.*)</color>$", re.DOTALL)


def strip_color_markup(name: str) -> str:
    """Remove ONE outer ``<color=0xAARRGGBB>…</color>`` wrapper if the whole
    string is such a span — so re-coloring a name REPLACES the wrapper instead
    of nesting a second one (review L4). Inner/partial markup is left alone.
    Pure — unit-testable without Tk."""
    m = _COLOR_WRAP_RE.match(name or "")
    return m.group(1) if m else (name or "")


# Whole-string <b>/<i>/<u> wrappers, for the preset-name B/I/U toggles. Matched
# case-insensitively; DOTALL so the wrapped run may itself hold a color span.
_STYLE_WRAP_RES = {
    "b": re.compile(r"(?is)^<b>(.*)</b>$"),
    "i": re.compile(r"(?is)^<i>(.*)</i>$"),
    "u": re.compile(r"(?is)^<u>(.*)</u>$"),
}


def toggle_style_markup(name: str, tag: str) -> str:
    """Toggle a whole-name ``<b>``/``<i>``/``<u>`` wrapper.

    If the entire ``name`` is already wrapped in ``<tag>…</tag>`` (``tag`` being
    ``"b"``, ``"i"`` or ``"u"``), unwrap it; otherwise wrap the whole name.
    Same strip-before-decide discipline as the color wrap — a re-toggle removes
    the outer wrapper rather than nesting a second one. Pure — unit-testable
    without Tk."""
    name = name or ""
    rx = _STYLE_WRAP_RES.get(tag)
    if rx is None:
        return name
    m = rx.match(name)
    if m is not None:
        return m.group(1)
    return f"<{tag}>{name}</{tag}>"


def rgb01_from_askcolor(rgb) -> list:
    """Convert an ``askcolor`` 0..255 RGB triple to ``[r, g, b]`` floats 0..1
    (the wire shape for a tab color). Pure."""
    return [round(c / 255.0, 6) for c in rgb]


def _rgb01_to_hex(color) -> str:
    """``[r, g, b]`` floats 0..1 → ``#rrggbb`` for a swatch background."""
    try:
        r, g, b = (max(0, min(255, int(round(float(c) * 255)))) for c in color)
        return f"#{r:02x}{g:02x}{b:02x}"
    except (TypeError, ValueError):
        return BG_ENTRY


def _parse_state_key(key):
    """``"flag_13"`` → ``("flag", 13)``; ``"background_44"`` → ``("background",
    44)``; anything else → ``None``. The prefix split is on the LAST underscore
    so it is robust to odd ids."""
    if not isinstance(key, str) or "_" not in key:
        return None
    prefix, _, num = key.rpartition("_")
    if prefix in ("flag", "background"):
        try:
            return (prefix, int(num))
        except ValueError:
            return None
    return None


class PackEditorWindow(tk.Toplevel):
    """Editor Toplevel for a single overview pack (see module docstring)."""

    def __init__(self, master, record, groups, categories, on_save):
        super().__init__(master)
        self.pack_id = record.pack_id
        self.pack_name = record.name
        # Work on a deep copy — the record and its pack are never mutated.
        self.pack = copy.deepcopy(record.pack)
        self.groups = dict(groups or {})
        self.categories = dict(categories or {})
        self.on_save = on_save

        self.title(f"Edit overview pack — {record.name}")
        self.geometry("980x680")
        self.minsize(820, 560)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        # Escape follows the SAME path as the window-close button (_cancel →
        # _close: after-cancel + grab_release + destroy), never a blind destroy.
        make_modal(self, master, on_cancel=self._cancel, base_bg=BG_DARK)

        self._loading_states = False          # guards Listbox programmatic loads
        self._search_after_id = None          # debounce handle (group search, L3)
        self._preset_prev_after_id = None     # debounce handle (name preview)
        # Preset name display↔raw maps for the tab comboboxes (rebuilt with the
        # Tabs sub-tab; seeded empty so handlers are safe before the first build).
        self._ov_raw_to_disp: dict = {}
        self._ov_disp_to_raw: dict = {}
        self._init_state()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 0))

        self._presets_frame = tk.Frame(self.notebook, bg=BG_PANEL)
        self._tabs_frame = tk.Frame(self.notebook, bg=BG_PANEL)
        self._appearance_frame = tk.Frame(self.notebook, bg=BG_PANEL)
        self._columns_frame = tk.Frame(self.notebook, bg=BG_PANEL)
        self._labels_frame = tk.Frame(self.notebook, bg=BG_PANEL)
        self._misc_frame = tk.Frame(self.notebook, bg=BG_PANEL)
        self.notebook.add(self._presets_frame, text="Presets")
        self.notebook.add(self._tabs_frame, text="Tabs")
        self.notebook.add(self._appearance_frame, text="Appearance")
        self.notebook.add(self._columns_frame, text="Columns")
        self.notebook.add(self._labels_frame, text="Ship labels")
        self.notebook.add(self._misc_frame, text="Misc")

        self._rebuild_presets_tab()
        self._rebuild_tabs_tab()
        self._rebuild_appearance_tab()
        self._rebuild_columns_tab()
        self._rebuild_labels_tab()
        self._rebuild_misc_tab()

        self._build_footer()

    # ── working-model intermediate state ────────────────────────────────────
    def _init_state(self):
        """Lift the wire-list sections (appearance / columns / ship labels) into
        editable Python structures. Presets, tabs and user_settings are edited
        on the model dataclasses in place, so they need no mirror here."""
        p = self.pack

        # Appearance -----------------------------------------------------------
        self._appearance_on = any(getattr(p, f) is not None
                                  for f in _APPEARANCE_FIELDS)
        # Parse blink/color pairs first so blink/color-only states (present in
        # stateBlinks/stateColors but NOT in flagOrder/backgroundOrder — the
        # golden fixture has exactly this) become editable rows and survive the
        # rebuild instead of being silently dropped.
        self._blink = {}
        for pair in (p.state_blinks or []):
            kk = _parse_state_key(pair[0] if pair else None)
            if kk is not None:
                self._blink[kk] = bool(pair[1]) if len(pair) > 1 else False
        self._colors = {}
        for pair in (p.state_colors or []):
            kk = _parse_state_key(pair[0] if pair else None)
            if kk is not None:
                self._colors[kk] = pair[1] if len(pair) > 1 else ""

        def _rows_for(kind, order, states):
            rows = list(order or [])
            for sid in (states or []):
                if sid not in rows:
                    rows.append(sid)
            for (k, sid) in list(self._blink) + list(self._colors):
                if k == kind and sid not in rows:
                    rows.append(sid)
            return rows

        self._flag_rows = _rows_for("flag", p.flag_order, p.flag_states)
        self._bg_rows = _rows_for("background", p.background_order,
                                  p.background_states)
        self._flag_enabled = set(p.flag_states or [])
        self._bg_enabled = set(p.background_states or [])

        # Columns --------------------------------------------------------------
        self._columns_on = (p.column_order is not None
                            or p.overview_columns is not None)
        self._col_rows = list(p.column_order or [])
        for c in osch.COLUMN_IDS:
            if c not in self._col_rows:
                self._col_rows.append(c)
        self._col_shown = set(p.overview_columns or [])

        # Ship labels ----------------------------------------------------------
        self._shiplabels_on = (p.ship_label_order is not None
                              or p.ship_labels is not None)
        self._label_rows = self._parse_ship_labels(p.ship_labels)

        # State ids offered by the preset always/filter list editors: the union
        # of the attested STATE_DEFS ids plus any id present anywhere in the pack.
        ids = set(osch.STATE_DEFS)
        for pr in (p.presets or []):
            ids.update(x for x in pr.always_shown_states if isinstance(x, int))
            ids.update(x for x in pr.filtered_states if isinstance(x, int))
        ids.update(x for x in self._flag_rows if isinstance(x, int))
        ids.update(x for x in self._bg_rows if isinstance(x, int))
        self._all_state_ids = sorted(ids)

        self._sel_preset = 0 if (p.presets) else None

    @staticmethod
    def _parse_ship_labels(ship_labels):
        """Parse the wire ``shipLabels`` (3-pair legacy or 4+-pair live/golden
        shape) into exactly 7 editable row dicts.

        Lossless (review M1): the editor edits post/pre/state/type; every other
        pair (bold/color/fontsize/italic/underline, anything future) is kept
        verbatim in ``row["extra"]`` and re-emitted by :meth:`build_pack`. An
        unknown ``type`` token is preserved as-is — a slot is unused ONLY when
        its type is genuinely null/'None'."""
        rows = []
        for entry in (ship_labels or []):
            outer = entry[0] if entry else None
            pairs = entry[1] if (entry and len(entry) > 1) else []
            d = {}
            extra = []
            for pr in (pairs or []):
                if not pr:
                    continue
                key = pr[0]
                val = pr[1] if len(pr) > 1 else None
                if key in _EDITED_LABEL_KEYS:
                    d[key] = val
                else:
                    extra.append([key, val])
            typ = d.get("type", outer)
            if typ is None or typ == "None":
                typ = "None"
            rows.append({
                "type": typ,
                "pre": d.get("pre") or "",
                "post": d.get("post") or "",
                "shown": bool(d.get("state", 0)),
                "extra": extra,
            })
        while len(rows) < 7:
            rows.append({"type": "None", "pre": "", "post": "", "shown": False,
                         "extra": []})
        return rows[:7]

    # ── small shared helpers ────────────────────────────────────────────────
    def _preset_names(self):
        return [p.name for p in (self.pack.presets or [])]

    def _current_preset(self):
        if self.pack.presets and self._sel_preset is not None \
                and 0 <= self._sel_preset < len(self.pack.presets):
            return self.pack.presets[self._sel_preset]
        return None

    def _color_name_values(self):
        vals = list(BASE_COLOR_NAMES)
        for name in self._colors.values():
            if name and name not in vals:
                vals.append(name)
        return vals

    def _btn(self, parent, text, command, style="Dark.TButton"):
        return ttk.Button(parent, text=text, command=command, style=style)

    def _enable_bar(self, parent, message, on_enable):
        """The placeholder shown when a section is ``None`` (off)."""
        wrap = tk.Frame(parent, bg=BG_PANEL)
        wrap.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)
        tk.Label(wrap, text=message, bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 10), justify=tk.LEFT, wraplength=560).pack(
            anchor="w", pady=(0, 10))
        self._btn(wrap, "Enable this section", on_enable).pack(anchor="w")

    def _section_header(self, parent, title, on_remove):
        bar = tk.Frame(parent, bg=BG_PANEL)
        bar.pack(fill=tk.X, padx=8, pady=(8, 2))
        tk.Label(bar, text=title, bg=BG_PANEL, fg=FG_ACCENT,
                 font=("Consolas", 11, "bold")).pack(side=tk.LEFT)
        self._btn(bar, "Remove section", on_remove, style="Red.TButton").pack(
            side=tk.RIGHT)
        return bar

    # ════════════════════════════════════════════════════════════════════════
    # PRESETS
    # ════════════════════════════════════════════════════════════════════════
    def _presets_enable(self):
        if self.pack.presets is None:
            self.pack.presets = []
            self._sel_preset = None
        self._rebuild_presets_tab()

    def _presets_remove(self):
        self.pack.presets = None
        self._sel_preset = None
        self._rebuild_presets_tab()
        self._rebuild_tabs_tab()          # tab combobox value lists change

    def _rebuild_presets_tab(self):
        for w in self._presets_frame.winfo_children():
            w.destroy()
        if self.pack.presets is None:
            self._enable_bar(
                self._presets_frame,
                "This pack has no preset filters. Presets are the group/state "
                "filters your tabs point at (alwaysShown beats filteredStates "
                "beats the group whitelist).",
                self._presets_enable)
            return
        self._section_header(self._presets_frame, "Presets", self._presets_remove)

        body = tk.Frame(self._presets_frame, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        # Left: preset list + CRUD + name entry + color-wrap helper.
        left = tk.Frame(body, bg=BG_PANEL)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        self._preset_list = tk.Listbox(
            left, width=30, height=16, exportselection=False,
            bg=BG_ENTRY, fg=FG_TEXT, selectbackground=FG_ACCENT,
            selectforeground=BG_DARK, highlightthickness=1,
            highlightbackground=BORDER_COLOR, font=("Consolas", 9))
        self._preset_list.pack(fill=tk.Y, expand=False)
        self._preset_list.bind("<<ListboxSelect>>", self._on_preset_pick)

        crud = tk.Frame(left, bg=BG_PANEL)
        crud.pack(fill=tk.X, pady=2)
        self._btn(crud, "New", self._preset_new).pack(side=tk.LEFT, padx=1)
        self._btn(crud, "Duplicate", self._preset_duplicate).pack(side=tk.LEFT, padx=1)
        self._btn(crud, "Delete", self._preset_delete, style="Red.TButton").pack(
            side=tk.LEFT, padx=1)

        # Name editor: the RAW markup entry (power users keep full control) +
        # a color/B/I/U toolbar that wraps the WHOLE name + a live rendered
        # preview showing how the client will draw it. RAW stays the model.
        nameblock = tk.Frame(left, bg=BG_PANEL)
        nameblock.pack(fill=tk.X, pady=(6, 0))
        namerow = tk.Frame(nameblock, bg=BG_PANEL)
        namerow.pack(fill=tk.X)
        tk.Label(namerow, text="Name", bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        self._preset_name_var = tk.StringVar()
        ent = tk.Entry(namerow, textvariable=self._preset_name_var, width=22,
                       bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_WHITE,
                       font=("Consolas", 9))
        ent.pack(side=tk.LEFT, padx=2)
        ent.bind("<FocusOut>", lambda e: self._preset_rename(self._preset_name_var.get()))
        ent.bind("<Return>", lambda e: self._preset_rename(self._preset_name_var.get()))
        ent.bind("<KeyRelease>", self._on_preset_name_keyrelease, add="+")

        toolbar = tk.Frame(nameblock, bg=BG_PANEL)
        toolbar.pack(fill=tk.X, pady=(2, 0))
        self._btn(toolbar, "color…", self._preset_wrap_color).pack(side=tk.LEFT)
        self._btn(toolbar, "B", lambda: self._preset_toggle_style("b")).pack(
            side=tk.LEFT, padx=(4, 0))
        self._btn(toolbar, "I", lambda: self._preset_toggle_style("i")).pack(
            side=tk.LEFT, padx=1)
        self._btn(toolbar, "U", lambda: self._preset_toggle_style("u")).pack(
            side=tk.LEFT, padx=1)

        prevrow = tk.Frame(nameblock, bg=BG_PANEL)
        prevrow.pack(fill=tk.X, pady=(2, 0))
        tk.Label(prevrow, text="looks like", bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 8)).pack(side=tk.LEFT)
        self._preset_preview = tk.Text(
            prevrow, height=1, width=26, bg=BG_DARK, fg=FG_TEXT,
            relief=tk.FLAT, bd=0, highlightthickness=1,
            highlightbackground=BORDER_COLOR, wrap="none", cursor="arrow",
            font=("Consolas", 9))
        self._preset_preview.pack(side=tk.LEFT, padx=(4, 0))
        self._preset_preview.configure(state=tk.DISABLED)

        # Right: two-pane group picker (search + category→group ☐/☑ tree) and,
        # below it, the two state list editors.
        right = tk.Frame(body, bg=BG_PANEL)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        srow = tk.Frame(right, bg=BG_PANEL)
        srow.pack(fill=tk.X)
        tk.Label(srow, text="Groups", bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        self._group_search_var = tk.StringVar()
        se = tk.Entry(srow, textvariable=self._group_search_var, width=24,
                      bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_WHITE,
                      font=("Consolas", 9))
        se.pack(side=tk.LEFT, padx=4)
        se.bind("<KeyRelease>", self._on_group_search_changed)
        self._group_count_lbl = tk.Label(srow, text="", bg=BG_PANEL, fg=FG_GREEN,
                                         font=("Consolas", 9))
        self._group_count_lbl.pack(side=tk.RIGHT)

        treewrap = tk.Frame(right, bg=BG_PANEL)
        treewrap.pack(fill=tk.BOTH, expand=True, pady=2)
        self._group_tree = ttk.Treeview(treewrap, show="tree", height=10,
                                        selectmode="none")
        gsb = ttk.Scrollbar(treewrap, orient=tk.VERTICAL,
                            command=self._group_tree.yview)
        self._group_tree.configure(yscrollcommand=gsb.set)
        self._group_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        gsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._group_tree.bind("<Button-1>", self._on_group_click)
        self._gid_item = {}            # group id -> tree item id
        self._item_gid = {}            # tree item id -> group id

        # State editors.
        states = tk.Frame(right, bg=BG_PANEL)
        states.pack(fill=tk.X, pady=(4, 0))
        self._always_list = self._make_state_list(states, "Always-show (override)",
                                                  "always")
        self._filter_list = self._make_state_list(states, "Filter-out (hide)",
                                                  "filter")

        self._refresh_preset_list()
        self._rebuild_group_tree("")
        if self.pack.presets:
            self._select_preset(0)

    def _make_state_list(self, parent, title, kind):
        col = tk.Frame(parent, bg=BG_PANEL)
        col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2)
        tk.Label(col, text=title, bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 8)).pack(anchor="w")
        lb = tk.Listbox(col, selectmode=tk.MULTIPLE, height=6, exportselection=False,
                        bg=BG_ENTRY, fg=FG_TEXT, selectbackground=FG_ACCENT,
                        selectforeground=BG_DARK, font=("Consolas", 8),
                        highlightthickness=1, highlightbackground=BORDER_COLOR)
        lb.pack(fill=tk.BOTH, expand=True)
        for sid in self._all_state_ids:
            lb.insert(tk.END, f"{sid}: {osch.state_name(sid)}")
        lb.bind("<<ListboxSelect>>", lambda e, k=kind: self._on_states_select(k))
        return lb

    def _on_group_search_changed(self, _evt=None):
        """Debounce the group-tree rebuild while typing (963 published groups
        re-inserted per keystroke is visible jank — review L3): re-arm a 150 ms
        one-shot; only the last keystroke in a burst rebuilds."""
        if self._search_after_id is not None:
            try:
                self.after_cancel(self._search_after_id)
            except (tk.TclError, ValueError):
                pass
        self._search_after_id = self.after(150, self._apply_group_search)

    def _apply_group_search(self):
        self._search_after_id = None
        try:
            alive = bool(self._group_tree.winfo_exists())
        except tk.TclError:
            alive = False
        if alive:
            self._rebuild_group_tree(self._group_search_var.get())

    def _rebuild_group_tree(self, filter_text=""):
        tree = self._group_tree
        tree.delete(*tree.get_children())
        self._gid_item.clear()
        self._item_gid.clear()
        q = (filter_text or "").strip().lower()
        by_cat = {}
        for gid_str, meta in self.groups.items():
            if not meta.get("pub"):
                continue
            name = meta.get("name", "")
            if q and q not in name.lower():
                continue
            by_cat.setdefault(meta.get("cat"), []).append((int(gid_str), name))
        for cat_id in sorted(by_cat, key=lambda c: self._cat_name(c).lower()):
            rows = sorted(by_cat[cat_id], key=lambda t: t[1].lower())
            parent = tree.insert("", "end", text=f"{self._cat_name(cat_id)}  ({len(rows)})",
                                 open=bool(q))
            for gid, name in rows:
                item = tree.insert(parent, "end", text=self._group_label(gid, name))
                self._gid_item[gid] = item
                self._item_gid[item] = gid
        self._refresh_group_count()

    def _cat_name(self, cat_id):
        return self.categories.get(str(cat_id), f"Category {cat_id}")

    def _group_label(self, gid, name=None):
        if name is None:
            meta = self.groups.get(str(gid))
            name = meta.get("name", f"Group {gid}") if meta else f"Group {gid}"
        p = self._current_preset()
        checked = p is not None and gid in p.groups
        return f"{'☑' if checked else '☐'} {name}  [{gid}]"

    def _on_group_click(self, event):
        item = self._group_tree.identify_row(event.y)
        gid = self._item_gid.get(item)
        if gid is not None:
            self._toggle_group(gid)

    def _toggle_group(self, gid):
        """Toggle group ``gid`` in the selected preset's group whitelist."""
        p = self._current_preset()
        if p is None:
            return
        if gid in p.groups:
            p.groups.remove(gid)
        else:
            p.groups.append(gid)
        item = self._gid_item.get(gid)
        if item is not None:
            self._group_tree.item(item, text=self._group_label(gid))
        self._refresh_group_count()

    def _refresh_group_count(self):
        p = self._current_preset()
        n = len(p.groups) if p is not None else 0
        if getattr(self, "_group_count_lbl", None) is not None:
            self._group_count_lbl.config(text=f"{n} groups selected")

    def _refresh_group_checks(self):
        for gid, item in self._gid_item.items():
            self._group_tree.item(item, text=self._group_label(gid))
        self._refresh_group_count()

    def _refresh_preset_list(self, keep_index=True):
        idx = self._sel_preset
        self._preset_list.delete(0, tk.END)
        for i, p in enumerate(self.pack.presets or []):
            # Show the name the client draws (tags stripped, glyph kept) and
            # tint the row with the author's chosen color. RAW stays the model.
            self._preset_list.insert(tk.END, om.display_text(p.name) or p.name)
            try:
                self._preset_list.itemconfig(
                    i, foreground=om.primary_color(p.name) or FG_TEXT)
            except tk.TclError:
                pass
        if keep_index and idx is not None and self.pack.presets \
                and 0 <= idx < len(self.pack.presets):
            self._preset_list.selection_clear(0, tk.END)
            self._preset_list.selection_set(idx)

    def _on_preset_pick(self, _evt=None):
        sel = self._preset_list.curselection()
        if sel:
            self._select_preset(sel[0])

    def _select_preset(self, idx):
        if not self.pack.presets or not (0 <= idx < len(self.pack.presets)):
            return
        self._sel_preset = idx
        self._preset_list.selection_clear(0, tk.END)
        self._preset_list.selection_set(idx)
        p = self.pack.presets[idx]
        self._preset_name_var.set(p.name)
        self._update_preset_preview()
        self._refresh_group_checks()
        self._load_preset_states(p)

    def _load_preset_states(self, preset):
        self._loading_states = True
        try:
            for lb, ids in ((self._always_list, preset.always_shown_states),
                            (self._filter_list, preset.filtered_states)):
                lb.selection_clear(0, tk.END)
                idset = set(ids or [])
                for i, sid in enumerate(self._all_state_ids):
                    if sid in idset:
                        lb.selection_set(i)
        finally:
            self._loading_states = False

    def _on_states_select(self, kind):
        if self._loading_states:
            return
        lb = self._always_list if kind == "always" else self._filter_list
        ids = [self._all_state_ids[i] for i in lb.curselection()]
        if kind == "always":
            self._set_always_states(ids)
        else:
            self._set_filter_states(ids)

    def _set_always_states(self, ids):
        p = self._current_preset()
        if p is not None:
            p.always_shown_states = list(ids)

    def _set_filter_states(self, ids):
        p = self._current_preset()
        if p is not None:
            p.filtered_states = list(ids)

    def _preset_new(self):
        self.pack.presets.append(osch.Preset(name="New preset"))
        self._sel_preset = len(self.pack.presets) - 1
        self._refresh_preset_list()
        self._select_preset(self._sel_preset)
        self._rebuild_tabs_tab()

    def _preset_duplicate(self):
        p = self._current_preset()
        if p is None:
            return
        self.pack.presets.append(copy.deepcopy(p))
        self.pack.presets[-1].name = p.name + " copy"
        self._sel_preset = len(self.pack.presets) - 1
        self._refresh_preset_list()
        self._select_preset(self._sel_preset)
        self._rebuild_tabs_tab()

    def _preset_delete(self):
        if not self.pack.presets or self._sel_preset is None:
            return
        del self.pack.presets[self._sel_preset]
        self._sel_preset = 0 if self.pack.presets else None
        self._refresh_preset_list()
        if self.pack.presets:
            self._select_preset(0)
        else:
            self._preset_name_var.set("")
            self._update_preset_preview()
            self._refresh_group_checks()
        self._rebuild_tabs_tab()

    def _preset_rename(self, new_name, refresh=True):
        """Rename the selected preset AND propagate the old→new name to every
        tab that referenced it (overview or bracket) so references never dangle."""
        p = self._current_preset()
        if p is None:
            return
        new_name = (new_name or "").strip()
        if not new_name:
            # Blank FocusOut: restore the model's current name into the Entry
            # instead of leaving a blank field over an unchanged model (L5).
            if hasattr(self, "_preset_name_var"):
                self._preset_name_var.set(p.name)
            return
        if new_name == p.name:
            return
        old = p.name
        p.name = new_name
        for t in (self.pack.tabs or []):
            if t.overview_preset == old:
                t.overview_preset = new_name
            if t.bracket_preset == old:
                t.bracket_preset = new_name
        # Keep the name entry in sync so a later _flush_text (which reads the
        # var) is idempotent and never reverts a handler-driven rename.
        if hasattr(self, "_preset_name_var"):
            self._preset_name_var.set(new_name)
        if refresh:
            self._refresh_preset_list()
            self._rebuild_tabs_tab()

    def _preset_wrap_color(self):
        p = self._current_preset()
        if p is None:
            return
        try:
            rgb, _hex = colorchooser.askcolor(parent=self, title="Preset name color")
        except tk.TclError:
            rgb = None
        if not rgb:
            return
        # Wrap the CURRENT (entry) name — stripping one existing outer wrapper
        # first so re-coloring replaces rather than nests (L4) — then route
        # through rename so the change propagates to any tab referencing the
        # old name.
        base = strip_color_markup(self._preset_name_var.get() or p.name)
        wrapped = wrap_color_markup(base, rgb)
        self._preset_name_var.set(wrapped)
        self._preset_rename(wrapped)
        self._update_preset_preview()

    def _preset_toggle_style(self, tag):
        """Wrap/unwrap the whole preset name in ``<b>``/``<i>``/``<u>`` and
        propagate (routes through rename so any tab referencing the old name
        follows). Operates on the current entry text, like the color button."""
        p = self._current_preset()
        if p is None:
            return
        base = self._preset_name_var.get() or p.name
        toggled = toggle_style_markup(base, tag)
        self._preset_name_var.set(toggled)
        self._preset_rename(toggled)
        self._update_preset_preview()

    def _on_preset_name_keyrelease(self, _evt=None):
        """Debounce a live preview refresh while typing the raw name (the model
        rename still lands on FocusOut/Return — this only repaints the preview)."""
        if self._preset_prev_after_id is not None:
            try:
                self.after_cancel(self._preset_prev_after_id)
            except (tk.TclError, ValueError):
                pass
        self._preset_prev_after_id = self.after(120, self._update_preset_preview)

    def _update_preset_preview(self):
        """Render the current raw name into the preset preview strip."""
        self._preset_prev_after_id = None
        prev = getattr(self, "_preset_preview", None)
        if prev is None:
            return
        try:
            if not prev.winfo_exists():
                return
        except tk.TclError:
            return
        om.render_into_text(prev, self._preset_name_var.get(),
                            base_font=("Consolas", 9))

    # ════════════════════════════════════════════════════════════════════════
    # TABS
    # ════════════════════════════════════════════════════════════════════════
    def _tabs_enable(self):
        if self.pack.tabs is None:
            self.pack.tabs = []
        self._rebuild_tabs_tab()

    def _tabs_remove(self):
        self.pack.tabs = None
        self._rebuild_tabs_tab()

    def _rebuild_tabs_tab(self):
        for w in self._tabs_frame.winfo_children():
            w.destroy()
        self._tab_rows = []
        if self.pack.tabs is None:
            self._enable_bar(
                self._tabs_frame,
                "This pack defines no overview tabs. A tab binds an overview "
                "preset + a bracket filter (+ optional color and per-tab "
                "columns). The client supports up to 20 tabs.",
                self._tabs_enable)
            return
        self._section_header(self._tabs_frame, "Tabs", self._tabs_remove)

        ctl = tk.Frame(self._tabs_frame, bg=BG_PANEL)
        ctl.pack(fill=tk.X, padx=8)
        self._btn(ctl, "Add tab", self._tab_add).pack(side=tk.LEFT, padx=2, pady=2)
        self._tabs_status = tk.Label(ctl, text=f"{len(self.pack.tabs)}/{osch.MAX_TABS} tabs",
                                     bg=BG_PANEL, fg=FG_DIM, font=("Consolas", 9))
        self._tabs_status.pack(side=tk.LEFT, padx=8)

        grid = tk.Frame(self._tabs_frame, bg=BG_PANEL)
        grid.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        heads = ["#", "Name", "Overview preset", "Bracket", "Color", "Columns", ""]
        for c, h in enumerate(heads):
            tk.Label(grid, text=h, bg=BG_PANEL, fg=FG_DIM,
                     font=("Consolas", 8, "bold")).grid(row=0, column=c, sticky="w",
                                                        padx=2)
        # Comboboxes show disambiguated display_text; the model keeps RAW names.
        self._ov_raw_to_disp, self._ov_disp_to_raw = \
            self._build_preset_display_maps()
        ov_values = [self._ov_raw_to_disp[n] for n in self._preset_names()]
        for r, tab in enumerate(self.pack.tabs, start=1):
            self._build_tab_row(grid, r, tab, ov_values)

    def _build_preset_display_maps(self):
        """Return ``(raw→display, display→raw)`` maps for the tab comboboxes.

        Built from the preset names PLUS every tab's current overview/bracket
        ref, so even a dangling reference round-trips losslessly (its display
        maps back to the exact raw). ``om.disambiguate`` guarantees unique
        display strings, so ``display→raw`` is well-defined. The bracket "show
        all" sentinel is handled by the row builder, never entered here."""
        raws = list(self._preset_names())
        for t in (self.pack.tabs or []):
            for ref in (t.overview_preset, t.bracket_preset):
                if ref and ref != osch.BRACKET_SHOW_ALL and ref not in raws:
                    raws.append(ref)
        raw_to_disp = om.disambiguate(raws)
        disp_to_raw = {d: r for r, d in raw_to_disp.items()}
        return raw_to_disp, disp_to_raw

    def _bracket_display_for(self, raw):
        """The bracket combobox display for a raw bracket value (the sentinel
        and blank both show the friendly 'show all' string)."""
        if not raw or raw == osch.BRACKET_SHOW_ALL:
            return _BRACKET_SHOW_ALL_DISPLAY
        return self._ov_raw_to_disp.get(raw, om.display_text(raw))

    def _build_tab_row(self, grid, r, tab, ov_values):
        i = r - 1
        tk.Label(grid, text=str(tab.index), bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).grid(row=r, column=0, padx=2)

        # Name cell: raw entry + a visible "c…" name-color-wrap button (L1 — the
        # right-click binding stays as a shortcut, not the only affordance), with
        # a live rendered preview below (RAW stays the model).
        namewrap = tk.Frame(grid, bg=BG_PANEL)
        namewrap.grid(row=r, column=1, padx=2, sticky="w")
        topline = tk.Frame(namewrap, bg=BG_PANEL)
        topline.pack(fill=tk.X)
        name_var = tk.StringVar(value=tab.name)
        ne = tk.Entry(topline, textvariable=name_var, width=14, bg=BG_ENTRY,
                      fg=FG_TEXT, insertbackground=FG_WHITE, font=("Consolas", 9))
        ne.pack(side=tk.LEFT)
        ne.bind("<KeyRelease>", lambda e, idx=i: self._on_tab_name_keyrelease(idx))
        ne.bind("<Button-3>", lambda e, idx=i: self._tab_wrap_color(idx))
        ttk.Button(topline, text="c…", width=3, style="Dark.TButton",
                   command=lambda idx=i: self._tab_wrap_color(idx)).pack(
            side=tk.LEFT, padx=(1, 0))
        name_prev = tk.Text(namewrap, height=1, width=16, bg=BG_DARK, fg=FG_TEXT,
                            relief=tk.FLAT, bd=0, highlightthickness=0,
                            wrap="none", cursor="arrow", font=("Consolas", 8))
        name_prev.pack(fill=tk.X, pady=(1, 0))
        name_prev.configure(state=tk.DISABLED)
        om.render_into_text(name_prev, tab.name, base_font=("Consolas", 8))

        ov_var = tk.StringVar(value=self._ov_raw_to_disp.get(
            tab.overview_preset, om.display_text(tab.overview_preset)))
        ov = ttk.Combobox(grid, textvariable=ov_var, values=ov_values, width=18)
        ov.grid(row=r, column=2, padx=2)
        ov.bind("<<ComboboxSelected>>", lambda e, idx=i: self._tab_set_overview(idx, self._tab_rows[idx]["overview"].get()))
        ov.bind("<KeyRelease>", lambda e, idx=i: self._tab_set_overview(idx, self._tab_rows[idx]["overview"].get()))

        br_var = tk.StringVar(value=self._bracket_display_for(tab.bracket_preset))
        br = ttk.Combobox(grid, textvariable=br_var,
                          values=[_BRACKET_SHOW_ALL_DISPLAY] + ov_values, width=16)
        br.grid(row=r, column=3, padx=2)
        br.bind("<<ComboboxSelected>>", lambda e, idx=i: self._tab_set_bracket(idx, self._tab_rows[idx]["bracket"].get()))
        br.bind("<KeyRelease>", lambda e, idx=i: self._tab_set_bracket(idx, self._tab_rows[idx]["bracket"].get()))

        # Color cell: swatch + a visible "×" clear-to-None button (L1 — the
        # right-click-to-clear binding stays as a shortcut).
        colorwrap = tk.Frame(grid, bg=BG_PANEL)
        colorwrap.grid(row=r, column=4, padx=2)
        swatch = tk.Button(colorwrap, text="  ", width=3, relief=tk.RIDGE,
                           bg=_rgb01_to_hex(tab.color) if tab.color else BG_ENTRY,
                           command=lambda idx=i: self._tab_pick_color(idx))
        swatch.pack(side=tk.LEFT)
        swatch.bind("<Button-3>", lambda e, idx=i: self._tab_clear_color(idx))
        ttk.Button(colorwrap, text="×", width=2, style="Dark.TButton",
                   command=lambda idx=i: self._tab_clear_color(idx)).pack(
            side=tk.LEFT, padx=(1, 0))

        col_txt = "custom" if tab.tab_columns is not None else "global"
        colbtn = self._btn(grid, col_txt, lambda idx=i: self._tab_edit_columns(idx))
        colbtn.grid(row=r, column=5, padx=2)

        self._btn(grid, "✕", lambda idx=i: self._tab_remove(idx),
                  style="Red.TButton").grid(row=r, column=6, padx=2)

        self._tab_rows.append({"name": name_var, "overview": ov_var,
                               "bracket": br_var, "swatch": swatch,
                               "colbtn": colbtn, "preview": name_prev})

    def _on_tab_name_keyrelease(self, idx):
        """Commit the tab name AND repaint its preview as the user types."""
        if not (0 <= idx < len(self._tab_rows)):
            return
        name = self._tab_rows[idx]["name"].get()
        self._tab_set_name(idx, name)
        self._render_tab_name_preview(idx, name)

    def _render_tab_name_preview(self, idx, name):
        prev = self._tab_rows[idx].get("preview") if idx < len(self._tab_rows) \
            else None
        if prev is None:
            return
        try:
            if prev.winfo_exists():
                om.render_into_text(prev, name, base_font=("Consolas", 8))
        except tk.TclError:
            pass

    def _tab_add(self):
        """Append a tab with the next contiguous index. Refused (returns False)
        once the pack already has MAX_TABS."""
        if self.pack.tabs is None:
            return False
        if len(self.pack.tabs) >= osch.MAX_TABS:
            if getattr(self, "_tabs_status", None) is not None:
                self._tabs_status.config(text=f"Maximum {osch.MAX_TABS} tabs",
                                         fg=FG_YELLOW)
            return False
        self.pack.tabs.append(osch.TabConfig(index=len(self.pack.tabs),
                                             bracket_preset=osch.BRACKET_SHOW_ALL))
        self._rebuild_tabs_tab()
        return True

    def _tab_remove(self, row):
        if self.pack.tabs is None or not (0 <= row < len(self.pack.tabs)):
            return
        del self.pack.tabs[row]
        self._reindex_tabs()
        self._rebuild_tabs_tab()

    def _reindex_tabs(self):
        for i, t in enumerate(self.pack.tabs or []):
            t.index = i

    def _tab_set_name(self, row, name):
        if self.pack.tabs and 0 <= row < len(self.pack.tabs):
            self.pack.tabs[row].name = name

    def _tab_set_overview(self, row, value):
        """Store the RAW preset name for the chosen combobox display (falls
        back to the value verbatim for a typed/unknown entry — lossless)."""
        if self.pack.tabs and 0 <= row < len(self.pack.tabs):
            self.pack.tabs[row].overview_preset = \
                self._ov_disp_to_raw.get(value, value)

    def _tab_set_bracket(self, row, value):
        if self.pack.tabs and 0 <= row < len(self.pack.tabs):
            self.pack.tabs[row].bracket_preset = self._resolve_bracket(value)

    def _resolve_bracket(self, value):
        if value == _BRACKET_SHOW_ALL_DISPLAY:
            return osch.BRACKET_SHOW_ALL
        return self._ov_disp_to_raw.get(value, value)

    def _tab_pick_color(self, row):
        if not (self.pack.tabs and 0 <= row < len(self.pack.tabs)):
            return
        try:
            rgb, _hex = colorchooser.askcolor(parent=self, title="Tab color")
        except tk.TclError:
            rgb = None
        if not rgb:
            return
        self.pack.tabs[row].color = rgb01_from_askcolor(rgb)
        if row < len(self._tab_rows):
            self._tab_rows[row]["swatch"].config(bg=_rgb01_to_hex(self.pack.tabs[row].color))

    def _tab_clear_color(self, row):
        if self.pack.tabs and 0 <= row < len(self.pack.tabs):
            self.pack.tabs[row].color = None
            if row < len(self._tab_rows):
                self._tab_rows[row]["swatch"].config(bg=BG_ENTRY)

    def _tab_wrap_color(self, row):
        if not (self.pack.tabs and 0 <= row < len(self.pack.tabs)):
            return
        try:
            rgb, _hex = colorchooser.askcolor(parent=self, title="Tab name color")
        except tk.TclError:
            rgb = None
        if not rgb:
            return
        # Replace (not nest) any existing outer color wrapper (L4).
        wrapped = wrap_color_markup(
            strip_color_markup(self.pack.tabs[row].name), rgb)
        self.pack.tabs[row].name = wrapped
        if row < len(self._tab_rows):
            self._tab_rows[row]["name"].set(wrapped)
            self._render_tab_name_preview(row, wrapped)

    def _tab_edit_columns(self, row):
        if not (self.pack.tabs and 0 <= row < len(self.pack.tabs)):
            return
        _ColumnPickerDialog(self, self.pack.tabs[row].tab_columns,
                            on_ok=lambda cols: self._set_tab_columns(row, cols))

    def _set_tab_columns(self, row, cols):
        """cols == None → use the global columns; a list → per-tab override."""
        if self.pack.tabs and 0 <= row < len(self.pack.tabs):
            self.pack.tabs[row].tab_columns = None if cols is None else list(cols)
            if row < len(self._tab_rows):
                self._tab_rows[row]["colbtn"].config(
                    text="custom" if cols is not None else "global")

    # ════════════════════════════════════════════════════════════════════════
    # APPEARANCE
    # ════════════════════════════════════════════════════════════════════════
    def _appearance_enable(self):
        self._appearance_on = True
        # Enabling an EMPTY section seeds the rows from the known state table
        # (sorted), matching how Columns seeds from COLUMN_IDS — otherwise the
        # user faces zero rows with no way to add states (review m2). Re-enable
        # after an in-session Remove keeps whatever rows were already loaded.
        if not self._flag_rows:
            self._flag_rows = sorted(osch.STATE_DEFS)
        if not self._bg_rows:
            self._bg_rows = sorted(osch.STATE_DEFS)
        self._rebuild_appearance_tab()

    def _appearance_remove(self):
        self._appearance_on = False
        self._rebuild_appearance_tab()

    def _rebuild_appearance_tab(self):
        for w in self._appearance_frame.winfo_children():
            w.destroy()
        if not self._appearance_on:
            self._enable_bar(
                self._appearance_frame,
                "This pack carries no appearance rules. Appearance sets the "
                "priority order of flag (foreground) and background state "
                "colors, whether each is enabled, whether it blinks, and its "
                "named color.",
                self._appearance_enable)
            return
        self._section_header(self._appearance_frame, "Appearance",
                             self._appearance_remove)
        body = tk.Frame(self._appearance_frame, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self._build_appearance_list(body, "Flag (foreground)", "flag",
                                    self._flag_rows, self._flag_enabled)
        self._build_appearance_list(body, "Background", "background",
                                    self._bg_rows, self._bg_enabled)

    def _build_appearance_list(self, parent, title, kind, rows, enabled):
        col = tk.Frame(parent, bg=BG_PANEL, bd=1, relief=tk.RIDGE)
        col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
        tk.Label(col, text=title + "  (top = highest priority)", bg=BG_PANEL,
                 fg=FG_DIM, font=("Consolas", 9, "bold")).pack(anchor="w", padx=4,
                                                              pady=2)
        heads = tk.Frame(col, bg=BG_PANEL)
        heads.pack(fill=tk.X, padx=4)
        for text, w in (("state", 22), ("on", 4), ("blink", 6), ("color", 10)):
            tk.Label(heads, text=text, bg=BG_PANEL, fg=FG_DIM, width=w,
                     anchor="w", font=("Consolas", 8)).pack(side=tk.LEFT)
        color_vals = self._color_name_values()
        for sid in rows:
            self._build_appearance_row(col, kind, sid, enabled, color_vals)

    def _build_appearance_row(self, parent, kind, sid, enabled, color_vals):
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill=tk.X, padx=4, pady=1)
        tk.Label(row, text=f"{sid}: {osch.state_name(sid)}", bg=BG_PANEL,
                 fg=FG_TEXT, width=22, anchor="w",
                 font=("Consolas", 8)).pack(side=tk.LEFT)

        en_var = tk.BooleanVar(value=sid in enabled)
        tk.Checkbutton(row, variable=en_var, bg=BG_PANEL, activebackground=BG_PANEL,
                       selectcolor=BG_ENTRY,
                       command=lambda: self._appearance_set_enabled(
                           kind, sid, en_var.get())).pack(side=tk.LEFT)

        bl_var = tk.BooleanVar(value=self._blink.get((kind, sid), False))
        tk.Checkbutton(row, variable=bl_var, bg=BG_PANEL, activebackground=BG_PANEL,
                       selectcolor=BG_ENTRY, width=4,
                       command=lambda: self._appearance_set_blink(
                           kind, sid, bl_var.get())).pack(side=tk.LEFT)

        cvar = tk.StringVar(value=self._colors.get((kind, sid), ""))
        cb = ttk.Combobox(row, textvariable=cvar, values=[""] + color_vals, width=10)
        cb.pack(side=tk.LEFT, padx=2)
        cb.bind("<<ComboboxSelected>>",
                lambda e: self._appearance_set_color(kind, sid, cvar.get()))
        cb.bind("<KeyRelease>",
                lambda e: self._appearance_set_color(kind, sid, cvar.get()))

        self._btn(row, "▲", lambda: self._appearance_move(kind, sid, -1)).pack(
            side=tk.LEFT)
        self._btn(row, "▼", lambda: self._appearance_move(kind, sid, 1)).pack(
            side=tk.LEFT)

    def _appearance_set_enabled(self, kind, sid, val):
        target = self._flag_enabled if kind == "flag" else self._bg_enabled
        if val:
            target.add(sid)
        else:
            target.discard(sid)

    def _appearance_set_blink(self, kind, sid, val):
        if val:
            self._blink[(kind, sid)] = True
        else:
            self._blink.pop((kind, sid), None)

    def _appearance_set_color(self, kind, sid, name):
        if name:
            self._colors[(kind, sid)] = name
        else:
            self._colors.pop((kind, sid), None)

    def _appearance_move(self, kind, sid, delta):
        rows = self._flag_rows if kind == "flag" else self._bg_rows
        if sid not in rows:
            return
        i = rows.index(sid)
        j = i + delta
        if 0 <= j < len(rows):
            rows[i], rows[j] = rows[j], rows[i]
            self._rebuild_appearance_tab()

    # ════════════════════════════════════════════════════════════════════════
    # COLUMNS
    # ════════════════════════════════════════════════════════════════════════
    def _columns_enable(self):
        self._columns_on = True
        self._rebuild_columns_tab()

    def _columns_remove(self):
        self._columns_on = False
        self._rebuild_columns_tab()

    def _rebuild_columns_tab(self):
        for w in self._columns_frame.winfo_children():
            w.destroy()
        if not self._columns_on:
            self._enable_bar(
                self._columns_frame,
                "This pack sets no global columns. Columns control the order "
                "and visibility of the overview's data columns (Icon, Distance, "
                "Name, Type, …).",
                self._columns_enable)
            return
        self._section_header(self._columns_frame, "Columns", self._columns_remove)
        body = tk.Frame(self._columns_frame, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        tk.Label(body, text="checked = shown;  ▲▼ = order", bg=BG_PANEL,
                 fg=FG_DIM, font=("Consolas", 8)).pack(anchor="w")
        self._col_widgets = {}
        for cid in self._col_rows:
            self._build_column_row(body, cid)

    def _build_column_row(self, parent, cid):
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill=tk.X, pady=1)
        var = tk.BooleanVar(value=cid in self._col_shown)
        tk.Checkbutton(row, variable=var, bg=BG_PANEL, activebackground=BG_PANEL,
                       selectcolor=BG_ENTRY,
                       command=lambda: self._column_set_shown(cid, var.get())).pack(
            side=tk.LEFT)
        tk.Label(row, text=cid, bg=BG_PANEL, fg=FG_TEXT, width=22, anchor="w",
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        self._btn(row, "▲", lambda: self._column_move(cid, -1)).pack(side=tk.LEFT)
        self._btn(row, "▼", lambda: self._column_move(cid, 1)).pack(side=tk.LEFT)
        self._col_widgets[cid] = var

    def _column_set_shown(self, cid, val):
        if val:
            self._col_shown.add(cid)
        else:
            self._col_shown.discard(cid)

    def _column_move(self, cid, delta):
        if cid not in self._col_rows:
            return
        i = self._col_rows.index(cid)
        j = i + delta
        if 0 <= j < len(self._col_rows):
            self._col_rows[i], self._col_rows[j] = self._col_rows[j], self._col_rows[i]
            self._rebuild_columns_tab()

    # ════════════════════════════════════════════════════════════════════════
    # SHIP LABELS
    # ════════════════════════════════════════════════════════════════════════
    def _shiplabels_enable(self):
        self._shiplabels_on = True
        self._rebuild_labels_tab()

    def _shiplabels_remove(self):
        self._shiplabels_on = False
        self._rebuild_labels_tab()

    def _rebuild_labels_tab(self):
        for w in self._labels_frame.winfo_children():
            w.destroy()
        if not self._shiplabels_on:
            self._enable_bar(
                self._labels_frame,
                "This pack defines no ship labels. Ship labels are the up-to-7 "
                "text components drawn next to a ship in space (pilot name, "
                "ship type, corp/alliance, …), each with optional pre/post text.",
                self._shiplabels_enable)
            return
        self._section_header(self._labels_frame, "Ship labels",
                             self._shiplabels_remove)
        body = tk.Frame(self._labels_frame, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        heads = ["#", "Type", "Pre", "Post", "Shown"]
        widths = [3, 14, 12, 12, 6]
        for c, (h, w) in enumerate(zip(heads, widths)):
            tk.Label(body, text=h, bg=BG_PANEL, fg=FG_DIM, width=w, anchor="w",
                     font=("Consolas", 8, "bold")).grid(row=0, column=c, padx=2)
        self._label_widgets = []
        for i in range(7):
            self._build_label_row(body, i)

    def _ship_label_type_values(self):
        """Dropdown values = the 7 known types + any unknown type token already
        present in the pack (preserved, never coerced — review M1)."""
        extras = sorted({str(r["type"]) for r in self._label_rows}
                        - set(SHIP_LABEL_TYPES))
        return SHIP_LABEL_TYPES + extras

    def _build_label_row(self, body, i):
        r = i + 1
        data = self._label_rows[i]
        tk.Label(body, text=str(i), bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).grid(row=r, column=0, padx=2)

        type_var = tk.StringVar(value=data["type"])
        tcb = ttk.Combobox(body, textvariable=type_var,
                           values=self._ship_label_type_values(),
                           state="readonly", width=13)
        tcb.grid(row=r, column=1, padx=2)
        tcb.bind("<<ComboboxSelected>>",
                 lambda e, idx=i: self._shiplabel_set_type(idx, self._label_widgets[idx]["type"].get()))

        pre_var = tk.StringVar(value=data["pre"])
        pe = tk.Entry(body, textvariable=pre_var, width=12, bg=BG_ENTRY, fg=FG_TEXT,
                      insertbackground=FG_WHITE, font=("Consolas", 9))
        pe.grid(row=r, column=2, padx=2)
        pe.bind("<KeyRelease>",
                lambda e, idx=i: self._shiplabel_set_pre(idx, self._label_widgets[idx]["pre"].get()))

        post_var = tk.StringVar(value=data["post"])
        po = tk.Entry(body, textvariable=post_var, width=12, bg=BG_ENTRY, fg=FG_TEXT,
                      insertbackground=FG_WHITE, font=("Consolas", 9))
        po.grid(row=r, column=3, padx=2)
        po.bind("<KeyRelease>",
                lambda e, idx=i: self._shiplabel_set_post(idx, self._label_widgets[idx]["post"].get()))

        shown_var = tk.BooleanVar(value=data["shown"])
        tk.Checkbutton(body, variable=shown_var, bg=BG_PANEL, activebackground=BG_PANEL,
                       selectcolor=BG_ENTRY,
                       command=lambda idx=i: self._shiplabel_set_shown(
                           idx, self._label_widgets[idx]["shown"].get())).grid(
            row=r, column=4, padx=2)

        self._label_widgets.append({"type": type_var, "pre": pre_var,
                                    "post": post_var, "shown": shown_var})

    def _shiplabel_set_type(self, row, typ):
        if 0 <= row < len(self._label_rows):
            self._label_rows[row]["type"] = typ

    def _shiplabel_set_pre(self, row, text):
        if 0 <= row < len(self._label_rows):
            self._label_rows[row]["pre"] = text

    def _shiplabel_set_post(self, row, text):
        if 0 <= row < len(self._label_rows):
            self._label_rows[row]["post"] = text

    def _shiplabel_set_shown(self, row, val):
        if 0 <= row < len(self._label_rows):
            self._label_rows[row]["shown"] = bool(val)

    # ════════════════════════════════════════════════════════════════════════
    # MISC (userSettings)
    # ════════════════════════════════════════════════════════════════════════
    def _rebuild_misc_tab(self):
        for w in self._misc_frame.winfo_children():
            w.destroy()
        if self.pack.user_settings is None:
            tk.Label(self._misc_frame,
                     text="No misc settings in this pack (added on import from a "
                          "file that has them).",
                     bg=BG_PANEL, fg=FG_DIM, font=("Consolas", 10),
                     wraplength=560, justify=tk.LEFT).pack(anchor="w", padx=16,
                                                          pady=16)
            return
        tk.Label(self._misc_frame, text="Misc (userSettings)", bg=BG_PANEL,
                 fg=FG_ACCENT, font=("Consolas", 11, "bold")).pack(anchor="w",
                                                                  padx=8, pady=(8, 2))
        body = tk.Frame(self._misc_frame, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self._misc_widgets = {}
        for pair in self.pack.user_settings:
            key = pair[0] if pair else ""
            val = bool(pair[1]) if len(pair) > 1 else False
            var = tk.BooleanVar(value=val)
            tk.Checkbutton(body, text=str(key), variable=var, bg=BG_PANEL,
                           fg=FG_TEXT, activebackground=BG_PANEL,
                           selectcolor=BG_ENTRY, anchor="w",
                           font=("Consolas", 9),
                           command=lambda k=key, v=var: self._misc_toggle(k, v.get())).pack(
                anchor="w")
            self._misc_widgets[key] = var

    def _misc_toggle(self, key, val):
        for pair in (self.pack.user_settings or []):
            if pair and pair[0] == key:
                if len(pair) > 1:
                    pair[1] = bool(val)
                else:
                    pair.append(bool(val))
                return

    # ════════════════════════════════════════════════════════════════════════
    # BUILD / SAVE / CANCEL
    # ════════════════════════════════════════════════════════════════════════
    def _build_footer(self):
        bar = tk.Frame(self, bg=BG_PANEL)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self._btn(bar, "Save", self._save, style="Dark.TButton").pack(
            side=tk.RIGHT, padx=8, pady=6)
        self._btn(bar, "Cancel", self._cancel, style="Dark.TButton").pack(
            side=tk.RIGHT, padx=2, pady=6)

    def _flush_text(self):
        """Commit the one field that only lands on focus-out — the preset name
        entry (its Entry binds Return/FocusOut, not KeyRelease). Tab name/combo
        and ship-label pre/post are kept current by their own KeyRelease/command
        bindings AND by direct handler calls, so they need no flush."""
        if self._current_preset() is not None and hasattr(self, "_preset_name_var"):
            self._preset_rename(self._preset_name_var.get(), refresh=False)

    def build_pack(self):
        """Assemble the final :class:`overview_schema.OverviewPack` from the
        working model + the appearance/columns/ship-label intermediate state.
        Presets, tabs and userSettings are edited on the model in place; this
        method reflects the list-of-pairs sections deterministically."""
        self._flush_text()
        p = self.pack

        # Appearance -----------------------------------------------------------
        if self._appearance_on:
            p.flag_order = list(self._flag_rows)
            p.flag_states = [s for s in self._flag_rows if s in self._flag_enabled]
            p.background_order = list(self._bg_rows)
            p.background_states = [s for s in self._bg_rows if s in self._bg_enabled]
            blinks = []
            for s in self._flag_rows:
                if self._blink.get(("flag", s)):
                    blinks.append([f"flag_{s}", True])
            for s in self._bg_rows:
                if self._blink.get(("background", s)):
                    blinks.append([f"background_{s}", True])
            p.state_blinks = blinks
            colors = []
            for s in self._flag_rows:
                c = self._colors.get(("flag", s))
                if c:
                    colors.append([f"flag_{s}", c])
            for s in self._bg_rows:
                c = self._colors.get(("background", s))
                if c:
                    colors.append([f"background_{s}", c])
            p.state_colors = colors
        else:
            for f in _APPEARANCE_FIELDS:
                setattr(p, f, None)

        # Columns --------------------------------------------------------------
        if self._columns_on:
            p.column_order = list(self._col_rows)
            p.overview_columns = [c for c in self._col_rows if c in self._col_shown]
        else:
            p.column_order = None
            p.overview_columns = None

        # Ship labels ----------------------------------------------------------
        if self._shiplabels_on:
            order, labels = [], []
            for row in self._label_rows:
                typ = row["type"]
                if not typ or typ == "None":
                    continue
                order.append(typ)
                pairs = [["post", row["post"]], ["pre", row["pre"]],
                         ["state", 1 if row["shown"] else 0], ["type", typ]]
                # Losslessness (M1): unedited pairs (bold/color/fontsize/…)
                # ride along verbatim; ALL pair keys emitted alphabetically.
                pairs.extend(list(x) for x in row.get("extra", []))
                pairs.sort(key=lambda pr: str(pr[0]))
                labels.append([typ, pairs])
            p.ship_label_order = order
            p.ship_labels = labels
        else:
            p.ship_label_order = None
            p.ship_labels = None

        return p

    def _confirm_warnings(self, warnings):
        msg = "This pack has validation warnings:\n\n" + "\n".join(
            f"• {w}" for w in warnings[:12])
        if len(warnings) > 12:
            msg += f"\n… and {len(warnings) - 12} more"
        msg += "\n\nSave anyway?"
        return bool(messagebox.askokcancel("Validation warnings", msg, parent=self))

    def _save(self):
        pack = self.build_pack()
        warnings = osch.validate(pack)
        if warnings and not self._confirm_warnings(warnings):
            return
        self.on_save(self.pack_id, pack)
        self._close()

    def _cancel(self):
        self._close()

    def _close(self):
        for attr in ("_search_after_id", "_preset_prev_after_id"):
            aid = getattr(self, attr, None)
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, attr, None)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass


class _ColumnPickerDialog(tk.Toplevel):
    """Tiny per-tab columns picker. ``on_ok(cols)`` — ``cols`` is a list of
    column ids, or ``None`` when the user chooses "use global columns"."""

    def __init__(self, master, current, on_ok):
        super().__init__(master)
        self.title("Per-tab columns")
        self.on_ok = on_ok
        # Escape == Cancel (both plain destroy); guarded transient/grab + base bg.
        make_modal(self, master, base_bg=BG_PANEL)

        self._use_global = tk.BooleanVar(value=current is None)
        tk.Checkbutton(self, text="Use global columns", variable=self._use_global,
                       bg=BG_PANEL, fg=FG_TEXT, activebackground=BG_PANEL,
                       selectcolor=BG_ENTRY, anchor="w",
                       command=self._sync_enable).pack(anchor="w", padx=8, pady=4)
        cur = set(current or [])
        self._vars = {}
        body = tk.Frame(self, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=8)
        for cid in osch.COLUMN_IDS:
            var = tk.BooleanVar(value=(cid in cur) if current is not None else False)
            cbtn = tk.Checkbutton(body, text=cid, variable=var, bg=BG_PANEL,
                                  fg=FG_TEXT, activebackground=BG_PANEL,
                                  selectcolor=BG_ENTRY, anchor="w",
                                  font=("Consolas", 9))
            cbtn.pack(anchor="w")
            self._vars[cid] = (var, cbtn)
        bar = tk.Frame(self, bg=BG_PANEL)
        bar.pack(fill=tk.X, pady=6)
        ttk.Button(bar, text="OK", style="Dark.TButton", command=self._ok).pack(
            side=tk.RIGHT, padx=8)
        ttk.Button(bar, text="Cancel", style="Dark.TButton",
                   command=self.destroy).pack(side=tk.RIGHT)
        self._sync_enable()

    def _sync_enable(self):
        state = tk.DISABLED if self._use_global.get() else tk.NORMAL
        for _var, cbtn in self._vars.values():
            cbtn.config(state=state)

    def _ok(self):
        if self._use_global.get():
            self.on_ok(None)
        else:
            self.on_ok([cid for cid, (var, _c) in self._vars.items() if var.get()])
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
