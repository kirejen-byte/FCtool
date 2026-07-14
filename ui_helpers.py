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

``make_modal(win, parent, *, on_cancel=None, base_bg=None)``
    The house modal-dialog contract, wired once so every dialog behaves the same:
      * D6 — ``transient(parent)`` and ``grab_set()`` are each guarded against
        ``TclError`` (a withdrawn/unmapped parent, or an unviewable window during
        headless tests, must degrade quietly rather than crash the opener).
      * D2 — ``<Escape>`` is bound to ``on_cancel`` (or ``win.destroy`` when no
        cancel handler is given), so muscle-memory dismissal works on EVERY
        modal, not just the ~14 that happened to bind it by hand. Callers pass
        the dialog's real close handler (its Cancel button command / WM_DELETE
        protocol handler) so Escape follows the SAME path — never a blind destroy
        that skips cleanup.
      * D5 — the window base colour is set once from the shared palette
        (``base_bg`` or the canonical ``ui_theme.BG_DARK``), retiring the
        BG_DARK-vs-BG_PANEL split across dialogs.

``attach_tooltip(widget, text)``
    The single hover-tooltip implementation (D9). Promoted verbatim-in-spirit
    from ``overview_manager_ui._attach_tooltip`` — the best-in-repo version, the
    only one that bound ``<Destroy>`` and so did not leak an orphaned Toplevel
    when a widget was destroyed mid-hover (the v3.5.2 tooltip-leak class). Themed
    from ``ui_theme`` (dark panel bg, light text — never the stray light-yellow
    ``#ffffe0`` that one bespoke copy rendered). The copy is also stashed on the
    widget as ``_tooltip_text`` so tests can assert it without simulating a hover.
"""
from __future__ import annotations

import tkinter as tk

import ui_theme

# Tooltip type is deliberately small/monospace to match the app's Consolas UI.
_TOOLTIP_FONT = ("Consolas", 8)


def make_modal(win, parent, *, on_cancel=None, base_bg=None):
    """Apply the house modal contract to ``win`` and return it.

    ``win``        the dialog Toplevel (already created + titled by the caller).
    ``parent``     the owning window; used as the transient master.
    ``on_cancel``  called on ``<Escape>``; defaults to ``win.destroy``. Pass the
                   dialog's own Cancel/close handler so Escape and the Cancel
                   button share one code path (cleanup, grab_release, etc.).
    ``base_bg``    the window background; defaults to ``ui_theme.BG_DARK``.

    ``transient``/``grab_set`` are each guarded against ``TclError`` so an
    unviewable window or withdrawn parent degrades quietly (D6).
    """
    try:
        win.transient(parent)
    except tk.TclError:
        pass
    try:
        win.grab_set()
    except tk.TclError:
        pass

    cancel = on_cancel if callable(on_cancel) else win.destroy
    win.bind("<Escape>", lambda _e=None: cancel())

    win.configure(bg=base_bg or ui_theme.BG_DARK)
    return win


def attach_tooltip(widget, text):
    """Attach a simple hover tooltip to ``widget`` (D9 shared helper) and return
    ``widget``.

    A borderless, dark-themed Toplevel is shown on ``<Enter>`` and destroyed on
    ``<Leave>``. It is ALSO destroyed on the widget's own ``<Destroy>`` so a
    widget torn down while the pointer is over it never orphans the tip (the
    v3.5.2 leak fix — this is the reason this implementation, not one of the
    other four, was promoted). All three binds use ``add="+"`` so they never
    clobber an existing binding on the widget.

    The tooltip copy is stashed on the widget as ``_tooltip_text`` so it is
    assertable in tests without delivering a synthetic hover event.
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
        try:
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tk.Label(tip, text=text, font=_TOOLTIP_FONT,
                     fg=ui_theme.FG_TEXT, bg=ui_theme.BG_PANEL,
                     borderwidth=1, relief=tk.SOLID, justify=tk.LEFT,
                     wraplength=340, padx=5, pady=3).pack()
            tip.wm_geometry(
                f"+{widget.winfo_rootx() + 12}"
                f"+{widget.winfo_rooty() + widget.winfo_height() + 4}")
            state["tip"] = tip
        except tk.TclError:
            state["tip"] = None

    widget.bind("<Enter>", _show, add="+")
    widget.bind("<Leave>", _hide, add="+")
    widget.bind("<Destroy>", _hide, add="+")
    return widget
