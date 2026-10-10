"""Searchable emoji picker for the ping editor.

A small borderless popup: a search box, a strip of categories, a FIXED grid of
``COLS`` x ``ROWS`` = 54 cells and a footer naming the highlighted emoji and
its ``:shortcode:``.  It is opened two ways:

* from a button (``take_focus=True``): the search box takes the keyboard;
* as an autocomplete while the user types ``:name`` in the editor
  (``take_focus=False``): the keyboard STAYS in the editor, which forwards
  Up / Down / Return / Tab / Esc to :meth:`EmojiPicker.move_selection`,
  :meth:`EmojiPicker.accept` and :meth:`EmojiPicker.close`, and calls
  :meth:`EmojiPicker.set_query` as the user types.

Layering: the pure model (paging, selection, labels, placement) sits above the
``Tk shell`` marker and imports nothing from Tk; the shell below it is the only
part that builds widgets.  No ``fc_gui``, no Pillow, no network module.

Rules the shell keeps
---------------------
**No grab, ever.**  A Tk grab deafens the app's always-on-top client previews
(``ui_helpers.make_modal`` explains the class of bug); the popup goes through
``make_modal(grab=False, center=False)`` -- not centred, because it hangs from
the point it was opened at.

**The grid is virtual, and it is ONE canvas.**  The 54 cells are items on a
single ``tk.Canvas`` -- an image item and a text item per cell, plus one
highlight rectangle -- made once per popup and re-used for every page,
category and query; a category of 500 emoji scrolls through them (wheel, the
two arrows, the arrow keys, PageUp / PageDown).  Nothing is created per emoji.
(54 Label widgets were measured mapping 2.3x slower than one canvas with the
same content, and the popup opens while the user is typing.)  Only the cells
whose content changed ask ``tiles.image(seq)``; nothing is preloaded.  A cell
whose tile is None, raises, or is not an image -- or a picker given
``tiles=None`` -- shows the raw character, cell by cell; a press picks only
what a cell actually shows.

**When it closes.**  On Esc, on a pick, and when the keyboard focus has left
both the popup and (``take_focus=False``) the widget that was driving it -- the
OWNER, the widget that had the focus when the popup opened; the popup also
closes when the owner is destroyed.  The focus rule is decided
``_FOCUS_SETTLE_MS`` after a ``<FocusOut>``, never on the event itself: Windows
takes the focus from the old window BEFORE it gives it to the new one, so a
hand-off inside the app first reads as "no focus".  A press anywhere inside the
popup cancels a pending decision (the ``motd_palette`` rule), and a popup that
never had the focus is never closed for that alone -- no ``<FocusOut>`` ever
reaches it.  **Limit:** the picker's ``<FocusOut>`` binding on the owner is
added after the owner's own; an owner whose own ``<FocusOut>`` handler returns
``"break"`` ends the chain before it and so suppresses the picker's check --
such an owner must call :meth:`EmojiPicker.close` itself.

**Callbacks never raise into Tk.**  ``on_pick`` and ``on_close`` run inside a
key or mouse binding (or inside the editor's own key handler, when it forwards
Return); a raise in either is swallowed and logged -- the exception TYPE only,
in ASCII.  ``on_close`` is called exactly once after the popup is gone,
however it closed; on a pick it runs BEFORE ``on_pick``.

**A pick is dispatched on the PRESS**, not the release: in autocomplete mode
the press moves the focus out of the editor, and whatever the editor's owner
does about that must not be able to tear the popup down between the two.

**Stacking.**  Measured 2026-10-09 (Tk 8.6.15): ``wm transient`` on an
``overrideredirect`` Toplevel does NOT make Windows treat it as an owned
window (``GetWindow(GW_OWNER)`` is null, and raising the parent covers it), so
the popup needs the ``-topmost`` band to stay over the window the user is
typing in -- and that band also floats over other applications.  The band is
therefore taken only while the application has the focus, and the popup closes
when the application loses it (the focus rule above).

**Lifetime.**  ``close()`` destroys the popup, cancels the one ``after`` this
module ever schedules, removes the search variable's trace and the two
bindings on the owner; twice is harmless.  A popup destroyed from outside (its
parent went away) tears down the same way through ``<Destroy>``.

Test seams (all on the instance): ``_focus_name`` (which widget has the
focus), ``_work_area`` (the monitor's usable rectangle) and the widget
attributes ``_win``, ``_entry``, ``_var``, ``_canvas`` (with its item ids
``_image_items``, ``_text_items``, ``_highlight``), ``_strip``,
``_name_label``, ``_code_label``, ``_range_label``, ``_page_up``,
``_page_down``.
"""

import tkinter as tk

import emoji_data
import ui_helpers
from ui_helpers import make_modal
from ui_theme import (BG_ENTRY, BG_PANEL, BG_SELECT, BORDER_COLOR, FG_ACCENT,
                      FG_DIM, FG_TEXT)

#: The grid. 9 x 6 = 54 cells is the whole budget of cells.
COLS = 9
ROWS = 6
PAGE = COLS * ROWS

#: Rows one wheel notch scrolls (half a page).
WHEEL_ROWS = 3

#: Category labels per strip line; the strip wraps instead of widening the popup.
STRIP_COLS = 5

#: Longest category label, in characters.
LABEL_MAX = 8

#: Side of one grid cell in px. The pack's tiles are 22 px; the rest is margin.
CELL_PX = 30

#: Group names whose first word does not make a good label.
_SHORT = {"Activities": "Activity"}

#: How long after a ``<FocusOut>`` the "has the focus left?" question is asked.
_FOCUS_SETTLE_MS = 150

_NO_MATCH = "No emoji match"
_NO_EMOJI = "No emoji available"


# --------------------------------------------------------------------------
# pure model
# --------------------------------------------------------------------------

def short_label(name) -> str:
    """A category's strip label: the first word of the table's group name
    (``Smileys & Emotion`` -> ``Smileys``), at most ``LABEL_MAX`` characters.
    Plain text, so the strip renders without the emoji pack."""
    text = str(name or "").strip()
    if text in _SHORT:
        return _SHORT[text]
    words = text.replace("&", " ").split()
    if not words:
        return "?"
    return words[0][:LABEL_MAX]


def describe(entry) -> tuple:
    """``(name, ":code:  :code2:")`` for the footer; ``("", "")`` for None.
    ``entry`` is duck-typed (``name``, ``shortcodes``)."""
    if entry is None:
        return ("", "")
    name = str(getattr(entry, "name", "") or "")
    codes = getattr(entry, "shortcodes", None) or ()
    if isinstance(codes, str):
        codes = (codes,)
    shown = "  ".join(":%s:" % code for code in codes
                      if isinstance(code, str) and code)
    return (name, shown)


def place_popup(x: int, y: int, width: int, height: int, bounds, *,
                flip_gap: int = 0) -> tuple:
    """Top-left corner for a ``width`` x ``height`` popup hung from ``(x, y)``.

    ``bounds`` is the usable area as EDGES ``(left, top, right, bottom)``.
    The popup hangs below the anchor; when that would run off the bottom and
    there is room above, it sits above instead, ``flip_gap`` px clear of the
    anchor (the line of text or the button the anchor is the bottom of).  Then
    it is clamped into the area; one that is larger than the area keeps its
    top-left corner inside."""
    left, top, right, bottom = bounds
    px, py = x, y
    if py + height > bottom and y - flip_gap - height >= top:
        py = y - flip_gap - height
    px = max(left, min(px, right - width))
    py = max(top, min(py, bottom - height))
    return (px, py)


class GridModel:
    """Which items the fixed grid shows and which one is highlighted.

    ``items`` is the whole list (a category, or a search result); the grid
    shows ``page`` of them starting at ``offset``, always a whole number of
    rows.  ``sel`` is the highlighted item's index in ``items`` (-1 when there
    is none) and is ALWAYS on the visible page."""

    def __init__(self, cols: int = COLS, rows: int = ROWS):
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self.items = ()
        self.offset = 0
        self.sel = -1

    @property
    def page(self) -> int:
        return self.cols * self.rows

    def set_items(self, items) -> None:
        """Show a new list from its start, the first item highlighted."""
        self.items = tuple(items or ())
        self.offset = 0
        self.sel = 0 if self.items else -1

    def visible(self) -> tuple:
        return self.items[self.offset:self.offset + self.page]

    def selected(self):
        """The highlighted item, or None."""
        if 0 <= self.sel < len(self.items):
            return self.items[self.sel]
        return None

    def selected_cell(self) -> int:
        """The highlighted item's cell on the page, or -1."""
        if self.sel >= 0 and self.offset <= self.sel < self.offset + self.page:
            return self.sel - self.offset
        return -1

    def has_cell(self, cell: int) -> bool:
        """Whether visible cell ``cell`` holds an item."""
        return 0 <= cell < self.page and self.offset + cell < len(self.items)

    def span(self) -> tuple:
        """``(first, last, total)`` shown, 1-based; zeros when empty."""
        total = len(self.items)
        if not total:
            return (0, 0, 0)
        return (self.offset + 1, min(self.offset + self.page, total), total)

    def max_offset(self) -> int:
        total_rows = -(-len(self.items) // self.cols)
        return max(0, total_rows - self.rows) * self.cols

    def move(self, dx: int, dy: int) -> bool:
        """Move the highlight ``dx`` columns and ``dy`` rows, clamped at every
        edge (never wrapping); the page follows it.  A move onto a short last
        row lands on that row's last item.  True when anything changed."""
        count = len(self.items)
        if not count:
            return False
        row, col = divmod(max(self.sel, 0), self.cols)
        row = min(max(row + dy, 0), (count - 1) // self.cols)
        col = min(max(col + dx, 0), self.cols - 1)
        return self._set(min(row * self.cols + col, count - 1))

    def select(self, index: int) -> bool:
        """Highlight item ``index``; ignored when there is no such item."""
        if not 0 <= index < len(self.items):
            return False
        return self._set(index)

    def select_cell(self, cell: int) -> bool:
        """Highlight the item in visible cell ``cell``; ignored when empty."""
        if not self.has_cell(cell):
            return False
        return self._set(self.offset + cell)

    def scroll(self, rows: int) -> bool:
        """Scroll the page by whole rows, clamped; the highlight keeps its
        cell so it never leaves the page.  True when the page moved."""
        count = len(self.items)
        if not count:
            return False
        target = min(max(self.offset + rows * self.cols, 0), self.max_offset())
        if target == self.offset:
            return False
        self.sel = min(max(self.sel + target - self.offset, 0), count - 1)
        self.offset = target
        return True

    def _set(self, sel: int) -> bool:
        before = (self.sel, self.offset)
        self.sel = sel
        row = sel // self.cols
        first = self.offset // self.cols
        if row < first:
            self.offset = row * self.cols
        elif row >= first + self.rows:
            self.offset = (row - self.rows + 1) * self.cols
        return (self.sel, self.offset) != before


# --------------------------------------------------------------------------
# Tk shell
# --------------------------------------------------------------------------

_ENTRY_FONT = ("Consolas", 10)
_TEXT_FONT = ("Consolas", 9)
_SMALL_FONT = ("Consolas", 8)
#: A NEGATIVE size is pixels: the cell is a fixed number of pixels, so the
#: character shown in place of a missing tile must not grow with the DPI.
_CELL_FONT = ("Segoe UI Emoji", -18)

_PAGE_UP_GLYPH = chr(0x25B2)        # the two paging arrows beside the grid
_PAGE_DOWN_GLYPH = chr(0x25BC)

#: The keys that move the highlight: (binding, columns, rows).
_MOVE_KEYS = (("<Up>", 0, -1), ("<Down>", 0, 1),
              ("<Left>", -1, 0), ("<Right>", 1, 0),
              ("<Prior>", 0, -ROWS), ("<Next>", 0, ROWS))


def _log_raise(what: str, exc) -> None:
    """One WARNING saying which callback raised and the exception's TYPE --
    never its message, which may carry anything (and this box's console is
    cp1252: a non-ASCII log line prints a "Logging error" traceback).  The
    logger is fetched HERE, never at import.  Never raises."""
    try:
        from app_log import get_logger
        kind = type(exc).__name__.encode("ascii", "backslashreplace")
        get_logger(__name__).warning("[Emoji] picker %s raised %s", what,
                                     kind.decode("ascii"))
    except Exception:
        pass


class EmojiPicker:
    """The picker. One object serves many openings; the popup itself exists
    only between :meth:`open_at` and :meth:`close`.  Tk-thread only.

    ``parent``   any widget of the window the popup belongs to.
    ``tiles``    an ``emoji_tiles.EmojiTiles`` (``image(seq)``), or None.
    ``on_pick``  called with the chosen sequence, AFTER the popup is closed.
    ``on_close`` called with no argument, exactly once after the popup is
                 gone, however it closed (the caller puts the focus back in
                 its editor); on a pick it runs before ``on_pick``.
    ``search`` / ``groups`` default to ``emoji_data``'s (injectable for tests).

    A raise in ``on_pick`` / ``on_close`` is swallowed and logged.
    """

    def __init__(self, parent, *, tiles, on_pick, search=None, groups=None,
                 on_close=None):
        self._parent = parent
        self._tiles = tiles
        self._on_pick = on_pick
        self._on_close = on_close
        self._search = search if callable(search) else emoji_data.search
        self._groups_fn = groups if callable(groups) else emoji_data.groups
        self._model = GridModel()
        #: The category shown for an empty query -- remembered across openings.
        self._group_index = 0
        self._reset()

    def _reset(self) -> None:
        """Forget everything that belongs to one popup."""
        self._win = None
        self._entry = None
        self._var = None
        self._trace_id = None
        self._muted = False             # set_query is writing the variable
        self._canvas = None             # the grid: ONE widget
        self._image_items = []          # canvas item ids, one pair per cell
        self._text_items = []
        self._highlight = None          # the one highlight rectangle
        self._cell_seq = []             # what each cell shows right now
        self._lit = -1                  # the cell painted as highlighted
        self._cursor = ""               # the canvas cursor as last set
        self._strip = []
        self._groups = []
        self._name_label = None
        self._code_label = None
        self._range_label = None
        self._page_up = None
        self._page_down = None
        self._query = None              # None = nothing loaded yet
        self._size = (1, 1)
        self._flip_gap = 0
        self._takes_focus = True
        self._focus_after = None        # the one ``after`` this module arms
        self._owner = None              # take_focus=False: the driving widget
        self._owner_binds = []          # [(sequence, funcid)] on the owner
        self._pointer = None            # where the pointer was last seen

    # ==================================================================== #
    # public API                                                           #
    # ==================================================================== #
    def open_at(self, x_root: int, y_root: int, *, query: str = "",
                take_focus: bool = True) -> None:
        """Show the popup hung from screen point ``(x_root, y_root)`` -- the
        bottom-left of the button or of the caret -- on that point's monitor,
        showing ``query``'s results (the remembered category when empty).

        ``take_focus=False`` leaves the keyboard where it is. Called on an
        open popup it moves it (and raises it) and applies ``query``; nothing
        is rebuilt. A coordinate that is no number falls back to the pointer's
        position -- never an exception."""
        x, y = self._anchor(x_root, y_root)
        take_focus = bool(take_focus)
        if self._win is not None and take_focus != self._takes_focus:
            # Another mode needs another popup. The caller still has a picker
            # open when this returns, so this is not a close it is told about.
            self._teardown(True, notify=False)
        fresh = self._win is None
        if fresh and not self._build():
            return
        self._takes_focus = take_focus
        self.set_query(query)
        self._place(x, y, fresh)
        if self._win is None:
            return
        if take_focus:
            self._focus_entry()
        elif self._owner is None:
            self._watch_owner()

    def is_open(self) -> bool:
        return self._win is not None

    def set_query(self, query: str) -> None:
        """Show ``query``'s search results (at most one page, best first), or
        the selected category when it is empty. No-op while closed."""
        if self._win is None:
            return
        text = "" if query is None else str(query)
        self._set_entry(text)
        self._apply_query(text)

    def move_selection(self, dx: int, dy: int) -> None:
        """Move the highlight by columns / rows, clamped at the edges."""
        if self._win is None:
            return
        if self._model.move(int(dx), int(dy)):
            self._render()

    def accept(self) -> bool:
        """Pick the highlighted emoji: close (``on_close``), then
        ``on_pick(seq)``. False -- and nothing happens -- when there is none.
        True even when ``on_pick`` raised: the pick was made, the raise is
        logged and never reaches the key or mouse binding that called this."""
        if self._win is None:
            return False
        seq = getattr(self._model.selected(), "seq", None)
        if not seq:
            return False
        self.close()
        self._call("on_pick", self._on_pick, seq)
        return True

    def close(self) -> None:
        """Destroy the popup and everything it armed, then ``on_close``.
        Harmless when closed (``on_close`` is not called again)."""
        self._teardown(True)

    def _call(self, what: str, callback, *args) -> None:
        """Run a caller's callback; a raise is logged, never let out."""
        if not callable(callback):
            return
        try:
            callback(*args)
        except Exception as exc:
            _log_raise(what, exc)

    def _anchor(self, x_root, y_root) -> tuple:
        try:
            return (int(x_root), int(y_root))
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            x, y = self._parent.winfo_pointerxy()
            return (int(x), int(y))
        except Exception:
            return (0, 0)

    # ==================================================================== #
    # build / teardown                                                     #
    # ==================================================================== #
    def _build(self) -> bool:
        """Create the popup, hidden and unplaced. False when it could not be
        built (the parent is gone)."""
        try:
            self._win = tk.Toplevel(self._parent)
        except (tk.TclError, RuntimeError):
            self._win = None
            return False
        win = self._win
        try:
            # Withdrawn until it has a size and a position: a fresh Toplevel
            # that is measured while visible maps in the screen corner first.
            win.wm_withdraw()
            win.wm_overrideredirect(True)
            make_modal(self._win, self._app_toplevel(), on_cancel=self.close,
                       base_bg=BORDER_COLOR, grab=False, center=False)
            self._load_groups()
            self._build_body(win)
            win.bind("<ButtonPress-1>", self._on_press, add="+")
            win.bind("<FocusOut>", self._on_focus_out, add="+")
            win.bind("<MouseWheel>", self._on_wheel, add="+")
            win.bind("<Destroy>", self._on_destroy, add="+")
            win.update_idletasks()
            # Measured ONCE, with an empty footer: the popup is given this
            # size outright, so a long emoji name is clipped, never a resize.
            self._size = (max(1, win.winfo_reqwidth()),
                          max(1, win.winfo_reqheight()))
            self._flip_gap = max(0, self._entry.winfo_reqheight())
        except tk.TclError:
            self._teardown(True, notify=False)      # it never opened
            return False
        return True

    def _load_groups(self) -> None:
        try:
            raw = list(self._groups_fn() or ())
        except Exception:
            raw = []
        groups = []
        for pair in raw:
            try:
                name, members = pair
                groups.append((str(name), tuple(members)))
            except (TypeError, ValueError):
                continue
        self._groups = groups
        if not 0 <= self._group_index < len(groups):
            self._group_index = 0

    def _build_body(self, win) -> None:
        # 1 px of the popup's own background shows around the body: the border.
        body = tk.Frame(win, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self._var = tk.StringVar(master=win)
        self._trace_id = self._var.trace_add("write", self._on_var_write)
        self._entry = tk.Entry(body, textvariable=self._var, font=_ENTRY_FONT,
                               bg=BG_ENTRY, fg=FG_TEXT,
                               insertbackground=FG_TEXT, relief=tk.FLAT)
        self._entry.pack(fill=tk.X, padx=4, pady=(4, 2))
        self._bind_keys(self._entry)
        self._bind_keys(win)
        # make_modal bound the popup's own <Escape>; this one stops the chain
        # at the entry so one key press closes exactly once.
        self._entry.bind("<Escape>", lambda _e: self._on_escape())

        strip = tk.Frame(body, bg=BG_PANEL)
        strip.pack(fill=tk.X, padx=4)
        for column in range(STRIP_COLS):
            strip.grid_columnconfigure(column, weight=1, uniform="strip")
        for index, (name, _members) in enumerate(self._groups):
            label = tk.Label(strip, text=short_label(name), font=_SMALL_FONT,
                             bg=BG_PANEL, fg=FG_DIM, padx=2, pady=1,
                             cursor="hand2")
            label.grid(row=index // STRIP_COLS, column=index % STRIP_COLS,
                       sticky="ew")
            label.bind("<Button-1>",
                       lambda _e, i=index: self._select_group(i))
            self._strip.append(label)

        middle = tk.Frame(body, bg=BG_PANEL)
        middle.pack(padx=4, pady=2)
        # The grid is ONE widget. Every cell is an image item (its tile) and
        # a text item (the character, when there is no tile) at the cell's
        # centre; both exist from the start and are only ever reconfigured.
        canvas = tk.Canvas(middle, width=COLS * CELL_PX, height=ROWS * CELL_PX,
                           bg=BG_PANEL, bd=0, highlightthickness=0)
        canvas.pack(side=tk.LEFT)
        self._canvas = canvas
        # Created first, so it is drawn under every cell.
        self._highlight = canvas.create_rectangle(
            0, 0, CELL_PX, CELL_PX, fill=BG_SELECT, outline="", state="hidden")
        for index in range(PAGE):
            cx = (index % COLS) * CELL_PX + CELL_PX // 2
            cy = (index // COLS) * CELL_PX + CELL_PX // 2
            self._image_items.append(
                canvas.create_image(cx, cy, state="hidden"))
            self._text_items.append(
                canvas.create_text(cx, cy, text="", font=_CELL_FONT,
                                   fill=FG_TEXT, state="hidden"))
        self._cell_seq = [None] * PAGE
        # Bound once, on the canvas; the handlers work out the cell from the
        # event's position and read what that cell shows NOW.
        canvas.bind("<Button-1>", self._on_canvas_press)
        canvas.bind("<Motion>", self._on_canvas_motion)

        pager = tk.Frame(middle, bg=BG_PANEL)
        pager.pack(side=tk.LEFT, fill=tk.Y, padx=(2, 0))
        self._page_up = tk.Label(pager, text=_PAGE_UP_GLYPH, font=_SMALL_FONT,
                                 bg=BG_PANEL, fg=BORDER_COLOR, cursor="hand2")
        self._page_up.pack(side=tk.TOP)
        self._page_up.bind("<Button-1>", lambda _e: self._scroll(-ROWS))
        self._page_down = tk.Label(pager, text=_PAGE_DOWN_GLYPH,
                                   font=_SMALL_FONT, bg=BG_PANEL,
                                   fg=BORDER_COLOR, cursor="hand2")
        self._page_down.pack(side=tk.BOTTOM)
        self._page_down.bind("<Button-1>", lambda _e: self._scroll(ROWS))

        footer = tk.Frame(body, bg=BG_PANEL)
        footer.pack(fill=tk.X, padx=4, pady=(0, 4))
        self._name_label = tk.Label(footer, text="", anchor="w",
                                    font=_TEXT_FONT, bg=BG_PANEL, fg=FG_TEXT)
        self._name_label.pack(fill=tk.X)
        second = tk.Frame(footer, bg=BG_PANEL)
        second.pack(fill=tk.X)
        # Packed FIRST: pack order is allocation priority, so a long shortcode
        # is clipped before the page read-out is.
        self._range_label = tk.Label(second, text="", anchor="e",
                                     font=_SMALL_FONT, bg=BG_PANEL, fg=FG_DIM)
        self._range_label.pack(side=tk.RIGHT)
        self._code_label = tk.Label(second, text="", anchor="w",
                                    font=_TEXT_FONT, bg=BG_PANEL, fg=FG_ACCENT)
        self._code_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _bind_keys(self, widget) -> None:
        """The navigation keys, on the entry AND on the popup (which holds
        the focus after a click on anything that is not the entry). Every
        handler answers "break", so the entry's copy ends the chain and the
        popup's copy never runs for the same key press."""
        for key, dx, dy in _MOVE_KEYS:
            widget.bind(key, lambda _e, dx=dx, dy=dy: self._key_move(dx, dy))
        widget.bind("<Return>", lambda _e: self._key_accept())
        widget.bind("<Tab>", lambda _e: self._key_accept())

    def _app_toplevel(self):
        try:
            return self._parent.winfo_toplevel()
        except tk.TclError:
            return self._parent

    def _teardown(self, destroy: bool, notify: bool = True) -> None:
        """The ONE way a popup ends. ``destroy`` False when Tk is already
        destroying it; ``notify`` False when the caller never had, or still
        has, an open picker (a failed build, a change of mode)."""
        win = self._win
        if win is None:
            return
        self._win = None                # every callback checks this first
        self._cancel_focus_after(win)
        self._release_owner()
        if self._var is not None and self._trace_id:
            try:
                self._var.trace_remove("write", self._trace_id)
            except Exception:
                pass
        if destroy:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self._reset()
        if notify:
            self._call("on_close", self._on_close)

    def _on_destroy(self, event) -> None:
        # <Destroy> reaches the popup's bindings for every descendant too.
        if self._win is not None and getattr(event, "widget", None) is self._win:
            self._teardown(False)

    # ==================================================================== #
    # content                                                              #
    # ==================================================================== #
    def _set_entry(self, text: str) -> None:
        var = self._var
        if var is None:
            return
        try:
            if var.get() == text:
                return
            self._muted = True
            var.set(text)
        except tk.TclError:
            pass
        finally:
            self._muted = False

    def _on_var_write(self, *_args) -> None:
        """The search box changed (a key, a paste, a cut)."""
        if self._win is None or self._muted or self._var is None:
            return
        try:
            text = self._var.get()
        except tk.TclError:
            return
        self._apply_query(text)

    def _apply_query(self, text: str) -> None:
        query = text.strip()
        if query == self._query:
            return
        self._query = query
        self._load()

    def _load(self) -> None:
        """Hand the model the list for the current query / category."""
        if self._query:
            try:
                found = list(self._search(self._query, PAGE) or ())[:PAGE]
            except Exception:
                found = []
        elif self._groups:
            found = self._groups[self._group_index][1]
        else:
            found = ()
        self._model.set_items(found)
        # The cells changed under a pointer that did not move: a hover must
        # not take the highlight off the first result (see _on_cell_motion).
        self._pointer = self._pointer_xy()
        self._restyle_strip()
        self._render()

    def _select_group(self, index: int) -> None:
        """A category was chosen: show it, ending any search."""
        if self._win is None or not 0 <= index < len(self._groups):
            return
        self._group_index = index
        self._set_entry("")
        self._query = ""
        self._load()

    def _scroll(self, rows: int) -> None:
        if self._win is not None and self._model.scroll(int(rows)):
            self._render()

    def _tile(self, seq):
        tiles = self._tiles
        if tiles is None:
            return None
        try:
            return tiles.image(seq)
        except Exception:
            return None

    def _render(self) -> None:
        """Repaint the cells that changed, the highlight and the footer."""
        if self._win is None:
            return
        visible = self._model.visible()
        count = len(visible)
        for index in range(len(self._cell_seq)):
            seq = None
            if index < count:
                seq = getattr(visible[index], "seq", None) or None
            if seq == self._cell_seq[index]:
                continue
            # Guarded PER CELL: one bad tile must not blank the cells after it.
            try:
                self._paint_cell(index, seq)
                self._cell_seq[index] = seq
            except tk.TclError:
                self._cell_seq[index] = None    # shows nothing: not pickable
        try:
            self._restyle_selection()
            self._update_footer()
        except tk.TclError:
            pass

    def _paint_cell(self, index: int, seq) -> None:
        """Show ``seq`` in cell ``index``: its tile, else the character."""
        canvas = self._canvas
        image_item = self._image_items[index]
        text_item = self._text_items[index]
        if seq is None:
            canvas.itemconfigure(image_item, image="", state="hidden")
            canvas.itemconfigure(text_item, text="", state="hidden")
            return
        image = self._tile(seq)
        if image is not None:
            try:
                canvas.itemconfigure(image_item, image=image, state="normal")
                canvas.itemconfigure(text_item, text="", state="hidden")
                return
            except tk.TclError:
                pass        # not an image: this cell falls back to the text
        canvas.itemconfigure(image_item, image="", state="hidden")
        canvas.itemconfigure(text_item, text=seq, state="normal")

    def _restyle_selection(self) -> None:
        lit = self._model.selected_cell()
        if lit == self._lit:
            return
        canvas = self._canvas
        if 0 <= lit < PAGE:
            x = (lit % COLS) * CELL_PX
            y = (lit // COLS) * CELL_PX
            canvas.coords(self._highlight, x, y, x + CELL_PX, y + CELL_PX)
            canvas.itemconfigure(self._highlight, state="normal")
        else:
            canvas.itemconfigure(self._highlight, state="hidden")
        self._lit = lit

    def _restyle_strip(self) -> None:
        try:
            for index, label in enumerate(self._strip):
                active = not self._query and index == self._group_index
                label.configure(fg=FG_ACCENT if active else FG_DIM,
                                bg=BG_ENTRY if active else BG_PANEL)
        except tk.TclError:
            pass

    def _update_footer(self) -> None:
        item = self._model.selected()
        if item is None:
            name, codes = (_NO_MATCH if self._query else _NO_EMOJI), ""
        else:
            name, codes = describe(item)
        first, last, total = self._model.span()
        span = "%d-%d of %d" % (first, last, total) if total > PAGE else ""
        self._name_label.configure(text=name,
                                   fg=FG_DIM if item is None else FG_TEXT)
        self._code_label.configure(text=codes)
        self._range_label.configure(text=span)
        offset = self._model.offset
        self._page_up.configure(fg=FG_ACCENT if offset > 0 else BORDER_COLOR)
        self._page_down.configure(
            fg=FG_ACCENT if offset < self._model.max_offset() else BORDER_COLOR)

    # ==================================================================== #
    # placement                                                            #
    # ==================================================================== #
    def _work_area(self, x: int, y: int) -> tuple:
        """Usable EDGES ``(left, top, right, bottom)`` of the monitor holding
        ``(x, y)``: the house enumeration ``center_over`` uses, then Tk's
        virtual desktop, then "anywhere". Override on the instance in tests."""
        for probe in (lambda: ui_helpers._monitor_work_rect(x, y),
                      lambda: ui_helpers._virtual_bounds(self._parent)):
            try:
                rect = probe()
            except Exception:
                rect = None
            if rect and len(rect) == 4 and rect[2] > rect[0] and rect[3] > rect[1]:
                return tuple(int(value) for value in rect)
        reach = 1 << 20
        return (x - reach, y - reach, x + reach, y + reach)

    def _place(self, x: int, y: int, fresh: bool) -> None:
        win = self._win
        if win is None:
            return
        width, height = self._size
        left, top = place_popup(x, y, width, height, self._work_area(x, y),
                                flip_gap=self._flip_gap)
        try:
            win.wm_geometry("%dx%d+%d+%d" % (width, height, left, top))
            if fresh:
                # Position and band while still withdrawn, then ONE deiconify:
                # the window maps where it belongs (facts.md -- on a mapped
                # window ``-topmost`` discards a pending move).
                if self._focus_name():
                    win.wm_attributes("-topmost", True)
                win.wm_deiconify()
                win.lift()
            else:
                win.update_idletasks()      # apply the move now ...
                win.lift()                  # ... and come back to the front
        except tk.TclError:
            pass

    # ==================================================================== #
    # keyboard                                                             #
    # ==================================================================== #
    def _key_move(self, dx: int, dy: int) -> str:
        self.move_selection(dx, dy)
        return "break"

    def _key_accept(self) -> str:
        self.accept()
        return "break"

    def _on_escape(self) -> str:
        self.close()
        return "break"

    # ==================================================================== #
    # mouse                                                                #
    # ==================================================================== #
    def _pointer_xy(self):
        try:
            return tuple(self._win.winfo_pointerxy())
        except (tk.TclError, AttributeError):
            return None

    @staticmethod
    def _cell_at(x, y) -> int:
        """The cell under canvas point ``(x, y)``, or -1."""
        try:
            col, row = int(x) // CELL_PX, int(y) // CELL_PX
        except (TypeError, ValueError):
            return -1
        if 0 <= col < COLS and 0 <= row < ROWS:
            return row * COLS + col
        return -1

    def _on_canvas_press(self, event):
        return self._on_cell_press(
            self._cell_at(getattr(event, "x", None), getattr(event, "y", None)))

    def _on_canvas_motion(self, event) -> None:
        index = self._cell_at(getattr(event, "x", None),
                              getattr(event, "y", None))
        filled = 0 <= index < len(self._cell_seq) and self._cell_seq[index]
        cursor = "hand2" if filled else ""
        if cursor != self._cursor and self._canvas is not None:
            self._cursor = cursor
            try:
                self._canvas.configure(cursor=cursor)
            except tk.TclError:
                pass
        self._on_cell_motion(index, event)

    def _on_cell_press(self, index: int):
        """A press on a cell picks its emoji (see the module docstring for
        why the press, not the release). A cell that SHOWS nothing -- empty,
        or one that could not be painted -- is not a pick."""
        if (self._win is None or not 0 <= index < len(self._cell_seq)
                or not self._cell_seq[index]
                or not self._model.has_cell(index)):
            return None
        self._model.select_cell(index)
        self.accept()
        return "break"

    def _on_cell_motion(self, index: int, event) -> None:
        """Hover highlights -- but only when the pointer really moved.
        Windows posts a mouse-move to a window that appears (or changes)
        under a RESTING pointer; acted on, that would take the highlight off
        the first result while the user is typing, and Return would then pick
        whatever the mouse happened to be parked over."""
        spot = (getattr(event, "x_root", None), getattr(event, "y_root", None))
        if spot == self._pointer:
            return
        self._pointer = spot
        if self._win is None or not self._model.select_cell(index):
            return
        try:
            self._restyle_selection()
            self._update_footer()
        except tk.TclError:
            pass

    def _on_wheel(self, event) -> str:
        delta = getattr(event, "delta", 0)
        if delta:
            self._scroll(-WHEEL_ROWS if delta > 0 else WHEEL_ROWS)
        return "break"

    def _on_press(self, _event=None) -> None:
        """A press landed somewhere inside the popup: whatever the focus is
        doing, the user is using the popup -- cancel a pending close. Bound
        on the Toplevel, a bindtag of every descendant. Returns None so the
        pressed widget's own binding still counts."""
        self._cancel_focus_after(self._win)

    # ==================================================================== #
    # focus                                                                #
    # ==================================================================== #
    def _focus_name(self) -> str:
        """Tk path of the widget of THIS application that has the keyboard
        focus; "" when another application has it (Tcl's ``focus -displayof``
        -- ``<Activate>`` / ``<Deactivate>`` are never delivered here)."""
        widget = self._win if self._win is not None else self._parent
        try:
            return str(widget.tk.call("focus", "-displayof", widget._w))
        except tk.TclError:
            return ""

    def _inside(self, name: str) -> bool:
        path = str(self._win)
        return name == path or name.startswith(path + ".")

    def _focus_entry(self) -> None:
        # focus_force, not focus_set: an overrideredirect popup is not made
        # the active window by being shown, so a plain focus request would
        # only be remembered and the keys would keep going to the editor.
        try:
            self._entry.focus_force()
            self._entry.icursor(tk.END)
        except tk.TclError:
            pass

    def _watch_owner(self) -> None:
        """``take_focus=False``: remember which widget is driving the popup
        and hear about it losing the focus or being destroyed. The popup
        itself holds no focus in this mode, so no ``<FocusOut>`` of its own
        could ever close it."""
        name = self._focus_name()
        if not name or self._inside(name):
            return
        try:
            owner = self._win.nametowidget(name)
        except (KeyError, tk.TclError):
            return
        binds = []
        try:
            for sequence, handler in (("<FocusOut>", self._on_focus_out),
                                      ("<Destroy>", self._on_owner_destroy)):
                binds.append((sequence, owner.bind(sequence, handler, add="+")))
        except tk.TclError:
            pass
        self._owner = owner
        self._owner_binds = binds

    def _release_owner(self) -> None:
        owner, binds = self._owner, self._owner_binds
        self._owner = None
        self._owner_binds = []
        if owner is None:
            return
        for sequence, funcid in binds:
            try:
                # funcid-scoped: the owner's own handlers stay.
                owner.unbind(sequence, funcid)
            except Exception:
                pass

    def _on_owner_destroy(self, event) -> None:
        """The driving widget is being destroyed while the popup's own
        parent may live on: nothing is left to drive the popup."""
        if (self._owner is None
                or getattr(event, "widget", None) is not self._owner):
            return
        # Its bindings die with it; unbinding a widget mid-destroy is noise.
        self._owner = None
        self._owner_binds = []
        self.close()

    def _on_focus_out(self, _event=None) -> None:
        """The popup, something in it, or the driving widget lost the focus.
        Decide once the hand-off has settled, not now."""
        win = self._win
        if win is None:
            return
        self._cancel_focus_after(win)
        try:
            self._focus_after = win.after(_FOCUS_SETTLE_MS, self._focus_check)
        except tk.TclError:
            self._focus_after = None

    def _focus_check(self) -> None:
        win = self._win
        if win is None:
            return
        self._cancel_focus_after(win)
        name = self._focus_name()
        if name and (self._inside(name)
                     or (self._owner is not None and name == str(self._owner))):
            return
        self.close()

    def _cancel_focus_after(self, win) -> None:
        handle = self._focus_after
        self._focus_after = None
        if handle is None or win is None:
            return
        try:
            win.after_cancel(handle)
        except (tk.TclError, ValueError):
            pass
