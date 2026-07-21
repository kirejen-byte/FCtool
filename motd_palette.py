"""motd_palette — the MOTD composer's "add anything" grouped palette + Quick-Add tray.

Two layers live here:

* **Pure core** (module top, Tk-free): the ranking / grouping / cap math
  (:func:`rank_group`, :func:`visible_slice`, :func:`group_order_for_mode`), a
  tiny LRU (:class:`_LRU`), and :class:`_EsiStream` — the debounced,
  seq-guarded, cache-backed ESI character-search worker whose executor and
  ``ui_post`` marshaller are injected so it is fully testable without threads.

* **Tk widgets**: :class:`MotdPalette` (a search bar + a borderless dropdown of
  grouped, per-character-styled rows) and :class:`QuickAddTray` (the always-on
  chip strip under the bar). The palette owns nothing about MOTD documents — its
  rows carry plain ``PaletteItem`` specs (a ``{"kind","params"}`` token form, or
  an ``action:*`` control) and every data source is an injected provider
  callable. ESI search is the only network path; it runs on a worker thread and
  marshals its result back through ``providers.ui_post`` — never touching Tk from
  the worker (house thread-safety invariant).

Colours come exclusively from :mod:`ui_theme` (imported by name — never a
re-typed hex literal, which the ``test_ui_theme`` identity guard forbids), and
tooltips from the shared leak-proof :func:`ui_helpers.attach_tooltip`.
"""
from __future__ import annotations

import threading
import tkinter as tk
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from ui_helpers import attach_tooltip
from ui_theme import (
    BG_ENTRY,
    BG_PANEL,
    BORDER_COLOR,
    FG_ACCENT,
    FG_DIM,
    FG_GREEN,
    FG_MAGENTA,
    FG_ORANGE,
    FG_TEXT,
    FG_YELLOW,
)

# --------------------------------------------------------------------------- #
# frozen data contracts                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class PaletteItem:
    """One selectable row (or chip). ``kind`` is a token kind (``char``/``fit``/
    ``system``/``channel``/one of the line/block kinds) OR an ``action:*``
    control (``action:switch_doctrine``/``action:rescan_channels``/
    ``action:more``/``action:searching``)."""
    kind: str
    params: dict
    label: str
    meta: str = ""
    group: str = ""


@dataclass
class PaletteProviders:
    """The injected data seam. Every callable is PURE/local & instant EXCEPT
    ``esi_char_search`` (blocking network — the palette threads it) and
    ``ui_post`` (marshals a callable onto the Tk thread)."""
    doctrine_fits: Callable[[], list]        # [PaletteItem] for the selected doctrine
    library_fits: Callable[[str], list]      # query -> [PaletteItem]
    characters_local: Callable[[str], list]  # query -> [PaletteItem]
    systems: Callable[[str], list]           # query -> [PaletteItem]
    channels: Callable[[str], list]          # query -> [PaletteItem] (recents-first)
    doctrines: Callable[[str], list]         # query -> [action:switch_doctrine items]
    lines_blocks: Callable[[], list]         # static line/block PaletteItems
    recents: Callable[[], list]              # MRU [PaletteItem]
    esi_char_search: Callable[[str], list]   # BLOCKING -> [{"id","name","category"}]
    ui_post: Callable[[Callable], None]      # marshal worker result to the Tk thread


GROUP_ORDER = (
    "Recent", "Doctrine fits", "Lines & blocks", "Fittings",
    "Characters", "Systems", "Channels", "Doctrines",
)

#: Groups shown when the bar is focused with an empty query (§6 zero-state).
ZERO_STATE_GROUPS = ("Recent", "Doctrine fits", "Lines & blocks", "Channels")

# action kinds -------------------------------------------------------------- #
ACTION_MORE = "action:more"
ACTION_SWITCH = "action:switch_doctrine"
ACTION_RESCAN = "action:rescan_channels"
ACTION_SEARCHING = "action:searching"

# per-kind glyph + colour (§6). Line kinds share ☰; the whole-doctrine block ▤. #
_KIND_GLYPH = {
    "char": ("◆", FG_ACCENT),          # ◆ cyan
    "fit": ("▣", FG_ORANGE),           # ▣ orange
    "system": ("✦", FG_GREEN),         # ✦ green
    "channel": ("#", FG_YELLOW),            # # yellow
    "fc_line": ("☰", FG_MAGENTA),      # ☰ magenta
    "staging_line": ("☰", FG_MAGENTA),
    "doctrine_line": ("☰", FG_MAGENTA),
    "tag_line": ("☰", FG_MAGENTA),
    "channel_line": ("☰", FG_MAGENTA),
    "doctrine_block": ("▤", FG_MAGENTA),  # ▤ magenta (block)
}
_ACTION_GLYPH = {
    ACTION_SWITCH: ("»", FG_DIM),      # »
    ACTION_RESCAN: ("⟳", FG_DIM),      # ⟳
    ACTION_MORE: ("…", FG_DIM),        # …
    ACTION_SEARCHING: ("⋯", FG_DIM),   # ⋯
}


def _glyph_for(kind: str):
    if kind in _KIND_GLYPH:
        return _KIND_GLYPH[kind]
    if kind in _ACTION_GLYPH:
        return _ACTION_GLYPH[kind]
    return ("•", FG_DIM)               # • fallback (unknown / stale)


def _item_name(item: PaletteItem) -> str:
    return item.params.get("name") or item.label


def _ellipsize(text: str, n: int = 24) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# pure ranking / grouping                                                      #
# --------------------------------------------------------------------------- #


def rank_group(query: str, items: list) -> list:
    """Rank ``items`` for ``query``: starts-with matches (alpha) before contains
    matches (alpha); non-matches dropped. Case-insensitive; matches on
    ``item.label``. Empty query returns items in provider order (unchanged)."""
    if not query:
        return list(items)
    ql = query.lower()
    starts, contains = [], []
    for it in items:
        lab = (it.label or "").lower()
        if lab.startswith(ql):
            starts.append(it)
        elif ql in lab:
            contains.append(it)
    starts.sort(key=lambda it: (it.label or "").lower())   # stable within ties
    contains.sort(key=lambda it: (it.label or "").lower())
    return starts + contains


def _more_item(group: str, hidden: int) -> PaletteItem:
    return PaletteItem(kind=ACTION_MORE,
                       params={"group": group, "hidden_count": hidden},
                       label=f"+{hidden} more…", meta="", group=group)


def visible_slice(groups: list, per_group: int = 4, total: int = 10) -> list:
    """Apply the row budget to ``groups`` (a list of ``(name, items)``).

    Returns ``[(name, shown_items, hidden_count)]``. Each group shows at most
    ``per_group`` real item rows; ``total`` caps the sum of real item rows across
    groups (the ``action:more`` signage row is free — it is appended to a
    truncated group's ``shown_items`` but does not count against ``total``).
    Once the item budget is spent, later groups are dropped entirely. Empty input
    groups are skipped.
    """
    out = []
    remaining = total
    for name, items in groups:
        if remaining <= 0:
            break
        if not items:
            continue
        take = min(per_group, remaining, len(items))
        shown = list(items[:take])
        hidden = len(items) - take
        if hidden > 0:
            shown.append(_more_item(name, hidden))
        remaining -= take
        out.append((name, shown, hidden))
    return out


def group_order_for_mode(mode: str) -> list:
    """The group ordering for a dropdown ``mode``:

    * ``"entity"`` (@ trigger): Characters, Fittings, Systems, Channels first.
    * ``"block"`` (/ trigger): Lines & blocks first.
    * anything else (``"bar"``): the canonical :data:`GROUP_ORDER`.
    """
    base = list(GROUP_ORDER)
    if mode == "block":
        pri = ["Lines & blocks"]
    elif mode == "entity":
        pri = ["Characters", "Fittings", "Systems", "Channels"]
    else:
        return base
    rest = [g for g in base if g not in pri]
    return pri + rest


# --------------------------------------------------------------------------- #
# LRU + ESI stream                                                             #
# --------------------------------------------------------------------------- #


class _LRU:
    """A minimal capacity-bounded LRU map. ``get`` and ``put`` both promote the
    key to most-recently-used; ``put`` past capacity evicts the LRU key."""

    def __init__(self, cap: int = 64):
        self._cap = cap
        self._d: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        return default

    def put(self, key, val):
        if key in self._d:
            self._d.move_to_end(key)
        self._d[key] = val
        while len(self._d) > self._cap:
            self._d.popitem(last=False)

    def __len__(self):
        return len(self._d)

    def __contains__(self, key):
        return key in self._d


_MISS = object()


class _EsiStream:
    """Runs blocking ESI character searches off the Tk thread and marshals the
    results back, discarding stale responses.

    * Each :meth:`request` bumps a monotonic sequence; a delivered response whose
      captured sequence is no longer current is dropped (a newer query has
      superseded it).
    * Results are cached per query (LRU 64); a cache hit is delivered
      synchronously with no worker.
    * ``search_fn`` failures degrade to ``[]`` silently.

    ``executor(fn)`` runs ``fn`` "in the background" (default: a daemon thread);
    ``ui_post(fn)`` marshals ``fn`` onto the Tk thread; ``on_result(query,
    results)`` is invoked there. All three are injected so tests drive the whole
    flow inline.
    """

    def __init__(self, search_fn, ui_post, on_result, *, cache_size: int = 64,
                 executor=None):
        self._search_fn = search_fn
        self._ui_post = ui_post
        self._on_result = on_result
        self._cache = _LRU(cache_size)
        self._seq = 0
        self._executor = executor or self._thread_executor

    @staticmethod
    def _thread_executor(fn):
        threading.Thread(target=fn, daemon=True).start()

    def request(self, query: str) -> None:
        self._seq += 1
        seq = self._seq
        cached = self._cache.get(query, _MISS)
        if cached is not _MISS:
            self._on_result(query, cached)     # synchronous cache hit (Tk thread)
            return

        def worker():
            try:
                results = self._search_fn(query)
            except Exception:
                results = []
            self._ui_post(lambda: self._deliver(seq, query, results))

        self._executor(worker)

    def _deliver(self, seq: int, query: str, results: list) -> None:
        # Cache every arrival (even a stale one is a valid result for its query),
        # but only surface the response that is still current.
        self._cache.put(query, results)
        if seq != self._seq:
            return
        self._on_result(query, results)


# --------------------------------------------------------------------------- #
# Quick-Add tray                                                               #
# --------------------------------------------------------------------------- #

_FIT_CHIP_CAP = 10          # deterministic stand-in for "2 wrapped rows" (§7)
_RECENT_CHAN_CAP = 5


class QuickAddTray(tk.Frame):
    """The always-visible chip strip under the search bar. Built and refreshed by
    :class:`MotdPalette`; exposed for tests as ``palette.tray``.

    Layout: a ``QUICK ADD`` label, then the selected doctrine's fit chips (capped
    at :data:`_FIT_CHIP_CAP`, then a ``+N more…`` chip) or a single dim hint chip
    when no doctrine is selected, then up to :data:`_RECENT_CHAN_CAP` recent
    ``#channel`` chips. Every fit/channel chip inserts via ``on_insert`` and drags
    via ``on_drag``; the more chip calls ``on_more``.
    """

    def __init__(self, master, providers: PaletteProviders, *, on_insert, on_drag,
                 on_more, **kw):
        super().__init__(master, **kw)
        self.configure(bg=BG_PANEL)
        self._providers = providers
        self._on_insert = on_insert
        self._on_drag = on_drag
        self._on_more = on_more

        # test-observable census
        self.fit_chips: list = []       # [(widget, PaletteItem)]
        self.channel_chips: list = []   # [(widget, PaletteItem)]
        self.more_chip = None
        self.hint_chip = None

        self.refresh()

    # -- build ------------------------------------------------------------- #
    def refresh(self) -> None:
        for w in list(self.winfo_children()):
            w.destroy()
        self.fit_chips = []
        self.channel_chips = []
        self.more_chip = None
        self.hint_chip = None

        tk.Label(self, text="QUICK ADD", font=("Consolas", 8, "bold"),
                 fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(4, 6))

        fits = list(self._providers.doctrine_fits() or [])
        if not fits:
            self.hint_chip = tk.Label(
                self, text="select a doctrine to quick-add its fits",
                font=("Consolas", 9, "italic"), fg=FG_DIM, bg=BG_PANEL)
            self.hint_chip.pack(side=tk.LEFT, padx=2)
        else:
            for it in fits[:_FIT_CHIP_CAP]:
                self._add_fit_chip(it)
            if len(fits) > _FIT_CHIP_CAP:
                self._add_more_chip(len(fits) - _FIT_CHIP_CAP)

        recents = list(self._providers.channels("") or [])[:_RECENT_CHAN_CAP]
        for it in recents:
            self._add_channel_chip(it)

    def _chip(self, text, fg):
        return tk.Label(self, text=text, font=("Consolas", 9),
                        fg=fg, bg=BG_ENTRY, padx=6, pady=1,
                        borderwidth=1, relief=tk.RIDGE)

    def _add_fit_chip(self, item: PaletteItem):
        chip = self._chip(_ellipsize(item.label, 24), FG_ORANGE)
        tip = item.label + (f"  [{item.meta}]" if item.meta else "")
        attach_tooltip(chip, tip)
        _bind_chip_dnd(chip, item, self._on_insert, self._on_drag)
        chip.pack(side=tk.LEFT, padx=2)
        self.fit_chips.append((chip, item))

    def _add_channel_chip(self, item: PaletteItem):
        name = _item_name(item)
        text = name if name.startswith("#") else "#" + name
        chip = self._chip(text, FG_YELLOW)
        _bind_chip_dnd(chip, item, self._on_insert, self._on_drag)
        chip.pack(side=tk.LEFT, padx=2)
        self.channel_chips.append((chip, item))

    def _add_more_chip(self, n: int):
        chip = tk.Label(self, text=f"+{n} more…", font=("Consolas", 9),
                        fg=FG_ACCENT, bg=BG_ENTRY, padx=6, pady=1,
                        borderwidth=1, relief=tk.RIDGE)
        chip.bind("<Button-1>", lambda _e=None: self._on_more())
        chip.pack(side=tk.LEFT, padx=2)
        self.more_chip = chip


# --------------------------------------------------------------------------- #
# shared drag-and-drop binding                                                 #
# --------------------------------------------------------------------------- #


def _bind_chip_dnd(widget, item, on_click, on_drag):
    """Bind press/motion/release so a stationary release is a click (``on_click``)
    and a moved release is a drop; motion beyond a small threshold streams
    ``on_drag`` phases (``"motion"`` / ``"drop"`` / ``"cancel"``)."""
    state = {"start": None, "dragging": False}

    def press(e):
        state["start"] = (e.x_root, e.y_root)
        state["dragging"] = False

    def motion(e):
        if state["start"] is None:
            return
        sx, sy = state["start"]
        if not state["dragging"]:
            if abs(e.x_root - sx) + abs(e.y_root - sy) < 5:
                return
            state["dragging"] = True
        on_drag(item, e.x_root, e.y_root, "motion")

    def release(e):
        if state["dragging"]:
            on_drag(item, e.x_root, e.y_root, "drop")
        elif state["start"] is not None:
            on_click(item)
        state["start"] = None
        state["dragging"] = False

    def cancel(_e=None):
        if state["dragging"]:
            on_drag(item, 0, 0, "cancel")
        state["start"] = None
        state["dragging"] = False

    widget.bind("<ButtonPress-1>", press, add="+")
    widget.bind("<B1-Motion>", motion, add="+")
    widget.bind("<ButtonRelease-1>", release, add="+")
    widget.bind("<Escape>", cancel, add="+")


# --------------------------------------------------------------------------- #
# the palette widget                                                           #
# --------------------------------------------------------------------------- #

_NAV_KEYS = {"Up", "Down", "Return", "Tab", "Escape", "Shift_L", "Shift_R",
             "Control_L", "Control_R", "Alt_L", "Alt_R"}


class MotdPalette(tk.Frame):
    """Search bar + grouped autocomplete dropdown + Quick-Add tray.

    The dropdown is a borderless Toplevel of Frame rows (NOT a Listbox — per-row,
    per-character styling is required). ``open_at_caret`` drives the inline-trigger
    (caret-anchored) mode; the bar itself drives the always-available bar mode.
    """

    def __init__(self, master, providers: PaletteProviders, on_insert,
                 on_switch_doctrine, on_rescan_channels, on_drag, **kw):
        super().__init__(master, **kw)
        self.configure(bg=BG_PANEL)
        self.providers = providers
        self._on_insert_ext = on_insert
        self._on_switch_doctrine = on_switch_doctrine
        self._on_rescan_channels = on_rescan_channels
        self._on_drag_ext = on_drag

        #: Task 4 sets this to persist recents; called on every insert (any path).
        self._recent_hook = None

        # dropdown state ---------------------------------------------------- #
        self._dd = None          # the Toplevel
        self._dd_body = None      # the container Frame rebuilt each render
        self._mode = "bar"
        self._caret_mode = False
        self._anchor_xy = None
        self._query = ""
        self._show_all = False    # set by an action:more expansion
        self._nav: list = []      # selectable row dicts, in visual order
        self._sel = -1
        self._rendered: list = []  # [(group_name, [PaletteItem])] as shown

        # debounce handles -------------------------------------------------- #
        self._local_after = None
        self._esi_after = None
        self._focus_after = None

        # ESI streaming ----------------------------------------------------- #
        self._esi_min_chars = 3
        self._esi_query = ""
        self._esi_pending = False
        self._esi_rows: list | None = None
        self._esi = _EsiStream(
            search_fn=providers.esi_char_search,
            ui_post=providers.ui_post,
            on_result=self._on_esi_result,
        )

        # widgets ----------------------------------------------------------- #
        self.entry = tk.Entry(self, font=("Consolas", 11), bg=BG_ENTRY,
                              fg=FG_TEXT, insertbackground=FG_TEXT,
                              relief=tk.FLAT)
        self.entry.pack(fill=tk.X, padx=2, pady=(2, 1))
        self.entry.bind("<KeyRelease>", self._on_key_release)
        self.entry.bind("<Down>", lambda e: self._nav_move(1))
        self.entry.bind("<Up>", lambda e: self._nav_move(-1))
        self.entry.bind("<Return>", lambda e: self._accept_selected())
        self.entry.bind("<Tab>", lambda e: self._accept_selected())
        self.entry.bind("<Escape>", lambda e: (self.close_dropdown(), "break")[1])
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)

        self.tray = QuickAddTray(self, providers, on_insert=self._do_insert,
                                 on_drag=self._on_drag_ext, on_more=self._tray_more)
        self.tray.pack(fill=tk.X, padx=2, pady=(1, 2))

    # ==================================================================== #
    # public API                                                           #
    # ==================================================================== #
    def open_at_caret(self, mode: str, anchor_xy: tuple, initial_query: str = "") -> None:
        self._anchor_xy = anchor_xy
        self._open(mode, initial_query, caret=True)
        if self._esi_eligible(initial_query):
            self._start_esi(initial_query)

    def close_dropdown(self) -> None:
        self._cancel_after("_local_after")
        self._cancel_after("_esi_after")
        if self._dd is not None:
            try:
                self._dd.destroy()
            except tk.TclError:
                pass
        self._dd = None
        self._dd_body = None
        self._nav = []
        self._rendered = []
        self._sel = -1

    def refresh_tray(self) -> None:
        self.tray.refresh()

    def set_esi_min_chars(self, n: int = 3) -> None:
        self._esi_min_chars = n

    # ==================================================================== #
    # test-observable accessors                                            #
    # ==================================================================== #
    def _dropdown_open(self) -> bool:
        return self._dd is not None

    def visible_group_names(self) -> list:
        return [g for g, _ in self._rendered]

    def items_in_group(self, name: str) -> list:
        for g, items in self._rendered:
            if g == name:
                return items
        return []

    def visible_items_selectable(self) -> list:
        return [d["item"] for d in self._nav]

    def _row_for_label(self, label: str):
        for d in self._nav:
            if d["item"].label == label:
                return d
        return None

    # ==================================================================== #
    # ESI eligibility + streaming                                          #
    # ==================================================================== #
    def _esi_eligible(self, query: str) -> bool:
        return len(query) >= self._esi_min_chars

    def _reset_esi(self) -> None:
        self._esi_pending = False
        self._esi_rows = None
        self._esi_query = ""

    def _start_esi(self, query: str) -> None:
        self._esi_query = query
        self._esi_pending = True
        self._esi_rows = None
        self._query = query
        self._render()                 # show the dim "Searching ESI…" row
        self._esi.request(query)

    def _on_esi_result(self, query: str, results: list) -> None:
        if query != self._esi_query:
            return                     # superseded query — ignore
        self._esi_rows = [self._esi_item(r) for r in results]
        self._esi_pending = False
        self._render()

    @staticmethod
    def _esi_item(r: dict) -> PaletteItem:
        return PaletteItem(kind="char",
                           params={"id": r.get("id"), "name": r.get("name", "")},
                           label=r.get("name", ""), meta="ESI", group="Characters")

    # ==================================================================== #
    # bar-mode entry handlers                                              #
    # ==================================================================== #
    def _on_focus_in(self, _e=None):
        self._open("bar", "", caret=False)

    def _on_focus_out(self, _e=None):
        # Delay so a click on a dropdown row lands before we tear the dropdown
        # down (autocomplete's convention).
        self._focus_after = self.after(150, self.close_dropdown)

    def _on_key_release(self, e):
        if e.keysym in _NAV_KEYS:
            return
        self._on_query_change(self.entry.get())

    def _on_query_change(self, query: str) -> None:
        self._cancel_after("_local_after")
        self._local_after = self.after(150, lambda: self._apply_query(query))
        self._cancel_after("_esi_after")
        if self._esi_eligible(query):
            self._esi_after = self.after(300, lambda: self._start_esi(query))
        else:
            self._reset_esi()

    def _apply_query(self, query: str) -> None:
        self._query = query
        self._show_all = False
        if not self._esi_eligible(query):
            self._reset_esi()
        self._render()

    def _cancel_after(self, attr: str) -> None:
        handle = getattr(self, attr, None)
        if handle is not None:
            try:
                self.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass
            setattr(self, attr, None)

    # ==================================================================== #
    # open / render                                                        #
    # ==================================================================== #
    def _open(self, mode: str, query: str = "", caret: bool = False) -> None:
        self._mode = mode
        self._caret_mode = caret
        self._query = query
        self._show_all = False
        self._reset_esi()
        self._ensure_dropdown()
        self._render()

    def _ensure_dropdown(self) -> None:
        if self._dd is not None:
            return
        self._dd = tk.Toplevel(self)
        self._dd.wm_overrideredirect(True)
        try:
            self._dd.attributes("-topmost", True)
        except tk.TclError:
            pass
        self._dd.configure(bg=BORDER_COLOR)

    def _total_for_mode(self) -> int:
        return 8 if self._caret_mode else 10

    def _compute_groups(self, query: str, mode: str) -> list:
        """Base groups (name, [items]) before ESI injection / rescan / capping."""
        p = self.providers
        if not query:
            return [
                ("Recent", list(p.recents() or [])),
                ("Doctrine fits", list(p.doctrine_fits() or [])),
                ("Lines & blocks", list(p.lines_blocks() or [])),
                ("Channels", list(p.channels("") or [])),
            ]
        by_name = {
            "Recent": rank_group(query, list(p.recents() or [])),
            "Doctrine fits": rank_group(query, list(p.doctrine_fits() or [])),
            "Lines & blocks": rank_group(query, list(p.lines_blocks() or [])),
            "Fittings": list(p.library_fits(query) or []),
            "Characters": list(p.characters_local(query) or []),
            "Systems": list(p.systems(query) or []),
            "Channels": list(p.channels(query) or []),
            "Doctrines": list(p.doctrines(query) or []),
        }
        return [(name, by_name[name]) for name in group_order_for_mode(mode)]

    def _inject_esi(self, base: list) -> list:
        """Append the searching row and/or streamed ESI rows into the Characters
        group, de-duping ESI rows against the local character rows by name."""
        if not (self._esi_pending or self._esi_rows):
            return base
        out = []
        for name, items in base:
            if name == "Characters":
                items = list(items)
                if self._esi_pending:
                    items.append(self._searching_item())
                if self._esi_rows:
                    local = {_item_name(it).lower() for it in items
                             if it.kind == "char"}
                    for it in self._esi_rows:
                        if _item_name(it).lower() not in local:
                            items.append(it)
            out.append((name, items))
        return out

    @staticmethod
    def _searching_item() -> PaletteItem:
        return PaletteItem(kind=ACTION_SEARCHING, params={},
                           label="Searching ESI…", meta="", group="Characters")

    @staticmethod
    def _rescan_item() -> PaletteItem:
        return PaletteItem(kind=ACTION_RESCAN, params={},
                           label="Rescan channels…", meta="", group="Channels")

    def _append_rescan(self, sliced: list) -> list:
        out = []
        for group, items, hidden in sliced:
            if group == "Channels":
                items = list(items) + [self._rescan_item()]
            out.append((group, items, hidden))
        return out

    def _render(self) -> None:
        if self._dd is None:
            return
        base = self._compute_groups(self._query, self._mode)
        base = self._inject_esi(base)
        if self._show_all:
            sliced = [(n, list(it), 0) for n, it in base if it]
        else:
            sliced = visible_slice(base, per_group=4, total=self._total_for_mode())
        sliced = self._append_rescan(sliced)
        self._rendered = [(g, list(items)) for g, items, _h in sliced]
        self._build_rows(sliced)

    # ==================================================================== #
    # row building + selection                                             #
    # ==================================================================== #
    def _build_rows(self, sliced: list) -> None:
        if self._dd_body is not None:
            try:
                self._dd_body.destroy()
            except tk.TclError:
                pass
        body = tk.Frame(self._dd, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self._dd_body = body
        self._nav = []

        for group, items, _hidden in sliced:
            header = tk.Label(body, text=group.upper(), font=("Consolas", 8, "bold"),
                              fg=FG_DIM, bg=BG_PANEL, anchor="w")
            header.pack(fill=tk.X, padx=6, pady=(4, 0))
            for item in items:
                self._build_row(body, item)

        if self._nav:
            self._sel = 0
            self._restyle_selection()
        else:
            self._sel = -1
        self._position_dropdown()

    def _build_row(self, parent, item: PaletteItem) -> None:
        selectable = item.kind != ACTION_SEARCHING
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill=tk.X)
        rule = tk.Frame(row, height=1, bg=BG_PANEL)   # BORDER_COLOR top rule when selected
        rule.pack(fill=tk.X, side=tk.TOP)
        inner = tk.Frame(row, bg=BG_PANEL)
        inner.pack(fill=tk.X, padx=6, pady=1)

        glyph, gcolor = _glyph_for(item.kind)
        gfg = FG_DIM if item.kind == ACTION_SEARCHING else gcolor
        glyph_label = tk.Label(inner, text=glyph, font=("Consolas", 10),
                               fg=gfg, bg=BG_PANEL, width=2)
        glyph_label.pack(side=tk.LEFT)

        # label with the unmatched tail accented (prefix-match case)
        prefix_text, tail_text = self._split_label(item)
        base_fg = FG_DIM if item.kind in (ACTION_SEARCHING, ACTION_MORE) else FG_TEXT
        prefix_label = tk.Label(inner, text=prefix_text, font=("Consolas", 10),
                                fg=base_fg, bg=BG_PANEL)
        prefix_label.pack(side=tk.LEFT)
        tail_label = tk.Label(inner, text=tail_text, font=("Consolas", 10),
                              fg=FG_ACCENT, bg=BG_PANEL)
        tail_label.pack(side=tk.LEFT)

        meta_label = tk.Label(inner, text=item.meta, font=("Consolas", 9),
                              fg=FG_DIM, bg=BG_PANEL)
        meta_label.pack(side=tk.RIGHT)

        rec = {"item": item, "frame": row, "rule": rule, "inner": inner,
               "glyph_label": glyph_label, "prefix_label": prefix_label,
               "tail_label": tail_label, "meta_label": meta_label,
               "prefix_text": prefix_text, "tail_text": tail_text,
               "base_fg": base_fg}

        if selectable:
            idx = len(self._nav)
            self._nav.append(rec)
            for w in (row, inner, glyph_label, prefix_label, tail_label, meta_label):
                w.bind("<Enter>", lambda _e=None, i=idx: self._hover(i), add="+")
            _bind_chip_dnd(inner, item, self._accept, self._on_drag_ext)
            _bind_chip_dnd(glyph_label, item, self._accept, self._on_drag_ext)
            _bind_chip_dnd(prefix_label, item, self._accept, self._on_drag_ext)
            _bind_chip_dnd(tail_label, item, self._accept, self._on_drag_ext)
            _bind_chip_dnd(meta_label, item, self._accept, self._on_drag_ext)

    def _split_label(self, item: PaletteItem):
        label = item.label or ""
        q = self._query
        if q and label.lower().startswith(q.lower()):
            return label[: len(q)], label[len(q):]
        return label, ""

    def _hover(self, idx: int) -> None:
        self._sel = idx
        self._restyle_selection()

    def _nav_move(self, delta: int):
        if self._nav:
            self._sel = (self._sel + delta) % len(self._nav)
            self._restyle_selection()
        return "break"

    def _restyle_selection(self) -> None:
        for i, rec in enumerate(self._nav):
            selected = (i == self._sel)
            bg = BG_ENTRY if selected else BG_PANEL
            rec["frame"].configure(bg=bg)
            rec["inner"].configure(bg=bg)
            rec["rule"].configure(bg=BORDER_COLOR if selected else BG_PANEL)
            rec["glyph_label"].configure(bg=bg)
            rec["meta_label"].configure(bg=bg)
            if selected:
                rec["prefix_label"].configure(bg=bg, fg=FG_ACCENT)
                rec["tail_label"].configure(bg=bg, fg=FG_ACCENT)
            else:
                rec["prefix_label"].configure(bg=bg, fg=rec["base_fg"])
                rec["tail_label"].configure(bg=bg, fg=FG_ACCENT)

    def _accept_selected(self):
        if 0 <= self._sel < len(self._nav):
            self._accept(self._nav[self._sel]["item"])
        return "break"

    def _accept(self, item: PaletteItem) -> None:
        kind = item.kind
        if kind == ACTION_SWITCH:
            self.close_dropdown()
            self._on_switch_doctrine(item.params.get("name", ""))
        elif kind == ACTION_RESCAN:
            self.close_dropdown()
            self._on_rescan_channels()
        elif kind == ACTION_MORE:
            self._show_all = True
            self._render()
        elif kind == ACTION_SEARCHING:
            return
        elif kind.startswith("action:"):
            return                     # forward-compat: unknown action = no-op
        else:
            self._do_insert(item)

    def _do_insert(self, item: PaletteItem) -> None:
        self._on_insert_ext(item)
        if callable(self._recent_hook):
            self._recent_hook(item)
        self.close_dropdown()

    def _tray_more(self) -> None:
        try:
            self.entry.focus_set()
        except tk.TclError:
            pass
        self._open("bar", "", caret=False)

    # ==================================================================== #
    # positioning                                                          #
    # ==================================================================== #
    def _position_dropdown(self) -> None:
        if self._dd is None:
            return
        try:
            if self._caret_mode and self._anchor_xy is not None:
                x, y = self._anchor_xy
            else:
                x = self.entry.winfo_rootx()
                y = self.entry.winfo_rooty() + self.entry.winfo_height()
            w = max(self.entry.winfo_width(), 360)
            self._dd.geometry(f"{w}x1+{int(x)}+{int(y)}")
            self._dd.update_idletasks()
            self._dd.geometry("")          # let it size to content
            self._dd.geometry(f"+{int(x)}+{int(y)}")
        except tk.TclError:
            pass
