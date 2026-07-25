"""
Tk panel for the battle ledger — a THIN renderer over `battle_ledger`.

Owns its own widgets so `fc_gui` gets wiring only (the map_tab.py precedent):
construct, pack, and call `update_view` with a `BattleLedgerView`. Every
decision worth arguing about — what the numbers mean, what is suppressed, what
the staleness string says — lives in the engine, so it is all testable without
a display. This file decides fonts, colours and geometry, nothing else.

THREADING CONTRACT — read before wiring:
    `update_view` touches Tk widgets and is therefore **UI-THREAD ONLY**.
    `BattleLedger.ingest` runs on the zKill poll thread; the wiring layer must
    marshal through the UI dispatcher, exactly as `_on_zkill_alert` already
    does:

        def _on_killmail(kill_data):              # zKill poll thread
            self._battle_ledger.ingest(kill_data)
            self._post_ui(self._refresh_battle_ledger)

        def _refresh_battle_ledger(self):         # Tk thread
            self._battle_ledger.tick()
            self._battle_ledger_panel.update_view(
                self._battle_ledger.render_model())

    Coalesce to <= 1 Hz — a busy fight delivers killmails faster than a repaint
    budget, and `BattleLedger.revision` is there to make the skip cheap.

PLACEMENT (measured 2026-07-25 against the CURRENT tree, i.e. AFTER the
scrollable-composition merge 08825b6 / 2aeb24f — see the module notes in
tests/test_battle_ledger.py). The panel is designed to be packed as a
`side=RIGHT` sibling of fc_gui's `comp_scroll_outer` inside `comp_left`, which
measured a ZERO-pixel effect on the Fleet Composition / Specialized Roles
split at both the app default (1200x900) and its minsize (1000x700). Two
properties of this file are what make that true and must not be undone:

  * the outer frame carries `width=<W>, height=1` with `pack_propagate(False)`
    — the same load-bearing trick as `_fleet_comp_canvas`. A frame that
    requests real height would inflate `comp_left`'s request and starve the
    X-Up Log drawer's cavity, which is precisely the regression commit 08825b6
    just fixed;
  * the content lives in its own scroll canvas, because the cavity it shares
    with the ship list is 225 px tall at the app default but only 68 px at its
    minsize, while the ledger's content is TALLER THAN THE CAVITY in every
    populated state. Re-measured 2026-07-25 at DEFAULT_WIDTH (`_body`
    reqheight): fast strip + header + stamp + 3 rows + lag/pods/structures
    notes = **223 px**; +243 px with the feed-off note; +253 px with the
    standings-unknown note; **273 px** with both, which is the worst case.
    Silently clipping any of that is the exact bug 08825b6 fixed one panel to
    the left — and the tallest states are the blind-spot ones, i.e. precisely
    the notes that must not be the thing that scrolls off.

Colours come from `ui_theme` by import (never a re-typed hex literal — the
palette guard asserts import IDENTITY). Tooltips use `ui_helpers.attach_tooltip`
(the only implementation with the `<Destroy>` cleanup).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import battle_ledger as bl
from ui_helpers import attach_tooltip
from ui_theme import (
    BG_PANEL,
    BORDER_COLOR,
    FG_ACCENT,
    FG_DIM,
    FG_GREEN,
    FG_ORANGE,
    FG_RED,
    FG_TEXT,
    FG_YELLOW,
)

# Measured: 240 px is comfortable and free. comp_left's own requested width is
# 336 px (driven by the Doctrine row's combobox), so any column up to ~300 px
# adds nothing to it and the panel split does not move at all. The ship list
# keeps 257 px at the app default and 157 px at minsize, against a widest
# Top-10 row label of 111 px — so no ship row truncates either.
DEFAULT_WIDTH = 240

_FONT_TITLE = ("Consolas", 8, "bold")
_FONT_CHIP = ("Consolas", 8, "bold")
_FONT_ROW = ("Consolas", 8)
_FONT_SMALL = ("Consolas", 7)

# Bucket -> row colour. Deliberately static: honesty rule 3 forbids animating
# these numbers as if the events were arriving now.
_ROW_FG = {
    bl.BUCKET_OURS: FG_RED,
    bl.BUCKET_ALLIES: FG_ORANGE,
    bl.BUCKET_ENEMIES: FG_GREEN,
}
_STATE_FG = {
    bl.STATE_FILLING: FG_YELLOW,
    bl.STATE_SETTLED: FG_DIM,
    bl.STATE_IDLE: FG_RED,
}

# The blank marker for a bucket the engine could not attribute. It exists
# because the engine hands the panel None rather than 0 — there is no zero to
# render, which is honesty rule 5 with the teeth in the type instead of in a
# reviewer's memory.
BLANK = "--"

_STATE_TIP = (
    "FILLING: killmails for this fight are still arriving, so every count is a "
    "floor.\n"
    "SETTLED: nothing new has reached us for "
    f"{int(bl.QUIET_S / 60)} minutes — longer than zKill's own p90 arrival lag, "
    "so the tail has landed.\n"
    "A later killmail re-opens the ledger rather than being dropped."
)
_STAMP_TIP = (
    "The time stamped is the newest killmail this fight has produced, not the "
    "clock on the wall. zKillboard receives killmails a median of ~2 minutes "
    "after they happen (CCP caches the source endpoint for 300 seconds), so "
    "this panel answers 'what did the trade cost', never 'what is happening "
    "this second'."
)
_FAST_TIP = (
    "A separate lane with its own latency, kept OUT of the table on purpose. "
    "Fast signals exist only for your own side, and a fast Ours number beside "
    "a 2-minute Enemies number invites a comparison that is false."
)
_DISMISS_TIP = "Dismiss this battle ledger."

# EVERY static string this panel types is held to the same rule the engine holds
# itself to (battle_ledger.assert_no_live_language) — the tooltips included,
# since a tooltip that says "updates live" is exactly as much of a lie as a chip
# that does. Not applied to FastStrip content, which the panel merely relays:
# the fast lane really IS fast and carries its own latency label to say so, and
# that exemption is named in battle_ledger._LIVE_LANGUAGE rather than left to
# omission here.
for _copy, _where in ((_STATE_TIP, "state tip"), (_STAMP_TIP, "stamp tip"),
                      (_FAST_TIP, "fast tip"), (_DISMISS_TIP, "dismiss tip")):
    bl.assert_no_live_language(_copy, _where)


def format_row(row, isk_format=bl.format_isk, floor_prefix=">=") -> str:
    """One bucket line, monospace-aligned: `Ours     >=6    1.2B`.

    A suppressed bucket renders the blank marker, never `0` — the engine
    already made that impossible by handing over None, and this is the other
    half of the same rule.
    """
    if row.ships is None:
        ships = BLANK.rjust(5)
        isk = BLANK
    else:
        ships = f"{floor_prefix}{row.ships}".rjust(5)
        isk = isk_format(row.isk)
    return f"{row.label:<8}{ships} {isk:>8}"


class BattleLedgerPanel:
    """The Fleet Management tab's battle-ledger column.

    fc_gui does exactly this and no more::

        self._battle_ledger_panel = BattleLedgerPanel(
            comp_left,
            pack_opts=dict(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=(0, 4)),
            register_scroll_canvas=self._register_scroll_canvas,
            isk_format=self._market_price_short,
        )

    then re-packs `comp_scroll_outer` after it (so the ship list takes the
    remaining cavity) and calls `update_view` from the UI thread.
    """

    def __init__(self, parent, *, width: int = DEFAULT_WIDTH,
                 pack_opts: dict | None = None,
                 register_scroll_canvas=None,
                 isk_format=None,
                 on_dismiss=None):
        self._pack_opts = dict(pack_opts or dict(side=tk.RIGHT, fill=tk.Y))
        self._isk_format = isk_format or bl.format_isk
        self._on_dismiss = on_dismiss
        self._visible = False
        self._last_view = None
        # Wrap inside the canvas, not the frame: the vertical scrollbar eats
        # ~17 px, so a wraplength keyed on the frame width would overflow.
        self._wrap = max(120, int(width) - 30)

        # height=1 + pack_propagate(False) is load-bearing, not tidiness: see
        # the module docstring (the X-Up Log drawer's cavity).
        self.frame = tk.Frame(parent, bg=BG_PANEL, width=width, height=1)
        self.frame.pack_propagate(False)

        # Own scroll canvas. Same width=1/height=1 rule for the same reason.
        self._canvas = tk.Canvas(self.frame, bg=BG_PANEL, highlightthickness=0,
                                 width=1, height=1)
        self._scrollbar = ttk.Scrollbar(self.frame, orient=tk.VERTICAL,
                                        command=self._canvas.yview)
        self._body = tk.Frame(self._canvas, bg=BG_PANEL)
        self._window = self._canvas.create_window((0, 0), window=self._body,
                                                  anchor=tk.NW)
        self._body.bind("<Configure>", self._sync_scrollregion)
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfig(self._window, width=e.width))
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        if callable(register_scroll_canvas):
            register_scroll_canvas(self._canvas)

        self._build_body()

    # ── construction ───────────────────────────────────────────────────────

    def _build_body(self):
        """Persistent widgets, built once. `update_view` only ever `config`s
        text and colour — no destroy/rebuild churn, and nothing that could read
        as an event firing (honesty rule 3)."""
        body = self._body

        # -- the FAST lane, structurally separate from the table (rule 4) ----
        # Its own bordered frame, its own latency label, above a rule. Nothing
        # in this file ever copies a value between this frame and the table.
        self._fast_frame = tk.Frame(body, bg=BG_PANEL,
                                    highlightbackground=BORDER_COLOR,
                                    highlightthickness=1)
        fast_head = tk.Frame(self._fast_frame, bg=BG_PANEL)
        fast_head.pack(fill=tk.X)
        self._fast_chip = tk.Label(fast_head, text="FAST", font=_FONT_CHIP,
                                   fg=FG_ACCENT, bg=BG_PANEL)
        self._fast_chip.pack(side=tk.LEFT, padx=(3, 0))
        self._fast_latency = tk.Label(fast_head, text="", font=_FONT_SMALL,
                                      fg=FG_DIM, bg=BG_PANEL)
        self._fast_latency.pack(side=tk.RIGHT, padx=(0, 3))
        # On the chip, not the frame: a tooltip bound to a container never fires
        # once the pointer is over a child label.
        attach_tooltip(self._fast_chip, _FAST_TIP)
        self._fast_text = tk.Label(self._fast_frame, text="", font=_FONT_SMALL,
                                   fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W,
                                   justify=tk.LEFT, wraplength=self._wrap - 6)
        self._fast_text.pack(fill=tk.X, padx=3, pady=(0, 2))
        self._fast_rule = tk.Frame(body, bg=BORDER_COLOR, height=1)

        # -- header: title + state chip -------------------------------------
        head = tk.Frame(body, bg=BG_PANEL)
        head.pack(fill=tk.X)
        self._head = head
        self._title = tk.Label(head, text="", font=_FONT_TITLE, fg=FG_ACCENT,
                               bg=BG_PANEL, anchor=tk.W)
        self._title.pack(side=tk.LEFT)
        self._chip = tk.Label(head, text="", font=_FONT_CHIP, fg=FG_YELLOW,
                              bg=BG_PANEL)
        self._chip.pack(side=tk.RIGHT)
        attach_tooltip(self._chip, _STATE_TIP)
        # Explicit dismiss, only when the host wires one (it maps to
        # BattleLedger.dismiss). Lives on the header row so it costs no height
        # in a cavity that is 68 px tall at the app's minsize.
        self._dismiss = None
        if callable(self._on_dismiss):
            self._dismiss = tk.Label(head, text="✕", font=_FONT_SMALL,
                                     fg=FG_DIM, bg=BG_PANEL, cursor="hand2")
            self._dismiss.pack(side=tk.RIGHT, padx=(0, 5))
            self._dismiss.bind("<Button-1>", lambda _e: self._on_dismiss())
            attach_tooltip(self._dismiss, _DISMISS_TIP)

        # -- the data clock (rule 1) ----------------------------------------
        self._stamp = tk.Label(body, text="", font=_FONT_SMALL, fg=FG_DIM,
                               bg=BG_PANEL, anchor=tk.W, justify=tk.LEFT,
                               wraplength=self._wrap)
        self._stamp.pack(fill=tk.X)
        attach_tooltip(self._stamp, _STAMP_TIP)

        # -- the bucket table ------------------------------------------------
        self._rows: dict[str, tk.Label] = {}
        for bucket in bl.BUCKET_ORDER:
            label = tk.Label(body, text="", font=_FONT_ROW,
                             fg=_ROW_FG[bucket], bg=BG_PANEL, anchor=tk.W)
            label.pack(fill=tk.X)
            self._rows[bucket] = label

        self._killed = tk.Label(body, text="", font=_FONT_SMALL, fg=FG_DIM,
                                bg=BG_PANEL, anchor=tk.W, justify=tk.LEFT,
                                wraplength=self._wrap)
        self._killed.pack(fill=tk.X, pady=(2, 0))

        # -- named blind spots (rule 5). ONE wrapping label: a variable number
        #    of note widgets would mean packing/forgetting on every repaint.
        self._notes = tk.Label(body, text="", font=_FONT_SMALL, fg=FG_ORANGE,
                               bg=BG_PANEL, anchor=tk.W, justify=tk.LEFT,
                               wraplength=self._wrap)
        self._notes.pack(fill=tk.X, pady=(2, 0))

    def _sync_scrollregion(self, _event=None):
        """Keep the scrollregion on the content, and clamp the view when the
        content SHRINKS.

        Tk re-clamps a canvas view only on an explicit xview/yview call, never
        when -scrollregion changes, so a panel scrolled to the bottom of a long
        note block that then shrinks would render BLANK. Same conditional clamp
        `_sync_comp_scrollregion` uses one panel to the left.
        """
        bbox = self._canvas.bbox("all")
        self._canvas.configure(scrollregion=bbox)
        if not bbox:
            return
        content_h = bbox[3] - bbox[1]
        view_h = self._canvas.winfo_height()
        max_top = max(0, content_h - view_h)
        if content_h > 0 and (self._canvas.canvasy(0) - bbox[1]) > max_top:
            self._canvas.yview_moveto(max_top / content_h)

    # ── visibility ─────────────────────────────────────────────────────────

    @property
    def visible(self) -> bool:
        return self._visible

    def show(self):
        if not self._visible:
            self.frame.pack(**self._pack_opts)
            self._visible = True

    def hide(self):
        if self._visible:
            self.frame.pack_forget()
            self._visible = False

    def destroy(self):
        try:
            self.frame.destroy()
        except tk.TclError:
            pass

    # ── the one entry point ────────────────────────────────────────────────

    def update_view(self, view) -> bool:
        """Render `view`. **UI THREAD ONLY** — see the module docstring.

        Returns True when anything was redrawn. Cheap to call at 1 Hz: it
        diffs the WHOLE view (a frozen dataclass of frozen dataclasses, so `==`
        is exact) and otherwise only re-`config`s existing labels.

        The diff is on the view rather than on `BattleLedger.revision` on
        purpose: revision tracks LEDGER changes, but the stamp line's
        "(2m 05s behind)" figure grows with the wall clock while the counts sit
        still, and that is the number the FC most needs to keep moving.
        """
        if not view.visible:
            self.hide()
            self._last_view = view
            return False

        redraw = view != self._last_view or not self._visible
        self.show()
        if not redraw:
            return False
        self._last_view = view

        self._title.config(text=view.title)
        self._chip.config(text=view.state_label,
                          fg=_STATE_FG.get(view.state, FG_DIM))
        self._stamp.config(text=view.stamp_line)

        for bucket, label in self._rows.items():
            row = view.row(bucket)
            if row is None:
                label.config(text="", fg=FG_DIM)
                continue
            text = format_row(row, self._isk_format, view.floor_prefix)
            if row.qualifier:
                text = f"{text}  ({row.qualifier})"
            label.config(text=text,
                         fg=FG_DIM if row.suppressed else _ROW_FG[bucket])

        self._killed.config(text=view.killed_note)
        self._notes.config(text="\n".join(note.text for note in view.notes))

        # The fast lane is packed/unpacked as a unit ABOVE its own rule, above
        # the header, so it can never be mistaken for a fourth table row.
        # `before=self._head` is what keeps it at the top: bare pack() appends,
        # so a strip that first appears mid-fight would otherwise land under
        # the notes and read as part of the ledger.
        if view.fast is None:
            self._fast_frame.pack_forget()
            self._fast_rule.pack_forget()
        else:
            self._fast_text.config(text=view.fast.text)
            self._fast_latency.config(text=view.fast.latency_label)
            self._fast_frame.pack(fill=tk.X, before=self._head)
            self._fast_rule.pack(fill=tk.X, pady=(2, 3), before=self._head)
        return True
