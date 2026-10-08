# slide_confirm.py
"""Slide-to-confirm — the anti-accident gate in front of a destructive action.

A button can be clicked by accident: by the second click of a double-click, by
a click aimed at whatever was under the pointer before the dialog opened, by
Enter or Space landing on a default button, by a stray tap on a touchpad. This
control replaces "click" with a gesture that none of those can produce — press
ON the handle, DRAG it across the track, RELEASE past the threshold — so the
action behind it (closing every EVE client, in the first caller) only happens
on purpose.

Design: ``docs/superpowers/specs/2026-10-07-close-all-clients-design.md`` §5/§8.

House shape (``cycle_roles.py`` / ``market_gap_dialog.py``): a **pure core**
first — ``SlideModel`` and the geometry maths, no Tk, no clock of their own —
and the **Tk shell** (``SlideToConfirm``, a ``tk.Canvas``) below the divider,
its imports kept down with it so the boundary is visible in the file. The
module imports ``tkinter`` and ``ui_theme`` only; it never imports ``fc_gui``.

The contract — every rule is here because its absence is an accident path:

* **The press must land ON the handle.** A click elsewhere on the track does
  nothing. A slider that jumps its handle to the click point (the stock
  ``ttk.Scale`` behaviour) turns one click near the far end into a confirm.
* **Presses are ignored for the first 400 ms after the control first maps.**
  The dialog opens from a red button; the second click of a double-click on
  that button lands on whatever the new dialog put under the pointer. The
  delay runs from the FIRST ``<Map>`` only (``arm()`` is idempotent) — a later
  re-map must not re-open or extend the window, and a control that never
  mapped is never armed at all.
* **The handle follows the pointer with the grab offset preserved**, clamped
  to the track. Without the offset the handle would jump by up to its own
  width at the press, which is free progress nobody dragged.
* **Confirm fires on RELEASE, with progress >= 0.9.** Never on motion: the
  user can drag all the way, think better of it, drag back and let go.
  Progress is the LAST DRAG position — the release event's own coordinates are
  deliberately not consulted, so a release carrying a stale or odd position
  cannot push a short drag over the line.
* **Everything else resets to 0**: a release short of the threshold, a press
  while already pressed (a release was lost — the gesture is no longer one
  continuous drag), a focus loss mid-drag (alt-tab, a toast, another dialog).
  A half-dragged handle is never left "primed" for a later nudge to finish.
* **One-shot.** Confirm LOCKS the model; no later press, drag, release or
  cancel changes it, so ``on_confirm`` runs at most once per control.
  ``on_confirm`` is the LAST statement of the release path because the caller
  is expected to destroy or replace this widget from inside it.
* **No keyboard path.** ``takefocus=0``, no key bindings, nothing here binds
  ``<Return>`` or ``<space>`` — a destructive default action reachable from
  the keyboard is the exact accident this control exists to remove. (The
  dialog keeps Escape = cancel; that is its business, and it is the safe
  direction.)
* **No timers, no animation, no sleeping.** Time enters only through the
  injected ``clock``; the shell never calls ``after``. A snap-back animation
  would need a timer that outlives a destroyed dialog, and would make "is the
  handle home?" a question with a time-dependent answer.
* **Bindings live on the canvas only.** An application grab (the dialog is a
  DECISION-class ``make_modal`` window) redirects presses OUTSIDE the grab
  tree to the grabbing toplevel; a pointer binding on the toplevel would turn
  a click on another FCTool window into a press here. The one toplevel binding
  is ``<FocusOut>`` → reset, added with ``add="+"`` (a plain ``bind``
  REPLACES every handler already on the sequence — ``docs/agents/map/facts.md``)
  and removed again, funcid-scoped, when the canvas is destroyed.
* **Fixed size, derived from the font.** The canvas requests exactly the
  track's size and must be packed WITHOUT ``fill`` — a stretched canvas would
  leave dead track beyond the model's travel. Every dimension comes from the
  font's line height and ``measure`` (a positive font size is POINTS, so it
  follows ``tk scaling``), and the minimum travel scales with ``tk scaling``
  too: a short track makes the gesture a twitch, and a twitch is an accident.
"""
from __future__ import annotations

import math
import time
from typing import NamedTuple

# release() verdicts.
CONFIRM = "confirm"
RESET = "reset"
IGNORED = "ignored"

#: Fraction of the travel a release must have reached to confirm.
THRESHOLD = 0.9
#: Seconds after the first map during which presses are ignored.
ARM_DELAY_S = 0.4

#: Minimum handle travel, in px AT ``REF_SCALING`` — scaled proportionally
#: with the live ``tk scaling`` (spec §5: >= 160 px at scaling 1.33).
MIN_TRAVEL_PX = 160
#: The nominal 96-dpi ``tk scaling`` the px figures above were chosen at.
REF_SCALING = 1.33


class SlideModel:
    """The gesture state machine — pure: no Tk, and no time but ``clock()``.

    Coordinates are track-relative px along the slide axis: the track spans
    ``[0, track_px)`` and the handle occupies ``[handle_x, handle_x +
    handle_px)``, with ``handle_x`` in ``[0, travel]`` where ``travel =
    track_px - handle_px``.

    ``arm()`` starts the arm delay (first call wins); until ``arm_delay_s`` has
    passed on ``clock``, every press is refused. A model that is never armed
    never accepts a press.
    """

    def __init__(self, track_px, handle_px, *, threshold=THRESHOLD,
                 arm_delay_s=ARM_DELAY_S, clock=time.monotonic):
        if not handle_px > 0 or not track_px > handle_px:
            raise ValueError(
                f"slide needs 0 < handle_px < track_px "
                f"(got handle_px={handle_px!r}, track_px={track_px!r})")
        self._track_px = track_px
        self._handle_px = handle_px
        self._travel = track_px - handle_px
        self._threshold = threshold
        self._arm_delay_s = arm_delay_s
        self._clock = clock
        self._armed_at = None       # clock() at the first arm(); None = never
        self._handle_x = 0
        self._grab_dx = 0           # pointer x minus handle_x at the press
        self._pressed = False
        self._locked = False

    # ── read-only state ──────────────────────────────────────────────────────
    @property
    def track_px(self):
        return self._track_px

    @property
    def handle_px(self):
        return self._handle_px

    @property
    def travel(self):
        """How far the handle can move: ``track_px - handle_px``."""
        return self._travel

    @property
    def handle_x(self):
        """The handle's left edge, track-relative, in ``[0, travel]``."""
        return self._handle_x

    @property
    def progress(self) -> float:
        """``handle_x / travel`` — 0.0 at home, 1.0 at the far end."""
        return self._handle_x / self._travel

    @property
    def pressed(self) -> bool:
        return self._pressed

    @property
    def locked(self) -> bool:
        """True once a release has confirmed. Final: nothing unlocks it."""
        return self._locked

    # ── gesture ──────────────────────────────────────────────────────────────
    def arm(self) -> None:
        """Start the arm delay. Idempotent — the FIRST call wins, so a re-map
        can neither re-open the ignore window nor push it later."""
        if self._armed_at is None:
            self._armed_at = self._clock()

    def press(self, x) -> bool:
        """A button press at track-relative ``x``. True iff a drag started.

        Refused (False) when locked, when not armed or still inside the arm
        delay, or when ``x`` is not on the handle. A press while ALREADY
        pressed is refused too and resets the handle: the previous gesture
        lost its release, so it is not one continuous drag any more.
        """
        if self._locked:
            return False
        if self._pressed:
            self._home()
            return False
        if (self._armed_at is None
                or self._clock() - self._armed_at < self._arm_delay_s):
            return False
        if not self._handle_x <= x < self._handle_x + self._handle_px:
            return False
        self._pressed = True
        self._grab_dx = x - self._handle_x
        return True

    def drag(self, x) -> float:
        """Pointer motion to track-relative ``x``; returns ``progress``.

        The handle keeps its grab offset under the pointer and is clamped to
        the track. A no-op unless a press is being held."""
        if self._pressed and not self._locked:
            self._handle_x = min(max(x - self._grab_dx, 0), self._travel)
        return self.progress

    def release(self) -> str:
        """The button came up. ``"confirm"`` (and the model LOCKS) when a held
        drag reached the threshold; ``"reset"`` (handle back to 0) when it did
        not; ``"ignored"`` when no press was being held or the model is
        already locked — so a second release can never confirm twice."""
        if self._locked or not self._pressed:
            return IGNORED
        if self.progress >= self._threshold:
            self._pressed = False
            self._locked = True
            return CONFIRM
        self._home()
        return RESET

    def cancel(self) -> None:
        """Abandon any drag in progress and send the handle home (focus loss,
        an off-handle press mid-drag). A no-op once locked."""
        if not self._locked:
            self._home()

    def _home(self) -> None:
        self._pressed = False
        self._handle_x = 0
        self._grab_dx = 0


class SlideGeometry(NamedTuple):
    """Px layout of the control. The track IS the canvas (no inset), so
    canvas coordinates are model coordinates."""
    track_w: int        # canvas width  == SlideModel.track_px
    track_h: int        # canvas height == handle height (full track height)
    handle_w: int       # == SlideModel.handle_px
    label_x: int        # left edge of the label — right of the home handle

    @property
    def travel(self) -> int:
        return self.track_w - self.handle_w


def min_travel_px(scaling) -> int:
    """``MIN_TRAVEL_PX`` scaled from ``REF_SCALING`` to ``scaling``.

    An unreadable, non-finite or non-positive scaling falls back to the
    reference — sizing must never raise inside a dialog's builder."""
    try:
        s = float(scaling)
    except (TypeError, ValueError):
        s = REF_SCALING
    if not math.isfinite(s) or s <= 0:
        s = REF_SCALING
    return math.ceil(MIN_TRAVEL_PX * (s / REF_SCALING))


def slide_geometry(line_px, label_px, glyph_px, scaling) -> SlideGeometry:
    """Size the control from font measurements (all px).

    ``line_px``   the font's line height (``metrics("linespace")``)
    ``label_px``  ``font.measure`` of the label text
    ``glyph_px``  ``font.measure`` of the handle glyph
    ``scaling``   the live ``tk scaling``

    The handle is as tall as the track and at least twice as wide as it is
    tall (a comfortable grab target). The travel is the larger of the scaled
    minimum and the room the label needs with a gap on both sides, which is
    what guarantees the label — drawn from ``label_x`` — clears the home
    handle and fits inside the track.
    """
    line = max(1, int(line_px))
    pad = max(4, line // 2)
    track_h = line + 2 * pad
    handle_w = max(2 * track_h, int(glyph_px) + 2 * pad)
    gap = 2 * pad
    travel = max(min_travel_px(scaling), int(label_px) + 2 * gap)
    return SlideGeometry(track_w=handle_w + travel, track_h=track_h,
                         handle_w=handle_w, label_x=handle_w + gap)


# ══════════════════════════════════════════════════════════════════════════════
# Tk shell — the canvas. Everything above this line is pure.
# ══════════════════════════════════════════════════════════════════════════════
import tkinter as tk                                              # noqa: E402
import tkinter.font as tkfont                                     # noqa: E402

# House palette, single source (ui_theme imports nothing and never drifts).
from ui_theme import BG_PANEL, FG_RED, FG_TEXT, FG_WHITE          # noqa: E402

# The house DESTRUCTIVE reds. ui_theme carries no button reds — these mirror
# fc_gui's ``Red.TButton`` style (background / active background), which this
# module cannot import; keep the two in step if that style ever changes.
RED_BUTTON = "#660022"
RED_BUTTON_ACTIVE = "#882233"

#: Drawn on the handle. U+00BB is in cp1252, and it is widget text only —
#: never logged (map/facts.md, the cp1252 console note).
HANDLE_GLYPH = "»"

_FONT_FAMILY = "Consolas"
_FONT_POINTS = 10       # POSITIVE = points: follows ``tk scaling`` by design


class SlideToConfirm(tk.Canvas):
    """The slide control: a fixed-size canvas whose whole area is the track.

    ``parent``      the container; pack/grid WITHOUT ``fill`` or ``sticky``
                    stretch — the requested size is the control's size.
    ``text``        the label, drawn right of the home handle (e.g. "slide to
                    close 5 clients"); it sizes the track if it is long.
    ``on_confirm``  zero-argument callable; called AT MOST ONCE, as the last
                    statement of a confirming release. It may destroy this
                    widget or its whole dialog.
    ``clock``       monotonic-seconds callable for the arm delay; defaults to
                    ``time.monotonic``. Tests inject a fake and never sleep.

    Test seams (an API, not a tidy-up target): ``model`` and the handlers
    ``_on_map`` / ``_on_press`` / ``_on_drag`` / ``_on_release`` /
    ``_on_focus_out``, each taking an event that needs only ``.x`` / ``.y``;
    ``handle_rect()``; ``geo``; the item ids ``_track_id``, ``_trail_id``,
    ``_label_id``, ``_handle_id``, ``_glyph_id``. Handlers are driven directly
    in tests: ``<Map>`` never fires under a withdrawn root and
    ``event_generate`` reaches no unmapped widget (map/facts.md).
    """

    def __init__(self, parent, text, on_confirm, *, clock=None):
        # A Font belongs to its interpreter and is deleted when the Python
        # object is collected — built per widget and kept on self.
        self._font = tkfont.Font(root=parent, family=_FONT_FAMILY,
                                 size=_FONT_POINTS, weight="bold")
        try:
            scaling = parent.tk.call("tk", "scaling")
        except tk.TclError:
            scaling = REF_SCALING
        geo = slide_geometry(self._font.metrics("linespace"),
                             self._font.measure(text),
                             self._font.measure(HANDLE_GLYPH), scaling)
        super().__init__(parent, width=geo.track_w, height=geo.track_h,
                         highlightthickness=0, borderwidth=0, bg=BG_PANEL,
                         takefocus=0)
        self.geo = geo
        self.model = SlideModel(geo.track_w, geo.handle_w,
                                clock=clock or time.monotonic)
        self._confirm_cb = on_confirm

        w, h = geo.track_w, geo.track_h
        # Stacking order = creation order: the handle rides OVER the label.
        self._track_id = self.create_rectangle(
            0, 0, w - 1, h - 1, fill=BG_PANEL, outline=RED_BUTTON)
        self._trail_id = self.create_rectangle(
            0, 0, 0, h, fill=RED_BUTTON, width=0)
        self._label_id = self.create_text(
            geo.label_x, h // 2, anchor="w", text=text, font=self._font,
            fill=FG_TEXT)
        self._handle_id = self.create_rectangle(
            0, 0, geo.handle_w, h, fill=RED_BUTTON_ACTIVE, width=0)
        self._glyph_id = self.create_text(
            geo.handle_w // 2, h // 2, anchor="center", text=HANDLE_GLYPH,
            font=self._font, fill=FG_WHITE)

        # Pointer bindings on the CANVAS only (module docstring: the grab).
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Map>", self._on_map)
        # Focus loss anywhere in the owning toplevel abandons a drag.
        # <FocusOut> propagates to the toplevel from whichever descendant
        # held the focus, so this is the one place that sees all of them.
        # add="+": never clobber the dialog's own handler. The funcid is kept
        # so destroy() removes exactly this handler and nothing else.
        self._focus_top = self.winfo_toplevel()
        self._focus_funcid = self._focus_top.bind(
            "<FocusOut>", self._on_focus_out, add="+")

    # ── geometry seam ────────────────────────────────────────────────────────
    def handle_rect(self):
        """The model's hit rectangle in canvas px ``(x0, y0, x1, y1)``,
        half-open on the right/bottom — and exactly where the handle is
        drawn: ``_redraw`` places the handle item at these coordinates."""
        x0 = self.model.handle_x
        return (x0, 0, x0 + self.model.handle_px, self.geo.track_h)

    # ── handlers ─────────────────────────────────────────────────────────────
    def _on_map(self, _event=None):
        self.model.arm()

    def _on_press(self, event):
        if 0 <= event.y < self.geo.track_h:
            self.model.press(event.x)
        else:
            # Not on the handle's rows. Still a press: if one is already
            # held, the gesture is broken — same rule as the model's.
            self.model.cancel()
        self._redraw()

    def _on_drag(self, event):
        self.model.drag(event.x)
        self._redraw()

    def _on_release(self, _event=None):
        verdict = self.model.release()
        self._redraw()
        if verdict == CONFIRM:
            # At most once: the model is locked and can never answer CONFIRM
            # again. LAST statement — the callee may destroy this widget.
            self._confirm_cb()

    def _on_focus_out(self, _event=None):
        """Bound on the owning TOPLEVEL, so it can outlive the canvas if the
        canvas is destroyed below Python's ``destroy()``; harmless then — the
        model reset is pure and ``_redraw`` swallows the dead widget."""
        self.model.cancel()
        self._redraw()

    def _redraw(self):
        try:
            x0, y0, x1, y1 = self.handle_rect()
            self.coords(self._handle_id, x0, y0, x1, y1)
            self.coords(self._trail_id, 0, y0, x0, y1)
            self.coords(self._glyph_id, (x0 + x1) // 2, y1 // 2)
            self.itemconfigure(
                self._handle_id,
                fill=FG_RED if self.model.pressed else RED_BUTTON_ACTIVE)
        except tk.TclError:
            pass        # destroyed canvas: nothing left to draw on

    def destroy(self):
        """Take this control's ``<FocusOut>`` handler off the toplevel (it is
        longer-lived when a dialog swaps the slide for a status line), then
        destroy. Funcid-scoped, so the toplevel's other handlers survive."""
        top, funcid = self._focus_top, self._focus_funcid
        self._focus_top = self._focus_funcid = None
        if top is not None and funcid:
            try:
                top.unbind("<FocusOut>", funcid)
            except tk.TclError:
                pass
        super().destroy()
