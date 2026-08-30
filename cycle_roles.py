# cycle_roles.py
"""Cycle-group ROLES — the pure criteria vocabulary and ring resolution.

A preview cycle group is either `mode == "chars"` (its manual member list —
today's behaviour, byte-identical) or `mode == "roles"`, where the ring is
COMPUTED at every hotkey press from the live clients' CURRENT ships. This
module owns that computation and nothing else: no Tk, no ESI, no network, no
disk, no threads.

Design: ``docs/superpowers/specs/2026-08-29-preview-settings-categories-and-cycle-roles-design.md``
(Part B). The Tk editor for these criteria (``RoleCriteriaPanel``) lives in THIS
module below the core — the ``range_check.py`` / ``market_gap_dialog.py`` house
shape (pure core first, Tk shell after it, the shell's imports kept down with
the shell so the boundary is visible in the file itself). The panel imports no
``fc_gui``: every seam to the app arrives as an injected callable.

Load-bearing decisions, each one a place this feature could be silently wrong:

* **Matching has ONE owner: ``fleet_composer.pilot_matches``** (the public alias
  of the fleet composer's rule matcher). This vocabulary is deliberately a
  SUBSET of that matcher's condition types, so a cycle-group "Tag" filter and a
  fleet-template tag rule can never disagree about what a hull is.
* **Fail closed.** ``normalize_criteria`` KEEPS a criterion whose kind is not in
  ``KINDS``, and such a criterion matches nobody: a typo'd include yields an
  EMPTY ring (visible in the dialog's live-match line) rather than silently
  widening to everyone, and a typo'd exclude excludes nobody. That is also why
  ``_matches`` gates on ``KINDS`` BEFORE delegating — the matcher answers True
  to condition types this vocabulary does not offer (``default`` matches every
  pilot, ``character`` matches by name), and a hand-edited config must not be
  able to reach them through a cycle group.
* **Members arrive pre-filtered to CLASSIFIED characters.** The caller drops
  clients with no overlay state or no ``ship_type_id`` (a login screen has no
  role, and an unclassifiable pilot must never be focused by "cycle Subcaps").
  Nothing here re-derives that; a member missing fields simply matches nothing.
* **Ring order is alphabetical, casefolded.** A computed ring has no manual
  order to preserve — that is a Characters-mode feature — so it is sorted for
  determinism instead of inheriting whatever order the client scan produced.
"""
from __future__ import annotations

from types import SimpleNamespace

from fleet_composer import pilot_matches

# The criterion vocabulary — a SUBSET of fleet_composer's condition types.
KINDS = ("ship_type", "ship_class", "doctrine_tag", "capital", "subcap")

# kind -> the label the dialog shows, plus its exact inverse.
KIND_LABELS = {
    "ship_type": "Ship type",
    "ship_class": "Ship class",
    "doctrine_tag": "Tag",
    "capital": "Capitals",
    "subcap": "Subcaps",
}
LABEL_TO_KIND = {label: kind for kind, label in KIND_LABELS.items()}

# Kinds whose criterion means nothing without a value (the dialog blocks OK on
# an empty one); capital/subcap are whole-vocabulary predicates and take none.
VALUE_KINDS = frozenset({"ship_type", "ship_class", "doctrine_tag"})

# One-click starting points, written in the same vocabulary the editor produces
# so a preset stays visible and editable after it is applied. Callers DEEP-COPY
# before handing one to a group — these lists are module state.
PRESETS = {
    "Subcaps": [{"kind": "subcap", "value": "", "exclude": False}],
    "Capitals": [{"kind": "capital", "value": "", "exclude": False}],
}


def normalize_criteria(raw) -> list[dict]:
    """`raw` — anything at all, straight off config — as a list of well-formed
    criterion dicts. Never raises.

    Non-list input, and any entry that is not a dict, is dropped. Every kept
    entry becomes exactly `{"kind": str, "value": str, "exclude": bool}`, with
    missing (or None, or otherwise falsy) `kind`/`value` defaulting to `""` and
    `exclude` to False. An UNKNOWN kind is KEPT on purpose — see the module
    docstring's fail-closed note. Returns fresh dicts: the caller's config
    structures are never aliased, so an edit here can never reach the store."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append({
            "kind": str(entry.get("kind") or ""),
            "value": str(entry.get("value") or ""),
            "exclude": bool(entry.get("exclude")),
        })
    return out


def _matches(criterion, member, tag_index):
    """One normalized criterion against one member, via the single matcher."""
    kind = criterion["kind"]
    if kind not in KINDS:
        return False        # fail closed; never reach the matcher's other types
    if kind in VALUE_KINDS and not criterion["value"]:
        # An empty value must never match: pilot_matches would equate "" with an
        # UNRESOLVED hull name (ship_type_name/ship_class default to "" too) —
        # fail closed instead of matching the ghost.
        return False
    condition = SimpleNamespace(type=kind, value=criterion["value"])
    return bool(pilot_matches(condition, member, tag_index))


def role_ring(criteria, members, tag_index) -> list[str]:
    """Ordered char keys for a roles-mode cycle group.

    `criteria` is the group's stored list (normalized here, so callers may pass
    it raw); `members` is an iterable of dicts shaped `{"name",
    "ship_type_name", "ship_class", "ship_type_id", "is_capital"}` — the
    caller's snapshot of the live, CLASSIFIED clients; `tag_index` is
    `{ship_type_id: {tag, ...}}` from the active doctrine (None/empty means no
    doctrine, so tag criteria match nothing).

    A member is in the ring iff it matches at least one INCLUDE criterion — or
    there are none, so an only-excludes list reads as "everyone but…" — and
    matches NO exclude. Names come back sorted casefold-alphabetically and
    deduped; a member with no name is dropped (it cannot be a ring stop).
    Inputs are never mutated."""
    crits = normalize_criteria(criteria)
    includes = [c for c in crits if not c["exclude"]]
    excludes = [c for c in crits if c["exclude"]]
    index = tag_index or {}
    names = []
    for member in members or ():
        name = member.get("name") if isinstance(member, dict) else None
        if not name:
            continue
        if includes and not any(_matches(c, member, index) for c in includes):
            continue
        if any(_matches(c, member, index) for c in excludes):
            continue
        names.append(str(name))
    return list(dict.fromkeys(sorted(names, key=str.casefold)))


# ══════════════════════════════════════════════════════════════════════════════
# Tk shell — the dialog's criteria editor. Everything above this line is pure.
# ══════════════════════════════════════════════════════════════════════════════
import copy                                                       # noqa: E402
import tkinter as tk                                              # noqa: E402
from tkinter import ttk                                           # noqa: E402

# House palette, single source (ui_theme imports nothing and never drifts).
from ui_theme import BG_PANEL, FG_DIM, FG_TEXT                     # noqa: E402

_MODE_INCLUDE = "Include"
_MODE_EXCLUDE = "Exclude"
_MODE_VALUES = (_MODE_INCLUDE, _MODE_EXCLUDE)


class _Row:
    """One criterion's live widgets. These attribute names are TEST SEAMS —
    the dialog suite drives the panel through them, so renaming one is an API
    change, not a tidy-up.

    ``_traces`` is NOT a test seam — it is internal bookkeeping: the
    (var, mode, callback-name) triples ``trace_add`` hands back, so
    ``_destroy_row`` can remove every trace before the row's widgets go away
    (see the constraint noted where the traces are added, in ``_build_row``)."""

    __slots__ = ("_mode_var", "_kind_var", "_value_var",
                 "_mode_combo", "_kind_combo", "_value_combo", "_del_btn",
                 "_traces")


class RoleCriteriaPanel(tk.Frame):
    """The roles-mode editor for ONE cycle group's criteria (design §B4).

    Ctor seams — every one a callable, so this class knows nothing about
    fc_gui, config, ESI or the SDE:

    ``get_criteria()``      the group's stored criteria (raw; normalized here).
    ``set_criteria(list)``  receives the FULL normalized list after every edit.
    ``ship_type_names()`` / ``ship_group_names()`` / ``tag_names()``
                            value-combobox catalogs, called lazily and cached
                            for the panel's lifetime (a modal dialog cannot
                            change the bundled SDE or the active doctrine under
                            itself); ``reload()`` drops the cache.
    ``live_members()``      ``(member dicts, unclassified_count)`` for the
                            live-match line — the caller's snapshot of the
                            CLASSIFIED clients plus how many it had to drop.
    ``tag_index()``         ``{ship_type_id: {tag}}`` from the active doctrine.
    ``on_change()``         optional; fired after a user EDIT, never after a
                            programmatic render (the dialog repaints its group
                            list row from it).

    **Edits flow through traced StringVars, not ``<<ComboboxSelected>>``.**
    That is deliberate and load-bearing for testability: a write trace fires
    for a dropdown pick, a keystroke, a ``.set()`` and even on a disabled
    widget, whereas a real key event is DROPPED on an unmapped widget
    (map/facts.md), which would make every headless assertion about typing
    vacuous. ``_loading`` guards the re-entrancy that buys.
    """

    def __init__(self, parent, *, get_criteria, set_criteria,
                 ship_type_names, ship_group_names, tag_names,
                 live_members, tag_index, on_change=None):
        super().__init__(parent, bg=BG_PANEL)
        self._get_criteria = get_criteria
        self._set_criteria = set_criteria
        self._providers = {"ship_type": ship_type_names,
                           "ship_class": ship_group_names,
                           "doctrine_tag": tag_names}
        self._live_members = live_members
        self._tag_index = tag_index
        self._on_change = on_change
        self._rows: list[_Row] = []
        self._catalogs: dict[str, list[str]] = {}
        self._loading = False       # True while the panel writes its own vars
        self._enabled = True

        preset_row = tk.Frame(self, bg=BG_PANEL)
        preset_row.pack(fill=tk.X)
        tk.Label(preset_row, text="Preset:", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=(0, 4))
        self._btn_preset_subcaps = ttk.Button(
            preset_row, text="Subcaps", style="Dark.TButton",
            command=lambda: self._apply_preset("Subcaps"))
        self._btn_preset_subcaps.pack(side=tk.LEFT, padx=(0, 4))
        self._btn_preset_capitals = ttk.Button(
            preset_row, text="Capitals", style="Dark.TButton",
            command=lambda: self._apply_preset("Capitals"))
        self._btn_preset_capitals.pack(side=tk.LEFT)

        tk.Label(self, text="Any Include matches; every Exclude removes one. "
                 "No filters = every pilot with a known ship.",
                 bg=BG_PANEL, fg=FG_DIM, font=("Consolas", 8),
                 justify=tk.LEFT, wraplength=420).pack(anchor="w", pady=(2, 2))

        self._rows_frame = tk.Frame(self, bg=BG_PANEL)
        self._rows_frame.pack(fill=tk.X)
        self._btn_add = ttk.Button(self, text="+ Add filter",
                                   style="Dark.TButton", command=self._add_row)
        self._btn_add.pack(anchor="w", pady=(4, 0))
        self._match_lbl = tk.Label(self, text="", bg=BG_PANEL, fg=FG_DIM,
                                   font=("Consolas", 8), justify=tk.LEFT,
                                   wraplength=420)
        self._match_lbl.pack(anchor="w", pady=(4, 0))

        self.reload()
        # A dialog close destroys this widget directly -- nothing walks _rows
        # and calls _delete_row/_render first. Without this, whatever rows
        # were last rendered keep their traces registered forever (see
        # _on_destroy).
        self.bind("<Destroy>", self._on_destroy)

    # ── public API ───────────────────────────────────────────────────────────
    def reload(self):
        """Re-read ``get_criteria()`` and re-render. The dialog calls this on
        every group-selection change — a render is NOT an edit, so nothing is
        written back and ``on_change`` never fires."""
        self._catalogs.clear()
        self._render(normalize_criteria(self._call(self._get_criteria, [])))

    def set_enabled(self, on):
        """Grey (or restore) every control — the dialog's no-group-selected
        state. Re-enabling restores each value field's PER-KIND state, so a
        Capitals row never comes back with an editable value."""
        self._enabled = bool(on)
        btn_state = "normal" if self._enabled else "disabled"
        combo_state = "readonly" if self._enabled else "disabled"
        for btn in (self._btn_preset_subcaps, self._btn_preset_capitals,
                    self._btn_add):
            _configure(btn, state=btn_state)
        for row in self._rows:
            _configure(row._mode_combo, state=combo_state)
            _configure(row._kind_combo, state=combo_state)
            _configure(row._del_btn, state=btn_state)
            self._sync_value_widget(row)

    # ── rendering ────────────────────────────────────────────────────────────
    def _render(self, criteria):
        self._loading = True
        try:
            for row in self._rows:
                self._destroy_row(row)
            self._rows = []
            for crit in criteria:
                self._rows.append(self._build_row(crit))
        finally:
            self._loading = False
        self._regrid()
        self._refresh_matches()

    def _build_row(self, crit):
        row = _Row()
        row._mode_var = tk.StringVar(
            value=_MODE_EXCLUDE if crit["exclude"] else _MODE_INCLUDE)
        # An unknown kind has no label — show the raw string so a hand-edited
        # config round-trips visibly instead of being silently rewritten.
        row._kind_var = tk.StringVar(
            value=KIND_LABELS.get(crit["kind"], crit["kind"]))
        row._value_var = tk.StringVar(value=crit["value"])
        combo_state = "readonly" if self._enabled else "disabled"
        btn_state = "normal" if self._enabled else "disabled"
        row._mode_combo = ttk.Combobox(
            self._rows_frame, textvariable=row._mode_var,
            values=list(_MODE_VALUES), state=combo_state, width=8,
            font=("Consolas", 9))
        row._kind_combo = ttk.Combobox(
            self._rows_frame, textvariable=row._kind_var,
            values=[KIND_LABELS[k] for k in KINDS], state=combo_state, width=11,
            font=("Consolas", 9))
        row._value_combo = ttk.Combobox(
            self._rows_frame, textvariable=row._value_var, values=[], width=20,
            font=("Consolas", 9))
        row._del_btn = ttk.Button(
            self._rows_frame, text="✕", width=2, style="Dark.TButton",
            command=lambda r=row: self._delete_row(r), state=btn_state)
        # Each trace closure captures `row`, which owns the StringVar — the
        # Tcl command table roots command -> lambda -> row -> Variable, so
        # every trace added here MUST be removed in _destroy_row, or the row
        # (3 Tcl vars + 3 Tcl commands) leaks for the process lifetime.
        mode_cb = row._mode_var.trace_add(
            "write", lambda *_a, r=row: self._on_edit(r))
        kind_cb = row._kind_var.trace_add(
            "write", lambda *_a, r=row: self._on_kind(r))
        value_cb = row._value_var.trace_add(
            "write", lambda *_a, r=row: self._on_value(r))
        row._traces = [(row._mode_var, "write", mode_cb),
                       (row._kind_var, "write", kind_cb),
                       (row._value_var, "write", value_cb)]
        self._sync_value_widget(row)
        return row

    def _destroy_row(self, row):
        """Single owner of row teardown — both ``_render``'s teardown loop and
        ``_delete_row`` go through this, never a second copy of the logic.
        Traces are removed before the widgets are destroyed, per the
        constraint noted where they are added in ``_build_row``."""
        for var, mode, cbname in row._traces:
            try:
                var.trace_remove(mode, cbname)
            except tk.TclError:
                pass
        row._traces = []
        for wdg in (row._mode_combo, row._kind_combo, row._value_combo,
                    row._del_btn):
            try:
                wdg.destroy()
            except tk.TclError:
                pass

    def _on_destroy(self, event):
        """Last-chance row teardown when the DIALOG closes the panel directly.

        ``<Destroy>`` bubbles from every descendant (each row's own widgets
        fire it first, bottom-up), not just this panel, so bail unless this
        event is for the panel itself. Without this, whatever rows were still
        rendered at close time never go through ``_destroy_row`` -- their
        traces (3 Tcl vars + 3 Tcl commands per row) are never removed, and
        the Tcl command table roots them (command -> lambda -> row ->
        Variable) for the rest of the process, same as the leak
        ``_destroy_row`` already prevents on every reload/delete. Teardown
        order during a window close is not guaranteed, so this (like
        ``_destroy_row``) tolerates widgets or a variable's trace that are
        already gone."""
        if event.widget is not self:
            return
        rows, self._rows = self._rows, []
        for row in rows:
            try:
                self._destroy_row(row)
            except tk.TclError:
                pass

    def _regrid(self):
        for wdg in self._rows_frame.winfo_children():
            wdg.grid_forget()
        for r, row in enumerate(self._rows):
            row._mode_combo.grid(row=r, column=0, sticky="w", padx=(0, 2), pady=1)
            row._kind_combo.grid(row=r, column=1, sticky="w", padx=2, pady=1)
            row._value_combo.grid(row=r, column=2, sticky="w", padx=2, pady=1)
            row._del_btn.grid(row=r, column=3, padx=(2, 0), pady=1)

    def _sync_value_widget(self, row):
        """Enable/disable + re-stock one row's value combobox for its kind."""
        kind = self._row_kind(row)
        if kind in VALUE_KINDS and self._enabled:
            _configure(row._value_combo, state="normal")
            self._filter_values(row, kind)
        else:
            _configure(row._value_combo, state="disabled", values=[])

    def _filter_values(self, row, kind):
        """Type-to-filter: the whole catalog, narrowed by what is typed so far
        (case-folded substring, the `_add_rule_row` autocomplete behaviour)."""
        typed = row._value_var.get().strip().lower()
        catalog = self._catalog(kind)
        _configure(row._value_combo,
                   values=[v for v in catalog if typed in v.lower()]
                   if typed else list(catalog))

    def _catalog(self, kind):
        if kind not in self._catalogs:
            provider = self._providers.get(kind)
            values = self._call(provider, ()) if provider is not None else ()
            self._catalogs[kind] = [str(v) for v in (values or ())]
        return self._catalogs[kind]

    # ── edits ────────────────────────────────────────────────────────────────
    def _add_row(self):
        self._rows.append(self._build_row(
            {"kind": "ship_type", "value": "", "exclude": False}))
        self._regrid()
        self._commit()

    def _delete_row(self, row):
        try:
            self._rows.remove(row)
        except ValueError:
            return
        self._destroy_row(row)
        self._regrid()
        self._commit()

    def _apply_preset(self, name):
        # DEEP COPY: PRESETS values are live module lists — handing one straight
        # to a group would let the next edit rewrite the constant for everybody.
        self._render(normalize_criteria(copy.deepcopy(PRESETS.get(name, []))))
        self._commit()

    def _on_edit(self, _row):
        if self._loading:
            return
        self._commit()

    def _on_kind(self, row):
        if self._loading:
            return
        # A KNOWN valueless kind clears its value (the data model says "" there)
        # — under the loading guard so the clear is ONE edit, not two. An
        # unknown kind is left alone, matching `_criteria`'s round-trip rule.
        kind = self._row_kind(row)
        if kind in KINDS and kind not in VALUE_KINDS and row._value_var.get():
            self._loading = True
            try:
                row._value_var.set("")
            finally:
                self._loading = False
        self._sync_value_widget(row)
        self._commit()

    def _on_value(self, row):
        if self._loading:
            return
        kind = self._row_kind(row)
        if kind in VALUE_KINDS:
            self._filter_values(row, kind)
        self._commit()

    def _commit(self):
        self._set_criteria(self._criteria())
        self._refresh_matches()
        if self._on_change is not None:
            self._on_change()

    # ── criteria <-> widgets ─────────────────────────────────────────────────
    def _row_kind(self, row):
        label = row._kind_var.get()
        return LABEL_TO_KIND.get(label, label)

    def _criteria(self):
        """The rows as a normalized criteria list. Values are handed on
        VERBATIM (whitespace included): the core does not strip, and the dialog
        owns validation — tidying here would hide an empty-ish filter from it.

        Only a KNOWN valueless kind (capital/subcap) has its value forced to ""
        — an unknown kind keeps whatever the config held, so opening the editor
        over a hand-edited group never quietly rewrites it."""
        out = []
        for row in self._rows:
            kind = self._row_kind(row)
            valueless = kind in KINDS and kind not in VALUE_KINDS
            value = "" if valueless else row._value_var.get()
            out.append({"kind": str(kind), "value": str(value),
                        "exclude": row._mode_var.get() == _MODE_EXCLUDE})
        return out

    # ── live match line ──────────────────────────────────────────────────────
    def _refresh_matches(self):
        criteria = self._criteria()
        members, skipped = self._members()
        index = self._call(self._tag_index, {}) or {}
        hulls = {}
        for member in members:
            if isinstance(member, dict):
                hulls.setdefault(str(member.get("name") or ""),
                                 str(member.get("ship_type_name") or ""))
        ring = role_ring(criteria, members, index)
        shown = [f"{n} ({hulls[n]})" if hulls.get(n) else n for n in ring]
        text = "Matches now: " + (", ".join(shown) if shown else "none")
        if skipped:
            text += f" · {skipped} without ship data"
        if not index and any(c["kind"] == "doctrine_tag" for c in criteria):
            text += " — no active doctrine, Tag filters match nothing"
        _configure(self._match_lbl, text=text)

    def _members(self):
        """``live_members()`` defended down to ``([], 0)`` — a match line that
        renders "none" is honest; one that raises takes the dialog with it."""
        snapshot = self._call(self._live_members, ([], 0))
        try:
            members, skipped = snapshot
            return list(members or ()), int(skipped or 0)
        except (TypeError, ValueError):
            return [], 0

    @staticmethod
    def _call(provider, fallback):
        if provider is None:
            return fallback
        try:
            return provider()
        except Exception:
            return fallback


def _configure(widget, **options):
    """`widget.configure(**options)` that survives a torn-down widget — the
    panel repaints rows the user may already have closed the dialog over."""
    try:
        widget.configure(**options)
    except tk.TclError:
        pass
