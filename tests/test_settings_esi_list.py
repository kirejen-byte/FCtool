import pytest
tk = pytest.importorskip("tkinter")
# Headless convention (mirrors tests/test_intel_stream_gui.py): no module-level
# root is created/destroyed at import time (that corrupts init.tcl resolution on
# this Windows Tcl build). Each test builds its own fresh tk.Tk() inside
# _make_host, and the "no display" intent is preserved by skipping on TclError.

import types

import fc_gui


class FakeAcct:
    """Stand-in for ESIAuth with just the attributes _rebuild_esi_char_list
    reads: character_name, is_authenticated, and has_scope()."""

    def __init__(self, name, *, authenticated=True, has_fittings=True):
        self.character_name = name
        self.is_authenticated = authenticated
        self._has_fittings = has_fittings

    def has_scope(self, _scope):
        return self._has_fittings


def _make_host(accounts, primary=None):
    """Minimal host with a real Frame + the rebuild method bound onto a
    SimpleNamespace (same scaffold pattern as test_intel_stream_gui.py)."""
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    host = types.SimpleNamespace()
    host.esi_accounts = accounts
    host.esi_auth = primary
    host._esi_chars_frame = tk.Frame(root)
    # no-op collaborators referenced by the bound method
    host._show_tooltip = lambda e, t: None
    host._hide_tooltip = lambda: None
    host._esi_set_primary = lambda a: None
    host._esi_disconnect = lambda a: None
    host._esi_login = lambda: None
    host._rebuild_esi_char_list = types.MethodType(
        fc_gui.FCToolGUI._rebuild_esi_char_list, host)
    return root, host


def _buttons_by_text(frame, text):
    """All ttk.Button children of `frame` whose text equals `text`."""
    out = []
    for w in frame.winfo_children():
        try:
            if w.winfo_class() == "TButton" and str(w.cget("text")) == text:
                out.append(w)
        except tk.TclError:
            pass
    return out


def _labels_with_text(frame, text):
    out = []
    for w in frame.winfo_children():
        try:
            if w.winfo_class() == "Label" and str(w.cget("text")) == text:
                out.append(w)
        except tk.TclError:
            pass
    return out


def test_char_rows_grid_align():
    primary = FakeAcct("Alpha")
    accounts = [
        primary,
        FakeAcct("A Very Long Character Name Indeed"),
        FakeAcct("Gamma", has_fittings=False),  # missing fittings scope
    ]
    root, host = _make_host(accounts, primary=primary)
    try:
        host._rebuild_esi_char_list()

        set_primary = _buttons_by_text(host._esi_chars_frame, "Set Primary")
        disconnect = _buttons_by_text(host._esi_chars_frame, "Disconnect")
        reauth = _buttons_by_text(host._esi_chars_frame, "Re-authorize")

        # One "Set Primary" and one "Disconnect" per account (every row).
        assert len(set_primary) == 3
        assert len(disconnect) == 3

        # Every "Set Primary" button shares one grid column.
        sp_cols = {b.grid_info()["column"] for b in set_primary}
        assert len(sp_cols) == 1
        # Every "Disconnect" button shares one grid column.
        dc_cols = {b.grid_info()["column"] for b in disconnect}
        assert len(dc_cols) == 1
        # Set Primary sits left of Disconnect.
        assert next(iter(sp_cols)) < next(iter(dc_cols))
        assert next(iter(sp_cols)) == 3
        assert next(iter(dc_cols)) == 4

        # The primary account's "Set Primary" is disabled; the others enabled.
        disabled = [b for b in set_primary if "disabled" in str(b["state"])]
        assert len(disabled) == 1

        # Exactly one Re-authorize button, and it lives in column 5.
        assert len(reauth) == 1
        assert reauth[0].grid_info()["column"] == 5

        # All name labels sit in column 1.
        name_cols = set()
        for acct in accounts:
            lbls = _labels_with_text(host._esi_chars_frame, acct.character_name)
            assert lbls, f"no name label for {acct.character_name}"
            for lbl in lbls:
                name_cols.add(lbl.grid_info()["column"])
        assert name_cols == {1}
    finally:
        root.destroy()


def test_empty_state():
    root, host = _make_host([], primary=None)
    try:
        host._rebuild_esi_char_list()
        children = host._esi_chars_frame.winfo_children()
        assert len(children) == 1
        assert str(children[0].cget("text")) == "No characters connected"
    finally:
        root.destroy()


def test_scope_flag_inline():
    primary = FakeAcct("Alpha")
    accounts = [
        primary,
        FakeAcct("Beta"),
        FakeAcct("Gamma", has_fittings=False),  # only this one lacks the scope
    ]
    root, host = _make_host(accounts, primary=primary)
    try:
        host._rebuild_esi_char_list()

        # The ⚠ flag exists exactly once, and it is grid-managed in column 2.
        warn = _labels_with_text(host._esi_chars_frame, "⚠")
        assert len(warn) == 1
        assert warn[0].grid_info()["column"] == 2

        # No separate full-width notice rows: every child of the frame is
        # grid-managed (grid_info() non-empty). A pack-managed notice Frame
        # would return an empty grid_info(). Because a widget can only have one
        # geometry manager, non-empty grid_info() for every child also proves
        # nothing is pack-managed.
        for w in host._esi_chars_frame.winfo_children():
            assert w.grid_info(), f"child {w} is not grid-managed"
    finally:
        root.destroy()
