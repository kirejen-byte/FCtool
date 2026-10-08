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
reported as still open, never as closed.

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
        outcome is ``GONE``, and such a run is done on its first tick;
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


# ── Tk shell ─────────────────────────────────────────────────────────────────
# (the dialog - `open_dialog` - lands here in a later bite; nothing above this
# line uses Tk, and nothing below it may be needed by the core)
