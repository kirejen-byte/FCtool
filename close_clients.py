"""Close Clients: ask every listed EVE client to close, then watch what happens.

The pure core of the red "Close Clients" button (the Tk dialog shell lives at
the bottom of this file). Nothing in here touches a window, the OS or Tk: the
two things that do - "is this still the same client?" and "ask this window to
close" - are INJECTED (``still_same`` / ``close_window``), which is also why
this module never imports ``ctypes``, ``fc_gui`` or ``eve_client_tracker``.
Clients are duck-typed: anything with ``.hwnd``, ``.pid``, ``.char_name`` and
``.is_login`` (``eve_client_tracker.ClientWindow`` in production).

DESIGN: "request close, then watch". ``close_listed`` posts ONE close request
to each listed client, in order, and never anything stronger - no re-post, no
answering a prompt, no force-kill. What EVE does with the request is not known
(it may exit at once, or put up its own "quit the game?" prompt), so the result
is not assumed: ``CloseRun`` re-checks the targets on each tick until they are
all gone or a deadline passes, and whatever is still open then is reported as
still open rather than treated as an error to escalate.

WHY CLOSED-NESS IS DECIDED PER TARGET. A target counts as closed only on what
``still_same(target)`` says about that exact window (hwnd + pid). It is never
inferred from a fresh sweep of the desktop: a failed window enumeration returns
an EMPTY list, and "none of my targets are in the list" would then read as
"everything closed" - the one wrong answer this feature must not give.

WHY ONE "NOT THE SAME" READING IS NOT ENOUGH. ``still_same`` is injected, and
the production predicate answers False for more than "that window is gone": it
swallows its own exceptions into False, and a window whose title cannot be read
for a moment is False too. A single False is therefore not proof and is never
latched. ``CloseRun`` counts a target closed only after TWO CONSECUTIVE
not-the-same readings taken on two different ticks (a ``GONE`` outcome from
``close_listed`` is the first of the two); any reading that says "still there",
and any check that raises, starts the count again. The report errs toward
"still open": a target with only ONE such reading when the deadline passes is
reported as still open, never as closed. Two readings defend against a
TRANSIENT blind read only: a failure to read that lasts two polls still reads
as closed, because the production predicate folds "cannot read the title" into
False.

Identity is the WINDOW, not the label: a listed login screen that has since
become a character is still the listed window and is still closed; a handle
that has been reused by a different process is "gone" and is left alone.

Layout rule for the list (``list_layout``), one constant: up to ``MAX_ROWS``
rows in one column, then two columns filled down-then-across, and a scrollbar
only past ``2 * MAX_ROWS`` rows - so 1 client is one row, 25 clients fit with
no scrolling, and the dialog's height is capped whatever the count.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

# The tallest the list ever gets, in rows. The dialog's "slide and button never
# pushed off-screen" guarantee rests on this cap, not on pack order.
MAX_ROWS = 13

# EVE's own maximum character-name length. Character names always fit; window
# titles in general are unbounded, so anything longer is clipped.
MAX_NAME_CHARS = 37

# The single non-ASCII character this module produces.
ELLIPSIS = "…"

POSTED = "posted"      # the close request was posted to the window
GONE = "gone"          # no longer the listed client at confirm: nothing sent
FAILED = "failed"      # the request could not be posted (see Outcome.error)

_UNNAMED = "(unnamed client)"

# How many CONSECUTIVE "not the same client" readings make a target closed.
# One is not proof (module docstring: the injected predicate also answers
# False when it could not look), so CloseRun never latches on a single one.
_CLOSED_AFTER_READS = 2


def _plural(n: int, noun: str) -> str:
    """``"1 client"`` / ``"3 clients"`` (every noun here takes a plain s)."""
    return "%d %s" % (n, noun if n == 1 else noun + "s")


def _row_name(name) -> str:
    """One list row for a character name: single-line, at most
    ``MAX_NAME_CHARS`` characters (code points) INCLUDING the ellipsis.

    Whitespace runs (a tab or newline in a hand-made window title included)
    collapse to one space - the list is one fixed-height text widget with a
    tab stop between its columns, so a stray tab or newline would move cells.

    A name that is too long is cut to ``MAX_NAME_CHARS - 1`` characters, the
    space the cut may have left at its end is dropped, and the ellipsis goes
    on - so a clipped row can be SHORTER than ``MAX_NAME_CHARS``, never longer.
    """
    text = " ".join(str(name or "").split()) or _UNNAMED
    if len(text) > MAX_NAME_CHARS:
        text = text[:MAX_NAME_CHARS - 1].rstrip() + ELLIPSIS
    return text


def describe(clients) -> list[str]:
    """The list rows: character names, then ONE row for all login screens.

    Rows sort case-insensitively on the name AS DISPLAYED - whitespace already
    collapsed, already clipped to ``MAX_NAME_CHARS`` - so what the user reads
    is in order whatever a raw window title started with (stable: rows that
    read the same keep their given order). Login screens have no name to
    show, so they collapse to ``Login screen`` / ``Login screens x3`` - N
    clients therefore never need more than N rows.
    """
    clients = list(clients)
    rows = sorted((_row_name(c.char_name) for c in clients if not c.is_login),
                  key=str.casefold)
    logins = sum(1 for c in clients if c.is_login)
    if logins == 1:
        rows.append("Login screen")
    elif logins > 1:
        rows.append("Login screens x%d" % logins)
    return rows


def list_layout(n_rows: int) -> tuple[int, int, bool]:
    """``(cols, visible_rows, scroll)`` for a list of ``n_rows`` rows.

    ``visible_rows`` is the height of the list in text lines (never more than
    ``MAX_ROWS``, and exactly the row count for a short list - no padding
    rows); ``scroll`` is True only when two full columns still do not fit.
    ``n_rows`` of 0 (or less) is ``(1, 0, False)``.
    """
    n = max(0, int(n_rows))
    cols = 1 if n <= MAX_ROWS else 2
    rows = -(-n // cols)                       # ceil(n / cols)
    return cols, min(rows, MAX_ROWS), rows > MAX_ROWS


def format_rows(rows, cols: int) -> list[tuple[str, str]]:
    """Lay ``rows`` out as ``(left, right)`` display lines, COLUMN-MAJOR.

    Two columns fill down, then across: the left column takes the first
    ``ceil(n / 2)`` rows and the right column the rest, so reading down the
    left column and then down the right one gives ``rows`` back in order. The
    right cell is ``""`` where there is nothing (always, in one column).
    """
    rows = list(rows)
    if cols < 2:
        return [(row, "") for row in rows]
    n = len(rows)
    per_col = -(-n // 2)
    return [(rows[i], rows[i + per_col] if i + per_col < n else "")
            for i in range(per_col)]


def header_text(clients) -> str:
    """``Close 1 EVE client?`` / ``Close 25 EVE clients? (23 characters, 2
    login screens)``.

    The breakdown is printed only when BOTH kinds are present; when everything
    is one kind it would just repeat the total.
    """
    clients = list(clients)
    logins = sum(1 for c in clients if c.is_login)
    chars = len(clients) - logins
    text = "Close %s?" % _plural(len(clients), "EVE client")
    if chars and logins:
        text += " (%s, %s)" % (_plural(chars, "character"),
                               _plural(logins, "login screen"))
    return text


def slide_text(n: int) -> str:
    """``slide to close 1 client`` / ``slide to close 25 clients``."""
    return "slide to close %s" % _plural(int(n), "client")


@dataclass(frozen=True)
class Outcome:
    """What happened to ONE listed client at confirm.

    ``status`` is ``POSTED``, ``GONE`` or ``FAILED``. ``error`` is 0 unless the
    status is ``FAILED``: then the Win32 error code, or -1 when there is none
    (including "the check or the call raised").
    """
    client: object
    status: str
    error: int = 0


def close_listed(listed, *, still_same, close_window) -> list[Outcome]:
    """Ask each LISTED client to close, in order; one ``Outcome`` per entry.

    For every client: ``still_same(client)`` first, and the request - if there
    is one - straight after it, before the next client is even looked at (the
    gap between "this is the listed client" and "ask it to close" is one call
    wide, never the length of the list). False -> ``GONE`` and ``close_window``
    is NOT called (the window closed by itself, or its handle now belongs to
    something else). Otherwise ``close_window(client.hwnd)``: 0 -> ``POSTED``,
    anything else -> ``FAILED`` with that code.

    One bad client never stops the rest: a raising ``still_same`` or
    ``close_window`` (or a result that is not a number) is ``FAILED`` with
    error -1, and when it was the identity check that raised, nothing is sent
    to that window - an unverified handle is never asked to close.

    At most ONE request per window handle, and identity is still judged PER
    ENTRY. Two entries on one hwnd are each put to ``still_same`` on their own
    (a stale entry is ``GONE`` whatever its twin was, and can never make the
    live one ``GONE``); only the REQUEST is shared - the second verified entry
    repeats the first's ``POSTED`` / ``FAILED`` and nothing is sent again. The
    request is booked as ``FAILED`` -1 BEFORE the call, so a ``close_window``
    that raised is not tried a second time either.
    """
    outcomes: list[Outcome] = []
    asked: dict = {}                 # hwnd -> (status, error) of its ONE request
    for client in listed:
        try:
            hwnd = client.hwnd
            if not still_same(client):         # identity: every entry, always
                outcome = Outcome(client, GONE)
            elif hwnd in asked:                # only the REQUEST is shared
                outcome = Outcome(client, *asked[hwnd])
            else:
                asked[hwnd] = (FAILED, -1)     # booked before the call
                code = int(close_window(hwnd))
                outcome = (Outcome(client, POSTED) if code == 0
                           else Outcome(client, FAILED, code))
                asked[hwnd] = (outcome.status, outcome.error)
        except Exception:
            outcome = Outcome(client, FAILED, -1)
        outcomes.append(outcome)
    return outcomes


def newcomers(listed, fresh) -> list:
    """Clients in ``fresh`` whose window HANDLE was not in the list the user saw.

    Keyed on ``hwnd`` and nothing else. A fresh window that happens to sit on
    a listed hwnd is therefore NOT a newcomer: that handle was already dealt
    with at confirm, where it was either re-validated (same pid) and asked to
    close, or found to be someone else's and reported gone.

    Computed once at confirm and only ever REPORTED: a client that started
    after the list was drawn was never shown to the user and is never closed.
    """
    seen = {c.hwnd for c in listed}
    return [c for c in (fresh or ()) if c.hwnd not in seen]


class CloseRun:
    """The watch after the requests went out: who has actually closed?

    ``tick()`` puts every target that is not closed yet to ``still_same``
    ONCE and returns ``"pending"`` or ``"done"``. It is ``"done"`` once every
    target is closed or ``timeout_s`` has elapsed since construction, and it
    STAYS done: later ticks check nothing and change nothing.

    THE TWO-READING RULE. A target is closed only after TWO CONSECUTIVE
    not-the-same readings, and one tick takes one reading - so never on a
    single look. ``still_same`` is injected and the production predicate turns
    its own exceptions, and a title it could not read for a moment, into
    False; latching on one False would report a running client as closed. A
    reading that says "still there" resets that target's count, and so does a
    check that raises HERE (cannot tell is not "closed").

    What that means for the caller:

      * a ``GONE`` outcome is the FIRST of the two readings, not a verdict:
        ``closed_count`` is 0 straight after construction even when every
        outcome is ``GONE``, and such a run can be done on its first tick
        (it is, when every one of them reads not-the-same again);
      * a ``POSTED`` or ``FAILED`` target that goes away needs two ticks
        (a ``FAILED`` one is watched like any other);
      * gone, then seen again, then gone: two MORE ticks;
      * at the deadline a target with only ONE not-the-same reading is a
        SURVIVOR. The report errs toward "still open".

    Decided PER TARGET, from the listed targets alone; there is no whole-
    desktop sweep in this class (module docstring: an empty sweep would read
    as "everything closed"). Closing is one-way: a target that reached two
    readings is never re-checked.

    No sleeping and no timer in here - the caller owns the cadence, and time
    is read only through ``clock``.
    """

    def __init__(self, outcomes, *, still_same, timeout_s: float = 15.0,
                 clock=time.monotonic):
        self.outcomes = tuple(outcomes)
        self._targets = [o.client for o in self.outcomes]
        # Consecutive not-the-same readings per target. GONE at confirm was
        # one reading already; nothing is closed until a tick confirms it.
        self._gone_reads = [1 if o.status == GONE else 0
                            for o in self.outcomes]
        self._closed = [False] * len(self._targets)
        self._still_same = still_same
        self._clock = clock
        self._deadline = clock() + float(timeout_s)
        self._done = False

    def tick(self) -> str:
        if self._done:
            return "done"
        for i, client in enumerate(self._targets):
            if self._closed[i]:
                continue
            try:
                still_open = bool(self._still_same(client))
            except Exception:
                still_open = True              # cannot tell is not "closed"
            if still_open:
                self._gone_reads[i] = 0        # consecutive, or it is nothing
                continue
            self._gone_reads[i] += 1
            if self._gone_reads[i] >= _CLOSED_AFTER_READS:
                self._closed[i] = True
        # Checked AFTER the sweep, so the tick that hits the deadline still
        # counts whatever closed just before it.
        if all(self._closed) or self._clock() >= self._deadline:
            self._done = True
            return "done"
        return "pending"

    @property
    def done(self) -> bool:
        return self._done

    @property
    def total(self) -> int:
        return len(self._targets)

    @property
    def closed_count(self) -> int:
        return sum(self._closed)

    @property
    def survivors(self) -> list:
        """The listed clients not counted closed, in listed order (a target
        with a single not-the-same reading is still one of them)."""
        return [c for c, closed in zip(self._targets, self._closed)
                if not closed]


def result_lines(run, n_newcomers: int = 0) -> tuple[str, str | None]:
    """``(headline, detail_or_None)`` for the end of a run.

    All closed -> ``Closed N clients.`` and no detail unless clients started
    after the list was made. Otherwise the headline counts what is still open
    and the detail says why that is not necessarily a failure. The newcomer
    sentence, when there is one, is the LAST line of the detail (separated by
    a newline).
    """
    total = run.total
    still_open = total - run.closed_count
    n_newcomers = max(0, int(n_newcomers or 0))
    late = None
    if n_newcomers == 1:
        late = "1 client started after this list was made and was left open."
    elif n_newcomers > 1:
        late = ("%d clients started after this list was made and were left "
                "open." % n_newcomers)
    if still_open <= 0:
        return "Closed %s." % _plural(total, "client"), late
    if total == 1:
        headline = "The client is still open."
    else:
        headline = "%d of %d clients %s still open." % (
            still_open, total, "is" if still_open == 1 else "are")
    detail = ("Still open: EVE may be asking you to confirm in %s, or EVE is "
              "running as administrator."
              % ("that client" if still_open == 1 else "each client"))
    if late:
        detail += "\n" + late
    return headline, detail


def closing_line(outcomes) -> str:
    """ONE sentence for the watch phase: what was ACTUALLY asked at confirm.

    Counted from the outcomes, so it never claims a request that did not go
    out - the line is on screen for the whole watch, and "every client was
    asked" is false exactly when the reader most needs the truth (nothing
    could be posted to an elevated client; everything had already exited):

      * every one ``POSTED``  -> ``Asked all 25 listed clients to close.``
        (``Asked the listed client to close.`` for one);
      * some ``POSTED``       -> ``Asked 3 of 5 listed clients to close (1
        already gone, 1 could not be asked).``;
      * none, any ``FAILED``  -> ``No close request could be sent.`` (with
        the same bracket when some were already gone);
      * none, all ``GONE``    -> ``The listed clients were already gone.``
    """
    statuses = [outcome.status for outcome in outcomes]
    total = len(statuses)
    posted = statuses.count(POSTED)
    gone = statuses.count(GONE)
    failed = statuses.count(FAILED)
    if not total:
        return "No client was listed."
    if posted == total:
        if total == 1:
            return "Asked the listed client to close."
        return "Asked all %d listed clients to close." % total
    notes = []
    if gone:
        notes.append("%d already gone" % gone)
    if failed:
        notes.append("%d could not be asked" % failed)
    why = " (%s)" % ", ".join(notes) if notes else ""
    if posted:
        return "Asked %d of %d listed clients to close%s." % (posted, total,
                                                              why)
    if failed:
        return "No close request could be sent%s." % (why if gone else "")
    if total == 1:
        return "The listed client was already gone."
    return "The listed clients were already gone."


# ── Tk shell ─────────────────────────────────────────────────────────────────
# Nothing above this line uses Tk, and nothing below it is needed by the core.
# The dialog's imports are kept down here with it so the boundary stays visible
# in the file (the slide_confirm.py shape).
import tkinter as tk                                              # noqa: E402
import tkinter.font as tkfont                                     # noqa: E402
from tkinter import ttk                                           # noqa: E402

from slide_confirm import SlideToConfirm                          # noqa: E402
from ui_helpers import make_modal                                 # noqa: E402
from ui_theme import (                                            # noqa: E402
    BG_DARK, BG_ENTRY, BORDER_COLOR, FG_ACCENT, FG_GREEN, FG_RED, FG_TEXT,
    FG_YELLOW,
)

#: The watch cadence: one ``CloseRun.tick()`` per poll. A target needs TWO
#: not-the-same readings on two ticks, so the fastest "all closed" is two
#: polls (~1 s) after confirm - it is never instant, by design.
POLL_MS = 500
#: How long "Closed N clients." stays up before the dialog closes itself.
AUTO_CLOSE_MS = 2500

PHASE_CONFIRM = "confirm"       # the question; the application grab is held
PHASE_CLOSING = "closing"       # requests sent, grab released, watching
PHASE_DONE = "done"             # the watch is over (or it failed)

#: The CONFIRM body line (wording: docs/eve_reference/mechanics.md).
IN_SPACE_WARNING = (
    "Closing a client in space is NOT a Safe Log Off - the ship stays in "
    "space until its log-off timer ends (5 min after NPC aggression, 15 min "
    "after capsuleer aggression).")
#: The CLOSING body is ``closing_line(outcomes)`` (what was really asked) and
#: then this, which is true whatever the outcomes were.
_CLOSING_NOTE = (
    "FCTool is watching - Dismiss stops the watch, it does not undo "
    "anything.")
_ERROR_TITLE = "Close Clients stopped early."
_ERROR_BODY = (
    "Something went wrong and FCTool stopped watching. Clients that were "
    "already asked to close may still be closing - check them, then dismiss "
    "this window.")

# Positive sizes are POINTS: every font below follows ``tk scaling``, and the
# slide sizes itself the same way, so the whole dialog scales together.
_TITLE_FONT = ("Consolas", 12, "bold")
_BODY_FONT = ("Consolas", 10)
_LIST_FONT = ("Consolas", 9)

_BODY_CHARS = 60        # the title/body wrap at about this many body characters
_COL_GAP_CHARS = 3      # gap between the two list columns, in "0" widths
_LIST_PADX = 8          # the list's inner padding, px
_LIST_BORDER = 1        # the 1 px frame drawn round the list
_LABEL_EXTRA_PX = 6     # a tk.Label asks for its text + 6 px (padx 1, bd 2)


class _CloseDialog:
    """The dialog's controller: three phases on ONE Toplevel.

    Built by ``open_dialog`` and hung on the window as ``win._cc`` - the test
    seam, and the only state the feature has (the host keeps nothing but a
    reference to the live window).

      ``phase``        ``"confirm"`` / ``"closing"`` / ``"done"``
      ``slide``        the ``SlideToConfirm``; None once confirmed
      ``list_text``    the read-only ``tk.Text`` holding the list
      ``scrollbar``    its ``ttk.Scrollbar``, or None when everything fits
      ``button``       the one button: "Cancel", then "Dismiss"
      ``title_label`` / ``body_label``   the two text lines
      ``run``          the ``CloseRun``; None until confirm
      ``error``        None, or the exception TYPE name once a step failed
      ``cancel()``     Cancel / Dismiss / Escape / the window X - one path
      ``confirm()``    what the slide calls; the ONLY way to ``close_window``,
                       and it runs ONCE (latched before the first request)
      ``tick()``       one poll step, without scheduling the next

    THE ONLY ROAD TO ``close_window`` IS THE SLIDE. Nothing in here binds a
    key, there is no default button, and the one button only ever closes this
    dialog. Escape, the X and the button all run ``cancel``, which sends
    nothing in any phase.

    WHAT IS LISTED IS WHAT IS ASKED. The clients are snapshotted once, at
    open; the rows are drawn from that snapshot and ``confirm`` walks the SAME
    snapshot. The fresh ``find_clients()`` taken at confirm is only ever
    COUNTED (how many started since) - a client the user was never shown is
    never asked to close.

    THE GRAB ENDS AT CONFIRM. While it is a question this is a DECISION
    dialog and holds the application grab. A Tk grab is app-wide, though: as
    long as it is held, every FCPreview tile and focus hotkey is dead to the
    FC. Once the requests are out there is nothing left to decide, and the
    watch can run for 15 s - so ``confirm`` releases the grab whatever else
    happens in it, including when it fails.

    ONE WIDTH FOR ALL THREE PHASES. The text changes under the FC's pointer
    (question -> progress -> result) and the list shrinks or goes; the window
    must not jump sideways with them. ``build`` works out one content width -
    wide enough for the widest wrapped line, the slide, and the list with ANY
    subset of its rows - and an invisible strut holds it. Only the HEIGHT
    follows the content.

    THE DIALOG CLOSES ITSELF IN EXACTLY ONE CASE: every listed client closed
    AND there is nothing else to read. "All closed, but a client started after
    the list was made" does NOT auto-close - that sentence is the only notice
    the FC gets that a client is still running, so it stays until dismissed.
    So does any result with a client still open, and any failure.

    The dialog OWNS its timer. At most one ``after`` job is pending at a time
    (the poll, or the auto-close - never both); it is cancelled on any cancel
    and on the window's own ``<Destroy>``, and every callback checks the
    window is still there first, so a late one is a no-op - a stray poll after
    the watch is over touches nothing, the pending auto-close included.
    """

    def __init__(self, win, listed, *, find_clients, still_same, close_window,
                 log, clock):
        self.win = win
        self._listed = listed
        self._find_clients = find_clients
        self._still_same = still_same
        self._close_window = close_window
        self._logger = log
        self._slide_clock = clock               # None = the slide's default
        self._clock = clock or time.monotonic
        self.phase = PHASE_CONFIRM
        self.run = None
        self.error = None
        self.slide = self.list_text = self.scrollbar = None
        self.button = self.title_label = self.body_label = None
        self._list_wrap = self._list_font = None
        self._n_new = 0
        self._closing_body = ""                 # set at confirm
        self._job = None                        # the ONE pending after id
        self._dead = False
        self._confirmed = False                 # confirm() is one-shot
        self._dismiss_logged = False            # "watch dismissed": once

    # ── build ────────────────────────────────────────────────────────────────
    def build(self):
        win = self.win
        # A Font belongs to its interpreter and dies with the Python object,
        # so the one used after the build (DONE re-measures the list) is kept.
        self._list_font = tkfont.Font(root=win, font=_LIST_FONT)
        header = header_text(self._listed)

        frame = tk.Frame(win, bg=BG_DARK, padx=18, pady=16,
                         highlightbackground=BORDER_COLOR,
                         highlightthickness=1)
        frame.pack(fill=tk.BOTH, expand=True)

        # Pack order is space priority, so the two controls go in FIRST, from
        # the bottom up. The row cap is what keeps the dialog short; this is
        # only the second line of defence - Tk clamps a toplevel to the screen
        # height, and on a very small screen it is then the LIST that gives,
        # never the slide or the button.
        btn_row = tk.Frame(frame, bg=BG_DARK)
        btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(14, 0))
        self.button = ttk.Button(btn_row, text="Cancel", style="Dark.TButton",
                                 command=self.cancel)
        self.button.pack(side=tk.RIGHT)
        self.slide = SlideToConfirm(frame, slide_text(len(self._listed)),
                                    self.confirm, clock=self._slide_clock)
        # No fill / expand: the canvas's requested size IS the track.
        self.slide.pack(side=tk.BOTTOM, pady=(14, 0))

        self.title_label = tk.Label(frame, text=header, font=_TITLE_FONT,
                                    fg=FG_RED, bg=BG_DARK, anchor=tk.W,
                                    justify=tk.LEFT)
        self.title_label.pack(side=tk.TOP, fill=tk.X)
        self.body_label = tk.Label(frame, text=IN_SPACE_WARNING,
                                   font=_BODY_FONT, fg=FG_TEXT, bg=BG_DARK,
                                   anchor=tk.W, justify=tk.LEFT)
        self.body_label.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))

        # ONE read-only Text, not a Listbox: a Listbox has no tab stops, and
        # the second column has to sit at a PIXEL position (see _show_list).
        self._list_wrap = tk.Frame(frame, bg=BG_ENTRY,
                                   highlightbackground=BORDER_COLOR,
                                   highlightthickness=_LIST_BORDER)
        self.list_text = tk.Text(
            self._list_wrap, font=_LIST_FONT, wrap="none", height=1, width=1,
            bg=BG_ENTRY, fg=FG_TEXT, cursor="arrow", takefocus=0,
            borderwidth=0, highlightthickness=0, padx=_LIST_PADX, pady=4,
            exportselection=False, selectbackground=BG_ENTRY,
            selectforeground=FG_TEXT, inactiveselectbackground=BG_ENTRY)
        self.list_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        list_px = self._show_list(describe(self._listed))

        # Wrap in PIXELS measured from the fonts (a bare wraplength number is
        # px and would not follow tk scaling): wide enough that the header
        # never wraps, and never narrower than the list or the slide.
        wrap_px = max(
            tkfont.Font(root=win, font=_BODY_FONT).measure("0") * _BODY_CHARS,
            tkfont.Font(root=win, font=_TITLE_FONT).measure(header),
            list_px, self.slide.geo.track_w)
        self.title_label.configure(wraplength=wrap_px)
        self.body_label.configure(wraplength=wrap_px)
        # ... and that IS the content width, in every phase: nothing in the
        # dialog asks for more (a label is its wrapped text + a fixed margin;
        # list_px covers the list box for any subset of the rows), and this
        # strut asks for exactly that much whatever the texts shrink to.
        # Without it the window follows its widest wrapped line, so "Closed
        # 25 clients." collapsed it to a stub with Dismiss somewhere else.
        tk.Frame(frame, width=wrap_px + _LABEL_EXTRA_PX, height=1,
                 bg=BG_DARK).pack(side=tk.TOP, after=self.body_label)

        # add="+" on the toplevel, always: make_modal's <Escape>, the slide's
        # <FocusOut> reset and the host's own <Destroy> all live there, and a
        # plain bind() REPLACES every handler on its sequence.
        win.bind("<MouseWheel>", self._on_wheel, add="+")
        win.bind("<Destroy>", self._on_destroy, add="+")
        # Focus rests on Cancel: the only key that does anything here (Space
        # on the focused button, or Escape) backs OUT.
        self.button.focus_set()

    # ── the list ─────────────────────────────────────────────────────────────
    def _show_list(self, rows) -> int:
        """Draw ``rows`` into the list and size it to them; returns the px
        width to RESERVE for the list box. No rows hides the list altogether.

        The height is EXACTLY the visible row count (1 client = 1 line), and
        in two-column mode the right cell starts at a pixel TAB STOP measured
        from the widest left cell. Never space padding: a CJK or emoji name
        is not one cell wide even in a fixed-width font, so padded columns
        would not line up. The width comes from the same pixel measurements
        (rounded UP to the Text's character unit, the width of "0"), so
        nothing is cut off and the list is no wider than it has to be.

        The RETURNED width is what the whole box (text, padding, border and
        the scrollbar when there is one) asks for with these rows OR ANY
        SUBSET OF THEM: the widest row is allowed for in both columns. DONE
        redraws the list with only the survivors, which fall into the columns
        differently - two long names that shared a column can end up side by
        side - and the dialog's one width (``build``) has to hold then too.
        """
        rows = list(rows)
        cols, visible, scroll = list_layout(len(rows))
        lines = format_rows(rows, cols)
        measure = self._list_font.measure
        unit = max(1, measure("0"))
        left_px = max([measure(left) for left, _right in lines] or [0])
        right_px = max([measure(right) for _left, right in lines] or [0])
        if cols > 1:
            tab_px = left_px + _COL_GAP_CHARS * unit
            tabs, need_px = (tab_px,), tab_px + right_px
        else:
            tabs, need_px = (), left_px

        text = self.list_text
        # Filled while NORMAL: insert/delete on a disabled Text are silent
        # no-ops, so the toggle around a refill is load-bearing.
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", "\n".join(
            left + ("\t" + right if right else "") for left, right in lines))
        text.configure(state="disabled", tabs=tabs, height=max(1, visible),
                       width=max(1, -(-need_px // unit)))
        text.yview_moveto(0.0)
        self._set_scrollbar(scroll)

        if not rows:
            self._list_wrap.pack_forget()
        elif not self._list_wrap.winfo_manager():
            self._list_wrap.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        widest = max(left_px, right_px)
        reserve_px = (2 * widest + _COL_GAP_CHARS * unit if cols > 1
                      else widest)
        bar_px = (self.scrollbar.winfo_reqwidth()
                  if self.scrollbar is not None else 0)
        return (-(-reserve_px // unit) * unit + 2 * _LIST_PADX
                + 2 * _LIST_BORDER + bar_px)

    def _set_scrollbar(self, wanted):
        """A scrollbar exists ONLY while two full columns do not fit."""
        if wanted and self.scrollbar is None:
            self.scrollbar = ttk.Scrollbar(self._list_wrap, orient=tk.VERTICAL,
                                           command=self.list_text.yview)
            self.list_text.configure(yscrollcommand=self.scrollbar.set)
            self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y,
                                before=self.list_text)
        elif not wanted and self.scrollbar is not None:
            bar, self.scrollbar = self.scrollbar, None
            self.list_text.configure(yscrollcommand="")
            bar.destroy()

    def _on_wheel(self, _event):
        """The wheel never leaves the dialog; scrolling the list is Tk's job.

        Tk hands a wheel event to the widget under the POINTER, whichever
        window Windows addressed it to (measured on this Tk, 8.6.15, with the
        focus on the button). So over the list it is the Text's - or the
        scrollbar's - own class binding that scrolls, once, and anywhere else
        in the dialog there is nothing to scroll: no scrolling code belongs
        here. A class binding does not consume the event, though, and the
        app's global wheel router sits on the "all" tag behind this toplevel's
        - the "break" keeps it from scrolling a tab behind the dialog.
        """
        return "break"

    # ── small helpers ────────────────────────────────────────────────────────
    def _alive(self) -> bool:
        if self._dead:
            return False
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def _set_lines(self, title, body, colour):
        self.title_label.configure(text=title, fg=colour)
        self.body_label.configure(text=body or "")
        if not body:
            self.body_label.pack_forget()       # no blank line under the title
        elif not self.body_label.winfo_manager():
            self.body_label.pack(side=tk.TOP, fill=tk.X, pady=(8, 0),
                                 after=self.title_label)

    def _drop_slide(self):
        slide, self.slide = self.slide, None
        if slide is not None:
            slide.destroy()

    def _release_grab(self):
        try:
            self.win.grab_release()
        except tk.TclError:
            pass

    def _log(self, level, message, *args):
        """Best effort, ASCII, COUNTS ONLY: never a character name or a window
        title (the log is the thing people paste into bug reports), and a
        logger that raises must not break the dialog."""
        if self._logger is None:
            return
        try:
            getattr(self._logger, level)(message, *args)
        except Exception:
            pass

    def _schedule(self, delay_ms, callback):
        self._cancel_job()
        if self._alive():
            self._job = self.win.after(delay_ms, callback)

    def _cancel_job(self):
        job, self._job = self._job, None
        if job is not None:
            try:
                self.win.after_cancel(job)
            except Exception:
                pass

    def _on_destroy(self, event):
        # <Destroy> reaches the toplevel's bindings for every descendant too
        # (the slide is destroyed at confirm): only the window's own counts.
        if event.widget is not self.win:
            return
        self._dead = True
        self._cancel_job()

    # ── the one exit ─────────────────────────────────────────────────────────
    def cancel(self):
        """Cancel, Dismiss, Escape and the window X. Sends nothing, in any
        phase: before confirm nothing has been asked of any client, and after
        it this only stops the watch - it cannot undo a request.

        Safe to call again, and on a window that is already gone: the "watch
        dismissed" line is logged ONCE per dialog (latched before the logger
        runs), however many times this is called."""
        if (self.phase == PHASE_CLOSING and self.run is not None
                and not self._dismiss_logged):
            self._dismiss_logged = True
            self._log_counts("watch dismissed")
        self._cancel_job()
        self._release_grab()
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    # ── confirm: the slide's callback ────────────────────────────────────────
    def confirm(self):
        """Ask every LISTED client to close, then start watching.

        ONE-SHOT, and latched BEFORE anything is sent: the phase only moves
        on once every request is out, so it cannot be the guard - a
        collaborator that let the event loop run mid-send (and with it a
        second confirm) would have every window asked twice. The latch is
        never cleared, so no later phase, failure or stray call can send
        again. A window that is already gone sends nothing either.

        The grab is released in a ``finally``: whatever happens to the
        requests, the app must get its tiles and hotkeys back, and a failure
        must leave an error line and a working Dismiss - never a stuck,
        grabbing window.
        """
        if self._confirmed or not self._alive():
            return
        self._confirmed = True
        try:
            try:
                self._n_new = self._count_newcomers()
                outcomes = close_listed(self._listed,
                                        still_same=self._still_same,
                                        close_window=self._close_window)
                self.run = CloseRun(outcomes, still_same=self._still_same,
                                    clock=self._clock)
            finally:
                self._release_grab()
            self._enter_closing(outcomes)
        except Exception as exc:
            self._fail(exc)

    def _count_newcomers(self) -> int:
        """Clients that started after the list was drawn - counted, reported,
        never closed. A snapshot that cannot be taken is "none": this number
        only adds a sentence to the result, it decides nothing."""
        try:
            fresh = list(self._find_clients() or ())
            return len(newcomers(self._listed, fresh))
        except Exception:
            return 0

    def _enter_closing(self, outcomes):
        self.phase = PHASE_CLOSING
        self._closing_body = "%s %s" % (closing_line(outcomes), _CLOSING_NOTE)
        self._drop_slide()
        self.button.configure(text="Dismiss")
        self._show_progress()
        statuses = [outcome.status for outcome in outcomes]
        self._log("info",
                  "Close Clients: requested - listed=%d posted=%d gone=%d "
                  "failed=%d newcomers=%d",
                  len(statuses), statuses.count(POSTED), statuses.count(GONE),
                  statuses.count(FAILED), self._n_new)
        self._schedule(POLL_MS, self._poll)

    def _show_progress(self):
        self._set_lines("Closing... %d of %d closed"
                        % (self.run.closed_count, self.run.total),
                        self._closing_body, FG_ACCENT)

    # ── the watch ────────────────────────────────────────────────────────────
    def _poll(self):
        """The ``after`` loop: one ``tick``, then the next poll while the
        watch is still on. A callback that outlived its window - or its watch
        - does NOTHING: once the watch is over the one pending job is the
        auto-close, and a stray poll must not cancel it."""
        if self.phase != PHASE_CLOSING or not self._alive():
            return
        self._cancel_job()      # spent when the timer ran this; harmless then
        if self.tick() == PHASE_CLOSING:
            self._schedule(POLL_MS, self._poll)

    def tick(self) -> str:
        """ONE poll step - no scheduling; returns the phase afterwards.

        A no-op outside the CLOSING phase and on a dead window, so a stray
        late call can neither restart a finished watch nor raise."""
        if (self.phase != PHASE_CLOSING or self.run is None
                or not self._alive()):
            return self.phase
        try:
            if self.run.tick() == "done":
                self._enter_done()
            else:
                self._show_progress()
        except Exception as exc:
            self._fail(exc)
        return self.phase

    def _enter_done(self):
        self.phase = PHASE_DONE
        self._cancel_job()      # the watch is over, whoever ran the last tick
        headline, detail = result_lines(self.run, self._n_new)
        survivors = self.run.survivors
        self._set_lines(headline, detail,
                        FG_YELLOW if survivors else FG_GREEN)
        # The list now answers "what is STILL open?" - only the survivors,
        # or nothing at all.
        self._show_list(describe(survivors))
        self._log_counts("finished")
        if not survivors and not detail:
            # Nothing left to read or to do: get out of the FC's way. With
            # a detail line (all closed, but a client started after the list
            # was made) the dialog STAYS - nothing else would tell the FC.
            self._schedule(AUTO_CLOSE_MS, self._auto_close)

    def _auto_close(self):
        self._cancel_job()
        if self._alive():
            self.cancel()

    def _log_counts(self, what):
        run = self.run
        self._log("info",
                  "Close Clients: %s - listed=%d closed=%d still_open=%d "
                  "newcomers=%d",
                  what, run.total, run.closed_count,
                  run.total - run.closed_count, self._n_new)

    def _fail(self, exc):
        """A step raised: stop, say so, stay dismissable. Only the exception
        TYPE is logged - its message could carry a window title."""
        self.phase = PHASE_DONE
        self.error = type(exc).__name__
        self._cancel_job()
        self._log("error", "Close Clients: stopped on an internal error (%s)",
                  self.error)
        try:
            self._drop_slide()
            self._set_lines(_ERROR_TITLE, _ERROR_BODY, FG_RED)
            self.button.configure(text="Dismiss")
        except Exception:
            pass        # a dying window: Escape and the X still cancel


def open_dialog(parent, clients, *, find_clients, still_same, close_window,
                log=None, clock=None):
    """Open the "Close Clients" dialog over ``parent``; returns its Toplevel.

    ``clients``       the snapshot to list AND to close (``.hwnd``, ``.pid``,
                      ``.char_name``, ``.is_login``); copied here, so nothing
                      the caller does to its list afterwards changes either.
    ``find_clients``  zero-argument callable returning a fresh snapshot;
                      called once, at confirm, only to COUNT newcomers.
    ``still_same``    ``fn(client) -> bool``: is that window still that client?
    ``close_window``  ``fn(hwnd) -> int``: ask ONE window to close; 0 = posted.
                      Reached only from the slide's confirm.
    ``log``           optional logger (``.info`` / ``.error``); counts only.
    ``clock``         optional monotonic-seconds callable shared by the slide's
                      arm delay and the watch's deadline. Tests inject a fake.

    One dialog, three phases (``_CloseDialog``): CONFIRM - the list, the
    in-space warning, the slide and a Cancel button, under the application
    grab; CLOSING - "Closing... k of N closed", grab released; DONE - closes
    itself only when every listed client closed and no client started after
    the list was made; otherwise it stays (listing what is still open, or
    saying that a newcomer was left running) until dismissed. The window
    keeps ONE width through all three.

    The window is created and handed to ``make_modal`` in THIS function: that
    is the shape ``tests/test_dialog_placement.py`` looks for (the dialog opens
    centred over the tool), and ``make_modal`` supplies transient, the grab
    and Escape = cancel. The window X is wired to the same cancel explicitly -
    ``make_modal`` binds Escape only. The caller owns the one-dialog-at-a-time
    guard and may bind the returned window with ``add="+"``.
    """
    listed = tuple(clients)
    win = tk.Toplevel(parent)
    win.title("Close Clients")
    win.resizable(False, False)
    ctl = _CloseDialog(win, listed, find_clients=find_clients,
                       still_same=still_same, close_window=close_window,
                       log=log, clock=clock)
    make_modal(win, parent, on_cancel=ctl.cancel)
    win.protocol("WM_DELETE_WINDOW", ctl.cancel)
    try:
        ctl.build()
    except Exception:
        ctl.cancel()        # never leave a half-built window holding the grab
        raise
    win._cc = ctl
    return win
