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
    ``map/facts.md``).

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


def attach_tooltip(widget, text, *, topmost=False):
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
    """
    widget._tooltip_text = text
    state = {"tip": None}

    def _hide(_e=None):
        tip = state.get("tip")
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass
            state["tip"] = None

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
            tip.wm_overrideredirect(True)
            tk.Label(tip, text=copy, font=_TOOLTIP_FONT,
                     fg=ui_theme.FG_TEXT, bg=ui_theme.BG_PANEL,
                     borderwidth=1, relief=tk.SOLID, justify=tk.LEFT,
                     wraplength=340, padx=5, pady=3).pack()
            tip.wm_geometry(
                f"+{widget.winfo_rootx() + 12}"
                f"+{widget.winfo_rooty() + widget.winfo_height() + 4}")
            if topmost:
                try:
                    tip.wm_attributes("-topmost", True)
                except tk.TclError:
                    pass
                try:
                    tip.lift()
                except tk.TclError:
                    pass
            state["tip"] = tip
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
