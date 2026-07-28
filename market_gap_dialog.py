# market_gap_dialog.py
"""Market "Gaps…" shopping-list dialog — per-ship selection + per-item removal.

Splits into a **pure core** (``GapSelection`` + the two summary formatters — no
Tk, no config, no scanner) and a **thin Tk shell** (``open_gap_dialog``). The
shell never touches snapshot/scanner/config state itself: the host injects
``build_gap`` and ``copy_text`` callables (the house provider pattern
``overview_manager_ui`` established), so this module imports only tkinter plus
the two containment-safe leaves ``ui_theme`` / ``ui_helpers``. It must never
import ``fc_gui`` or ``market_scanner``.

Why the module exists (design 2026-07-27 §1): the dialog used to run the ENTIRE
doctrine at whatever the persisted seed targets said. There was no way to ask
"what do I need for 10 Onyxes and 5 Scythes, hulls only, and I already have the
Warp Disruptors" without editing the doctrine's persistent per-fit seed targets,
exporting, then editing them back.

Scratch-only by owner decision: in-dialog quantities are pre-filled from the
saved seed targets, edits apply to THIS report and are forgotten on close.
Nothing here writes config. The hidden-item set is sticky within the window
(survives every rebuild, cleared by ``Restore all``) and likewise forgotten.

**Honesty guards are load-bearing (§4.2).** An empty ship selection and an empty
category set both make the backend return an EMPTY gap list, which the summary
would render as *"No gaps — the market covers … ✓"*. That is a lie. Both states
short-circuit BEFORE ``build_gap`` is called and render their own message
instead, and both disable the Copy buttons. ``is_empty_selection()`` /
``is_empty_categories()`` / ``guard_message()`` are pure so this is testable
without Tk. A third, subtler case: a genuinely-covered market with items hidden
must not read as a clean bill of health either, so the ``(N hidden · Restore
all)`` suffix rides the "No gaps" line too.
"""
from __future__ import annotations

import copy
import logging
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

# Shared house palette + dialog/tooltip contract (both stdlib-only leaves).
# NEVER re-declare a colour literal here — ui_theme is the single source of
# truth and tests assert import identity.
from ui_theme import (
    BG_DARK, BG_ENTRY, BG_PANEL,
    BORDER_COLOR,
    FG_ACCENT, FG_DIM, FG_GREEN, FG_ORANGE, FG_RED, FG_TEXT, FG_WHITE,
)
from ui_helpers import attach_tooltip, make_modal

log = logging.getLogger(__name__)

# ── Contract constants ───────────────────────────────────────────────────────

#: The six BoM roles ``market_scanner.gap_list`` understands, in display order.
#: Re-declared locally rather than imported (the tkinter-only isolation rule);
#: must stay in lockstep with ``market_scanner.Component.role``.
CATEGORY_ORDER = ("hull", "module", "subsystem", "charge", "drone", "cargo")
CATEGORY_LABELS = {
    "hull": "Hulls", "module": "Modules", "subsystem": "Subsystems",
    "charge": "Charges", "drone": "Drones", "cargo": "Cargo",
}

#: Honesty-guard copy (§4.2). Rendered INSTEAD of a gap table, never alongside.
EMPTY_SELECTION_MSG = "Select at least one ship to seed."
EMPTY_CATEGORIES_MSG = "Tick at least one item category."

#: Treeview columns. ``REMOVE_COL`` is looked up by ID, never by index — a
#: future column reorder must not silently move the ✕ hit target (§9).
REMOVE_COL = "remove"
COLUMNS = ("item", "needed", "available", "short", REMOVE_COL)
HEADINGS = {"item": "Item", "needed": "needed", "available": "available",
            "short": "short", REMOVE_COL: ""}
_COL_WIDTH = {"item": 260, "needed": 74, "available": 84, "short": 68,
              REMOVE_COL: 28}
REMOVE_GLYPH = "✕"          # ✕ — GUI text only, never logged (cp1252 box)

# Ship picker geometry: rows scroll past this many entries.
_SHIP_ROWS_VISIBLE = 8
_SHIP_ROW_H = 25


def _as_int(value, default=0):
    """Best-effort int coercion that never raises (bools included by design)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Pure core (§4.1) ─────────────────────────────────────────────────────────


@dataclass
class ShipPick:
    """One doctrine member as the picker sees it.

    ``label`` is the fit name (what the row shows), ``hull`` the hull name
    rendered in parentheses ("" when unknown). ``include``/``qty`` are the
    SCRATCH state — seeded from the saved seed target, never written back.
    """

    fit_id: str
    label: str = ""
    hull: str = ""
    include: bool = True
    qty: int = 0


def hidden_suffix(n):
    """The ``(N hidden · Restore all)`` parenthetical, or "" when nothing is
    hidden. Named the affordance so the summary line itself tells the user how
    to undo a removal."""
    n = max(0, _as_int(n, 0))
    if n <= 0:
        return ""
    return f"({n} hidden · Restore all)"


def summary_line(gap, hidden=0):
    """Pure formatter: the one-line summary above the gap table.

    ``N item(s) short for <target basis>`` where the basis is the GapList's own
    ``target_desc`` — which ``GapSelection.visible()`` has already rewritten to
    the dialog's ``basis_desc()``, so the line never claims the doctrine's saved
    config when the user has overridden it. Empty items → a 'covered' line
    naming the same basis. Mirrors ``FCToolGUI._market_gap_summary`` verbatim,
    plus the hidden-items suffix (§4.2: a covered market WITH items hidden must
    not read as a clean bill of health).
    """
    items = list(getattr(gap, "items", []) or [])
    target = str(getattr(gap, "target_desc", "") or "the seed target")
    n = len(items)
    if n == 0:
        base = f"No gaps — the market covers {target} ✓"
    else:
        noun = "item" if n == 1 else "items"
        base = f"{n} {noun} short for {target}"
    suffix = hidden_suffix(hidden)
    return f"{base}  {suffix}" if suffix else base


class GapSelection:
    """Scratch selection state for one open Gaps window. Pure: no Tk, no config.

    Owns three independent axes:

    * **ships** — per-fit include flag + quantity, resolved into ``gap_list``'s
      ``per_fit_targets`` map (excluded → ``0``, which contributes nothing);
    * **categories** — the six BoM roles, resolved into ``gap_list``'s
      ``components`` argument (``None`` when all six are on, i.e. byte-identical
      to the full-BoM call);
    * **hidden** — type_ids the user ✕'d out, which survive every rebuild and
      are filtered by :meth:`visible`.

    The opening ship state is snapshotted so ``Reset to doctrine defaults`` can
    restore it (include + qty ONLY — it deliberately does not clear hidden items
    or category toggles).
    """

    def __init__(self, picks, *, doctrine_name="", default_qty=20,
                 categories=None, hidden=None):
        # De-duplicate by fit_id, keeping the FIRST occurrence (display order =
        # doctrine member order). Duplicates are representable — neither
        # ``fittings_store.add_fit_to_doctrine`` nor the MOTD→doctrine import
        # guards against adding the same fit twice — and EVERYTHING downstream
        # here is keyed by fit_id: ``pick()`` resolves to the first match while
        # ``per_fit_targets()``'s comprehension lets the last one win, so a
        # shadowed row's checkbox silently stopped changing anything. Collapsing
        # on the way in costs nothing: ``gap_list`` reads one target per fit_id
        # for every member carrying it, so one row means the same shopping list.
        self._picks = []
        seen = set()
        for p in (picks or ()):
            if p is None or p.fit_id in seen:
                continue
            seen.add(p.fit_id)
            self._picks.append(p)
        self.doctrine_name = str(doctrine_name or "doctrine")
        self.default_qty = max(0, _as_int(default_qty, 20))
        if categories is None:
            self._categories = set(CATEGORY_ORDER)
        else:
            self._categories = {str(c) for c in categories
                                if str(c) in CATEGORY_ORDER}
        self._hidden = {_as_int(t, 0) for t in (hidden or ())}
        # Opening state, for reset_to_defaults(). Also the yardstick basis_desc()
        # uses to decide whether the report still describes the saved config.
        self._defaults = {p.fit_id: (bool(p.include), max(0, _as_int(p.qty, 0)))
                          for p in self._picks}
        for p in self._picks:
            p.include = bool(p.include)
            p.qty = max(0, _as_int(p.qty, 0))

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def from_members(cls, members, *, resolve_target, fit_info,
                     doctrine_name, doctrine_target):
        """Build the picker state from a doctrine's members.

        ``resolve_target(member) -> int`` gives the member's SAVED seed target
        (the host passes ``FCToolGUI._market_seed_target`` bound to the
        doctrine); ``fit_info(fit_id)`` gives the display labels and may return a
        ``Fit``-shaped object, a ``(label, hull)`` pair, a bare string or None.

        A member whose saved target is ``0`` ("don't seed this fit") starts
        UNCHECKED — re-checking it defaults its qty to ``doctrine_target``.
        """
        target_default = max(0, _as_int(doctrine_target, 20))
        picks = []
        for mem in (members or ()):
            fid = getattr(mem, "fit_id", None)
            if fid is None:
                continue
            fid = str(fid)
            try:
                target = _as_int(resolve_target(mem), target_default)
            except Exception:
                log.exception("[gaps] seed-target resolution failed for %s", fid)
                target = target_default
            target = max(0, target)
            label, hull = _fit_labels(fit_info, fid)
            picks.append(ShipPick(fit_id=fid, label=(label or fid), hull=hull,
                                  include=target > 0, qty=target))
        return cls(picks, doctrine_name=doctrine_name,
                   default_qty=target_default)

    # ── read-only views ──────────────────────────────────────────────────────
    @property
    def picks(self):
        """The live ShipPick list (display order = doctrine member order)."""
        return list(self._picks)

    @property
    def hidden(self):
        """Copy of the hidden type_id set."""
        return set(self._hidden)

    def categories(self):
        """Ticked roles, in ``CATEGORY_ORDER``."""
        return [r for r in CATEGORY_ORDER if r in self._categories]

    def is_category_on(self, role):
        return str(role) in self._categories

    def pick(self, fit_id):
        """The ShipPick for ``fit_id`` (or None). Live object — mutating it
        directly bypasses the clamps; go through the setters."""
        fid = str(fit_id)
        return next((p for p in self._picks if p.fit_id == fid), None)

    # ── resolution into gap_list kwargs ──────────────────────────────────────
    def per_fit_targets(self):
        """``fit_id -> target`` for EVERY pick: ``qty`` when included, ``0`` when
        not. Always complete, so ``gap_list``'s ``target_fits`` fallback is never
        consulted for a listed fit (a 0 target is already skipped at
        ``market_scanner.py``'s ``units <= 0`` guard)."""
        return {p.fit_id: (max(0, int(p.qty)) if p.include else 0)
                for p in self._picks}

    def components(self):
        """``None`` when all six categories are on — byte-identical to today's
        full-BoM call — otherwise the ticked roles in ``CATEGORY_ORDER``."""
        if len(self._categories) >= len(CATEGORY_ORDER):
            return None
        return self.categories()

    # ── basis description (the honesty seam) ─────────────────────────────────
    def _is_default_picks(self):
        """True when every pick still carries its opening include+qty."""
        for p in self._picks:
            want = self._defaults.get(p.fit_id)
            if want is None:
                return False
            if (bool(p.include), int(p.qty)) != want:
                return False
        return True

    def _saved_basis(self):
        """What ``gap_list`` itself would have called this report before the
        picker existed: one uniform target → ``"20x <doctrine>"``; targets that
        genuinely differ across fits → ``"per-fit seed targets · <doctrine>"``."""
        targets = set(self.per_fit_targets().values())
        if len(targets) > 1:
            return f"per-fit seed targets · {self.doctrine_name}"
        uniform = next(iter(targets)) if targets else self.default_qty
        return f"{uniform}x {self.doctrine_name}"

    def _category_suffix(self):
        """``" · hulls only"`` / ``" · hulls + modules"`` … or "" when all six
        categories are on (nothing was narrowed, so nothing to disclose)."""
        roles = self.categories()
        if not roles or len(roles) >= len(CATEGORY_ORDER):
            return ""
        labels = [CATEGORY_LABELS[r].lower() for r in roles]
        if len(labels) == 1:
            return f" · {labels[0]} only"
        return " · " + " + ".join(labels)

    def basis_desc(self):
        """The human basis this report is actually for — stamped onto
        :meth:`visible`'s GapList as ``target_desc``.

        Untouched selection + all categories → today's string, unchanged.
        Otherwise the ships actually asked for (``"20x Onyx HIC + 5x Scythe
        Fleet Logi"``), collapsing past three included fits to
        ``"<N> fits · <doctrine>"``, plus the category suffix when narrowed.
        """
        suffix = self._category_suffix()
        if self._is_default_picks() and not suffix:
            return self._saved_basis()
        included = [p for p in self._picks if p.include and int(p.qty) > 0]
        if not included:
            return f"0 fits · {self.doctrine_name}{suffix}"
        if len(included) > 3:
            return f"{len(included)} fits · {self.doctrine_name}{suffix}"
        body = " + ".join(f"{int(p.qty)}x {p.label or p.fit_id}"
                          for p in included)
        return f"{body}{suffix}"

    # ── mutation ─────────────────────────────────────────────────────────────
    def set_include(self, fit_id, on):
        """Include/exclude a fit. Re-checking a ship whose qty is 0 (the saved
        "don't seed this fit" state) defaults it to the doctrine target, so a
        freshly-ticked ship can never contribute a silent nothing."""
        p = self.pick(fit_id)
        if p is None:
            return
        p.include = bool(on)
        if p.include and int(p.qty) <= 0:
            p.qty = max(1, self.default_qty)

    def set_qty(self, fit_id, qty):
        """Set a fit's scratch quantity, clamped to >= 0 (garbage → 0)."""
        p = self.pick(fit_id)
        if p is None:
            return
        p.qty = max(0, _as_int(qty, 0))

    def set_all_included(self, on):
        for p in self._picks:
            self.set_include(p.fit_id, on)

    def reset_to_defaults(self):
        """Restore the opening include+qty state. Deliberately does NOT clear
        hidden items or category toggles (§4.3)."""
        for p in self._picks:
            want = self._defaults.get(p.fit_id)
            if want is None:
                continue
            p.include, p.qty = bool(want[0]), int(want[1])

    def set_category(self, role, on):
        role = str(role)
        if role not in CATEGORY_ORDER:
            return
        if on:
            self._categories.add(role)
        else:
            self._categories.discard(role)

    def hide(self, type_id):
        """Drop one item from the table and BOTH exports. Sticky across
        rebuilds; cleared only by :meth:`restore_all` or closing the window."""
        self._hidden.add(_as_int(type_id, 0))

    def restore_all(self):
        self._hidden.clear()

    # ── rendering / honesty guards ───────────────────────────────────────────
    def is_empty_selection(self):
        """No ship included, or every included qty is 0 (§4.2 guard 1)."""
        return not any(p.include and int(p.qty) > 0 for p in self._picks)

    def is_empty_categories(self):
        """No category ticked (§4.2 guard 2)."""
        return not self._categories

    def guard_message(self):
        """The honesty message to render INSTEAD of calling ``gap_list``, or
        ``None`` when the selection is answerable. Selection is checked first
        (the picker sits above the category row, so that is the first thing the
        user can fix)."""
        if self.is_empty_selection():
            return EMPTY_SELECTION_MSG
        if self.is_empty_categories():
            return EMPTY_CATEGORIES_MSG
        return None

    def visible(self, gap):
        """A NEW GapList with hidden items filtered out and ``target_desc`` set
        to :meth:`basis_desc`.

        Never mutates ``gap`` — ``market_scanner.GapList`` is a plain (non-frozen)
        dataclass, and mutating the build result would compound the hidden set
        across rebuilds (§9). The copy keeps ``gap``'s own class, so
        ``janice()`` / ``multibuy()`` keep working untouched.
        """
        items = [it for it in (getattr(gap, "items", None) or [])
                 if _as_int(getattr(it, "type_id", None), -1) not in self._hidden]
        desc = self.basis_desc()
        try:
            out = copy.copy(gap)
            out.items = items
            out.target_desc = desc
            return out
        except Exception:
            return type(gap)(items=items, target_desc=desc)

    def hidden_count(self, gap):
        """How many of ``gap.items`` this selection is hiding (0 when none)."""
        return sum(1 for it in (getattr(gap, "items", None) or [])
                   if _as_int(getattr(it, "type_id", None), -1) in self._hidden)


def _fit_labels(fit_info, fit_id):
    """``(label, hull)`` from an injected ``fit_info(fit_id)`` provider.

    Tolerant of every shape the host might hand back — a ``Fit``-shaped object
    (``.name`` / ``.hull_name``), a ``(label, hull)`` pair, a bare name string,
    or None — so the wiring side is free to pass ``fittings.get_fit`` directly.
    A raising provider degrades to blank labels, never to a broken dialog.
    """
    if fit_info is None:
        return "", ""
    try:
        info = fit_info(fit_id)
    except Exception:
        log.exception("[gaps] fit_info lookup failed for %s", fit_id)
        return "", ""
    if info is None:
        return "", ""
    if isinstance(info, str):
        return info, ""
    if isinstance(info, (tuple, list)):
        label = str(info[0]) if len(info) > 0 and info[0] else ""
        hull = str(info[1]) if len(info) > 1 and info[1] else ""
        return label, hull
    label = str(getattr(info, "name", "") or "")
    hull = str(getattr(info, "hull_name", "")
               or getattr(info, "hull", "") or "")
    return label, hull


# ── Tk shell (§4.3) ──────────────────────────────────────────────────────────


def open_gap_dialog(parent, *, doctrine_name, picks, doctrine_target,
                    build_gap, copy_text, on_close=None):
    """Open the market-gaps shopping-list dialog and return its ``Toplevel``.

    ``parent``          owning window (the app root).
    ``doctrine_name``   title/basis text.
    ``picks``           list[ShipPick] — the doctrine members, pre-filled from
                        the saved seed targets (see ``GapSelection.from_members``).
    ``doctrine_target`` the doctrine-level seed bar; a re-checked 0-target ship
                        defaults to it.
    ``build_gap(per_fit_targets, components) -> (gap, error)``
                        injected builder. ``gap`` is a ``market_scanner.GapList``
                        (or None on failure), ``error`` a short user-facing
                        string (or None on success). This module never touches
                        snapshot/scanner/config state itself.
    ``copy_text(text) -> bool``  injected clipboard writer.
    ``on_close``        optional zero-arg callback fired once, on any close path
                        (Close button, WM_DELETE, ``<Escape>``).

    The dialog is TRULY modal (``ui_helpers.make_modal`` adds the grab the old
    in-``fc_gui`` version lacked) and is centred over ``parent``. Rebuilds are
    inline on every include toggle / qty commit / category toggle — ``gap_list``
    is pure and in-memory, so there is no "Recalculate" button.

    Test surface (attributes on the returned Toplevel): ``gap_selection``,
    ``gap_tree``, ``gap_rebuild()``, ``gap_export(kind)``, ``gap_summary_text()``,
    ``gap_error_text()``, ``gap_include_vars``, ``gap_qty_vars``,
    ``gap_category_vars``, ``gap_copy_buttons``, ``gap_hide_selected()``,
    ``gap_restore_all()``, ``gap_build_menu()``, ``gap_status_text()``,
    ``gap_toggle_include()``, ``gap_toggle_category()``, ``gap_click()``,
    ``gap_wheel()``, ``gap_ships_canvas``, ``gap_ships_rows``.
    """
    dname = str(doctrine_name or "doctrine")
    sel = GapSelection(list(picks or ()), doctrine_name=dname,
                       default_qty=doctrine_target)

    dlg = tk.Toplevel(parent)
    dlg.title(f"Market gaps — {dname}")

    closed = {"done": False}

    def _close(_evt=None):
        if not closed["done"]:
            closed["done"] = True
            if callable(on_close):
                try:
                    on_close()
                except Exception:
                    log.exception("[gaps] on_close callback failed")
        try:
            dlg.destroy()
        except tk.TclError:
            pass

    # House modal contract: guarded transient + grab_set + <Escape> → the SAME
    # close path as the Close button (never a blind destroy).
    make_modal(dlg, parent, on_cancel=_close)
    dlg.protocol("WM_DELETE_WINDOW", _close)

    body = tk.Frame(dlg, bg=BG_DARK, padx=18, pady=16,
                    highlightbackground=BORDER_COLOR, highlightthickness=1)
    body.pack(fill=tk.BOTH, expand=True)
    tk.Label(body, text=f"Market gaps — {dname}",
             font=("Consolas", 12, "bold"), fg=FG_ACCENT, bg=BG_DARK,
             anchor=tk.W).pack(fill=tk.X, pady=(0, 8))

    state = {"gap": None, "vis": None, "error": None}
    include_vars, qty_vars, qty_entries, off_labels = {}, {}, {}, {}
    category_vars = {}

    # ── ship picker ──────────────────────────────────────────────────────────
    ships_wrap = tk.Frame(body, bg=BG_PANEL, highlightbackground=BORDER_COLOR,
                          highlightthickness=1)
    ships_wrap.pack(fill=tk.X, pady=(0, 8))
    tk.Label(ships_wrap, text="Ships to seed", font=("Consolas", 9, "bold"),
             fg=FG_DIM, bg=BG_PANEL, anchor=tk.W).pack(fill=tk.X, padx=8,
                                                       pady=(5, 2))

    rows_host = tk.Frame(ships_wrap, bg=BG_PANEL)
    rows_host.pack(fill=tk.X, padx=8)
    n_picks = len(sel.picks)
    canvas_h = max(1, min(n_picks, _SHIP_ROWS_VISIBLE)) * _SHIP_ROW_H
    ships_canvas = tk.Canvas(rows_host, bg=BG_PANEL, highlightthickness=0,
                             height=canvas_h)
    ships_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ships_sb = ttk.Scrollbar(rows_host, orient=tk.VERTICAL,
                             command=ships_canvas.yview)
    ships_canvas.configure(yscrollcommand=ships_sb.set)
    if n_picks > _SHIP_ROWS_VISIBLE:
        ships_sb.pack(side=tk.RIGHT, fill=tk.Y)
    rows_inner = tk.Frame(ships_canvas, bg=BG_PANEL)
    _rows_win = ships_canvas.create_window((0, 0), window=rows_inner,
                                           anchor="nw")
    rows_inner.bind("<Configure>", lambda _e: ships_canvas.configure(
        scrollregion=ships_canvas.bbox("all")))
    ships_canvas.bind("<Configure>", lambda e: ships_canvas.itemconfig(
        _rows_win, width=e.width))

    def _on_mousewheel(evt):
        """Wheel-scroll the ship picker when the pointer is over it.

        Tk does NOT deliver ``<MouseWheel>`` to the hovered widget (it goes to
        the focused one), so — like ``fc_gui._on_global_mousewheel`` — the bind
        lives on the TOPLEVEL and the target is found with ``winfo_containing``,
        then walked up its ``master`` chain. Bound on the dialog rather than
        registered with the app's global router because that router only serves
        canvases passed to ``FCToolGUI._register_scroll_canvas``, which this
        module must not reach (it is fc_gui-free by design); the toplevel's
        bindtag runs before ``all``, so this wins, and the ``"break"`` stops the
        router from scrolling anything else on the same turn. Pointer anywhere
        else (the gap Treeview, which has its own ttk class binding) → return
        None and stand down.
        """
        if n_picks <= _SHIP_ROWS_VISIBLE:
            return None                  # nothing to scroll; don't jitter rows
        try:
            w = dlg.winfo_containing(evt.x_root, evt.y_root)
        except Exception:
            return None
        while w is not None:
            if w is rows_host:
                break
            w = getattr(w, "master", None)
        if w is None:
            return None
        delta = _as_int(getattr(evt, "delta", 0), 0)
        amount = -1 * int(delta / 120)
        if amount == 0:
            if not delta:
                return None
            amount = -1 if delta > 0 else 1
        try:
            ships_canvas.yview_scroll(amount, "units")
        except tk.TclError:
            return None
        return "break"

    dlg.bind("<MouseWheel>", _on_mousewheel)

    def _on_include(fit_id):
        sel.set_include(fit_id, bool(include_vars[fit_id].get()))
        _sync_row(fit_id)
        rebuild()

    def _commit_qty(fit_id):
        """Commit a qty entry. No-op'd when the parsed value is UNCHANGED so a
        bare <FocusOut> never churns the table or steals focus mid-edit (§9 —
        the same guard ``_commit_member_seed_target`` carries)."""
        p = sel.pick(fit_id)
        if p is None:
            return
        raw = (qty_vars[fit_id].get() or "").strip()
        parsed = _as_int(raw, -1) if raw else -1
        if parsed < 0:
            # blank / garbage / negative: revert the field, change nothing.
            qty_vars[fit_id].set(str(int(p.qty)))
            return
        if parsed == int(p.qty):
            # Unchanged — normalise the display ("007" → "7") but do NOT rebuild.
            if raw != str(int(p.qty)):
                qty_vars[fit_id].set(str(int(p.qty)))
            return
        sel.set_qty(fit_id, parsed)
        qty_vars[fit_id].set(str(int(p.qty)))
        rebuild()

    def _sync_row(fit_id):
        """Push selection state back into one row's widgets (after All / None /
        Reset, or an include toggle that bumped a 0 qty to the doctrine bar)."""
        p = sel.pick(fit_id)
        if p is None:
            return
        try:
            include_vars[fit_id].set(bool(p.include))
            qty_vars[fit_id].set(str(int(p.qty)))
            off_labels[fit_id].config(text="" if p.include else "off")
        except tk.TclError:
            pass

    for pick in sel.picks:
        fid = pick.fit_id
        row = tk.Frame(rows_inner, bg=BG_PANEL)
        row.pack(fill=tk.X, pady=1)
        # Every Tk variable here is MASTERED ON THE DIALOG. With no master they
        # bind to tkinter._default_root — the wrong interpreter the moment the
        # app's root is not the default root, and a lifetime that outlives the
        # window (the intermittent "main thread is not in main loop" unraisable).
        ivar = tk.BooleanVar(master=dlg, value=bool(pick.include))
        include_vars[fid] = ivar
        tk.Checkbutton(
            row, text=(pick.label or fid), variable=ivar,
            font=("Consolas", 9), fg=FG_TEXT, bg=BG_PANEL,
            selectcolor=BG_ENTRY, activebackground=BG_PANEL,
            activeforeground=FG_ACCENT, anchor=tk.W,
            command=lambda f=fid: _on_include(f),
        ).pack(side=tk.LEFT)
        tk.Label(row, text=(f"({pick.hull})" if pick.hull else ""),
                 font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL).pack(
                     side=tk.LEFT, padx=(4, 0))
        qvar = tk.StringVar(master=dlg, value=str(int(pick.qty)))
        qty_vars[fid] = qvar
        # Packed right-to-left: the "off" marker sits rightmost, then the entry.
        off = tk.Label(row, text=("" if pick.include else "off"),
                       font=("Consolas", 8), fg=FG_DIM, bg=BG_PANEL, width=3,
                       anchor=tk.W)
        off.pack(side=tk.RIGHT)
        off_labels[fid] = off
        ent = tk.Entry(row, textvariable=qvar, width=5, font=("Consolas", 9),
                       bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                       justify=tk.RIGHT, borderwidth=1, relief=tk.RIDGE)
        ent.pack(side=tk.RIGHT, padx=(6, 2))
        qty_entries[fid] = ent
        ent.bind("<Return>", lambda _e, f=fid: _commit_qty(f))
        ent.bind("<FocusOut>", lambda _e, f=fid: _commit_qty(f))
        attach_tooltip(ent, "How many of THIS fit this report is for. Scratch "
                            "only — nothing is written back to the doctrine's "
                            "saved seed targets.")

    pick_btns = tk.Frame(ships_wrap, bg=BG_PANEL)
    pick_btns.pack(fill=tk.X, padx=8, pady=(3, 6))

    def _set_all(on):
        sel.set_all_included(on)
        for p in sel.picks:
            _sync_row(p.fit_id)
        rebuild()

    def _reset_picks():
        sel.reset_to_defaults()
        for p in sel.picks:
            _sync_row(p.fit_id)
        rebuild()

    ttk.Button(pick_btns, text="All", style="Dark.TButton",
               command=lambda: _set_all(True)).pack(side=tk.LEFT, padx=(0, 4))
    ttk.Button(pick_btns, text="None", style="Dark.TButton",
               command=lambda: _set_all(False)).pack(side=tk.LEFT, padx=(0, 4))
    reset_btn = ttk.Button(pick_btns, text="Reset to doctrine defaults",
                           style="Dark.TButton", command=_reset_picks)
    reset_btn.pack(side=tk.LEFT)
    attach_tooltip(reset_btn, "Restore the ships and quantities this window "
                              "opened with. Leaves removed items and the "
                              "category filter alone.")

    # ── category filter ──────────────────────────────────────────────────────
    cat_row = tk.Frame(body, bg=BG_DARK)
    cat_row.pack(fill=tk.X, pady=(0, 6))
    tk.Label(cat_row, text="Include:", font=("Consolas", 9), fg=FG_DIM,
             bg=BG_DARK).pack(side=tk.LEFT, padx=(0, 4))

    def _on_category(role):
        sel.set_category(role, bool(category_vars[role].get()))
        rebuild()

    for role in CATEGORY_ORDER:
        cvar = tk.BooleanVar(master=dlg, value=sel.is_category_on(role))
        category_vars[role] = cvar
        tk.Checkbutton(
            cat_row, text=CATEGORY_LABELS[role], variable=cvar,
            font=("Consolas", 8), fg=FG_TEXT, bg=BG_DARK,
            selectcolor=BG_ENTRY, activebackground=BG_DARK,
            activeforeground=FG_ACCENT,
            command=lambda r=role: _on_category(r),
        ).pack(side=tk.LEFT)

    # ── summary line (+ the clickable hidden-items affordance) ───────────────
    sum_row = tk.Frame(body, bg=BG_DARK)
    sum_row.pack(fill=tk.X, pady=(2, 4))
    summary_lbl = tk.Label(sum_row, text="", font=("Consolas", 10), fg=FG_TEXT,
                           bg=BG_DARK, justify=tk.LEFT, wraplength=520,
                           anchor=tk.W)
    summary_lbl.pack(side=tk.LEFT)
    hidden_lbl = tk.Label(sum_row, text="", font=("Consolas", 9), fg=FG_ACCENT,
                          bg=BG_DARK, cursor="hand2", anchor=tk.W)
    hidden_lbl.pack(side=tk.LEFT, padx=(6, 0))
    attach_tooltip(hidden_lbl, "Click to bring every removed item back.")

    # ── gap table ────────────────────────────────────────────────────────────
    tree_wrap = tk.Frame(body, bg=BG_DARK)
    tree_wrap.pack(fill=tk.BOTH, expand=True)
    tree = ttk.Treeview(tree_wrap, columns=COLUMNS, show="headings",
                        selectmode="extended", height=12)
    for col in COLUMNS:
        tree.heading(col, text=HEADINGS[col])
        tree.column(col, width=_COL_WIDTH[col],
                    anchor=(tk.W if col == "item" else tk.CENTER),
                    stretch=(col == "item"))
    tree_sb = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=tree_sb.set)
    tree_sb.pack(side=tk.RIGHT, fill=tk.Y)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    row_type_ids = {}          # tree iid -> type_id

    def _selected_type_ids():
        return [row_type_ids[i] for i in tree.selection() if i in row_type_ids]

    def _hide_selected(_evt=None):
        tids = _selected_type_ids()
        if not tids:
            return
        for tid in tids:
            sel.hide(tid)
        rebuild()

    def _restore_all(_evt=None):
        if not sel.hidden:
            return
        sel.restore_all()
        rebuild()

    def _build_menu():
        """Mint the right-click menu for the current selection (never posted
        here, so tests can inspect it without entering Tk's menu loop)."""
        menu = tk.Menu(dlg, tearoff=0, bg=BG_PANEL, fg=FG_TEXT,
                       activebackground=FG_ACCENT, activeforeground=BG_DARK)
        n = len(_selected_type_ids())
        menu.add_command(label=("Remove" if n <= 1 else f"Remove {n} items"),
                         state=(tk.NORMAL if n else tk.DISABLED),
                         command=_hide_selected)
        menu.add_separator()
        menu.add_command(label="Restore all",
                         state=(tk.NORMAL if sel.hidden else tk.DISABLED),
                         command=_restore_all)
        return menu

    def _on_right_click(evt):
        row = tree.identify_row(evt.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        menu = _build_menu()
        # Destroy off <Unmap> (fires once Tk unposts) rather than straight after
        # tk_popup returns — an immediate destroy races Tk's idle-scheduled
        # command invocation and swallows the click (the intel-menu pattern).
        menu.bind("<Unmap>", lambda _e: menu.after(100, menu.destroy))
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()
        return menu

    def _on_click(evt):
        """A click in the ✕ column hides that row. The column is resolved by ID
        (``-id``), never by index, so a future column reorder cannot silently
        move the hit target (§9)."""
        if tree.identify_region(evt.x, evt.y) != "cell":
            return
        try:
            cid = tree.column(tree.identify_column(evt.x), "id")
        except tk.TclError:
            return
        if cid != REMOVE_COL:
            return
        iid = tree.identify_row(evt.y)
        tid = row_type_ids.get(iid)
        if tid is None:
            return
        sel.hide(tid)
        rebuild()

    tree.bind("<Button-1>", _on_click, add="+")
    tree.bind("<Button-3>", _on_right_click)
    tree.bind("<Delete>", _hide_selected)
    hidden_lbl.bind("<Button-1>", _restore_all)

    status = tk.Label(body, text="", font=("Consolas", 9), fg=FG_GREEN,
                      bg=BG_DARK, anchor=tk.W)
    status.pack(fill=tk.X, pady=(8, 0))

    def _set_status(text, fg=FG_GREEN):
        try:
            status.config(text=text, fg=fg)
        except tk.TclError:
            pass

    # ── exports + buttons ────────────────────────────────────────────────────
    def _export(kind):
        vis = state.get("vis")
        if vis is None:
            return ""
        try:
            return vis.janice() if kind == "janice" else vis.multibuy()
        except Exception:
            log.exception("[gaps] export failed (%s)", kind)
            return ""

    def _copy(kind):
        label = "Janice" if kind == "janice" else "Multibuy"
        text = _export(kind)
        ok = False
        try:
            ok = bool(copy_text(text))
        except Exception:
            log.exception("[gaps] clipboard copy failed")
        if ok:
            _set_status(f"Copied {label} list ✓", FG_GREEN)
        else:
            _set_status("Copy failed — clipboard unavailable.", FG_RED)

    btn_row = tk.Frame(body, bg=BG_DARK)
    btn_row.pack(fill=tk.X, pady=(12, 0))
    # Packed right-to-left: Close rightmost, then the two copy buttons.
    ttk.Button(btn_row, text="Close", style="Dark.TButton",
               command=_close).pack(side=tk.RIGHT, padx=(6, 0))
    multibuy_btn = ttk.Button(btn_row, text="Copy for Multibuy",
                              style="Dark.TButton",
                              command=lambda: _copy("multibuy"))
    multibuy_btn.pack(side=tk.RIGHT, padx=(6, 0))
    janice_btn = ttk.Button(btn_row, text="Copy for Janice",
                            style="Green.TButton",
                            command=lambda: _copy("janice"))
    janice_btn.pack(side=tk.RIGHT, padx=(6, 0))

    # ── rebuild ──────────────────────────────────────────────────────────────
    def rebuild():
        """Re-run the injected builder and repaint. Honesty guards short-circuit
        BEFORE ``build_gap`` — an empty selection or empty category set would
        otherwise come back as an empty gap list and render as "No gaps"."""
        # "Copied Janice list ✓" describes the list AS IT WAS WHEN COPIED. Every
        # rebuild changes the list out from under it, so leaving the tick up
        # claims a clipboard that no longer matches the window — the same class
        # of lie the honesty guards exist to prevent. (The no-op qty commit
        # deliberately does not rebuild, so it does not erase the receipt.)
        _set_status("")
        guard = sel.guard_message()
        if guard is not None:
            gap, error = None, guard
        else:
            try:
                gap, error = build_gap(sel.per_fit_targets(), sel.components())
            except Exception:
                log.exception("[gaps] build_gap failed")
                gap, error = None, "Could not build the gap list (see log)."
        state["gap"] = gap
        state["error"] = error
        state["vis"] = None if (gap is None or error) else sel.visible(gap)

        for iid in tree.get_children(""):
            tree.delete(iid)
        row_type_ids.clear()

        if state["vis"] is None:
            summary_lbl.config(text=(error or "No gap list available."),
                               fg=FG_ORANGE)
            hidden_lbl.config(text="")
            _set_copy_state(tk.DISABLED)
            return

        vis = state["vis"]
        n_hidden = sel.hidden_count(gap)
        summary_lbl.config(text=summary_line(vis), fg=FG_TEXT)
        hidden_lbl.config(text=hidden_suffix(n_hidden))
        for it in (getattr(vis, "items", None) or []):
            iid = tree.insert("", tk.END, values=(
                getattr(it, "name", ""),
                f"{_as_int(getattr(it, 'needed', 0)):,}",
                f"{_as_int(getattr(it, 'available', 0)):,}",
                f"{_as_int(getattr(it, 'short', 0)):,}",
                REMOVE_GLYPH,
            ))
            row_type_ids[iid] = _as_int(getattr(it, "type_id", None), -1)
        _set_copy_state(tk.NORMAL if row_type_ids else tk.DISABLED)

    def _set_copy_state(st):
        for btn in (janice_btn, multibuy_btn):
            try:
                btn.config(state=st)
            except tk.TclError:
                pass

    # ── test/host surface ────────────────────────────────────────────────────
    dlg.gap_selection = sel
    dlg.gap_tree = tree
    dlg.gap_ships_canvas = ships_canvas
    dlg.gap_ships_rows = rows_host
    dlg.gap_wheel = _on_mousewheel
    dlg.gap_rebuild = rebuild
    dlg.gap_export = _export
    dlg.gap_copy = _copy
    dlg.gap_include_vars = include_vars
    dlg.gap_qty_vars = qty_vars
    dlg.gap_qty_entries = qty_entries
    dlg.gap_category_vars = category_vars
    dlg.gap_copy_buttons = (janice_btn, multibuy_btn)
    dlg.gap_hide_selected = _hide_selected
    dlg.gap_restore_all = _restore_all
    dlg.gap_click = _on_click
    dlg.gap_build_menu = _build_menu
    dlg.gap_commit_qty = _commit_qty
    dlg.gap_toggle_include = _on_include
    dlg.gap_toggle_category = _on_category
    dlg.gap_set_all = _set_all
    dlg.gap_reset_picks = _reset_picks
    dlg.gap_close = _close
    dlg.gap_row_type_ids = row_type_ids
    dlg.gap_summary_text = lambda: (
        summary_lbl.cget("text")
        + (f"  {hidden_lbl.cget('text')}" if hidden_lbl.cget("text") else ""))
    dlg.gap_status_text = lambda: status.cget("text")
    dlg.gap_current = lambda: state.get("vis")
    dlg.gap_error_text = lambda: state.get("error")

    rebuild()
    _center_over(dlg, parent)
    return dlg


def _center_over(dlg, parent):
    """Centre ``dlg`` over ``parent`` (kept from the retired
    ``FCToolGUI._finalize_modal_dialog``). Fully guarded: a headless/unmapped
    parent must not stop the dialog opening."""
    try:
        dlg.update_idletasks()
        rx, ry = parent.winfo_rootx(), parent.winfo_rooty()
        rw, rh = parent.winfo_width(), parent.winfo_height()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        dlg.geometry(f"+{rx + max(0, (rw - w) // 2)}+{ry + max(0, (rh - h) // 3)}")
    except Exception:
        pass
