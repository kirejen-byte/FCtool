"""A small, transient, over-client toast: one tiny override-redirect Toplevel
positioned over an EVE client's window rect, auto-dismissing after a few seconds.

Built for the implant-removal reminder (see ``implant_reminder.py`` and
``docs/superpowers/spikes/2026-07-25-implant-reminder/RESEARCH.md``), but the
window itself is content-agnostic — title + body + callbacks.

**Why this and not ``eveo_overlay.OverlayWindow``.** The overlay spans the whole
virtual desktop (``GetSystemMetrics(76..79)``) AND is colour-keyed
(``-transparentcolor``); a layered window covering the game's full render surface
forces DWM off its independent-flip / MPO fast path, which is what produced the
in-game FPS drops the project hit twice. The culprit is COVERAGE, not re-assert
frequency: shipped FCPreview tiles are layered, topmost AND re-topped at 4 Hz
with no complaint. This toast is in the tile's size class, lives ~10 s, and only
appears while the pilot is docked in station — the lowest-stakes seconds in the
game. It re-tops exactly twice (once at show, once ~1 s later against a
z-order insertion race), never on a tick.

House rules honoured here:

* Placement is ONLY via ``win32.set_window_pos`` in PHYSICAL px — never Tk
  ``geometry()``, which is logical px under PMv2 and would misplace on a
  mixed-DPI desktop (the trap documented at ``preview_tile.py:11``).
* Ex-styles ``WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`` are applied to the GA_ROOT
  hwnd BEFORE the first map, so the toast never lands in Alt-Tab and a click on
  it never pulls focus off the EVE client.
* Tk's ``wm attributes -alpha`` REWRITES the whole ``GWL_EXSTYLE`` word on the
  opaque↔layered transition, so the ex-style bits are re-asserted after EVERY
  ``set_alpha`` (the shipped ``TileWindow._exclude_from_alt_tab`` pattern).
* It positions its OWN hwnd only. ``window_activator`` remains the only module
  that touches a real client window's state — the same carve-out ``preview_tile``
  already has.
* Tk-thread only. Workers marshal via the host's ``_post_ui``.

All Win32 sits behind an injectable backend (``preview_tile._real_tile_win32`` by
default, which is exactly the styling/placement surface this needs); tests inject
a fake and never touch ctypes.
"""
from __future__ import annotations

import tkinter as tk

from ui_theme import BG_PANEL, FG_DIM, FG_TEXT, FG_YELLOW

#: Default toast box in PHYSICAL px. Wide enough for a character name plus three
#: implant names, short enough to stay out of the way of the client's HUD.
DEFAULT_W = 430
DEFAULT_H = 78
#: One re-assert of the topmost band, ~1 s after showing, in case another
#: topmost window was inserted right behind us. NOT a tick — see the module
#: docstring on why coverage, not cadence, is what costs frames.
RETOP_MS = 1000
#: Fade-out shape: total ms and step count, applied after the hold expires.
FADE_MS = 500
FADE_STEPS = 10
#: Resting opacity. Slightly translucent so the client stays readable beneath.
ALPHA = 0.94
#: The hint line's shipped wording, and the default every existing caller gets
#: without asking. The right-click half is a PROMISE about ``on_snooze``: a
#: caller that wires no snooze (the FC HUD's intel pop-up) must pass its own
#: hint, or the toast advertises a "not this session" nobody implemented.
DEFAULT_HINT = "click to dismiss  ·  right-click: not this session"
#: One BODY line's height in px -- Consolas 9's line space on this box,
#: measured, not guessed. ``DEFAULT_H`` already carries one such line (plus the
#: title, the hint and the 1 px accent border), so a multi-line body costs this
#: much per EXTRA line. See ``height_for``.
BODY_LINE_PX = 14


def height_for(body_lines):
    """Toast height for a body of `body_lines` lines. Pure.

    ``DEFAULT_H`` is the ONE-line box every shipped caller uses, so that is
    both the answer for 1 and the floor: a shorter box would only clip the
    title or the hint. Junk degrades to the same floor rather than raising --
    this runs on a user click, and a mis-sized toast beats no toast."""
    try:
        lines = int(body_lines)
    except (TypeError, ValueError):
        lines = 1
    return DEFAULT_H + max(0, lines - 1) * BODY_LINE_PX


def place_over(client_rect, w=DEFAULT_W, h=DEFAULT_H):
    """Top-left corner (physical px) for a ``w`` x ``h`` toast over ``client_rect``.

    ``client_rect`` is ``(left, top, right, bottom)`` EDGES, matching
    ``eve_client_tracker.get_rect`` (and the ``monitor_pin`` convention) — NOT
    x/y/w/h. The toast is centred on BOTH axes over the client, then clamped so
    it can never sit outside the client on either axis (a client narrower or
    shorter than the toast pins it to the client's top-left corner).

    Returns ``None`` for a missing or degenerate rect, so the caller can decide
    on its own fallback rather than guessing a screen position here. Pure."""
    try:
        left, top, right, bottom = (int(v) for v in client_rect)
    except (TypeError, ValueError):
        return None
    cw, ch = right - left, bottom - top
    if cw <= 0 or ch <= 0:
        return None
    w = max(1, int(w))
    h = max(1, int(h))
    x = left + (cw - w) // 2
    y = top + (ch - h) // 2
    # Clamp inside the client rect (never past the right/bottom edge, never
    # before the left/top edge — the max() runs last so a too-small client
    # still yields the client's own origin).
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return (x, y)


class ClientToast:
    """One transient over-client message. Tk-thread only.

    Lifecycle: ``ClientToast(...)`` builds it hidden, ``show(rect)`` places and
    maps it, the hold timer fades and destroys it. ``dismiss()`` is idempotent
    and safe to call from any of the callbacks."""

    def __init__(self, root, title, body, *, win32=None, seconds=12.0,
                 width=DEFAULT_W, height=DEFAULT_H,
                 on_dismiss=None, on_snooze=None, accent=FG_YELLOW,
                 hint=DEFAULT_HINT):
        if win32 is None:                                  # pragma: no cover
            from preview_tile import _real_tile_win32
            win32 = _real_tile_win32()
        self._win32 = win32
        self._on_dismiss = on_dismiss
        self._on_snooze = on_snooze
        self._w = max(1, int(width))
        self._h = max(1, int(height))
        try:
            self._seconds = max(0.5, float(seconds))
        except (TypeError, ValueError):
            self._seconds = 12.0
        self._alive = True
        self._after_ids: list = []
        self._alpha = ALPHA

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.configure(bg=accent)          # 1px accent border via padding
        self.top.withdraw()

        inner = tk.Frame(self.top, bg=BG_PANEL)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        self._title_lbl = tk.Label(
            inner, text=str(title), bg=BG_PANEL, fg=accent, anchor="w",
            font=("Consolas", 11, "bold"))
        self._title_lbl.pack(fill="x", padx=8, pady=(6, 0))

        self._body_lbl = tk.Label(
            inner, text=str(body), bg=BG_PANEL, fg=FG_TEXT, anchor="w",
            justify="left", font=("Consolas", 9))
        self._body_lbl.pack(fill="x", padx=8)

        self._hint_lbl = tk.Label(
            inner, text=str(hint), bg=BG_PANEL, fg=FG_DIM, anchor="w",
            font=("Consolas", 7))
        self._hint_lbl.pack(fill="x", padx=8, pady=(1, 4))

        for w in (self.top, inner, self._title_lbl, self._body_lbl,
                  self._hint_lbl):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Button-3>", self._on_right_click)

        # Resolve the REAL top-level hwnd and style it BEFORE the first map, so
        # the toast is never briefly Alt-Tab-able / activatable.
        self.top.update_idletasks()
        self._hwnd = self._win32.get_root_hwnd(self.top.winfo_id())
        self._restyle()

    # ── internals ────────────────────────────────────────────────────────────
    def _restyle(self):
        """(Re-)assert WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE on our own hwnd.
        Idempotent in the backend, so re-asserting after every alpha change is
        cheap — and mandatory, because -alpha rewrites the whole ex-style word."""
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

    def _on_right_click(self, _ev=None):
        """Dismiss, then snooze if the caller wired one. With no ``on_snooze``
        it is a plain dismiss -- which is exactly why the hint line is a
        parameter (see ``DEFAULT_HINT``): a toast that cannot snooze must not
        advertise one."""
        cb = self._on_snooze
        self.dismiss()
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    # ── public surface ───────────────────────────────────────────────────────
    @property
    def hwnd(self):
        return self._hwnd

    def current_alpha(self):
        """Last requested window alpha (mirror for tests / logging)."""
        return self._alpha

    @property
    def alive(self):
        return self._alive

    def show(self, client_rect, fallback_xy=None):
        """Place over ``client_rect`` (EDGES, physical px) and start the hold.

        Falls back to ``fallback_xy`` when the rect is missing/degenerate; with
        neither, the toast is not shown at all and self-destructs — better than
        parking a mystery box at (0, 0)."""
        if not self._alive:
            return False
        xy = place_over(client_rect, self._w, self._h)
        if xy is None:
            xy = fallback_xy
        if xy is None:
            self.dismiss()
            return False
        x, y = int(xy[0]), int(xy[1])
        # Map INVISIBLE, place, then raise the opacity. The Toplevel carries no
        # Tk geometry (placement is SetWindowPos-only, physical px), so anything
        # that lets it paint before the move — a stray update_idletasks() in
        # particular — can flash it opaque at Tk's default position
        # (primary-monitor top-left) for one frame before it jumps over the
        # client. The shipped precedent, TileWindow.place, deiconifies and places
        # with NOTHING in between; do the same, and belt-and-braces it with
        # alpha 0 across the map. Each _set_alpha re-asserts the ex-style word,
        # which -alpha rewrites on the opaque<->layered transition.
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
