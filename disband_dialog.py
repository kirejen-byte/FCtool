# disband_dialog.py
"""Kick > Disband Fleet end to end: the flow, the run, and the slide question.

``fc_gui`` keeps thin wiring only (``FCToolGUI._disband_fleet``): it builds a
``DisbandFlow`` through ``launch`` with its collaborators injected - the
dispatcher, the result modal, the MOTD-link disarm, the autopush in-flight
flag and the MOTD baseline recorder - the way Close Clients is split. The pure
rules (what is kicked, the MOTD-first order, the result text) live in
``kick_pods``.

THE FLOW, by thread:

* Tk: the gate (boss + an authenticated character), else a note and nothing.
* worker: the roster read; every pilot but the FC is a candidate.
* Tk: the slide-to-confirm question (``open_dialog``), listing them by name.
* Tk, on the slide: "Auto-update MOTD" is turned off FIRST (live AND
  remembered, through its one writer) - else the next autopush writes the
  template back over the disband message.
* worker: ``run`` - wait out an autopush PUT already in flight, MOTD, kicks;
  if that PUT outlived the wait it may land AFTER the message, so the run
  waits for it again and writes the message back (or says it could not).
* Tk: the MOTD baseline is moved to the disband message (re-arming the link
  in the same fleet then pushes the template again), one counts-only INFO
  line, the result modal.

ONE RUN AT A TIME: from the click to the result (fetch, question, run) the
flow is ``busy``; a second Disband meanwhile only gets a short note.

THE DIALOG - the Close Clients mechanism, ``slide_confirm.SlideToConfirm``
inside a ``make_modal`` DECISION dialog. ONE phase only, the question. The
slide's confirm closes this window (releasing the application grab, so the
FCPreview tiles and hotkeys work again at once) and THEN calls ``on_confirm``.

THE ONLY ROAD TO ``on_confirm`` IS THE SLIDE: nothing here binds a key, there
is no default button, and the one button (Cancel), Escape and the window X all
run ``cancel``, which calls only ``on_cancel``. Both are one-shot.

WHAT IS LISTED IS WHAT IS KICKED: the flow lists the names of exactly the ids
it passes to the run, and the run re-reads the roster and kicks only those
still in the fleet - a pilot who joins after this list was drawn stays.
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

import kick_pods
from kick_pods import DISBAND_MOTD, pilots
from slide_confirm import SlideToConfirm
from ui_helpers import make_modal
from ui_theme import BG_DARK, BG_ENTRY, BORDER_COLOR, FG_RED, FG_TEXT

#: How long the run waits for an autopush PUT already in flight - once before
#: the MOTD, and once more after the kicks when it outlived the first wait.
AUTOPUSH_WAIT_S = 15.0
_POLL_S = 0.1

BUSY_TEXT = "Disband Fleet is already running."
NOT_BOSS_TEXT = ("You must be the current fleet boss to disband the fleet.\n"
                 "Use 'Refresh fleet' after forming or joining a fleet, then "
                 "try again.")


# -- the run (worker thread; no Tk) ------------------------------------------

def wait_clear(inflight, wait_s, *, clock, sleep) -> bool:
    """Poll ``inflight()`` until it is falsy or ``wait_s`` pass; True = clear."""
    deadline = clock() + wait_s
    while inflight():
        if clock() >= deadline:
            return False
        sleep(_POLL_S)
    return True


def run(auth, fleet_id, ids, own_id, *, inflight, clock=None, sleep=None,
        wait_s=AUTOPUSH_WAIT_S):
    """Disband's worker half. Returns ``(res, motd_risk)``.

    ``res`` is ``kick_pods.disband``'s tuple. ``motd_risk`` is True when an
    autopush PUT was still in flight after the run and the disband message
    could not be written back after it (the template may be what the fleet
    sees). ``inflight`` reads the autopush flag (the link is already off, so
    no NEW autopush starts; only one already dispatched can land)."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    cleared = wait_clear(inflight, wait_s, clock=clock, sleep=sleep)
    res = kick_pods.disband(auth, fleet_id, ids, own_id)
    if cleared or res[0] is None:           # None: nothing was touched
        return res, False
    if not wait_clear(inflight, wait_s, clock=clock, sleep=sleep):
        return res, True
    try:
        again = bool(auth.set_fleet_motd(fleet_id, DISBAND_MOTD))
    except Exception:
        again = False
    if not again:
        return res, True
    return (True,) + tuple(res[1:]), False


def _spawn(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


# -- the flow (Tk thread, except _fetch / _work) -------------------------------

class DisbandFlow:
    """One Disband Fleet, click to result. Collaborators (all injected):

    ``post_ui(fn, *a)``  the worker -> Tk dispatcher.
    ``notify(text, tone)``  a modal on the Tk thread; tone "ok" / "warn" /
                         "info" / "error".
    ``disarm_link()``    Tk thread: turn "Auto-update MOTD" off for good;
                         truthy when it was on (live or remembered).
    ``inflight()``       worker thread: is an autopush PUT in flight?
    ``on_motd_set(markup)``  Tk thread, after the message was written: move
                         the autopush baseline to it.
    ``log``              logger; counts only, never a name.
    ``resolve_name(cid)``  optional; default ``zkill_monitor.resolve_name``.
    """

    def __init__(self, parent, auth, fleet_id, is_boss, *, post_ui, notify,
                 disarm_link, inflight, on_motd_set, log, resolve_name=None,
                 clock=None, sleep=None):
        self.parent, self.auth = parent, auth
        self.fleet_id, self.is_boss = fleet_id, bool(is_boss)
        self._post_ui, self._notify = post_ui, notify
        self._disarm_link, self._inflight = disarm_link, inflight
        self._on_motd_set, self._log = on_motd_set, log
        self._resolve_name = resolve_name
        self._clock, self._sleep = clock, sleep
        self._link_off = False
        self.busy = False

    def start(self):
        auth = self.auth
        if not self.fleet_id or not self.is_boss:
            self._notify(NOT_BOSS_TEXT, "warn")
            return
        self.own = getattr(auth, "character_id", None) if auth else None
        if auth is None or not auth.is_authenticated or not self.own:
            self._notify("No authenticated character.", "warn")
            return
        self.busy = True
        try:
            _spawn(self._fetch)
        except Exception:
            self._log.exception("Disband Fleet: the roster read could not start")
            self._end("The fleet roster could not be read. See fctool.log.",
                      "error")

    def _fetch(self):                      # worker
        rows, err = [], None
        try:
            resolve = self._resolve_name
            if resolve is None:
                from zkill_monitor import resolve_name as _rn

                def resolve(cid):
                    return _rn(cid, "character")
            members = self.auth.get_fleet_members(fleet_id=self.fleet_id)
            if not isinstance(members, list):
                raise RuntimeError("the fleet roster did not load")
            rows = sorted(
                ((cid, resolve(cid) or str(cid))
                 for cid in kick_pods.member_ids(members, self.own)),
                key=lambda r: (r[1].casefold(), r[0]))
        except Exception as e:
            err = str(e)
        self._post_ui(self._confirm, rows, err)

    def _confirm(self, rows, err):
        if err:
            self._end(f"Could not read fleet members:\n{err}", "error")
            return
        if not rows:
            self._end("No one else is in the fleet.", "info")
            return
        ids = [cid for cid, _n in rows]
        try:
            open_dialog(self.parent, [n for _c, n in rows],
                        lambda: self._go(ids), on_cancel=self._cancelled,
                        log=self._log)
        except Exception:
            self._log.exception("Disband Fleet: the dialog could not be opened")
            self._end("The Disband Fleet dialog could not be opened. "
                      "See fctool.log.", "error")

    def _cancelled(self):
        self.busy = False

    def _go(self, ids):
        """The slide confirmed: link off FIRST, then the run on a worker. It
        never raises (the dialog would only log it): a failure here shows the
        "run failed" result."""
        try:
            self._link_off = bool(self._disarm_link())
            _spawn(self._work, ids)
        except Exception:
            self._log.exception("Disband Fleet: the run could not start")
            self._finish(None, False)

    def _work(self, ids):                  # worker
        res, risk = None, False
        try:
            res, risk = run(self.auth, self.fleet_id, ids, self.own,
                            inflight=self._inflight, clock=self._clock,
                            sleep=self._sleep)
        except Exception:
            self._log.exception("Disband Fleet: the run failed")
        self._post_ui(self._finish, res, risk)

    def _finish(self, res, risk):
        self.busy = False
        if res is not None and res[0]:
            try:
                self._on_motd_set(DISBAND_MOTD)
            except Exception:
                self._log.exception("Disband Fleet: the MOTD baseline failed")
        motd, kicked, failed, skipped, reread = res or (None, 0, 0, 0, None)
        self._log.info("Disband Fleet: motd=%s risk=%s kicked=%d failed=%d "
                       "skipped=%d reread=%s link_off=%s", motd, risk, kicked,
                       failed, skipped, reread, self._link_off)
        self._notify(*kick_pods.disband_result_text(
            res, link_off=self._link_off, motd_risk=risk))

    def _end(self, text, tone):
        self.busy = False
        self._notify(text, tone)


def launch(previous, *args, **kwargs):
    """Start a Disband unless ``previous`` (the last flow) is still busy, in
    which case only its note is shown. Returns the flow to keep."""
    if previous is not None and previous.busy:
        previous._notify(BUSY_TEXT, "info")
        return previous
    flow = DisbandFlow(*args, **kwargs)
    flow.start()
    return flow


# -- the slide question (Tk shell) ---------------------------------------------

#: The list shows at most this many rows; more scroll.
MAX_ROWS = 12

_TITLE_FONT = ("Consolas", 12, "bold")    # positive sizes are POINTS
_BODY_FONT = ("Consolas", 10)
_LIST_FONT = ("Consolas", 9)
_BODY_CHARS = 60      # the title/body wrap at about this many body characters

BODY = (f'FCTool sets the fleet MOTD to "{DISBAND_MOTD}", then kicks every '
        "pilot listed below. You stay behind in a one-man fleet: ESI has no "
        "disband call. Kicks cannot be undone.")


def header_text(n: int) -> str:
    return f"Disband the fleet? Kick {pilots(n)}."


def slide_text(n: int) -> str:
    return f"slide to kick {pilots(n)}"


class _DisbandDialog:
    """Controller hung on the window as ``win._dd`` (the test seam):
    ``slide``, ``list_text``, ``scrollbar`` (None when every row fits),
    ``button``, ``title_label``, ``body_label``, ``cancel()``, ``confirm()``.
    """

    def __init__(self, win, names, on_confirm, *, on_cancel=None, clock=None,
                 log=None):
        self.win = win
        self._names = names
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        self._clock = clock
        self._logger = log
        self._done = False          # confirm or cancel has run: one-shot
        self.slide = self.list_text = self.scrollbar = None
        self.button = self.title_label = self.body_label = None

    def build(self):
        win, n = self.win, len(self._names)
        header = header_text(n)
        frame = tk.Frame(win, bg=BG_DARK, padx=18, pady=16,
                         highlightbackground=BORDER_COLOR,
                         highlightthickness=1)
        frame.pack(fill=tk.BOTH, expand=True)

        # Pack order is space priority: the controls first, from the bottom,
        # so a tiny screen squeezes the LIST, never the slide or the button.
        btn_row = tk.Frame(frame, bg=BG_DARK)
        btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(14, 0))
        self.button = ttk.Button(btn_row, text="Cancel", style="Dark.TButton",
                                 command=self.cancel)
        self.button.pack(side=tk.RIGHT)
        self.slide = SlideToConfirm(frame, slide_text(n), self.confirm,
                                    clock=self._clock)
        self.slide.pack(side=tk.BOTTOM, pady=(14, 0))   # no fill: fixed track

        self.title_label = tk.Label(frame, text=header, font=_TITLE_FONT,
                                    fg=FG_RED, bg=BG_DARK, anchor=tk.W,
                                    justify=tk.LEFT)
        self.title_label.pack(side=tk.TOP, fill=tk.X)
        self.body_label = tk.Label(frame, text=BODY, font=_BODY_FONT,
                                   fg=FG_TEXT, bg=BG_DARK, anchor=tk.W,
                                   justify=tk.LEFT)
        self.body_label.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))

        list_font = tkfont.Font(root=win, font=_LIST_FONT)
        unit = max(1, list_font.measure("0"))
        need_px = max([list_font.measure(name) for name in self._names] or [0])
        wrap = tk.Frame(frame, bg=BG_ENTRY, highlightbackground=BORDER_COLOR,
                        highlightthickness=1)
        wrap.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        self.list_text = tk.Text(
            wrap, font=_LIST_FONT, wrap="none", height=max(1, min(n, MAX_ROWS)),
            width=max(1, -(-need_px // unit)), bg=BG_ENTRY, fg=FG_TEXT,
            cursor="arrow", takefocus=0, borderwidth=0, highlightthickness=0,
            padx=8, pady=4, exportselection=False, selectbackground=BG_ENTRY,
            selectforeground=FG_TEXT, inactiveselectbackground=BG_ENTRY)
        self.list_text.insert("1.0", "\n".join(self._names))
        self.list_text.configure(state="disabled")   # read-only AFTER the fill
        if n > MAX_ROWS:
            self.scrollbar = ttk.Scrollbar(wrap, orient=tk.VERTICAL,
                                           command=self.list_text.yview)
            self.list_text.configure(yscrollcommand=self.scrollbar.set)
            self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.list_text.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Wrap in PIXELS measured from the fonts, so it follows tk scaling.
        wrap_px = max(
            tkfont.Font(root=win, font=_BODY_FONT).measure("0") * _BODY_CHARS,
            tkfont.Font(root=win, font=_TITLE_FONT).measure(header),
            self.slide.geo.track_w)
        self.title_label.configure(wraplength=wrap_px)
        self.body_label.configure(wraplength=wrap_px)

        # The wheel scrolls the list (its class binding) and stops here: the
        # app's global wheel router must not scroll a tab behind the dialog.
        # add="+": make_modal's <Escape> and the slide's <FocusOut> live on
        # this toplevel too, and a plain bind() replaces them.
        win.bind("<MouseWheel>", lambda _e: "break", add="+")
        self.button.focus_set()     # the only key that does anything backs OUT

    def _alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def _close(self):
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _hand_over(self, callback, what):
        if callback is None:
            return
        try:
            callback()
        except Exception:
            if self._logger is not None:
                try:
                    self._logger.exception("Disband Fleet: the %s failed", what)
                except Exception:
                    pass

    def cancel(self):
        """Cancel, Escape and the window X: close, then ``on_cancel`` once."""
        self._close()
        if not self._done:
            self._done = True
            self._hand_over(self._on_cancel, "cancel")

    def confirm(self):
        """The slide's callback: close the window, THEN hand over. One-shot,
        latched before anything runs; a dead window hands over nothing."""
        if self._done or not self._alive():
            return
        self._done = True
        self._close()
        self._hand_over(self._on_confirm, "confirm")


def open_dialog(parent, names, on_confirm, *, on_cancel=None, clock=None,
                log=None):
    """Open the "Disband Fleet" question over ``parent``; returns its Toplevel.

    ``names``       the display names of exactly the pilots the run will kick
                    (copied here).
    ``on_confirm``  zero-argument callable, called AT MOST ONCE, after the
                    window is gone, when the slide confirms.
    ``on_cancel``   optional zero-argument callable, called at most once when
                    the question is backed out of (and never after a confirm).
    ``clock``       optional monotonic-seconds callable for the slide's arm
                    delay (tests inject a fake).
    ``log``         optional logger; only ``exception`` is used, never a name.

    Created and handed to ``make_modal`` in THIS function: the shape
    ``tests/test_dialog_placement.py`` looks for (centred over the tool, the
    application grab, Escape = cancel). The window X is wired to the same
    cancel explicitly.
    """
    names = tuple(names)
    win = tk.Toplevel(parent)
    win.title("Disband Fleet")
    win.resizable(False, False)
    ctl = _DisbandDialog(win, names, on_confirm, on_cancel=on_cancel,
                         clock=clock, log=log)
    make_modal(win, parent, on_cancel=ctl.cancel)
    win.protocol("WM_DELETE_WINDOW", ctl.cancel)
    try:
        ctl.build()
    except Exception:
        ctl.cancel()        # never leave a half-built window holding the grab
        raise
    win._dd = ctl
    return win
