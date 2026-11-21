# disband_dialog.py
"""The slide-to-confirm question in front of Kick > Disband Fleet (Tk shell).

The Close Clients mechanism - ``slide_confirm.SlideToConfirm`` inside a
``make_modal`` DECISION dialog - put in front of a different destructive act:
set the fleet MOTD to "Fleet is over", then kick every listed pilot. ONE phase
only, the question. The slide's confirm closes this window (releasing the
application grab, so the FCPreview tiles and hotkeys work again at once) and
THEN calls ``on_confirm``; the run, its worker and its result modal belong to
the caller (``FCToolGUI._disband_fleet``), the pure rules to ``kick_pods``.

THE ONLY ROAD TO ``on_confirm`` IS THE SLIDE: nothing here binds a key, there
is no default button, and the one button (Cancel), Escape and the window X all
run ``cancel``, which calls nothing. ``confirm`` is one-shot.

WHAT IS LISTED IS WHAT IS KICKED: the caller lists the names of exactly the
ids it will pass to the run, and the run re-reads the roster and kicks only
those still in the fleet - a pilot who joins after this list was drawn stays.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from kick_pods import DISBAND_MOTD, pilots
from slide_confirm import SlideToConfirm
from ui_helpers import make_modal
from ui_theme import BG_DARK, BG_ENTRY, BORDER_COLOR, FG_RED, FG_TEXT

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

    def __init__(self, win, names, on_confirm, *, clock=None, log=None):
        self.win = win
        self._names = names
        self._on_confirm = on_confirm
        self._clock = clock
        self._logger = log
        self._confirmed = False
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

    def cancel(self):
        """Cancel, Escape and the window X: close, call nothing."""
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def confirm(self):
        """The slide's callback: close the window, THEN hand over. One-shot,
        latched before anything runs; a dead window hands over nothing."""
        if self._confirmed or not self._alive():
            return
        self._confirmed = True
        self.cancel()
        try:
            self._on_confirm()
        except Exception:
            if self._logger is not None:
                try:
                    self._logger.exception("Disband Fleet: the confirm failed")
                except Exception:
                    pass


def open_dialog(parent, names, on_confirm, *, clock=None, log=None):
    """Open the "Disband Fleet" question over ``parent``; returns its Toplevel.

    ``names``       the display names of exactly the pilots the run will kick
                    (copied here).
    ``on_confirm``  zero-argument callable, called AT MOST ONCE, after the
                    window is gone, when the slide confirms.
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
    ctl = _DisbandDialog(win, names, on_confirm, clock=clock, log=log)
    make_modal(win, parent, on_cancel=ctl.cancel)
    win.protocol("WM_DELETE_WINDOW", ctl.cancel)
    try:
        ctl.build()
    except Exception:
        ctl.cancel()        # never leave a half-built window holding the grab
        raise
    win._dd = ctl
    return win
