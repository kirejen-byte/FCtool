# ui_helpers.py
"""Shared Tk dialog + tooltip helpers — the house widget-behaviour contract.

Containment-safe leaf module: it imports ONLY the standard library (``tkinter``)
and the equally-leaf :mod:`ui_theme` palette. It MUST never import ``fc_gui`` or
any feature module — that is what lets ``fc_gui`` and every standalone window
module (fleet templates, infra manager, overview manager/editor, markup editor,
...) share ONE modal + tooltip implementation without the copy-paste drift that
previously shipped ~11 subtly-different dialog setups and 5 divergent tooltips in
one app (see OPTIMIZATION_REVIEW.md findings D2, D5, D6, D7, D9).

The helpers:

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

``center_over(win, parent, *, margin=8, size=None, monitor_rect_fn=None)``
    Centre a dialog over the window that opened it, clamped inside the monitor
    the parent lives on. Windows places a fresh ``Toplevel`` wherever it likes
    (the owner saw every dialog land in the top-right corner of a secondary
    monitor), so the house contract now says: a dialog belongs over the tool.
    ``make_modal`` calls it for every caller; hand-built dialogs that do not go
    through ``make_modal`` call it themselves. Never raises — a window it cannot
    measure is simply left where Tk put it.

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

import logging
import tkinter as tk

import ui_theme

log = logging.getLogger(__name__)

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


def _parse_geometry(spec):
    """Parse a Tk ``"WxH+X+Y"`` geometry string into ``(w, h, x, y)``.

    ONLY the full ``WxH+X+Y`` form is understood. Tk also emits size-only
    (``"600x400"``) and position-only (``"+300+250"``) forms; both return None
    here, deliberately — a caller that got one of those learned nothing about
    the other half and must not pretend otherwise. Negative offsets in either
    Tk spelling (``+-12`` and ``-12``) ARE handled. Never raises.
    """
    try:
        text = str(spec)
        size, sep, pos = text.partition("+")
        if not sep:
            return None
        w_s, _x, h_s = size.partition("x")
        # pos is "X+Y" or "X+-Y" / "-X+-Y" — split on the separators Tk emits.
        parts, cur = [], ""
        for ch in pos:
            if ch in "+-" and cur not in ("", "-"):
                parts.append(cur)
                cur = "" if ch == "+" else "-"
            elif ch == "-" and cur == "":
                cur = "-"
            elif ch != "+":
                cur += ch
        if cur not in ("", "-"):
            parts.append(cur)
        if len(parts) != 2:
            return None
        return (int(w_s), int(h_s), int(parts[0]), int(parts[1]))
    except Exception:
        return None


def _parent_rect(parent):
    """``(x, y, w, h)`` on-screen rect of ``parent``, or None.

    ``winfo_geometry()`` FIRST, deliberately. On Windows a Tk geometry string
    carries the FRAME origin with the CLIENT size (measured: a root at
    ``600x400+150+120`` reports ``winfo_rootx() == 158``, ``rooty() == 151`` —
    the 8 px resize border and 31 px title bar), and ``wm_geometry("+x+y")``
    sets the FRAME origin too. Centring frame-origin against frame-origin makes
    those two offsets cancel EXACTLY, so the dialog's client area lands dead
    centre over the parent's; measuring the parent by ``winfo_rootx/rooty``
    instead leaves the dialog 8 px right and 31 px low. ``winfo_root*`` is the
    fallback for a parent whose geometry string is not yet realised (an
    unmapped window reports ``1x1+0+0``), where the small skew beats no
    placement at all.
    """
    try:
        parsed = _parse_geometry(parent.winfo_geometry())
    except Exception:
        parsed = None
    if parsed is not None:
        gw, gh, gx, gy = parsed
        if gw > 1 and gh > 1:
            return (gx, gy, gw, gh)
    try:
        w, h = int(parent.winfo_width()), int(parent.winfo_height())
        x, y = int(parent.winfo_rootx()), int(parent.winfo_rooty())
    except Exception:
        return None
    if w > 1 and h > 1:
        return (x, y, w, h)
    return None


def _window_size(win):
    """``(w, h)`` of the dialog, or None when nothing sane can be measured.

    ``winfo_width``/``winfo_height`` first: after ``update_idletasks()`` they
    carry an explicit ``geometry("WxH")`` request (measured — a withdrawn but
    laid-out Toplevel reports the requested 420x460, not 1x1), which the
    REQUESTED size would miss entirely for a dialog whose content is smaller
    than the window it asked for. Requested size is the fallback for a dialog
    that never got an explicit size.
    """
    for getters in (("winfo_width", "winfo_height"),
                    ("winfo_reqwidth", "winfo_reqheight")):
        try:
            w = int(getattr(win, getters[0])())
            h = int(getattr(win, getters[1])())
        except Exception:
            return None
        if w > 1 and h > 1:
            return (w, h)
    return None


def _monitor_work_rect(x, y):
    """Work-area EDGES ``(l, t, r, b)`` of the monitor containing ``(x, y)``.

    Delegates to ``monitor_pin`` — the repo's ONE monitor enumerator
    (EnumDisplayMonitors + GetMonitorInfoW, physical pixels, EDGES rects, and
    already injectable/testable). Imported LAZILY so this module keeps its
    stdlib-only import surface and still works when the enumeration is
    unavailable (non-Windows, or a ctypes failure): the caller then falls back
    to Tk's own virtual-desktop bounds.

    Falls back to the NEAREST monitor when the point is in a gap between
    displays (MonitorFromPoint/NEAREST semantics). Returns None if nothing can
    be enumerated.
    """
    try:
        import monitor_pin
        monitors = monitor_pin.list_monitors()
    except Exception:
        return None
    if not monitors:
        return None
    for mon in monitors:
        l, t, r, b = mon.rect
        if l <= x < r and t <= y < b:
            return tuple(mon.work)
    # No monitor contains the point (a gap, or an off-screen parent): nearest
    # by squared distance from the point to each monitor's clamped rect.
    def _dist(mon):
        l, t, r, b = mon.rect
        dx = max(l - x, 0, x - (r - 1))
        dy = max(t - y, 0, y - (b - 1))
        return dx * dx + dy * dy
    try:
        return tuple(min(monitors, key=_dist).work)
    except Exception:
        return None


def _virtual_bounds(win):
    """Tk's own virtual-desktop EDGES ``(l, t, r, b)``, or None.

    ``winfo_vroot*`` spans EVERY monitor; ``winfo_screenwidth/height`` report
    the PRIMARY monitor only and would yank a dialog back onto screen 1 when
    the app lives on screen 2 (measured: 3840x1080 @ x=-1920 vs 1920x1080).
    """
    try:
        l = int(win.winfo_vrootx())
        t = int(win.winfo_vrooty())
        r = l + int(win.winfo_vrootwidth())
        b = t + int(win.winfo_vrootheight())
    except Exception:
        return None
    return (l, t, r, b) if r > l and b > t else None


def _clamp_into(v, lo, hi):
    """Clamp ``v`` into ``[lo, hi]``; pin to ``lo`` when the span is inverted
    (the dialog is bigger than the monitor in that axis — keep its TOP-LEFT
    visible, which is where the title bar and the close button live)."""
    if hi < lo:
        return lo
    return max(lo, min(v, hi))


def _begin_hidden(win):
    """Make ``win`` invisible for the measure-and-place window, WITHOUT costing
    it the keyboard focus. Returns a ``restore()`` callable, or None when
    nothing was hidden (and so nothing must be restored). Never raises.

    **Why a dialog has to be hidden at all.** ``update_idletasks()`` on a fresh
    non-withdrawn Toplevel MAPS it at the window manager's position, so a
    measure-then-move sequence shows the dialog in the screen corner this whole
    change exists to stop using, and then jumps it. Hidden, the geometry
    request lands before anyone sees the window.

    **Why NOT ``wm_withdraw()``.** Measured: withdrawing a never-mapped
    Toplevel and deiconifying it DISCARDS that toplevel's focus record —
    ``focus_lastfor()`` comes back as the toplevel itself instead of the widget
    the builder called ``focus_set()`` on. Five dialogs lost their caret that
    way, including the paste dialog, where it meant Ctrl+V went nowhere.
    ``-alpha 0.0`` hides the window just as completely and leaves the record
    intact (measured: ``focus_lastfor()`` is still the Entry).

    **The flush is load-bearing.** ``wm_attributes("-alpha", ...)`` fires a
    ``SetWindowPos`` whose ``WM_WINDOWPOSCHANGED`` reports the CURRENT position,
    which Tk adopts as authoritative — exactly the clobber ``map/facts.md``
    documents for ``-topmost``. Measured here too: restoring alpha straight
    after ``wm_geometry("+x+y")`` discarded the move and the dialog stayed at
    +0+0. So ``restore()`` flushes the pending move with ``update_idletasks()``
    BEFORE touching alpha — free of the usual flash cost, because the window is
    still invisible while it moves.

    The caller's own alpha is saved and put back, never a hard-coded 1.0 — and
    a window that is ALREADY at alpha 0 is left alone entirely (None, no
    restore). That is the nested-hide case: ``make_modal`` hides a dialog for
    the build and the builder's own closing ``center_over`` re-enters here.
    Without the guard the inner call reads 0.0 as the caller's own value and
    its restore writes 0.0 back — a permanently blank dialog, saved today only
    by the outer restore happening to run last. The outer hide owns the reveal.

    ``wm_withdraw`` survives only as the fallback for a platform/toolkit that
    refuses ``-alpha``, and it is a LOSSY one. The focus record is captured
    BEFORE the withdraw (afterwards it already reads as the toplevel) and
    re-issued after, which measurably repairs a record that had SETTLED. It
    cannot repair a ``focus_set()`` the caller made moments earlier in the same
    builder: Tk does not resolve that until the next event pass, so at this
    instant neither ``focus_lastfor()`` nor ``tk.call("focus")`` can name the
    widget (both answer the toplevel / the root — measured). That is the whole
    reason the alpha path is the primary one rather than a nicety: it never
    disturbs the pending focus in the first place.
    """
    try:
        if win.winfo_ismapped():
            return None          # already on screen: moving it is the whole job
        if win.wm_state() != "normal":
            return None          # deliberately withdrawn/iconified: not ours
    except Exception:
        return None

    unsupported = object()
    try:
        prev_alpha = win.wm_attributes("-alpha")
    except Exception:
        prev_alpha = unsupported
    if prev_alpha is not unsupported:
        try:
            already_hidden = prev_alpha is not None and float(prev_alpha) <= 0.0
        except (TypeError, ValueError):
            already_hidden = False
        if already_hidden:
            # A NESTED hide: the window is invisible already, so there is
            # nothing to hide and — critically — nothing to restore. Hiding
            # again would read that 0.0 as "the caller's own alpha" and the
            # restore would write 0.0 straight back, leaving the dialog
            # permanently blank. Live shape: make_modal hides a dialog for the
            # build, and the builder's own closing center_over re-enters here
            # (market_gap_dialog does exactly that). The OUTER hide owns the
            # reveal; this one declines.
            return None
        try:
            win.wm_attributes("-alpha", 0.0)
        except Exception:
            pass
        else:
            def _restore_alpha():
                try:
                    win.update_idletasks()   # apply the move BEFORE the alpha
                except Exception:            # SetWindowPos can discard it
                    pass
                try:
                    win.wm_attributes(
                        "-alpha", 1.0 if prev_alpha is None else prev_alpha)
                except Exception:
                    pass
            return _restore_alpha

    # Fallback only. Capture the focus record first -- wm_withdraw destroys it.
    try:
        focused = win.focus_lastfor()
    except Exception:
        focused = None
    try:
        win.wm_withdraw()
    except Exception:
        return None

    def _restore_withdrawn():
        try:
            win.wm_deiconify()
        except Exception:
            pass
        try:
            if focused is not None and str(focused) != str(win):
                focused.focus_set()          # best effort; see the docstring
        except Exception:
            pass
    return _restore_withdrawn


def center_over(win, parent, *, margin=8, size=None, monitor_rect_fn=None,
                hide=True):
    """Centre ``win`` over ``parent`` and return the applied ``(x, y)``, or None.

    **Why this exists.** ``make_modal`` set ``transient()`` and a grab but never
    a position, so Windows placed every dialog by its own cascade rule — which
    on the owner's multi-monitor layout parked "Add to doctrine", the tag
    pickers and friends in the TOP-RIGHT corner, nowhere near the main window
    they came from. A dialog belongs over the tool that opened it.

    ``margin``          keep-out inset from the monitor's work-area edges.
    ``size``            optional ``(w, h)`` override; by default the dialog
                        measures itself (see ``_window_size``).
    ``monitor_rect_fn`` optional ``fn(x, y) -> (l, t, r, b)`` work-area EDGES
                        for the monitor containing a point, so a caller (or a
                        test) can supply its own enumeration. Default: the
                        ``monitor_pin`` backend, then Tk's virtual desktop.
    ``hide``            hide the dialog around the measurement (see
                        ``_begin_hidden``). Pass False when the CALLER already
                        hid it and owns the restore — ``make_modal`` does,
                        because it has to hide before the caller builds the
                        content and can only place after.

    **Placement, and the map-order trap.** An unmapped dialog is hidden
    (``-alpha 0.0``, never ``wm_withdraw``, which would cost it the keyboard
    focus — see ``_begin_hidden``) around the measurement, because
    ``update_idletasks()`` on a fresh visible Toplevel MAPS it at the window
    manager's position and the dialog would appear in the wrong place before
    moving. An already-mapped window is simply moved, which files a PENDING
    move — and a following ``wm_attributes("-topmost", ...)`` DISCARDS that
    pending move (measured 2026-08-25; ``-alpha`` does the same, which is why
    the restore flushes first). **A caller that sets ``-topmost`` must do so
    BEFORE centring**, never after.

    Never raises, by contract: it is called from dialog builders and from a
    deferred ``after`` callback, and a window it cannot measure (a destroyed
    dialog, an exotic parent, a headless stub) must leave the caller's dialog
    exactly as it found it. Returns None in every such case.
    """
    try:
        prect = _parent_rect(parent)

        # Hide an unmapped dialog around the measurement (see the docstring)
        # so update_idletasks() cannot map it at the WM's position first.
        restore = _begin_hidden(win) if hide else None
        try:
            try:
                win.update_idletasks()
            except Exception:
                pass
            dsize = size if size else _window_size(win)
            if not dsize:
                return None
            dw, dh = int(dsize[0]), int(dsize[1])
            if dw <= 1 or dh <= 1:
                return None

            if prect is not None:
                px, py, pw, ph = prect
                x = px + (pw - dw) // 2
                y = py + (ph - dh) // 2
                cx, cy = px + pw // 2, py + ph // 2
            else:
                # No usable parent rect (withdrawn/iconified, never laid out):
                # fall back to the monitor the POINTER is on — the display the
                # user is actually looking at.
                try:
                    cx, cy = int(win.winfo_pointerx()), int(win.winfo_pointery())
                except Exception:
                    return None
                x = y = None

            bounds = None
            if monitor_rect_fn is not None:
                try:
                    bounds = monitor_rect_fn(cx, cy)
                except Exception:
                    bounds = None
            if not bounds:
                bounds = _monitor_work_rect(cx, cy)
            if not bounds:
                bounds = _virtual_bounds(win)
            if not bounds:
                return None
            l, t, r, b = (int(v) for v in bounds)

            if x is None:
                # Pointer-monitor fallback: centre of that monitor's work area.
                x = l + ((r - l) - dw) // 2
                y = t + ((b - t) - dh) // 2

            x = _clamp_into(x, l + margin, r - margin - dw)
            y = _clamp_into(y, t + margin, b - margin - dh)
            win.wm_geometry(f"+{int(x)}+{int(y)}")
            return (int(x), int(y))
        finally:
            if restore is not None:
                restore()
    except Exception:
        return None


def _center_when_built(win, parent):
    """``make_modal``'s deferred centring: hide, let the caller build, place,
    show. Never raises.

    **Why it has to be deferred.** ``make_modal`` runs BEFORE the caller packs
    a single widget, so the only size available at that moment is a bare
    Toplevel's 200x200 default — centring on that would be visibly off. And the
    size cannot simply be re-measured a moment later either: a Toplevel's
    REQUESTED size is not recomputed until the packer's idle pass, and inside
    ``after_idle`` the window is still 1x1 and unmapped even after an explicit
    ``update_idletasks()`` (both measured). The first honest measurement is a
    plain ``after(0)`` timer, which runs after the geometry pass.

    **Why it hides first.** By then Tk would already have MAPPED the dialog at
    the window manager's position, so a post-hoc move is a visible jump from
    exactly the screen corner this whole change exists to stop using. Hiding
    the dialog for the length of the caller's build keeps it out of sight until
    it has a position; the ``after(0)`` pass then centres it and reveals it
    ONCE, so it appears directly where it belongs (the ``attach_tooltip``
    pattern, ``map/facts.md``). The hide is ``-alpha 0.0``, NOT
    ``wm_withdraw()`` — withdrawing discards the toplevel's focus record and
    the dialog opens with no caret in its Entry (see ``_begin_hidden``). The
    reveal lives in a ``finally``: a dialog that could not be measured must
    still be shown.

    This function owns the hide, so the deferred pass is told ``hide=False``:
    letting ``center_over`` hide again would read the ALREADY-ZERO alpha as the
    caller's own value and restore the dialog to invisible.

    Only a window on its way to being mapped is hidden. An already-mapped
    window — or any stub whose state cannot be read — is placed once, from the
    same deferred pass, and never moved twice.
    """
    restore = _begin_hidden(win)

    def _place_and_show():
        try:
            center_over(win, parent, hide=restore is None)
        finally:
            if restore is not None:
                restore()

    try:
        win.after(0, _place_and_show)
    except Exception:
        # No scheduler (a headless stub, a dead interpreter): do it now rather
        # than leave a hidden dialog nobody will ever show.
        _place_and_show()


def make_modal(win, parent, *, on_cancel=None, base_bg=None, grab=True,
               center=True):
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
    ``center``     centre the dialog over ``parent`` (``center_over``).
                   **Default True** — the house behaviour: Windows otherwise
                   places a fresh Toplevel by its own cascade rule, which on a
                   multi-monitor layout parked every dialog in a screen corner
                   far from the main window. Pass ``False`` only for a window
                   that positions itself deliberately.
                   **Ordering:** a caller that sets ``wm_attributes("-topmost",
                   True)`` must do so BEFORE ``make_modal`` — on an
                   already-mapped window ``-topmost`` DISCARDS a pending
                   geometry move (measured, ``map/facts.md``) — or skip
                   ``center`` and call ``center_over`` itself afterwards.

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
    if center:
        _center_when_built(win, parent)
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


# ── glyph buttons ───────────────────────────────────────────────────────────
#: The caption-strip action glyph's press model, lifted out of the FC HUD
#: chrome so an ordinary tab header can carry the same control (2026-09-12,
#: the Fleet tab's links reset). ``info_tile`` keeps its OWN copy on purpose:
#: its glyphs also have to disarm the tile's corner-resize arming and its
#: strip-drag anchor, neither of which exists outside a HUD tile, and its
#: handlers return "break" to truncate the toplevel's bindtags. What is
#: SHARED is the look and the gesture — and those are what this reproduces.
GLYPH_FONT = ("Consolas", 9, "bold")


def make_glyph_button(parent, glyph, tooltip, callback, *, palette=None,
                      topmost=False):
    """A flat, one-character text button: a ``tk.Label`` that acts like one.

    Returns the Label, already built but NOT packed — the caller owns layout.

    Why a Label and not a ``ttk.Button``: this is chrome, not a form control.
    A themed button brings its own border, padding and focus ring, none of
    which belongs beside a section heading.

    THE GESTURE is the FC HUD's, kept identical so the two surfaces feel the
    same, and every part of it is load-bearing:

      * hover repaints the glyph ``FG_ACCENT``, so a control with no border
        still announces that it is one;
      * a press LATCHES the button and ``<Leave>`` CANCELS the latch. That is
        the destructive-action guard: Tk's implicit pointer grab delivers the
        release to the widget that took the press, so without it a user who
        pressed, thought better of it and dragged off would still have fired
        the callback. The callbacks this is used for are unconfirmed by
        design (no "are you sure?" dialog), and dragging off IS the cancel;
      * the latch is cleared BEFORE the callback runs, so a raising callback
        cannot leave the button armed;
      * the callback is swallowed and logged. It runs inside a Tk binding: an
        exception escaping here reaches ``report_callback_exception`` and
        costs the user a traceback dialog over his game.

    ``tooltip`` is attached with ``attach_tooltip`` when truthy, and the four
    binds below ALL pass ``add="+"`` — mandatory, not tidiness: a plain
    ``bind()`` REPLACES a widget's whole script for that sequence, which
    silently threw the tip's own ``<Enter>``/``<Leave>`` handlers away the
    first time this gesture was written (``docs/agents/map/facts.md``).

    ``palette`` (keyword-only): the FC HUD's ``{name: colour}`` dict shape, so
    a caller inside a tile can hand its own. Anything that is not a dict — and
    any missing key — falls back to ``ui_theme``, which is what an fc_gui
    caller wants anyway. ``topmost`` is passed straight through to
    ``attach_tooltip`` (True only inside an always-on-top window).
    """
    pal = palette if isinstance(palette, dict) else {}
    bg = pal.get("BG_PANEL", ui_theme.BG_PANEL)
    fg_dim = pal.get("FG_DIM", ui_theme.FG_DIM)
    fg_hot = pal.get("FG_ACCENT", ui_theme.FG_ACCENT)

    lbl = tk.Label(parent, text=str(glyph), bg=bg, fg=fg_dim,
                   font=GLYPH_FONT, cursor="hand2")
    if tooltip:
        attach_tooltip(lbl, str(tooltip), topmost=topmost)

    # A dict, not a closed-over name: the handlers below are separate
    # functions and two of them have to WRITE the latch.
    state = {"pressed": False}

    def _paint(colour):
        try:
            lbl.configure(fg=colour)
        except tk.TclError:
            pass

    def _enter(_event=None):
        _paint(fg_hot)

    def _leave(_event=None):
        state["pressed"] = False
        _paint(fg_dim)

    def _press(_event=None):
        state["pressed"] = True

    def _release(_event=None):
        if not state["pressed"]:
            return
        state["pressed"] = False
        if not callable(callback):
            return
        try:
            callback()
        except Exception:
            # ASCII only: a non-cp1252 glyph raises inside logging on this
            # box, so the message never carries the character itself.
            log.warning("glyph button action failed", exc_info=True)

    lbl.bind("<Enter>", _enter, add="+")
    lbl.bind("<Leave>", _leave, add="+")
    lbl.bind("<Button-1>", _press, add="+")
    lbl.bind("<ButtonRelease-1>", _release, add="+")
    # The four handlers, reachable from the widget. This is a TEST SEAM and it
    # is here because ``event_generate`` reaches nothing on an UNMAPPED widget
    # (measured, ``docs/agents/map/facts.md``): a headless test can drive the
    # real gesture through these, and prove the binds themselves with a
    # bind-script census, without mapping a window. It costs four attributes
    # and mirrors ``info_tile``'s own glyphs, whose handlers are real methods
    # for the same reason.
    lbl._glyph_enter = _enter
    lbl._glyph_leave = _leave
    lbl._glyph_press = _press
    lbl._glyph_release = _release
    return lbl
