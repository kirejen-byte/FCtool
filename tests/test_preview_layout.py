import preview_layout as pl


def test_clamp_pulls_offscreen_rect_back_inside():
    bounds = (0, 0, 1920, 1080)
    assert pl.clamp_rect((2000, 500, 200, 100), bounds) == (1720, 500, 200, 100)
    assert pl.clamp_rect((-50, -50, 200, 100), bounds) == (0, 0, 200, 100)


def test_snap_to_grid_rounds_position():
    assert pl.snap_to_grid(147, 273, 100, 50) == (100, 250)
    assert pl.snap_to_grid(151, 274, 100, 50) == (200, 250)


def test_snap_to_edges_uses_eveo_threshold():
    # threshold = max(20, w // 10) — EVE-O parity (ThumbnailManager.cs:831)
    others = [(500, 100, 384, 236)]
    x, y = pl.snap_to_edges((870, 108, 384, 236), others)
    assert (x, y) == (884, 100)  # left edge → other's right edge, top aligned
    assert pl.snap_to_edges((1400, 800, 384, 236), others) == (1400, 800)  # too far


def test_grid_arrange_lays_out_row_major_with_margin():
    rects = pl.grid_arrange(5, tile_w=300, tile_h=200, bounds=(0, 0, 1000, 900),
                            origin=(10, 10), gap=8)
    assert rects[0] == (10, 10, 300, 200)
    assert rects[1] == (318, 10, 300, 200)
    assert rects[2] == (626, 10, 300, 200)
    assert rects[3] == (10, 218, 300, 200)   # wrapped — 3 per row fits in 1000


def test_login_stack_offsets():
    assert pl.login_stack_pos(0, (5, 5)) == (5, 5)
    assert pl.login_stack_pos(2, (5, 5)) == (53, 53)  # +24px per index


def test_cycle_next_and_prev_wrap_and_skip_missing():
    order = ["a", "b", "c", "d"]
    live = {"a", "c", "d"}
    assert pl.cycle_next(order, "a", live, +1) == "c"   # b not live → skipped
    assert pl.cycle_next(order, "d", live, +1) == "a"   # wraps
    assert pl.cycle_next(order, "a", live, -1) == "d"
    assert pl.cycle_next([], "x", {"y"}, +1) == "y"     # empty order → live sorted
    assert pl.cycle_next(order, "zz", live, +1) == "a"  # unknown current → first live


def test_zoom_rect_nw_holds_top_left():
    # nw anchor: top-left corner is fixed; grows right + down only.
    assert pl.zoom_rect((100, 200, 300, 200), 2.0, "nw") == (100, 200, 600, 400)


def test_zoom_rect_center_holds_center():
    # c anchor: center fixed; grows symmetrically. 300->600 (+300), 200->400 (+200).
    assert pl.zoom_rect((100, 200, 300, 200), 2.0, "c") == (-50, 100, 600, 400)


def test_zoom_rect_se_holds_bottom_right():
    # se anchor: bottom-right fixed; grows left + up.
    assert pl.zoom_rect((100, 200, 300, 200), 2.0, "se") == (-200, 0, 600, 400)


def test_zoom_rect_ne_holds_top_right():
    assert pl.zoom_rect((100, 200, 300, 200), 2.0, "ne") == (-200, 200, 600, 400)


def test_zoom_rect_sw_holds_bottom_left():
    assert pl.zoom_rect((100, 200, 300, 200), 2.0, "sw") == (100, 0, 600, 400)


def test_zoom_rect_edge_anchors_center_the_free_axis():
    # n: top fixed, x centered.  s: bottom fixed, x centered.
    assert pl.zoom_rect((100, 200, 300, 200), 2.0, "n") == (-50, 200, 600, 400)
    assert pl.zoom_rect((100, 200, 300, 200), 2.0, "s") == (-50, 0, 600, 400)
    # w: left fixed, y centered.  e: right fixed, y centered.
    assert pl.zoom_rect((100, 200, 300, 200), 2.0, "w") == (100, 100, 600, 400)
    assert pl.zoom_rect((100, 200, 300, 200), 2.0, "e") == (-200, 100, 600, 400)


def test_zoom_rect_rounds_and_defaults_unknown_anchor_to_nw():
    # factor 1.5 → 300*1.5=450, 200*1.5=300 (both exact).
    assert pl.zoom_rect((0, 0, 300, 200), 1.5, "nw") == (0, 0, 450, 300)
    # unknown anchor falls back to nw (never crashes).
    assert pl.zoom_rect((10, 20, 100, 100), 2.0, "??") == (10, 20, 200, 200)
    # factor <= 1 is a no-op (nothing to zoom).
    assert pl.zoom_rect((10, 20, 100, 100), 1.0, "c") == (10, 20, 100, 100)
    assert pl.zoom_rect((10, 20, 100, 100), 0.5, "c") == (10, 20, 100, 100)
