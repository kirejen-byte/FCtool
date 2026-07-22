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
import tkinter.font as tkfont
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from ui_helpers import attach_tooltip
from ui_theme import (
    BG_DARK,
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
    # Optional persistence seam for collapsed dropdown categories (Item 1). Safe
    # no-op defaults so existing constructions/tests need no change.
    collapsed_groups: Callable[[], list] = lambda: []            # -> [group_name]
    save_collapsed: Callable[[list], None] = lambda groups: None  # persist the set


GROUP_ORDER = (
    "Recent", "Doctrine fits", "Lines & blocks", "Fittings",
    "Characters", "Systems", "Channels", "Doctrines",
)

#: Groups shown when the bar is focused with an empty query (§6 zero-state).
ZERO_STATE_GROUPS = ("Recent", "Doctrine fits", "Lines & blocks", "Channels")

#: Per-group row caps in zero-state (owner rule): every group always renders —
#: there is NO shared total budget that could drop a later group — so recent
#: channels + doctrine fits are visible without searching.
ZERO_STATE_CAPS = {
    "Recent": 3, "Doctrine fits": 3, "Lines & blocks": 2, "Channels": 2,
}

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


def used_channel_items(recents_items, cap=None) -> list:
    """The channels the user has ACTUALLY used before (Item 2): the ``channel`` /
    ``channel_line`` tokens from the recents MRU, deduped by channel name
    (case-insensitive, first/most-recent wins), order-preserving, optionally
    capped at ``cap``.

    Discovered-but-never-used channels (e.g. the auto-joined "Alliance" you can't
    leave) never entered a MOTD, so they never entered recents and are excluded by
    construction. A ``channel_line`` with no channel name is skipped. Feeds BOTH
    the Quick-Add tray channel chips and the zero-state dropdown Channels slice;
    the full discovered list stays reachable by TYPING (query-mode Channels)."""
    out, seen = [], set()
    for it in recents_items or []:
        if it.kind not in ("channel", "channel_line"):
            continue
        name = (it.params.get("name") or "").strip()
        if not name and it.kind == "channel":
            name = (it.label or "").strip()      # plain channel: label == name
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if cap is not None and len(out) >= cap:
            break
    return out


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


def zero_state_slice(groups: list, caps: dict | None = None) -> list:
    """Zero-state slicing (owner rule): each group is capped by its OWN per-group
    cap (:data:`ZERO_STATE_CAPS`) with NO shared total budget, so every non-empty
    group always renders — a busy Recent/Doctrine fits can no longer starve
    Channels. A truncated group still gets an ``action:more`` row (last) carrying
    its ``hidden_count``. Returns ``[(name, shown_items, hidden_count)]``."""
    caps = caps if caps is not None else ZERO_STATE_CAPS
    out = []
    for name, items in groups:
        if not items:
            continue
        cap = caps.get(name, 4)
        shown = list(items[:cap])
        hidden = len(items) - len(shown)
        if hidden > 0:
            shown.append(_more_item(name, hidden))
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

_FIT_CHIP_CAP = 10          # fallback fit count when the width is unknown (§7)
_RECENT_CHAN_CAP = 5
_MAX_FIT_ROWS = 2           # §6: fit chips wrap into at most two rows, then +N more
_QA_LABEL_TEXT = "QUICK ADD"
_QA_LABEL_PAD = 10          # the label's own pack padx (4 + 6)
_QA_HINT_TEXT = "select a doctrine to quick-add its fits"
_QA_CHIP_CHROME = 16        # padx (6+6) + border/relief for the default measurer


def _chip_channel_text(item: PaletteItem) -> str:
    name = _item_name(item)
    return name if name.startswith("#") else "#" + name


def _qa_row_width(row: list, gap: int) -> int:
    """Total pixel width of a planned row: sum of token widths + inter-token gaps."""
    if not row:
        return 0
    return sum(w for _t, w in row) + gap * (len(row) - 1)


def _qa_has_chip(row: list) -> bool:
    """True if ``row`` holds anything other than the leading QUICK ADD label."""
    return any(tok[0] != "label" for tok, _w in row)


def _plan_quick_add_rows(avail, label_w, fit_ws, chan_ws, more_w_fn, *,
                         hint_w=None, gap=4, max_fit_rows=_MAX_FIT_ROWS,
                         max_fits_unconstrained=_FIT_CHIP_CAP):
    """Pure, Tk-free width-aware wrapping planner for the Quick-Add tray.

    Lays out a leading ``QUICK ADD`` label, then either the doctrine fit chips
    (``fit_ws``) or a single dim hint chip (``hint_w``), then the recent-channel
    chips (``chan_ws``). Fit chips wrap into at most ``max_fit_rows`` rows; any
    overflow collapses into ONE trailing ``+N more…`` chip that is guaranteed to
    itself fit (shown fits are dropped as needed and folded into ``N``). Channel
    chips flow onto fresh row(s) below, wrapping as many rows as the width needs.

    ``avail <= 1`` (or ``None``) means the width is unknown yet (widget not mapped
    / headless): wrapping is disabled and fits fall back to ``max_fits_unconstrained``
    followed by a ``+N more`` — preserving the pre-resize single-row behaviour.

    Widths are opaque pixels from an injected measure seam; ``more_w_fn(n)`` gives
    the width of a ``+n more…`` chip (its text width depends on ``n``). Returns::

        {avail, rows, fit_rows, fit_shown, more, chan_shown, gap}

    ``rows`` is ``[[(token, width), ...], ...]``; each ``token`` is one of
    ``("label",)``, ``("hint",)``, ``("fit", i)``, ``("chan", j)``, ``("more", n)``.
    """
    unconstrained = avail is None or avail <= 1
    rows: list = []
    row: list = [(("label",), label_w)]

    def commit():
        nonlocal row
        rows.append(row)
        row = []

    fit_shown = 0
    more = 0

    if hint_w is not None:
        # No doctrine selected -> one dim hint chip trails the label (wrapping to
        # its own row only if the label + hint cannot share the first row).
        if not unconstrained and _qa_row_width(row, gap) + gap + hint_w > avail:
            commit()
        row.append((("hint",), hint_w))
        commit()
    else:
        n = len(fit_ws)
        i = 0
        while i < n:
            if unconstrained and i >= max_fits_unconstrained:
                break
            w = fit_ws[i]
            add = w + (gap if row else 0)
            if unconstrained or not _qa_has_chip(row) or \
                    _qa_row_width(row, gap) + add <= avail:
                row.append((("fit", i), w))     # fits (or forced: row has no chip yet)
                i += 1
            elif len(rows) + 1 < max_fit_rows:  # current row is full -> wrap if allowed
                commit()
            else:
                break                            # out of fit rows
        fit_shown = i
        if i < n:
            more = n - i
            mw = more_w_fn(more)
            # The +N more chip must itself fit: drop trailing shown fits (folding
            # each into N) until it does, never dropping the label.
            while (not unconstrained and _qa_has_chip(row) and
                   _qa_row_width(row, gap) + gap + mw > avail):
                tok, _wp = row[-1]
                if tok[0] != "fit":
                    break
                row.pop()
                i -= 1
                fit_shown -= 1
                more += 1
                mw = more_w_fn(more)
            row.append((("more", more), mw))
        commit()

    fit_rows = len(rows)

    # Recent channel chips onto fresh row(s) below the fit region.
    chan_shown = 0
    if chan_ws:
        row = []
        for j, w in enumerate(chan_ws):
            if (not unconstrained and row and
                    _qa_row_width(row, gap) + gap + w > avail):
                rows.append(row)
                row = []
            row.append((("chan", j), w))
            chan_shown += 1
        rows.append(row)

    return {"avail": avail, "rows": rows, "fit_rows": fit_rows,
            "fit_shown": fit_shown, "more": more, "chan_shown": chan_shown,
            "gap": gap}


class QuickAddTray(tk.Frame):
    """The always-visible chip strip under the search bar. Built and refreshed by
    :class:`MotdPalette`; exposed for tests as ``palette.tray``.

    Layout: a ``QUICK ADD`` label, then the selected doctrine's fit chips — or a
    single dim hint chip when no doctrine is selected — then up to
    :data:`_RECENT_CHAN_CAP` recent ``#channel`` chips. The strip is
    **width-aware**: chips wrap into as many rows as the tray's current width
    needs (fit chips capped at :data:`_MAX_FIT_ROWS` rows, then a single accurate
    ``+N more…`` chip), re-laid-out on ``<Configure>`` (debounced through
    ``after_idle`` and guarded so a resize that does not change the width is a
    no-op). The wrapping math lives in the pure :func:`_plan_quick_add_rows`; the
    available-width and text-measure functions are injectable seams
    (:meth:`set_layout_metrics`) so the responsive contract is deterministically
    testable. Every fit/channel chip inserts via ``on_insert`` and drags via
    ``on_drag``; the more chip calls ``on_more``.
    """

    def __init__(self, master, providers: PaletteProviders, *, on_insert, on_drag,
                 on_more, **kw):
        super().__init__(master, **kw)
        self.configure(bg=BG_PANEL)
        self._providers = providers
        self._on_insert = on_insert
        self._on_drag = on_drag
        self._on_more = on_more

        # layout seams (overridable for tests via set_layout_metrics) --------- #
        self._chip_font = tkfont.Font(family="Consolas", size=9)
        self._avail_width_fn = lambda: self.winfo_width()
        self._measure_fn = self._default_measure
        self._gap = 4
        self._laid_out_width = None
        self._relayout_pending = False

        # cached provider data (re-fetched by refresh, re-laid-out on resize) -- #
        self._fit_items: list = []
        self._recent_items: list = []

        # test-observable census
        self.fit_chips: list = []       # [(widget, PaletteItem)]
        self.channel_chips: list = []   # [(widget, PaletteItem)]
        self.more_chip = None
        self.hint_chip = None
        self.last_plan = None           # the most recent _plan_quick_add_rows dict

        self.bind("<Configure>", self._on_configure)
        self.refresh()

    # -- layout seams ------------------------------------------------------ #
    def _default_measure(self, text: str) -> int:
        return self._chip_font.measure(text) + _QA_CHIP_CHROME

    def set_layout_metrics(self, *, avail_width_fn=None, measure_fn=None,
                           gap=None) -> None:
        """Override the responsive-layout seams and re-lay-out immediately.

        ``avail_width_fn() -> px`` supplies the tray's available width;
        ``measure_fn(text) -> px`` the rendered width of a chip's text; ``gap`` the
        inter-chip spacing. Used by tests to drive wrapping deterministically."""
        if avail_width_fn is not None:
            self._avail_width_fn = avail_width_fn
        if measure_fn is not None:
            self._measure_fn = measure_fn
        if gap is not None:
            self._gap = gap
        self._rebuild(force=True)

    # -- build ------------------------------------------------------------- #
    def refresh(self) -> None:
        """Re-fetch provider data and re-lay-out the strip at the current width.

        Channel chips are the channels the user has ACTUALLY used (recents MRU),
        never the full discovered/auto-joined list — see :func:`used_channel_items`.
        Zero used channels ⇒ zero channel chips."""
        self._fit_items = list(self._providers.doctrine_fits() or [])
        self._recent_items = used_channel_items(
            list(self._providers.recents() or []), cap=_RECENT_CHAN_CAP)
        self._rebuild(force=True)

    def _on_configure(self, _e=None) -> None:
        # Debounce a width change into a single idle relayout; guard re-entrancy
        # (our own repacking fires <Configure> again with an unchanged width).
        if self._relayout_pending:
            return
        self._relayout_pending = True
        try:
            self.after_idle(self._deferred_relayout)
        except tk.TclError:
            self._relayout_pending = False

    def _deferred_relayout(self) -> None:
        self._relayout_pending = False
        try:
            self._rebuild(force=False)
        except tk.TclError:
            pass

    def _rebuild(self, *, force: bool) -> None:
        avail = self._avail_width_fn()
        if not force and avail == self._laid_out_width:
            return                          # width unchanged -> nothing to do
        self._laid_out_width = avail

        for w in list(self.winfo_children()):
            w.destroy()
        self.fit_chips = []
        self.channel_chips = []
        self.more_chip = None
        self.hint_chip = None

        fits = self._fit_items
        chans = self._recent_items
        label_w = self._measure_fn(_QA_LABEL_TEXT) + _QA_LABEL_PAD
        if fits:
            fit_ws = [self._measure_fn(_ellipsize(it.label, 24)) for it in fits]
            hint_w = None
        else:
            fit_ws = []
            hint_w = self._measure_fn(_QA_HINT_TEXT)
        chan_ws = [self._measure_fn(_chip_channel_text(it)) for it in chans]

        plan = _plan_quick_add_rows(
            avail, label_w, fit_ws, chan_ws,
            lambda k: self._measure_fn(f"+{k} more…"),
            hint_w=hint_w, gap=self._gap, max_fit_rows=_MAX_FIT_ROWS,
            max_fits_unconstrained=_FIT_CHIP_CAP)
        self.last_plan = plan

        for row_tokens in plan["rows"]:
            rowf = tk.Frame(self, bg=BG_PANEL)
            rowf.pack(side=tk.TOP, fill=tk.X, anchor="w")
            for token, _w in row_tokens:
                self._build_token(rowf, token, fits, chans)

    def _build_token(self, parent, token, fits, chans) -> None:
        kind = token[0]
        if kind == "label":
            tk.Label(parent, text=_QA_LABEL_TEXT, font=("Consolas", 8, "bold"),
                     fg=FG_DIM, bg=BG_PANEL).pack(side=tk.LEFT, padx=(4, 6))
        elif kind == "hint":
            self.hint_chip = tk.Label(
                parent, text=_QA_HINT_TEXT,
                font=("Consolas", 9, "italic"), fg=FG_DIM, bg=BG_PANEL)
            self.hint_chip.pack(side=tk.LEFT, padx=2)
        elif kind == "fit":
            self._add_fit_chip(parent, fits[token[1]])
        elif kind == "chan":
            self._add_channel_chip(parent, chans[token[1]])
        elif kind == "more":
            self._add_more_chip(parent, token[1])

    def _chip(self, parent, text, fg):
        return tk.Label(parent, text=text, font=("Consolas", 9),
                        fg=fg, bg=BG_ENTRY, padx=6, pady=1,
                        borderwidth=1, relief=tk.RIDGE)

    def _add_fit_chip(self, parent, item: PaletteItem):
        chip = self._chip(parent, _ellipsize(item.label, 24), FG_ORANGE)
        tip = item.label + (f"  [{item.meta}]" if item.meta else "")
        attach_tooltip(chip, tip)
        _bind_chip_dnd(chip, item, self._on_insert, self._on_drag)
        chip.pack(side=tk.LEFT, padx=2)
        self.fit_chips.append((chip, item))

    def _add_channel_chip(self, parent, item: PaletteItem):
        chip = self._chip(parent, _chip_channel_text(item), FG_YELLOW)
        _bind_chip_dnd(chip, item, self._on_insert, self._on_drag)
        chip.pack(side=tk.LEFT, padx=2)
        self.channel_chips.append((chip, item))

    def _add_more_chip(self, parent, n: int):
        chip = tk.Label(parent, text=f"+{n} more…", font=("Consolas", 9),
                        fg=FG_ACCENT, bg=BG_ENTRY, padx=6, pady=1,
                        borderwidth=1, relief=tk.RIDGE)
        chip.bind("<Button-1>", lambda _e=None: self._on_more())
        chip.pack(side=tk.LEFT, padx=2)
        self.more_chip = chip


# --------------------------------------------------------------------------- #
# shared drag-and-drop binding                                                 #
# --------------------------------------------------------------------------- #


#: Cursor→ghost offset (px). Mirrors pill_canvas' in-canvas pill ghost so the two
#: drag affordances look and behave identically (spec §5).
_GHOST_DX, _GHOST_DY = 12, 10


def _make_drag_ghost(master, item):
    """The house ghost-chip for a palette/tray drag (spec §5): a borderless,
    semi-transparent ``overrideredirect`` Toplevel that follows the cursor so the
    user sees WHAT they are dragging out of the menu. Look mirrors
    :meth:`pill_canvas.PillCanvas._create_ghost` (accent bg, dark fg, ``-alpha
    0.85``, ``-topmost``); the label is the dragged item's glyph + its (ellipsised
    ~28-char) label. Returns the Toplevel, or ``None`` if creation fails (headless
    / teardown) — every mover/destroyer guards on ``None``."""
    glyph, _color = _glyph_for(item.kind)
    text = f"{glyph} {_ellipsize(item.label or '', 28)}"
    try:
        ghost = tk.Toplevel(master)
        ghost.wm_overrideredirect(True)
        try:
            ghost.attributes("-topmost", True)
            ghost.attributes("-alpha", 0.85)
        except tk.TclError:
            pass
        tk.Label(ghost, text=text, bg=FG_ACCENT, fg=BG_DARK,
                 font=("Consolas", 9), padx=6, pady=2,
                 relief=tk.SOLID, borderwidth=1).pack()
        return ghost
    except tk.TclError:
        return None


def _move_drag_ghost(ghost, x_root, y_root):
    if ghost is not None:
        try:
            ghost.geometry(f"+{int(x_root) + _GHOST_DX}+{int(y_root) + _GHOST_DY}")
        except tk.TclError:
            pass


def _destroy_drag_ghost(ghost):
    if ghost is not None:
        try:
            ghost.destroy()
        except tk.TclError:
            pass


def _bind_chip_dnd(widget, item, on_click, on_drag):
    """Bind press/motion/release so a stationary release is a click (``on_click``)
    and a moved release is a drop; motion beyond a small threshold streams
    ``on_drag`` phases (``"motion"`` / ``"drop"`` / ``"cancel"``).

    Crossing the drag threshold also spawns the follow-the-cursor drag ghost
    (:func:`_make_drag_ghost`), moved on every subsequent motion and destroyed on
    drop, cancel/Esc, AND widget destruction. The ghost is a self-contained side
    effect — the ``on_drag`` phase emission/ordering is unchanged (the wiring's
    drop-caret contract, pinned by the drag-phase tests, depends on it)."""
    state = {"start": None, "dragging": False, "ghost": None}

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
            state["ghost"] = _make_drag_ghost(widget, item)   # threshold → ghost
        _move_drag_ghost(state["ghost"], e.x_root, e.y_root)
        on_drag(item, e.x_root, e.y_root, "motion")

    def release(e):
        if state["dragging"]:
            _destroy_drag_ghost(state["ghost"])
            state["ghost"] = None
            on_drag(item, e.x_root, e.y_root, "drop")
        elif state["start"] is not None:
            on_click(item)
        state["start"] = None
        state["dragging"] = False

    def cancel(_e=None):
        if state["dragging"]:
            _destroy_drag_ghost(state["ghost"])
            state["ghost"] = None
            on_drag(item, 0, 0, "cancel")
        state["start"] = None
        state["dragging"] = False

    def _on_destroy(_e=None):
        # A mid-drag teardown (e.g. an async tray rebuild) must not strand the
        # ghost Toplevel; fires for the widget's own <Destroy>.
        _destroy_drag_ghost(state["ghost"])
        state["ghost"] = None

    widget.bind("<ButtonPress-1>", press, add="+")
    widget.bind("<B1-Motion>", motion, add="+")
    widget.bind("<ButtonRelease-1>", release, add="+")
    widget.bind("<Escape>", cancel, add="+")
    widget.bind("<Destroy>", _on_destroy, add="+")
    # Test seam: the event closures are otherwise unreachable — expose them so a
    # drag can be driven deterministically with synthetic events.
    widget._dnd = {"press": press, "motion": motion, "release": release,
                   "cancel": cancel, "state": state}


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
        self._expanded: set = set()  # group names uncapped by an action:more click
        #: Groups the user has collapsed via a header click. Loaded from (and
        #: persisted through) the providers seam; survives dropdown open/close AND
        #: sessions. A collapsed group renders only its header (▸ + a dim count);
        #: its rows are hidden and skipped by keyboard nav. Independent of
        #: ``_expanded`` — a collapsed group ignores its ``+N more`` expansion until
        #: it is re-expanded.
        self._collapsed: set = set(providers.collapsed_groups() or [])
        self._rendered_counts: dict = {}  # group -> full real-row count (header)
        self._headers: list = []          # [{group, widget, collapsed, count}]
        self._nav: list = []      # selectable row dicts, in visual order
        self._sel = -1
        self._rendered: list = []  # [(group_name, [PaletteItem])] as shown

        # A press on a dropdown row is a gesture in flight: an async ESI
        # re-render that tears the body down mid-click would otherwise destroy
        # the very row being released, dropping the insert (BUG C). Defer such a
        # render until the gesture completes.
        self._row_gesture_active = False
        self._pending_render = False

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
        self._headers = []
        self._rendered_counts = {}
        self._sel = -1
        self._row_gesture_active = False   # no rows left to release on
        self._pending_render = False

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

    def _header_text(self, group: str):
        """The rendered header label text for ``group`` (or None) — test seam for
        the collapsed ``NAME ▸ <count>`` / expanded ``NAME ▾`` glyph+count."""
        for h in self._headers:
            if h["group"] == group:
                return h["widget"].cget("text")
        return None

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
        self._expanded = set()         # a new query invalidates prior expansions
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
        self._expanded = set()
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
            # Zero-state Channels follows the same used-only rule as the tray:
            # only channels the user has actually inserted before (recents),
            # never the discovered/auto-joined list. Discovered channels stay
            # reachable by typing (the query branch below uses p.channels(query)).
            return [
                ("Recent", list(p.recents() or [])),
                ("Doctrine fits", list(p.doctrine_fits() or [])),
                ("Lines & blocks", list(p.lines_blocks() or [])),
                ("Channels", used_channel_items(list(p.recents() or []))),
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

    def _append_esi_signage(self, sliced: list, base: list) -> list:
        """Add the ``Searching ESI…`` indicator and/or streamed ESI rows to the
        Characters group AS SIGNAGE — after the per-group cap, like the rescan row —
        so they show even when local matches fill the cap. ESI rows are de-duped by
        name against the (full, pre-cap) local character set.

        When there are ZERO local matches the slicers drop the empty Characters
        group entirely, so the group is SYNTHESIZED and inserted at its
        ``group_order_for_mode`` slot — otherwise the primary ESI use case
        (searching a character with no local record) would render nothing."""
        if not (self._esi_pending or self._esi_rows):
            return sliced
        local = set()
        for name, items in base:
            if name == "Characters":
                local = {_item_name(it).lower() for it in items if it.kind == "char"}
                break

        def signage_rows():
            rows = []
            if self._esi_pending:
                rows.append(self._searching_item())
            if self._esi_rows:
                for it in self._esi_rows:
                    if _item_name(it).lower() not in local:
                        rows.append(it)
            return rows

        out, seen_chars = [], False
        for group, items, hidden in sliced:
            if group == "Characters":
                seen_chars = True
                items = list(items) + signage_rows()
            out.append((group, items, hidden))
        if not seen_chars:
            rows = signage_rows()
            if rows:
                out = self._insert_group_at_order_slot(out, "Characters", rows)
        return out

    def _insert_group_at_order_slot(self, groups: list, name: str, items: list) -> list:
        """Insert ``(name, items, 0)`` into ``groups`` at its position per
        ``group_order_for_mode(self._mode)`` (before the first present group that
        sorts after it), so a synthesized group leads/sits where it belongs."""
        order = group_order_for_mode(self._mode)
        try:
            idx = order.index(name)
        except ValueError:
            idx = len(order)
        insert_at = len(groups)
        for i, (g, _items, _h) in enumerate(groups):
            gi = order.index(g) if g in order else len(order)
            if gi > idx:
                insert_at = i
                break
        result = list(groups)
        result.insert(insert_at, (name, list(items), 0))
        return result

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
        if self._row_gesture_active:
            # A row press is in flight (click/drag). Rebuilding now would destroy
            # the pressed widget and swallow its release (BUG C); defer until the
            # gesture ends, then render with the freshest data.
            self._pending_render = True
            return
        base = self._compute_groups(self._query, self._mode)
        if not self._query:
            sliced = zero_state_slice(base)          # all four groups always render
        else:
            sliced = visible_slice(base, per_group=4, total=self._total_for_mode())
        if self._expanded:
            sliced = self._apply_expansions(sliced, base)   # uncap clicked groups
        sliced = self._append_esi_signage(sliced, base)   # signage AFTER the cap
        sliced = self._append_rescan(sliced)
        self._build_rows(sliced)                           # sets _rendered (collapse-aware)

    def _apply_expansions(self, sliced: list, base: list) -> list:
        """Uncap every group the user expanded via a ``+N more`` click: replace its
        capped slice with the full (pre-cap) item list and drop its more row. Groups
        the user did not expand are returned untouched (spec §6: expand in place,
        others unchanged)."""
        base_map = dict(base)
        out = []
        for name, items, hidden in sliced:
            if name in self._expanded and name in base_map:
                out.append((name, list(base_map[name]), 0))
            else:
                out.append((name, items, hidden))
        return out

    def _expand_group(self, group: str) -> None:
        """Handle an ``action:more`` activation (click or Enter): uncap ``group`` in
        place and re-render. Crucially cancels the pending focus-out close so a
        mouse click — which shifts focus off the entry — does not tear the dropdown
        down 150 ms later. Entry focus is deliberately NOT re-grabbed: ``focus_set``
        fires ``<FocusIn>`` -> ``_on_focus_in`` -> a fresh empty-query ``_open`` that
        would wipe the query and the expansion itself."""
        self._cancel_after("_focus_after")
        if group:
            self._expanded.add(group)
        else:                              # defensive: no group tag -> expand all shown
            self._expanded.update(g for g, _ in self._rendered)
        self._render()

    def _toggle_group_collapsed(self, group: str) -> None:
        """Collapse/expand a category from a header click. Cancels the pending
        focus-out close (the click shifts focus off the entry) — mirroring
        ``_expand_group`` — then persists the new set and re-renders. Deliberately
        does NOT re-grab entry focus (``focus_set`` would fire ``<FocusIn>`` ->
        ``_on_focus_in`` -> a fresh empty-query ``_open`` that wipes the query)."""
        if not group:
            return
        self._cancel_after("_focus_after")
        if group in self._collapsed:
            self._collapsed.discard(group)
        else:
            self._collapsed.add(group)
        self._persist_collapsed()
        self._render()

    def _persist_collapsed(self) -> None:
        """Push the collapsed set through the providers seam (never raises)."""
        try:
            self.providers.save_collapsed(sorted(self._collapsed))
        except Exception:
            pass

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
        self._rendered = []
        self._rendered_counts = {}
        self._headers = []

        for group, items, hidden in sliced:
            count = self._group_real_count(items, hidden)
            self._rendered_counts[group] = count
            collapsed = group in self._collapsed
            self._build_group_header(body, group, collapsed, count)
            if collapsed:                       # header only; rows hidden + nav-skipped
                self._rendered.append((group, []))
                continue
            self._rendered.append((group, list(items)))
            for item in items:
                self._build_row(body, item)

        if self._nav:
            self._sel = 0
            self._restyle_selection()
        else:
            self._sel = -1
        self._position_dropdown()

    @staticmethod
    def _group_real_count(items: list, hidden: int) -> int:
        """The full number of real (non-action) rows a group holds: the capped
        rows still shown PLUS the ``hidden`` overflow the ``+N more`` row stands
        for. Drives the collapsed-header count; updates as ESI rows stream into
        (e.g.) the Characters group."""
        real = sum(1 for it in items if not it.kind.startswith("action:"))
        return real + max(0, hidden)

    def _build_group_header(self, parent, group: str, collapsed: bool,
                            count: int) -> None:
        """A clickable category header. Collapsed → ``NAME ▸ <count>`` (dim count of
        hidden rows); expanded → ``NAME ▾``. Clicking toggles the group's collapsed
        state (persisted)."""
        glyph = "▸" if collapsed else "▾"        # ▸ collapsed / ▾ expanded
        text = f"{group.upper()} {glyph} {count}" if collapsed \
            else f"{group.upper()} {glyph}"
        header = tk.Label(parent, text=text, font=("Consolas", 8, "bold"),
                          fg=FG_DIM, bg=BG_PANEL, anchor="w", cursor="hand2")
        header.pack(fill=tk.X, padx=6, pady=(4, 0))
        header.bind("<Button-1>",
                    lambda _e=None, g=group: self._toggle_group_collapsed(g))
        self._headers.append({"group": group, "widget": header,
                              "collapsed": collapsed, "count": count})

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
                w.bind("<ButtonPress-1>",
                       lambda _e=None: self._begin_row_gesture(), add="+")
                w.bind("<ButtonRelease-1>",
                       lambda _e=None: self._end_row_gesture(), add="+")
            _bind_chip_dnd(inner, item, self._accept, self._on_drag_ext)
            _bind_chip_dnd(glyph_label, item, self._accept, self._on_drag_ext)
            _bind_chip_dnd(prefix_label, item, self._accept, self._on_drag_ext)
            _bind_chip_dnd(tail_label, item, self._accept, self._on_drag_ext)
            _bind_chip_dnd(meta_label, item, self._accept, self._on_drag_ext)

    def _begin_row_gesture(self) -> None:
        """A dropdown row was pressed — mark a gesture in flight so an async ESI
        ``_render`` defers rather than destroying the row before its release.

        Also cancel the focus-out close armed when the press pulled focus off the
        entry: a slow (or main-loop-stall-delayed) release arriving >150 ms later
        would otherwise fire that timer first, tear the row down, and swallow the
        insert (same 'inserts nothing, menu closed' symptom — for local rows too).
        Clicking AWAY from any row never runs this, so dismiss-on-click-away still
        works."""
        self._row_gesture_active = True
        self._cancel_after("_focus_after")

    def _end_row_gesture(self) -> None:
        """The row press ended (release). Flush any render deferred during the
        gesture — but via ``after_idle`` so the CURRENT release dispatch (the DnD
        handler that runs ``_accept`` → insert) finishes first; running it inline
        here would destroy the row mid-dispatch and swallow the very insert we are
        protecting."""
        if not self._row_gesture_active:
            return
        self._row_gesture_active = False
        if self._pending_render:
            self._pending_render = False
            try:
                self.after_idle(self._render)
            except tk.TclError:
                pass

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
            self._expand_group(item.params.get("group", ""))
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
