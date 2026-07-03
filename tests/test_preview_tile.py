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
