"""The refit command's over-client toast — outcome lines plus CLICKABLE options.

Design: ``docs/superpowers/specs/2026-09-11-doctrine-refits-design.md`` §14.
Tk-thread only; the pure grammar it reports on lives in ``refit_command.py``.

A deliberate sibling of ``range_check.RangeToast`` and ``client_toast``, not a
subclass of either: ``ClientToast`` renders one title + one body string and
``RangeToast`` renders a per-cell red/green grid, while this needs plain text
rows PLUS rows that answer a click with a fit id. What is SHARED is the pure
geometry helper ``client_toast.place_over`` and the alpha / fade / re-top
constants, so the three toasts cannot drift apart in placement or feel.

House rules honoured (full account in ``client_toast``'s docstring):

* Placement ONLY via ``win32.set_window_pos`` in PHYSICAL px — never Tk
  ``geometry()``, which is logical px under PMv2 and misplaces on a mixed-DPI
  desktop.
* Ex-styles are re-asserted after EVERY alpha change, because Tk's
  ``wm attributes -alpha`` rewrites the whole ``GWL_EXSTYLE`` word on the
  opaque<->layered transition.
* Measuring happens in the constructor while the window is still WITHDRAWN;
  nothing may flush the event loop between the map and the move, or the toast
  flashes opaque at Tk's default position for one frame.
* **No ``grab_set``, ever.** Any Tk grab deafens the FCPreview tiles
  (``docs/agents/map/facts.md``) — a toast that stole the grab would freeze the
  FC's client previews while it faded. Pinned by a source guard in the tests.
* It positions its OWN hwnd only; ``window_activator`` stays the only module
  that touches a real client window.

The click contract, which is the whole reason this class exists: a click on an
OPTION row is a decision (``on_pick(fit_id)`` then dismiss); a click ANYWHERE
else is a refusal (dismiss, no callback). Default failure is no change, the
same promise ``refit_command`` keeps on the engine side, so letting the toast
fade or mis-clicking the background can never swap a fit.
"""
from __future__ import annotations

import tkinter as tk

from client_toast import (ALPHA, FADE_MS, FADE_STEPS, RETOP_MS, place_over,
                          scaled_bounds)
from ui_theme import BG_PANEL, BORDER_COLOR, FG_ACCENT, FG_DIM, FG_TEXT

#: Toast box bounds in px at the BASELINE display scaling (96 dpi / tk scaling
#: 1.333). Same class of window as ``RangeToast``'s, so the two land on the
#: client at the same scale — including the ceiling's DPI treatment: every
#: label here is sized in POINTS, so the content grows with the monitor's dpi
#: and the CEILINGS are scaled to match at runtime by
#: ``client_toast.scaled_bounds`` (the live value is ``RefitToast.max_size``).
#: Without that, an option row is clipped at the window edge on a high-dpi
#: display and reads as a DIFFERENT fit — the one wrong answer a click-to-swap
#: toast must not give. Measured 2026-09-19 for the widest realistic toast (a
#: long title, 3 long lines, ``MAX_OPTIONS`` long option labels + overflow +
#: hint): 460px at tk scaling 1.333, 512px at 1.5, 566px at 1.667, 674px at
#: 2.0, 782px at 2.333, 998px at 3.0 — all clear of the SCALED ceiling (760 /
#: 854 / 951 / 1139 / 1331 / 1708), whereas the old fixed 760 started clipping
#: from ~2.3 upward. This toast has far more headroom than ``RangeToast``: its
#: content is one column of text, not a grid. The FLOORS are deliberately not
#: scaled.
MIN_W, MAX_W = 260, 760
MIN_H, MAX_H = 60, 560
#: Hold before the fade begins, matching the range check's feel.
TOAST_SECONDS = 10.0
#: How many option rows are worth clicking before the list becomes a wall. The
#: overflow is COUNTED, never silently dropped (see ``MORE_FMT``).
MAX_OPTIONS = 6
#: The overflow line. A toast that hid options without saying so would read as
#: "those are all the refits", which is the one wrong answer here.
MORE_FMT = "…and {n} more"
#: Footer hint. The right-click half of ``client_toast.DEFAULT_HINT`` is a
#: promise about a snooze this toast does not implement, so it has its own.
HINT = "click an option to swap  ·  click elsewhere to dismiss"
_FONT = "Consolas"


class RefitToast:
    """One refit outcome over the EVE client that sent the command.

    ``lines`` are plain text rows (the outcome message, the MOTD-push note).
    ``options`` are ``[(fit_id, label)]`` rows rendered in the accent colour
    with a hand cursor; clicking one calls ``on_pick(fit_id)`` and dismisses.

    Lifecycle mirrors its siblings: construct (builds hidden and measures),
    ``show(rect)`` places and maps, the hold timer fades and destroys.
    ``dismiss()`` is idempotent and safe to call from inside a callback."""

    def __init__(self, root, title, lines, options, *, on_pick,
                 win32=None, seconds=TOAST_SECONDS, on_dismiss=None,
                 accent=None, max_options=MAX_OPTIONS):
        if win32 is None:                                  # pragma: no cover
            from preview_tile import _real_tile_win32
            win32 = _real_tile_win32()
        self._win32 = win32
        self._on_pick = on_pick
        self._on_dismiss = on_dismiss
        self._accent = accent or FG_ACCENT
        try:
            self._seconds = max(0.5, float(seconds))
        except (TypeError, ValueError):
            self._seconds = TOAST_SECONDS
        try:
            self._max_options = max(1, int(max_options))
        except (TypeError, ValueError):
            self._max_options = MAX_OPTIONS
        self._alive = True
        self._after_ids: list = []
        self._alpha = ALPHA
        self._dismissers: list = []
        #: ``fit_id -> Label`` for the rendered option rows, in render order.
        #: Public so a test can click one without hunting the widget tree.
        self.option_widgets: dict = {}

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.configure(bg=self._accent)     # 1px accent border via padding
        self.top.withdraw()

        inner = tk.Frame(self.top, bg=BG_PANEL)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self._dismissers.extend([self.top, inner])

        self._build(inner, title, lines, options)

        # Measure while STILL WITHDRAWN — an idle flush here cannot flash the
        # window, and show() must not flush at all.
        self._w, self._h = self._measure()

        self._hwnd = self._win32.get_root_hwnd(self.top.winfo_id())
        self._restyle()
        for widget in self._dismissers:
            widget.bind("<Button-1>", self._on_click)

    # ── build ────────────────────────────────────────────────────────────────
    def _label(self, parent, text, fg, size=9, bold=False):
        font = (_FONT, size, "bold") if bold else (_FONT, size)
        return tk.Label(parent, text=text, bg=BG_PANEL, fg=fg, anchor="w",
                        justify="left", font=font)

    def _build(self, inner, title, lines, options):
        lbl = self._label(inner, str(title), self._accent, size=11, bold=True)
        lbl.pack(fill="x", padx=8, pady=(6, 2))
        self._dismissers.append(lbl)

        rule = tk.Frame(inner, bg=BORDER_COLOR, height=1)
        rule.pack(fill="x", padx=8)
        self._dismissers.append(rule)

        for text in (lines or ()):
            row = self._label(inner, str(text), FG_TEXT)
            row.pack(fill="x", padx=8, pady=(3, 0))
            self._dismissers.append(row)

        shown = list(options or ())[:self._max_options]
        hidden = max(0, len(options or ()) - len(shown))
        for fit_id, label in shown:
            row = self._label(inner, str(label), self._accent)
            row.configure(cursor="hand2")
            row.pack(fill="x", padx=8, pady=(3, 0))
            # Bound per ROW, and the handler returns "break": every descendant
            # carries the Toplevel in its bindtags, so without the break the
            # top's dismiss binding would also run for this click.
            row.bind("<Button-1>",
                     lambda _ev, fid=fit_id: self._on_option(fid))
            self.option_widgets[fit_id] = row
        if hidden:
            more = self._label(inner, MORE_FMT.format(n=hidden), FG_DIM,
                               size=7)
            more.pack(fill="x", padx=8, pady=(3, 0))
            self._dismissers.append(more)

        hint = self._label(inner, HINT, FG_DIM, size=7)
        hint.pack(fill="x", padx=8, pady=(3, 4))
        self._dismissers.append(hint)

    def _measure(self):
        """Content size in px, floored and capped at the DPI-SCALED ceiling.
        See ``RangeToast._measure``: Tk lays out in logical px while
        ``set_window_pos`` takes physical px, and measuring keeps the two in
        the relationship the shipped toasts already assume.

        ``MAX_W``/``MAX_H`` are the 96-dpi figures, so the ceiling is resolved
        through ``scaled_bounds`` FIRST — the labels are point-sized and grow
        with the monitor. The result is cached and published as ``max_size``,
        so "fits" and "clamped" are tellable apart."""
        self._max_w, self._max_h = scaled_bounds(self.top, MAX_W, MAX_H)
        try:
            self.top.update_idletasks()
            w = int(self.top.winfo_reqwidth())
            h = int(self.top.winfo_reqheight())
        except tk.TclError:                                # pragma: no cover
            return MIN_W, MIN_H
        return (max(MIN_W, min(w + 2, self._max_w)),
                max(MIN_H, min(h + 2, self._max_h)))

    # ── internals (mirror ClientToast / RangeToast) ──────────────────────────
    def _restyle(self):
        try:
            self._win32.exclude_from_alt_tab(self._hwnd)
        except Exception:
            pass

    def _set_alpha(self, a):
        self._alpha = a
        try:
            self.top.attributes("-alpha", a)
        except tk.TclError:
            return
        self._restyle()

    def _after(self, ms, fn):
        if not self._alive:
            return
        try:
            self._after_ids.append(self.top.after(int(ms), fn))
        except tk.TclError:
            pass

    def _cancel_timers(self):
        for aid in self._after_ids:
            try:
                self.top.after_cancel(aid)
            except (tk.TclError, ValueError):
                pass
        self._after_ids = []

    def _on_click(self, _ev=None):
        self.dismiss()

    def _on_option(self, fit_id):
        """A decision: hand the fit id back, then close. The callback runs
        BEFORE the destroy so a handler may inspect the toast, and its failure
        can never leave the window parked on the client."""
        cb = self._on_pick
        try:
            if cb is not None:
                cb(fit_id)
        finally:
            self.dismiss()
        return "break"

    def _retop_once(self):
        if not self._alive:
            return
        try:
            self._win32.retop(self._hwnd)
        except Exception:
            pass

    def _begin_fade(self, step=0):
        if not self._alive:
            return
        if step >= FADE_STEPS:
            self.dismiss()
            return
        self._set_alpha(ALPHA * (1.0 - (step + 1) / float(FADE_STEPS)))
        self._after(max(1, FADE_MS // FADE_STEPS),
                    lambda: self._begin_fade(step + 1))

    # ── public surface ───────────────────────────────────────────────────────
    @property
    def hwnd(self):
        return self._hwnd

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def size(self):
        return (self._w, self._h)

    @property
    def max_size(self):
        """The EFFECTIVE ``(width, height)`` ceiling this toast was measured
        against — ``(MAX_W, MAX_H)`` scaled for the display's tk scaling.
        ``size == max_size`` on an axis means the content was CLAMPED there
        (clipped at the window edge); ``size < max_size`` means it fits."""
        return (self._max_w, self._max_h)

    def current_alpha(self):
        return self._alpha

    def show(self, client_rect, fallback_xy=None):
        """Place over ``client_rect`` (EDGES, physical px) and start the hold.

        Nothing may flush the event loop between the map and the move — the
        Toplevel carries no Tk geometry, so a stray ``update_idletasks`` here
        flashes it at Tk's default position for one frame before it jumps over
        the client (measuring happens in the constructor for that reason)."""
        if not self._alive:
            return False
        xy = place_over(client_rect, self._w, self._h)
        if xy is None:
            xy = fallback_xy
        if xy is None:
            self.dismiss()
            return False
        x, y = int(xy[0]), int(xy[1])
        self._set_alpha(0.0)
        try:
            self.top.deiconify()
        except tk.TclError:
            return False
        try:
            self._win32.set_window_pos(self._hwnd, x, y, self._w, self._h)
        except Exception:
            pass
        self._set_alpha(ALPHA)
        self._after(RETOP_MS, self._retop_once)
        self._after(int(self._seconds * 1000), self._begin_fade)
        return True

    def dismiss(self):
        """Destroy the toast. Idempotent; safe from a bound callback."""
        if not self._alive:
            return
        self._alive = False
        self._cancel_timers()
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        cb = self._on_dismiss
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
