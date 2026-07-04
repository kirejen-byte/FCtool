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


# ── (caption-onvideo) on-video anchored label + live style + name ellipsize ───
def test_set_label_style_stores_color_size_anchor():
    root = _root()
    try:
        tile = _make_tile(root)
        tile.set_label_style(color="#ff8800", size=16, anchor="bottom-right")
        assert tile._label_color == "#ff8800"
        assert tile._label_size == 16
        assert tile._label_anchor == "bottom-right"
        tile.destroy()
    finally:
        root.destroy()


def test_set_video_label_draws_and_empty_hides():
    root = _root()
    try:
        tile = _make_tile(root)
        tile.place(0, 0, 384, 216)
        tile.set_label_style(color="#00d4ff", size=12, anchor="top-left")
        tile.set_video_label("Cyno - Onyx")
        # the on-video canvas has drawn outline + fill items for the text
        items = tile._label_canvas.find_all()
        assert len(items) >= 2                    # >=1 outline + 1 fill
        assert tile.video_label_text() == "Cyno - Onyx"
        # empty text clears the canvas (nothing drawn)
        tile.set_video_label("")
        assert tile._label_canvas.find_all() == ()
        assert tile.video_label_text() == ""
        tile.destroy()
    finally:
        root.destroy()


def test_set_video_label_uses_configured_fill_color():
    root = _root()
    try:
        tile = _make_tile(root)
        tile.place(0, 0, 384, 216)
        tile.set_label_style(color="#ff0000", size=12, anchor="top-left")
        tile.set_video_label("Cyno")
        fills = {tile._label_canvas.itemcget(i, "fill")
                 for i in tile._label_canvas.find_all()}
        assert "#ff0000" in fills                 # configured fill present
        assert "#000000" in fills                 # black outline present
        tile.destroy()
    finally:
        root.destroy()


def test_caption_name_ellipsized_on_narrow_tile():
    root = _root()
    try:
        tile = _make_tile(root)
        tile.place(0, 0, 120, 216)                # narrow tile
        tile.set_caption("A" * 60, dot="#3fbf5f", chip="", tag="")
        shown = tile._name_lbl.cget("text")
        assert shown.endswith("…")                # name truncated to fit the row
        assert len(shown) < 60
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


# ── (BUG B) LEFT-drag-to-move on the caption strip (title-bar semantics) ──────
def _move_tile(root, w32=None):
    """Tile with a recording on_move_end callback for left/right drag tests."""
    moves = []
    tile = pt.TileWindow(
        root, "kirejen", PALETTE, win32=w32 or FakeTileWin32(), dwm=FakeDwm(),
        on_activate=lambda k: None, on_minimize=lambda k: None,
        on_move_end=lambda k, x, y: moves.append((k, x, y)),
        on_resize_end=lambda k, w, h: None)
    return tile, moves


def test_left_drag_on_strip_past_jitter_moves_via_win32():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile, moves = _move_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        hwnd = w32.get_root_hwnd(tile.top.winfo_id())
        n = len(w32.placed)
        # press on the strip, drag +50,+30 (past _MOVE_JITTER), release
        tile._on_strip_b1_press(_Evt(x_root=10, y_root=10))
        tile._on_strip_b1_motion(_Evt(x_root=60, y_root=40))
        # a set_window_pos with the GA_ROOT hwnd and the right delta was issued
        assert w32.placed[-1] == (hwnd, 150, 230, 384, 216 + pt.STRIP_H)
        assert len(w32.placed) > n
        tile._on_strip_b1_release(_Evt(x_root=60, y_root=40))
        # move committed via on_move_end with the final position
        assert moves[-1] == ("kirejen", 150, 230)
        tile.destroy()
    finally:
        root.destroy()


def test_left_click_within_jitter_on_strip_still_activates():
    root = _root()
    try:
        activated = []
        tile = pt.TileWindow(
            root, "kirejen", PALETTE, win32=FakeTileWin32(), dwm=FakeDwm(),
            on_activate=lambda k: activated.append(k),
            on_minimize=lambda k: None,
            on_move_end=lambda k, x, y: None,
            on_resize_end=lambda k, w, h: None)
        tile.place(100, 200, 384, 216)
        # a plain left press+release within jitter on the strip = activate, no move
        tile._on_strip_b1_press(_Evt(x_root=10, y_root=10))
        tile._on_strip_b1_release(_Evt(x_root=12, y_root=11))
        assert activated == ["kirejen"]
        tile.destroy()
    finally:
        root.destroy()


def test_lock_layout_makes_left_drag_noop():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile, moves = _move_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        tile.set_lock_layout(True)
        n = len(w32.placed)
        tile._on_strip_b1_press(_Evt(x_root=10, y_root=10))
        tile._on_strip_b1_motion(_Evt(x_root=200, y_root=200))   # far drag
        tile._on_strip_b1_release(_Evt(x_root=200, y_root=200))
        assert len(w32.placed) == n        # no placement while locked
        assert moves == []                 # no move committed
        tile.destroy()
    finally:
        root.destroy()


def test_lock_layout_makes_right_drag_noop():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile, moves = _move_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        tile.set_lock_layout(True)
        n = len(w32.placed)
        tile._on_b3_press(_Evt(state=0, x_root=10, y_root=10))
        tile._on_b3_motion(_Evt(state=0, x_root=200, y_root=200))
        tile._on_b3_release(_Evt(state=0, x_root=200, y_root=200))
        assert len(w32.placed) == n        # right-drag move suppressed while locked
        assert moves == []
        tile.destroy()
    finally:
        root.destroy()


def test_lock_layout_via_constructor_param():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile = pt.TileWindow(
            root, "kirejen", PALETTE, win32=w32, dwm=FakeDwm(),
            on_activate=lambda k: None, on_minimize=lambda k: None,
            on_move_end=lambda k, x, y: None,
            on_resize_end=lambda k, w, h: None, lock_layout=True)
        tile.place(100, 200, 384, 216)
        n = len(w32.placed)
        tile._on_strip_b1_press(_Evt(x_root=10, y_root=10))
        tile._on_strip_b1_motion(_Evt(x_root=200, y_root=200))
        assert len(w32.placed) == n        # locked from construction → no move
        tile.destroy()
    finally:
        root.destroy()


def test_right_drag_still_moves_when_unlocked():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile, moves = _move_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        hwnd = w32.get_root_hwnd(tile.top.winfo_id())
        tile._on_b3_press(_Evt(state=0, x_root=10, y_root=10))
        tile._on_b3_motion(_Evt(state=0, x_root=70, y_root=50))
        assert w32.placed[-1] == (hwnd, 160, 240, 384, 216 + pt.STRIP_H)
        tile._on_b3_release(_Evt(state=0, x_root=70, y_root=50))
        assert moves[-1] == ("kirejen", 160, 240)
        tile.destroy()
    finally:
        root.destroy()


# ── corner-hover resize (SE/NW/NE/SW) + cursor feedback ───────────────────────
def _resize_tile(root, w32=None):
    """Tile with a recording on_resize_end callback for corner-resize tests."""
    resizes = []
    tile = pt.TileWindow(
        root, "kirejen", PALETTE, win32=w32 or FakeTileWin32(), dwm=FakeDwm(),
        on_activate=lambda k: None, on_minimize=lambda k: None,
        on_move_end=lambda k, x, y: None,
        on_resize_end=lambda k, w, h: resizes.append((k, w, h)))
    return tile, resizes


def test_corner_hover_arms_se_and_sets_nwse_cursor():
    root = _root()
    try:
        tile, _ = _resize_tile(root)
        tile.place(100, 200, 384, 216)
        # pointer near the bottom-right (SE) corner of the tile window
        # window spans x:100..484, y:200..200+216+STRIP_H
        h = 216 + pt.STRIP_H
        tile._on_corner_motion(_Evt(x_root=100 + 384 - 3, y_root=200 + h - 3))
        assert tile._corner == "se"
        assert str(tile.top.cget("cursor")) == "size_nw_se"
        tile.destroy()
    finally:
        root.destroy()


def test_corner_hover_arms_ne_and_sets_nesw_cursor():
    root = _root()
    try:
        tile, _ = _resize_tile(root)
        tile.place(100, 200, 384, 216)
        # top-right corner (NE) → size_ne_sw
        tile._on_corner_motion(_Evt(x_root=100 + 384 - 2, y_root=200 + 2))
        assert tile._corner == "ne"
        assert str(tile.top.cget("cursor")) == "size_ne_sw"
        tile.destroy()
    finally:
        root.destroy()


def test_moving_off_corner_restores_normal_cursor():
    root = _root()
    try:
        tile, _ = _resize_tile(root)
        tile.place(100, 200, 384, 216)
        tile._on_corner_motion(_Evt(x_root=100 + 384 - 3, y_root=200 + 3))  # NE
        assert tile._corner == "ne"
        # move into the middle → no corner armed, cursor back to normal
        tile._on_corner_motion(_Evt(x_root=100 + 190, y_root=200 + 100))
        assert tile._corner is None
        assert str(tile.top.cget("cursor")) in ("", "arrow")
        tile.destroy()
    finally:
        root.destroy()


def test_se_drag_grows_wh_with_nw_anchored():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile, resizes = _resize_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        hwnd = w32.get_root_hwnd(tile.top.winfo_id())
        h = 216 + pt.STRIP_H
        # arm SE, press there, drag +40,+30, release
        tile._on_corner_motion(_Evt(x_root=100 + 384 - 3, y_root=200 + h - 3))
        tile._corner_press(_Evt(x_root=100 + 384 - 3, y_root=200 + h - 3))
        tile._corner_motion(_Evt(x_root=100 + 384 - 3 + 40, y_root=200 + h - 3 + 30))
        # NW corner anchored → x,y unchanged; w/h grown by the drag delta
        last = w32.placed[-1]
        assert last[0] == hwnd
        assert last[1:3] == (100, 200)               # NW anchor fixed
        assert last[3] == 384 + 40                   # width grew
        assert last[4] == (216 + 30) + pt.STRIP_H    # body_h grew, +STRIP_H window
        tile._corner_release(_Evt(x_root=100 + 384 - 3 + 40, y_root=200 + h - 3 + 30))
        assert resizes[-1] == ("kirejen", 384 + 40, 216 + 30)
        tile.destroy()
    finally:
        root.destroy()


def test_nw_drag_keeps_se_anchored_and_shrinks():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile, resizes = _resize_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        hwnd = w32.get_root_hwnd(tile.top.winfo_id())
        # arm NW at the top-left corner, drag +20,+10 (inward → shrink)
        tile._on_corner_motion(_Evt(x_root=100 + 2, y_root=200 + 2))
        assert tile._corner == "nw"
        tile._corner_press(_Evt(x_root=100 + 2, y_root=200 + 2))
        tile._corner_motion(_Evt(x_root=100 + 22, y_root=200 + 12))
        last = w32.placed[-1]
        assert last[0] == hwnd
        # SE corner fixed at (100+384, 200+216+STRIP_H); dragging NW in by (20,10)
        # → x,y shift by +20,+10 and w/h shrink by the same.
        assert last[1:3] == (120, 210)
        assert last[3] == 384 - 20
        assert last[4] == (216 - 10) + pt.STRIP_H
        tile._corner_release(_Evt(x_root=100 + 22, y_root=200 + 12))
        assert resizes[-1] == ("kirejen", 384 - 20, 216 - 10)
        tile.destroy()
    finally:
        root.destroy()


def test_corner_resize_clamps_to_min_size():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile, resizes = _resize_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        h = 216 + pt.STRIP_H
        # SE drag far to the top-left → would go negative; clamps at min
        tile._on_corner_motion(_Evt(x_root=100 + 384 - 3, y_root=200 + h - 3))
        tile._corner_press(_Evt(x_root=100 + 384 - 3, y_root=200 + h - 3))
        tile._corner_motion(_Evt(x_root=100 - 500, y_root=200 - 500))
        last = w32.placed[-1]
        assert last[3] == 120                         # min width
        assert last[4] == 68 + pt.STRIP_H             # min body height
        tile._corner_release(_Evt(x_root=100 - 500, y_root=200 - 500))
        assert resizes[-1] == ("kirejen", 120, 68)
        tile.destroy()
    finally:
        root.destroy()


def test_lock_layout_blocks_corner_resize_and_cursor_stays_normal():
    root = _root()
    try:
        w32 = FakeTileWin32()
        tile, resizes = _resize_tile(root, w32=w32)
        tile.place(100, 200, 384, 216)
        tile.set_lock_layout(True)
        n = len(w32.placed)
        h = 216 + pt.STRIP_H
        tile._on_corner_motion(_Evt(x_root=100 + 384 - 3, y_root=200 + h - 3))
        assert tile._corner is None                   # never arms while locked
        assert str(tile.top.cget("cursor")) in ("", "arrow")
        tile._corner_press(_Evt(x_root=100 + 384 - 3, y_root=200 + h - 3))
        tile._corner_motion(_Evt(x_root=100 + 500, y_root=200 + 500))
        tile._corner_release(_Evt(x_root=100 + 500, y_root=200 + 500))
        assert len(w32.placed) == n                   # no resize while locked
        assert resizes == []
        tile.destroy()
    finally:
        root.destroy()


def test_corner_resize_suppresses_strip_move_and_activate():
    root = _root()
    try:
        w32 = FakeTileWin32()
        moved, activated = [], []
        tile = pt.TileWindow(
            root, "kirejen", PALETTE, win32=w32, dwm=FakeDwm(),
            on_activate=lambda k: activated.append(k),
            on_minimize=lambda k: None,
            on_move_end=lambda k, x, y: moved.append((x, y)),
            on_resize_end=lambda k, w, h: None)
        tile.place(100, 200, 384, 216)
        h = 216 + pt.STRIP_H
        # arm SE, then run the strip press/motion/release path — a corner gesture
        # must NOT move the tile nor activate on release.
        tile._on_corner_motion(_Evt(x_root=100 + 384 - 3, y_root=200 + h - 3))
        tile._on_strip_b1_press(_Evt(x_root=100 + 384 - 3, y_root=200 + h - 3))
        tile._on_strip_b1_motion(_Evt(x_root=100 + 384 - 3 + 40, y_root=200 + h - 3 + 30))
        tile._on_strip_b1_release(_Evt(x_root=100 + 384 - 3 + 40, y_root=200 + h - 3 + 30))
        assert moved == []
        assert activated == []
        tile.destroy()
    finally:
        root.destroy()
