"""Regression guard for Bug 1: the shared Dark/Green/Red.TButton ttk styles must
resolve to a concrete, non-empty font so clam-themed buttons cannot collapse to
~6x6 px with invisible text. Replicates just the style setup from
fc_gui._build_ui rather than booting the full GUI (which loads the real config).
"""
import tkinter as tk
from tkinter import ttk, font as tkfont

import pytest


def _make_root():
    """Create a withdrawn Tk root, skipping the test when no display/Tcl is
    available (headless CI, no X server)."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    return root


def test_button_styles_have_nonempty_font():
    root = _make_root()
    try:
        style = ttk.Style()
        style.theme_use("clam")
        fam = next(
            (f for f in ("Consolas", "Courier New", "DejaVu Sans Mono",
                         "Liberation Mono", "Lucida Console", "Monaco",
                         "TkFixedFont")
             if f in set(tkfont.families(root))),
            "TkFixedFont")
        fnt = tkfont.Font(family=fam, size=10)
        style.configure("Dark.TButton", font=fnt, padding=(8, 4))
        assert style.lookup("Dark.TButton", "font") != ""
        b = ttk.Button(root, text="OK", style="Dark.TButton")
        root.update_idletasks()
        assert b.cget("text") == "OK"
        assert b.winfo_reqwidth() >= 30 and b.winfo_reqheight() >= 18
    finally:
        root.destroy()
