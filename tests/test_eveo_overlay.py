import sys

import pytest
tk = pytest.importorskip("tkinter")
# Headless convention: no module-level root create/destroy (corrupts init.tcl
# on this Windows Tcl build). Fresh tk.Tk() per test; skip on TclError.
# `sys` is needed at COLLECTION time by the platform skipif on the real-window
# guard test — without this import the whole file errors during collection.

from eveo_overlay import OverlayWindow


PALETTE = {
    "font_size": 11, "color": "#00d4ff", "anchor": "top-left",
    "transparent_key": "#010203",
}


# Sentinel offset so a resolved PARENT handle is always distinct from the child
# winfo_id() that Tk hands us. GetAncestor(child, GA_ROOT) must return this
# distinct top-level handle — that is the window Tk applied -transparentcolor /
# WS_EX_LAYERED to, and the window HWND_TOPMOST must be asserted on.
_PARENT_OFFSET = 100000


class FakeOverlayWin32:
    GA_ROOT = 2

    def __init__(self):
        self.applied = []      # PARENT hwnds we applied layered/click-through to
        self.retops = []       # PARENT hwnds we re-asserted topmost on
        self.ancestor_calls = []   # (hwnd, flag) tuples GetAncestor was called with

    def get_parent(self, hwnd):
        # Tk's real top-level is the parent of the child winfo_id() window.
        return hwnd + _PARENT_OFFSET

    def get_ancestor(self, hwnd, flag):
        self.ancestor_calls.append((hwnd, flag))
        if flag == self.GA_ROOT:
            return hwnd + _PARENT_OFFSET
        return hwnd

    def make_click_through(self, hwnd):
        self.applied.append(hwnd)

    def set_topmost(self, hwnd):
        self.retops.append(hwnd)


def _make():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    w32 = FakeOverlayWin32()
    ov = OverlayWindow(root, PALETTE, win32=w32)
    return root, ov, w32


def _expected_parent(ov):
    """The PARENT sentinel the fake resolves for this overlay's child window."""
    return int(ov.top.winfo_id()) + _PARENT_OFFSET


def _canvas_texts(ov):
    c = ov._canvas
    return [c.itemcget(i, "text")
            for i in c.find_all() if c.type(i) == "text"]


def test_set_labels_draws_text_items():
    root, ov, _ = _make()
    try:
        ov.set_labels([((10, 10, 210, 130), "Cyno")])
        texts = _canvas_texts(ov)
        # shadow + foreground => the label text appears twice
        assert texts.count("Cyno") == 2
    finally:
        root.destroy()


def test_empty_hides_window():
    root, ov, _ = _make()
    try:
        ov.set_labels([((0, 0, 10, 10), "X")])
        ov.set_labels([])
        assert ov._canvas.find_all() == ()
        assert ov.visible is False
    finally:
        root.destroy()


def test_blank_label_not_drawn():
    root, ov, _ = _make()
    try:
        ov.set_labels([((0, 0, 10, 10), "")])
        assert _canvas_texts(ov) == []
    finally:
        root.destroy()


def test_retop_calls_win32_on_resolved_parent():
    root, ov, w32 = _make()
    try:
        # retop() only fires when the window is visible; show it first.
        ov.set_labels([((0, 0, 10, 10), "A")])
        w32.retops.clear()
        ov.retop()
        assert len(w32.retops) == 1
        # HWND_TOPMOST must land on the RESOLVED top-level (parent sentinel),
        # never the raw child winfo_id() — otherwise SetWindowPos is a no-op.
        assert w32.retops[0] == _expected_parent(ov)
        assert w32.retops[0] != int(ov.top.winfo_id())
    finally:
        root.destroy()


def test_hwnd_resolves_to_toplevel_parent():
    root, ov, w32 = _make()
    try:
        # _hwnd() must resolve the true top-level via GetAncestor(GA_ROOT),
        # not return the TkChild winfo_id().
        resolved = ov._hwnd()
        assert resolved == _expected_parent(ov)
        assert resolved != int(ov.top.winfo_id())
        # GetAncestor was queried with GA_ROOT (2) on the child window.
        assert (int(ov.top.winfo_id()), w32.GA_ROOT) in w32.ancestor_calls
    finally:
        root.destroy()


def test_anchor_positions_differ():
    root, ov, _ = _make()
    try:
        ov.set_labels([((100, 100, 300, 260), "A")])
        c = ov._canvas
        fg = [i for i in c.find_all() if c.type(i) == "text"][-1]
        x_tl, y_tl = c.coords(fg)

        ov.set_anchor("bottom-right")
        ov.set_labels([((100, 100, 300, 260), "A")])
        fg2 = [i for i in c.find_all() if c.type(i) == "text"][-1]
        x_br, y_br = c.coords(fg2)

        assert (x_br, y_br) != (x_tl, y_tl)
        assert x_br > x_tl and y_br > y_tl
    finally:
        root.destroy()


def test_style_applied_once_on_resolved_parent():
    root, ov, w32 = _make()
    try:
        # make_click_through is applied on first show, to the RESOLVED parent —
        # Tk applies -transparentcolor/WS_EX_LAYERED to the top-level, so the
        # click-through ex-style bits must go on that same top-level, not the
        # TkChild winfo_id().
        ov.set_labels([((0, 0, 10, 10), "A")])
        assert len(w32.applied) >= 1
        assert w32.applied[0] == _expected_parent(ov)
        assert w32.applied[0] != int(ov.top.winfo_id())
        # styling is one-shot (idempotent) — a second draw does not re-apply.
        ov.set_labels([((0, 0, 10, 10), "B")])
        assert len(w32.applied) == 1
    finally:
        root.destroy()


@pytest.mark.skipif(sys.platform != "win32", reason="real-window Win32 guard")
def test_real_window_styles_land_on_true_toplevel():
    """End-to-end guard on the REAL window (no fake): create an OverlayWindow
    with the real _real_overlay_win32, map it, resolve the styled hwnd, and read
    the actual ex-style/style bits back via ctypes — proving _hwnd() targets the
    true top-level (WS_CHILD clear) and that WS_EX_LAYERED landed on THAT hwnd.
    This is the one test that would have caught the child-vs-parent bug."""
    import ctypes
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    try:
        # Real wrapper (not the fake) — exercises the production styling path.
        ov = OverlayWindow(root, PALETTE)         # win32=None -> real singleton
        ov.set_labels([((0, 0, 40, 20), "GUARD")])  # maps + styles + retops
        root.update_idletasks()
        root.update()

        hwnd = ov._hwnd()
        assert hwnd, "resolved hwnd must be non-zero"

        GWL_STYLE = -16
        GWL_EXSTYLE = -20
        WS_CHILD = 0x40000000
        WS_EX_LAYERED = 0x00080000

        user32 = ctypes.windll.user32
        getptr = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        getptr.restype = ctypes.c_ssize_t
        getptr.argtypes = [ctypes.c_void_p, ctypes.c_int]

        style = int(getptr(hwnd, GWL_STYLE))
        exstyle = int(getptr(hwnd, GWL_EXSTYLE))

        # (a) resolved hwnd is a true top-level: WS_CHILD must be CLEAR.
        assert (style & WS_CHILD) == 0, (
            f"styled hwnd still WS_CHILD (style=0x{style:08x}) — _hwnd() "
            "returned the TkChild, not the top-level")
        # (b) the layered ex-style Tk/our wrapper set is on THIS same hwnd.
        assert (exstyle & WS_EX_LAYERED) != 0, (
            f"WS_EX_LAYERED not set on resolved hwnd (exstyle=0x{exstyle:08x})")
    finally:
        root.destroy()
