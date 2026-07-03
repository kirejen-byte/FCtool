"""Headless unit test for the collapsible X-Up Log drawer toggle.

The toggle (``FCToolGUI._toggle_xup_log``) only flips a bool, retitles the
header label, and packs / forgets the body frame. None of that needs a real
Tcl interpreter, so we bind the real method onto a ``types.SimpleNamespace``
host (the repo's Tk-free convention) with fake widgets that record their
``config`` / ``pack`` / ``pack_forget`` calls. This keeps the test fast and
display-independent while still exercising the actual production method.
"""

import types

import fc_gui


class _FakeLabel:
    """Records the last text passed to config(text=...)."""

    def __init__(self, text=""):
        self.text = text

    def config(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs["text"]


class _FakeBody:
    """Records pack() / pack_forget() calls and current visibility."""

    def __init__(self):
        self.packed = False
        self.pack_calls = []
        self.forget_calls = 0

    def pack(self, **kwargs):
        self.packed = True
        self.pack_calls.append(kwargs)

    def pack_forget(self):
        self.packed = False
        self.forget_calls += 1


def _make_host():
    host = types.SimpleNamespace()
    host._xup_log_expanded = False
    host._xup_log_toggle_btn = _FakeLabel(text="▶ X-Up Log")
    host._xup_log_body = _FakeBody()
    host._toggle_xup_log = types.MethodType(
        fc_gui.FCToolGUI._toggle_xup_log, host)
    return host


def test_starts_collapsed():
    host = _make_host()
    assert host._xup_log_expanded is False
    assert host._xup_log_body.packed is False
    assert host._xup_log_toggle_btn.text == "▶ X-Up Log"


def test_toggle_expands_then_collapses():
    host = _make_host()

    # First click: expand — body packs, arrow flips to ▼.
    host._toggle_xup_log()
    assert host._xup_log_expanded is True
    assert host._xup_log_body.packed is True
    assert host._xup_log_toggle_btn.text == "▼ X-Up Log"
    # Body packed with the canonical drawer geometry.
    assert host._xup_log_body.pack_calls[-1] == {
        "fill": fc_gui.tk.X, "padx": 6, "pady": (0, 6)}

    # Second click: collapse — body forgotten, arrow flips back to ▶.
    host._toggle_xup_log()
    assert host._xup_log_expanded is False
    assert host._xup_log_body.packed is False
    assert host._xup_log_body.forget_calls == 1
    assert host._xup_log_toggle_btn.text == "▶ X-Up Log"


def test_toggle_is_idempotent_over_pairs():
    host = _make_host()
    for _ in range(3):
        host._toggle_xup_log()
        host._toggle_xup_log()
    # Back to the collapsed baseline after an even number of clicks.
    assert host._xup_log_expanded is False
    assert host._xup_log_body.packed is False
    assert host._xup_log_toggle_btn.text == "▶ X-Up Log"
