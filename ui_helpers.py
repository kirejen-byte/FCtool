# ui_helpers.py
"""Shared Tk dialog + tooltip helpers — the house widget-behaviour contract.

Containment-safe leaf module: it imports ONLY the standard library (``tkinter``)
and the equally-leaf :mod:`ui_theme` palette. It MUST never import ``fc_gui`` or
any feature module — that is what lets ``fc_gui`` and every standalone window
module (fleet templates, infra manager, overview manager/editor, markup editor,
...) share ONE modal + tooltip implementation without the copy-paste drift that
previously shipped ~11 subtly-different dialog setups and 5 divergent tooltips in
one app (see OPTIMIZATION_REVIEW.md findings D2, D5, D6, D7, D9).

Two helpers:

``make_modal(win, parent, *, on_cancel=None, base_bg=None, grab=True)``
    The house modal-dialog contract, wired once so every dialog behaves the same:
      * D6 — ``transient(parent)`` and ``grab_set()`` are each guarded against
        ``TclError`` (a withdrawn/unmapped parent, or an unviewable window during
        headless tests, must degrade quietly rather than crash the opener).
      * ``grab=False`` opts out of the GRAB ONLY (transient, ``<Escape>`` and
        the themed background still apply) — for the long-lived REFERENCE
        window class, which must not freeze the app's other Toplevels. See the
        function docstring for why that class exists.
      * D2 — ``<Escape>`` is bound to ``on_cancel`` (or ``win.destroy`` when no
        cancel handler is given), so muscle-memory dismissal works on EVERY
        modal, not just the ~14 that happened to bind it by hand. Callers pass
        the dialog's real close handler (its Cancel button command / WM_DELETE
        protocol handler) so Escape follows the SAME path — never a blind destroy
        that skips cleanup.
      * D5 — the window base colour is set once from the shared palette
        (``base_bg`` or the canonical ``ui_theme.BG_DARK``), retiring the
        BG_DARK-vs-BG_PANEL split across dialogs.

``attach_tooltip(widget, text, *, topmost=False)``
    The single hover-tooltip implementation (D9). Promoted verbatim-in-spirit
    from ``overview_manager_ui._attach_tooltip`` — the best-in-repo version, the
    only one that bound ``<Destroy>`` and so did not leak an orphaned Toplevel
    when a widget was destroyed mid-hover (the v3.5.2 tooltip-leak class). Themed
    from ``ui_theme`` (dark panel bg, light text — never the stray light-yellow
    ``#ffffe0`` that one bespoke copy rendered). The copy is also stashed on the
    widget as ``_tooltip_text`` so tests can assert it without simulating a hover.
    ``topmost=True`` pins the tip's own ``-topmost`` attribute so it stacks
    above an owner that is itself ``HWND_TOPMOST`` (the FC HUD tiles) — a plain
    tip has no z-order relationship to a topmost owner and is created BELOW it
    at the pointer position, i.e. never actually seen (owner-reported
    2026-08-02: hovering a HUD tile's fleet rows showed no tooltip). Default
    False: the vast majority of callers attach to widgets inside ordinary
    windows, where an always-topmost tip would float over OTHER applications
    too (the same band hazard documented on ``autocomplete.py`` in
    ``map/facts.md``). A tip shown this way is also REGISTERED for
    ``relift_topmost_tooltips`` (below), because being on top once is not the
    same as staying there.

``relift_topmost_tooltips()``
    Re-lift every live ``topmost=True`` tip. Called by whoever re-asserts
    HWND_TOPMOST on OTHER windows in the same band — see its docstring.

``hide_tooltip(widget)``
    Tear down ``widget``'s currently-shown tip explicitly, for the gestures
    where ``<Leave>`` cannot fire — see its docstring.

``update_tooltip(widget, text)``
    Change the copy of an ALREADY-attached tooltip. It exists because
    ``attach_tooltip``'s binds use ``add="+"``: calling it again on the same
    widget stacks a second set of ``<Enter>``/``<Leave>``/``<Destroy>``
    handlers, so a panel that re-attached on every repaint would accumulate
    binds for the life of the app. ``_show`` therefore reads
    ``widget._tooltip_text`` LIVE at hover time rather than closing over the
    string it was attached with, and this helper is simply the supported way to
    re-stash it — bind ONCE, re-stash as often as the data changes.
"""
from __future__ import annotations

import tkinter as tk

import ui_theme

# Tooltip type is deliberately small/monospace to match the app's Consolas UI.
_TOOLTIP_FONT = ("Consolas", 8)

# Live tips shown with topmost=True — the ONLY tips that have a z-order
# relationship worth re-asserting. Populated by attach_tooltip's ``_show``,
# discarded by its ``_hide`` (which both <Leave> and the widget's <Destroy>
# call) and pruned by relift_topmost_tooltips(). At most one entry per hovered
# widget in practice, and empty whenever nothing is hovered.
_TOPMOST_TIPS = set()


def relift_topmost_tooltips():
    """Re-lift every live ``topmost=True`` tooltip. Never raises.

    **Why this exists.** ``-topmost`` puts a window in the always-on-top BAND;
    it does not pin a position INSIDE that band. ``attach_tooltip``'s ``_show``
    lifts the tip once, which is correct at that instant — but the FCPreview
    tick re-asserts every tile's topmost state ~4x/second with
    ``SetWindowPos(hwnd, HWND_TOPMOST, ...)`` and **no SWP_NOZORDER**, so each
    tile is moved to the TOP of the band. One tick after the implant tooltip
    appears, every overlapping tile is above it and the tip is gone from view
    (owner-reported: "the tooltip shows for a moment, then hides behind the
    other previews"). The same reorder happens in ``_preview_switch_to``'s
    immediate post-activation re-assert and in ``preview_tile``'s hover
    zoom/restore.

    The cure is ordering, not a new mechanism: whoever re-tops a batch of
    windows calls this afterwards, so the tip ends the batch on top again.
    ``lift()`` is HWND_TOP, which for a window already in the topmost band
    means the top of THAT band — exactly the call ``_show`` already relies on,
    just repeated.

    Cheap by contract: it runs on a ~250 ms tick, so the no-tooltip case (the
    overwhelming majority) is one empty-set test and no Tk call at all. A tip
    torn down out of band (destroyed parent, dead interpreter) is pruned here
    rather than raising into the tick.
    """
    if not _TOPMOST_TIPS:
        return
    for tip in tuple(_TOPMOST_TIPS):
        try:
            if not tip.winfo_exists():
                _TOPMOST_TIPS.discard(tip)
                continue
            tip.lift()
        except Exception:
            # A dead tip (TclError) or anything else out of a Tk call: drop it.
            # Nothing here may escape into a caller's tick loop.
            _TOPMOST_TIPS.discard(tip)


def make_modal(win, parent, *, on_cancel=None, base_bg=None, grab=True):
    """Apply the house modal contract to ``win`` and return it.

    ``win``        the dialog Toplevel (already created + titled by the caller).
    ``parent``     the owning window; used as the transient master.
    ``on_cancel``  called on ``<Escape>``; defaults to ``win.destroy``. Pass the
                   dialog's own Cancel/close handler so Escape and the Cancel
                   button share one code path (cleanup, grab_release, etc.).
    ``base_bg``    the window background; defaults to ``ui_theme.BG_DARK``.
    ``grab``       take the application-wide input grab. **Default True** — the
                   house behaviour for a DECISION dialog, one the user must
                   answer before the app can sensibly continue. Pass ``False``
                   for a long-lived REFERENCE window: one the user is expected
                   to READ while carrying on working elsewhere in the app.
                   ``grab=False`` skips ONLY ``grab_set()``; transient,
                   ``<Escape>`` → cancel and the themed background all still
                   apply, so the window is otherwise a house dialog.

    **Why the grab opt-out exists (a whole CLASS of window, not one instance).**
    ``grab_set()`` is an application-wide Tk input grab: while it is held, every
    OTHER Toplevel this process owns stops receiving pointer and keyboard
    events. FCTool is not a one-window app — alongside its dialogs it owns the
    FCPreview client tiles, the star map, the overlay and the toasts, and the
    tiles in particular are how the FC switches EVE clients mid-fight (a plain
    ``<Button-1>`` binding on each tile's own Toplevel). So a grabbing window
    does not merely block input to itself; it silently deafens the FC's client
    switcher for as long as the window is open, and because the DWM thumbnails
    keep compositing at the OS level the tiles go on animating while dead to
    clicks — the symptom reads as "the previews froze", never as "that dialog
    did it". v4.1.0 shipped exactly this regression by adding a grab to the
    market gaps window. Any window the FC is meant to read WHILE FLYING belongs
    in the ``grab=False`` class; anything that must be answered first keeps the
    default. A non-grabbing window can be opened twice, so its opener owns a
    single-window guard — the grab was providing that implicitly.

    ``transient``/``grab_set`` are each guarded against ``TclError`` so an
    unviewable window or withdrawn parent degrades quietly (D6).
    """
    try:
        win.transient(parent)
    except tk.TclError:
        pass
    if grab:
        try:
            win.grab_set()
        except tk.TclError:
            pass

    cancel = on_cancel if callable(on_cancel) else win.destroy
    win.bind("<Escape>", lambda _e=None: cancel())

    win.configure(bg=base_bg or ui_theme.BG_DARK)
    return win


def attach_tooltip(widget, text, *, topmost=False, place_above=False,
                    place_fn=None):
    """Attach a simple hover tooltip to ``widget`` (D9 shared helper) and return
    ``widget``.

    A borderless, dark-themed Toplevel is shown on ``<Enter>`` and destroyed on
    ``<Leave>``. It is ALSO destroyed on the widget's own ``<Destroy>`` so a
    widget torn down while the pointer is over it never orphans the tip (the
    v3.5.2 leak fix — this is the reason this implementation, not one of the
    other four, was promoted). All three binds use ``add="+"`` so they never
    clobber an existing binding on the widget.

    The tooltip copy is stashed on the widget as ``_tooltip_text`` so it is
    assertable in tests without delivering a synthetic hover event — and
    ``_show`` READS it back from there rather than closing over ``text``, so
    ``update_tooltip`` can change the copy later without re-binding (see the
    module docstring: re-attaching stacks ``add="+"`` handlers).

    ``topmost`` (keyword-only, default False): when True, the shown tip also
    gets its own ``-topmost`` attribute set and is lifted, so it stacks above
    an owner window that is itself always-on-top. Pass this for tooltips
    attached inside a ``HWND_TOPMOST`` window (e.g. the FC HUD tiles) — a tip
    with no topmost handling of its own is stacked BELOW its topmost owner at
    the pointer position: created, but invisible.

    ``place_above`` (keyword-only, default False): when True, the tip is hung
    ABOVE the widget (its bottom edge just above the widget's top) instead of
    the default just-below-the-widget position. Pass this when the space
    directly below the widget is covered by something that would occlude the
    tip — specifically the FCPreview implant icon, which sits in a tile's TOP
    caption strip directly above the DWM-composited live-video body: a
    below-the-widget tip lands over that body and the compositor draws the
    live thumbnail OVER it, so it is never seen even while ``-topmost``
    (``map/preview.md``). Default False keeps every other caller's tip below
    the widget, unchanged.

    ``place_fn`` (keyword-only, default None): an optional callback
    ``place_fn(tip) -> bool`` that positions the already-built tip Toplevel
    itself. It is called with the tip after ``tip.update_idletasks()`` (so a
    requested-geometry read is realised); if it returns a truthy value it owns
    the tip's geometry and the default winfo-based placement (``place_above``
    or the below-the-widget default) is skipped entirely, otherwise the
    default placement runs as if ``place_fn`` were never passed. While the
    callback runs, the tip is still WITHDRAWN (not yet mapped): the preceding
    ``update_idletasks()`` realises ``winfo_reqheight()`` correctly, but
    ``winfo_ismapped()`` reads False inside the callback. Used by the
    FCPreview implant icon: its tile is positioned by external Win32
    ``SetWindowPos`` (physical px) outside Tk's geometry manager, so the
    tile's own ``self._pos`` -- the same authoritative physical position that
    ``body_screen_rect`` and the drag anchors use -- is the robust source: it
    does not depend on Tk having pumped the external move's messages yet
    (measured 2026-08-25: Tk's own ``winfo_rootx``/``winfo_rooty`` in fact
    matched the physical position live here -- an earlier "stale winfo" theory
    was WRONG). The screen-corner bug this callback was added to work around
    was actually ``wm_attributes(-topmost)`` clobbering a still-pending
    ``wm_geometry`` move on a freshly-mapped tip (see ``_show`` below), not a
    stale-position read.
    """
    widget._tooltip_text = text
    state = {"tip": None}

    def _hide(_e=None):
        tip = state.get("tip")
        if tip is not None:
            # Deregister BEFORE destroying: a tip that never reaches
            # relift_topmost_tooltips() alive must not be left for it to prune.
            _TOPMOST_TIPS.discard(tip)
            try:
                tip.destroy()
            except tk.TclError:
                pass
            state["tip"] = None

    # The teardown, reachable by anyone holding the widget — see hide_tooltip.
    widget._tooltip_hide = _hide

    def _show(_e=None):
        _hide()
        # Read the copy LIVE, not from the closure: update_tooltip re-stashes
        # `_tooltip_text` on the widget, and a tooltip whose text is empty has
        # nothing to say — draw no empty box.
        copy = getattr(widget, "_tooltip_text", text)
        if not copy:
            return
        try:
            tip = tk.Toplevel(widget)
            # Withdrawn until fully positioned: update_idletasks on a
            # NON-withdrawn fresh Toplevel MAPS it at (0,0); a wm_geometry call
            # on the now-mapped tip only files a PENDING move (it reads back
            # correctly, but the move hasn't landed); and wm_attributes(
            # -topmost) then fires a SetWindowPos whose WM_WINDOWPOSCHANGED
            # reports the CURRENT (0,0) -- which Tk adopts as authoritative,
            # DISCARDING the pending move (measured 2026-08-25: the geometry
            # request itself read back "+0+0" after the -topmost call). THAT
            # was the tooltip-in-the-screen-corner bug -- the math in every
            # prior placement fix was correct, this clobber wasn't. Deiconifying
            # ONCE at the end (after -topmost is applied) maps the tip directly
            # at its final position, which also removes the old one-frame (0,0)
            # flash.
            tip.wm_withdraw()
            tip.wm_overrideredirect(True)
            tk.Label(tip, text=copy, font=_TOOLTIP_FONT,
                     fg=ui_theme.FG_TEXT, bg=ui_theme.BG_PANEL,
                     borderwidth=1, relief=tk.SOLID, justify=tk.LEFT,
                     wraplength=340, padx=5, pady=3).pack()
            # place_fn (e.g. the FCPreview implant icon) gets first refusal: it
            # positions the tip from a source OTHER than Tk winfo_root* -- the
            # robust choice for a widget whose top-level is moved by external
            # SetWindowPos outside Tk's geometry manager, since it doesn't
            # depend on Tk having pumped that move's messages yet (see its own
            # docstring paragraph above). Only fall through to the winfo-based
            # placement below when there is no callback, or it declines
            # (returns falsy) or raises.
            placed = False
            if place_fn is not None:
                try:
                    tip.update_idletasks()
                    placed = bool(place_fn(tip))
                except Exception:
                    placed = False
            if not placed:
                if place_above:
                    # Hang the tip ABOVE the widget instead of below it. Used for the
                    # FCPreview implant icon: it lives in the tile's TOP caption strip,
                    # directly above the DWM-composited live-video body, and the default
                    # below-the-widget tip lands over that body where the compositor
                    # draws the live thumbnail OVER it — occluded even while -topmost
                    # (map/preview.md). Above the top-strip icon clears the body.
                    #
                    # winfo_REQheight, not winfo_height: the tip is not yet mapped when
                    # we place it, so on an overrideredirect Toplevel winfo_height()
                    # reads 1 and the tip would drop right back over the body. The
                    # requested height is the real content height and is map-independent
                    # once update_idletasks() has realised the geometry request.
                    tip.update_idletasks()
                    x = widget.winfo_rootx() + 12
                    above_y = widget.winfo_rooty() - tip.winfo_reqheight() - 4
                    if above_y >= 0:
                        y = above_y
                    else:
                        # No room above -- the tile is anchored near the screen TOP
                        # (FCPreview login tiles default to login_position [5,5]).
                        # A negative above_y would place the tip partly OFF-SCREEN
                        # above the display (Tk parses "+x+-N" geometry fine -- a
                        # prior theory that this was a geometry-PARSING bug was
                        # WRONG, measured 2026-08-25). Fall back to BELOW THE WHOLE
                        # TILE instead: the DWM video body sits ABOVE the tile's
                        # bottom edge, so a below-the-tile tip still clears it, and a
                        # top-anchored tile's bottom edge is comfortably on-screen.
                        # This can never be negative. (The actual reported
                        # screen-corner bug was the -topmost pending-move clobber
                        # documented where the tip is created above, not this
                        # placement math.)
                        top = widget.winfo_toplevel()
                        y = top.winfo_rooty() + top.winfo_height() + 4
                    tip.wm_geometry(f"+{x}+{y}")
                else:
                    tip.wm_geometry(
                        f"+{widget.winfo_rootx() + 12}"
                        f"+{widget.winfo_rooty() + widget.winfo_height() + 4}")
            if topmost:
                try:
                    tip.wm_attributes("-topmost", True)
                except tk.TclError:
                    pass
            tip.wm_deiconify()
            if topmost:
                try:
                    tip.lift()
                except tk.TclError:
                    pass
            state["tip"] = tip
            if topmost:
                # Register for relift_topmost_tooltips(): the lift above is
                # correct only until the next batch of HWND_TOPMOST re-asserts
                # on the tiles around it moves them back over the tip.
                _TOPMOST_TIPS.add(tip)
        except tk.TclError:
            state["tip"] = None

    widget.bind("<Enter>", _show, add="+")
    widget.bind("<Leave>", _hide, add="+")
    widget.bind("<Destroy>", _hide, add="+")
    return widget


def update_tooltip(widget, text):
    """Re-stash an already-attached tooltip's copy and return ``widget``.

    The supported way to give a repainting widget a tooltip whose text follows
    its data. **Never call ``attach_tooltip`` again for that** — its three binds
    use ``add="+"``, so re-attaching stacks a fresh ``<Enter>``/``<Leave>``/
    ``<Destroy>`` handler set on every repaint and leaks one per call for the
    life of the widget. This only writes the attribute ``_show`` reads.

    Safe on a widget that never had a tooltip attached (it just stashes the
    string, which no handler will read) — so a caller need not branch.
    """
    widget._tooltip_text = text
    return widget


def hide_tooltip(widget):
    """Hide ``widget``'s tip now, if one is up. Never raises; returns ``widget``.

    **Why an explicit teardown exists.** ``attach_tooltip`` hides on ``<Leave>``,
    which covers every ordinary hover — but a press that becomes a DRAG takes
    the implicit pointer grab, and a grabbed pointer generates no ``<Leave>``
    for the widget it started on. So a drag begun on a tooltipped widget leaves
    the tip up for the whole gesture. In FCPreview that widget is the implant
    icon, whose Label carries the tile's move/resize bindings: the tip would sit
    at the tile's PRESS-time position (stale the moment the tile moves) while
    every motion event re-asserts HWND_TOPMOST on the tile over it and the
    ~4 Hz tick relift pulls it back — a strobe. The gesture owner calls this
    once the drag is real instead.

    Idempotent and unconditional by design: callers fire it from a motion
    handler, so a second call, a widget whose tip was never shown, and a widget
    that never had a tooltip attached at all are all silent no-ops. Nothing
    (a dead Tk interpreter included) escapes into the drag in progress.
    """
    hide = getattr(widget, "_tooltip_hide", None)
    if hide is not None:
        try:
            hide()
        except Exception:
            pass
    return widget
