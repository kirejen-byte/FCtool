import pytest

tk = pytest.importorskip("tkinter")
import preview_tile as pt


class FakeTileWin32:
    def __init__(self):
        self.styled = []
        self.placed = []
        self.retops = []

    def make_tool_noactivate(self, hwnd):
        self.styled.append(hwnd)

    def set_window_pos(self, hwnd, x, y, w, h):
        self.placed.append((hwnd, x, y, w, h))

    def retop(self, hwnd):
        self.retops.append(hwnd)

    def get_root_hwnd(self, tk_id):
        return tk_id + 100000  # fake GA_ROOT mapping


class FakeDwm:
    def __init__(self):
        self.calls = []

    def register(self, dest, src):
        self.calls.append(("register", dest, src))
        return 7

    def unregister(self, t):
        self.calls.append(("unregister", t))

    def update(self, t, rect, **kw):
        self.calls.append(("update", rect))

    def query_source_size(self, t):
        return (1920, 1080)


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")


PALETTE = dict(BG_PANEL="#16213e", BG_DARK="#1a1a2e", FG_TEXT="#e0e0e0",
               FG_ACCENT="#00d4ff", FG_DIM="#808090")


def test_tile_applies_styles_and_places_via_win32_not_tk():
    root = _root()
    try:
        w32, dwm = FakeTileWin32(), FakeDwm()
        tile = pt.TileWindow(root, "kirejen", PALETTE, win32=w32, dwm=dwm,
                             on_activate=lambda k: None, on_minimize=lambda k: None,
                             on_move_end=lambda k, x, y: None,
                             on_resize_end=lambda k, w, h: None)
        tile.place(100, 200, 384, 216)
        assert len(w32.styled) == 1            # styles applied once, on the GA_ROOT hwnd
        assert w32.placed[-1][1:] == (100, 200, 384, 216 + pt.STRIP_H)
        tile.destroy()
    finally:
        root.destroy()


def test_attach_source_registers_letterboxed_below_strip():
    root = _root()
    try:
        w32, dwm = FakeTileWin32(), FakeDwm()
        tile = pt.TileWindow(root, "kirejen", PALETTE, win32=w32, dwm=dwm,
                             on_activate=lambda k: None, on_minimize=lambda k: None,
                             on_move_end=lambda k, x, y: None,
                             on_resize_end=lambda k, w, h: None)
        tile.place(0, 0, 384, 216)
        tile.attach_source(555)
        kinds = [c[0] for c in dwm.calls]
        assert kinds[:2] == ["register", "update"]
        rect = dwm.calls[1][1]
        assert rect[1] >= pt.STRIP_H          # thumbnail never covers the strip
        tile.detach()
        assert ("unregister", 7) in dwm.calls
        tile.destroy()
    finally:
        root.destroy()


def test_caption_and_badge_render_text():
    root = _root()
    try:
        tile = pt.TileWindow(root, "kirejen", PALETTE, win32=FakeTileWin32(),
                             dwm=FakeDwm(), on_activate=lambda k: None,
                             on_minimize=lambda k: None,
                             on_move_end=lambda k, x, y: None,
                             on_resize_end=lambda k, w, h: None)
        tile.set_caption("Kirejen", dot="#3fbf5f", chip="FC", tag="Cyno")
        assert "Kirejen" in tile.caption_text()
        tile.set_badge("MINIMIZED")
        assert "MINIMIZED" in tile.caption_text()
        tile.destroy()
    finally:
        root.destroy()


def _make_tile(root, w32=None, dwm=None):
    return pt.TileWindow(root, "kirejen", PALETTE, win32=w32 or FakeTileWin32(),
                         dwm=dwm or FakeDwm(), on_activate=lambda k: None,
                         on_minimize=lambda k: None,
                         on_move_end=lambda k, x, y: None,
                         on_resize_end=lambda k, w, h: None)


def test_hover_opacity_enter_leave_and_active_stays_hover():
    root = _root()
    try:
        tile = _make_tile(root)
        tile.configure_hover(inactive=0.85, hover=1.0)
        assert tile.current_alpha() == 0.85          # inactive applied on configure
        tile._on_enter(None)
        assert tile.current_alpha() == 1.0           # hover on enter
        tile._on_leave(None)
        assert tile.current_alpha() == 0.85          # back to inactive on leave
        tile.set_active(True)                        # active tile stays at hover
        assert tile.current_alpha() == 1.0
        tile._on_leave(None)                         # leaving an active tile keeps hover
        assert tile.current_alpha() == 1.0
        tile.set_active(False)
        assert tile.current_alpha() == 0.85
        tile.destroy()
    finally:
        root.destroy()


def test_zoom_on_enter_replaces_rect_and_restores_on_leave():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile = _make_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        tile.configure_zoom(enabled=True, factor=2.0, anchor="nw")
        placed_before = len(w32.placed)
        tile._on_enter(None)
        # zoomed rect re-placed via win32 (body_h doubled, +STRIP_H on window height)
        assert w32.placed[-1] == (w32.get_root_hwnd(tile.top.winfo_id()),
                                  100, 200, 768, 432 + pt.STRIP_H)
        assert w32.retops                             # retop after zoom
        assert len(w32.placed) > placed_before
        tile._on_leave(None)
        # restored to the original rect
        assert w32.placed[-1] == (w32.get_root_hwnd(tile.top.winfo_id()),
                                  100, 200, 384, 216 + pt.STRIP_H)
        tile.destroy()
    finally:
        root.destroy()


def test_zoom_disabled_does_not_move_tile_on_hover():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile = _make_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        tile.configure_zoom(enabled=False, factor=2.0, anchor="nw")
        n = len(w32.placed)
        tile._on_enter(None)
        tile._on_leave(None)
        assert len(w32.placed) == n                   # no re-placement when zoom off
        tile.destroy()
    finally:
        root.destroy()


def test_zoom_suppressed_while_dragging():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile = _make_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        tile.configure_zoom(enabled=True, factor=2.0, anchor="nw")
        # simulate an in-progress drag (right-button move mode active)
        tile._mode = "move"
        tile._press_root = (0, 0)
        n = len(w32.placed)
        tile._on_enter(None)
        assert len(w32.placed) == n                   # no zoom while dragging
        tile.destroy()
    finally:
        root.destroy()


# ── (C4) mouse-model modifier ladder: exclude / switch-external / highlight ────
class _Evt:
    """Minimal <ButtonRelease-1> event stand-in (no real Tk event needed)."""

    def __init__(self, state=0, x_root=0, y_root=0):
        self.state = state
        self.x_root = x_root
        self.y_root = y_root


def _tile_with_all_callbacks(root, w32=None):
    calls = {"activate": [], "minimize": [], "exclude": [], "external": []}
    tile = pt.TileWindow(
        root, "kirejen", PALETTE, win32=w32 or FakeTileWin32(), dwm=FakeDwm(),
        on_activate=lambda k: calls["activate"].append(k),
        on_minimize=lambda k: calls["minimize"].append(k),
        on_move_end=lambda k, x, y: None,
        on_resize_end=lambda k, w, h: None,
        on_exclude=lambda k: calls["exclude"].append(k),
        on_switch_external=lambda: calls["external"].append(1),
    )
    return tile, calls


def test_plain_left_release_activates():
    root = _root()
    try:
        tile, calls = _tile_with_all_callbacks(root)
        tile._on_b1_press(_Evt(x_root=10, y_root=10))
        tile._on_b1_release(_Evt(state=0x0000, x_root=10, y_root=10))
        assert calls["activate"] == ["kirejen"]
        assert calls["minimize"] == [] and calls["exclude"] == []
        assert calls["external"] == []
        tile.destroy()
    finally:
        root.destroy()


def test_ctrl_left_release_minimizes():
    root = _root()
    try:
        tile, calls = _tile_with_all_callbacks(root)
        tile._on_b1_press(_Evt(x_root=10, y_root=10))
        tile._on_b1_release(_Evt(state=0x0004, x_root=10, y_root=10))   # Ctrl
        assert calls["minimize"] == ["kirejen"]
        assert calls["activate"] == [] and calls["exclude"] == []
        tile.destroy()
    finally:
        root.destroy()


def test_shift_left_release_toggles_cycle_exclusion():
    root = _root()
    try:
        tile, calls = _tile_with_all_callbacks(root)
        tile._on_b1_press(_Evt(x_root=10, y_root=10))
        tile._on_b1_release(_Evt(state=0x0001, x_root=10, y_root=10))   # Shift only
        assert calls["exclude"] == ["kirejen"]
        assert calls["activate"] == [] and calls["minimize"] == []
        assert calls["external"] == []
        tile.destroy()
    finally:
        root.destroy()


def test_ctrl_shift_left_release_switches_external():
    root = _root()
    try:
        tile, calls = _tile_with_all_callbacks(root)
        tile._on_b1_press(_Evt(x_root=10, y_root=10))
        tile._on_b1_release(_Evt(state=0x0005, x_root=10, y_root=10))   # Ctrl+Shift
        assert calls["external"] == [1]
        assert calls["activate"] == [] and calls["minimize"] == []
        assert calls["exclude"] == []
        tile.destroy()
    finally:
        root.destroy()


def test_modifier_click_still_suppressed_after_a_drag():
    root = _root()
    try:
        tile, calls = _tile_with_all_callbacks(root)
        tile._on_b1_press(_Evt(x_root=10, y_root=10))
        # released far from press → treated as a drag, not a click: no callback
        tile._on_b1_release(_Evt(state=0x0001, x_root=200, y_root=200))
        assert calls == {"activate": [], "minimize": [], "exclude": [],
                         "external": []}
        tile.destroy()
    finally:
        root.destroy()


def test_set_excluded_shows_badge_dot_and_clears():
    root = _root()
    try:
        tile = _make_tile(root)
        tile.set_excluded(True)
        assert tile.is_excluded() is True
        # a visible marker is present on the strip after exclusion
        assert tile._excl_lbl.cget("text") != ""
        tile.set_excluded(False)
        assert tile.is_excluded() is False
        assert tile._excl_lbl.cget("text") == ""
        tile.destroy()
    finally:
        root.destroy()
