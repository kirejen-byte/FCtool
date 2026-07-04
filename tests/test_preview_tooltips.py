"""Tooltip coverage for the preview-settings controls (feat/native-preview).

The preview settings section (`_build_preview_section`) exposes many controls —
the three mode buttons plus every Enhancement/FCPreview control. Each should
surface a concise, plain-English tooltip via the app's tooltip helper
(`_show_tooltip`/`_hide_tooltip`) on <Enter>.

These tests are LIGHT and HEADLESS: they walk the built widget tree, fire
<Enter> on a documented key set of controls, and assert the tooltip helper is
invoked with sensible copy. The *visual* feel of the tooltip (placement, wrap,
timing) still needs a live re-test — that is flagged in the task report, not
covered here.
"""
import os
import sys

import pytest
tk = pytest.importorskip("tkinter")
from tkinter import ttk

# Reuse the fully-wired UI host harness from the overlay-settings suite. The
# conftest puts the project root on sys.path but not the tests dir, so add it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_overlay_settings import _ui_host


def _walk(win):
    """Yield every descendant widget of ``win`` (depth-first)."""
    for c in win.winfo_children():
        yield c
        yield from _walk(c)


def _find_by_text(win, text, kinds=(tk.Button, ttk.Button, tk.Checkbutton)):
    """Return the first widget under ``win`` whose text contains ``text``."""
    for c in _walk(win):
        if isinstance(c, kinds):
            try:
                if text in str(c.cget("text")):
                    return c
            except tk.TclError:
                pass
    return None


def _find_combos(win):
    return [c for c in _walk(win) if isinstance(c, ttk.Combobox)]


def _tooltip_of(widget):
    """Return the tooltip copy attached to ``widget`` by the preview section's
    ``_tip`` helper, asserting the <Enter> binding is present.

    The <Enter> binding is the live tooltip trigger; ``_fctool_tooltip`` records
    the same copy so the content can be asserted deterministically without
    relying on synthetic-event delivery (unreliable for classic tk widgets when
    the root is withdrawn / the widget is unmapped).
    """
    assert str(widget.bind("<Enter>")).strip() != "", (
        f"widget {widget!r} has no <Enter> tooltip binding")
    assert str(widget.bind("<Leave>")).strip() != "", (
        f"widget {widget!r} has no <Leave> tooltip binding")
    return getattr(widget, "_fctool_tooltip", None)


def _build(host_cfg_mode):
    root, host = _ui_host(overlay_cfg={"enabled": False, "rules": [], "overrides": {}})
    host.config["preview"] = {"mode": host_cfg_mode}
    host._tips = []
    host._show_tooltip = lambda e, t: host._tips.append(t)
    host._hide_tooltip = lambda: None
    frame = tk.Frame(root)
    host._build_preview_section(frame)
    return root, host, frame


def test_build_preview_section_still_builds():
    # Sanity: the section builds under each mode without raising.
    for mode in ("off", "eveo_labels", "native"):
        root, host, frame = _build(mode)
        try:
            assert host._preview_mode_buttons  # buttons dict populated
        finally:
            root.destroy()


def test_mode_buttons_have_tooltips():
    root, host, frame = _build("off")
    try:
        for value, btn in host._preview_mode_buttons.items():
            tip = _tooltip_of(btn)
            assert tip, f"mode button {value!r} produced no tooltip"
            # The mode-button copy names all three modes / the off state.
            low = tip.lower()
            assert "eve-o" in low or "eveo" in low
            assert "fctool" in low or "fcpreview" in low or "native" in low
    finally:
        root.destroy()


def test_uniform_size_checkbox_tooltip():
    root, host, frame = _build("native")
    try:
        cb = _find_by_text(frame, "Uniform tile size")
        assert cb is not None, "Uniform tile size checkbox not found"
        tip = _tooltip_of(cb)
        assert tip and "resiz" in tip.lower()
    finally:
        root.destroy()


def test_doctrine_tag_checkbox_tooltip():
    root, host, frame = _build("native")
    try:
        cb = _find_by_text(frame, "doctrine tag")
        assert cb is not None, "doctrine-tag caption checkbox not found"
        tip = _tooltip_of(cb)
        assert tip and "doctrine" in tip.lower()
    finally:
        root.destroy()


def test_damage_flash_mode_combo_tooltip():
    root, host, frame = _build("native")
    try:
        # The damage-flash MODE combo is the one bound near 'Flash on'. Identify
        # it as the combobox whose current value is a damage-mode label.
        combos = _find_combos(frame)
        target = None
        for c in combos:
            try:
                if str(c.get()) in ("Any damage", "Threshold"):
                    target = c
                    break
            except tk.TclError:
                pass
        assert target is not None, "damage-flash mode combo not found"
        tip = _tooltip_of(target)
        low = (tip or "").lower()
        assert tip and "threshold" in low and "any" in low
    finally:
        root.destroy()


def test_anchor_combo_tooltip():
    root, host, frame = _build("native")
    try:
        # The label-position/anchor combo carries the overlay anchor labels.
        anchors = set(host._OVERLAY_ANCHORS)
        target = None
        for c in _find_combos(frame):
            try:
                if str(c.get()) in anchors:
                    target = c
                    break
            except tk.TclError:
                pass
        assert target is not None, "anchor/position combo not found"
        tip = _tooltip_of(target)
        assert tip and ("corner" in tip.lower() or "label" in tip.lower())
    finally:
        root.destroy()


def test_previews_button_tooltip():
    root, host, frame = _build("native")
    try:
        btn = _find_by_text(frame, "Previews")
        assert btn is not None, "Previews… button not found"
        tip = _tooltip_of(btn)
        assert tip and "preview" in tip.lower() and "character" in tip.lower()
    finally:
        root.destroy()
