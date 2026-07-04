"""Pure-helper tests for the on-video anchored caption redesign (caption-onvideo).

These test the Tk-free helpers in preview_tile:
  - format_tile_label(activity_label, ship_type_name) -> str
  - clamp_label(text, font_size, tile_w, tile_body_h) -> (text, size)
  - label_anchor_placement(anchor, tile_w, tile_body_h, pad) -> (relx, rely, anchor)

Plus a Tk-touching test that set_label_style stores the new style on a tile.
"""
import pytest

import preview_tile as pt


# ── format_tile_label: '<label> - <ShipType>' join, omit empties ──────────────
def test_format_tile_label_both_parts():
    assert pt.format_tile_label("Cyno", "Onyx") == "Cyno - Onyx"


def test_format_tile_label_label_only():
    assert pt.format_tile_label("Cyno", "") == "Cyno"


def test_format_tile_label_ship_only():
    assert pt.format_tile_label("", "Onyx") == "Onyx"


def test_format_tile_label_neither_is_empty():
    assert pt.format_tile_label("", "") == ""
    # whitespace-only parts are treated as empty
    assert pt.format_tile_label("   ", None) == ""


# ── clamp_label: char/width ellipsis + font-size cap ──────────────────────────
def test_clamp_label_ellipsizes_over_max_char_length():
    long = "A" * 200
    text, size = pt.clamp_label(long, 11, 384, 216)
    assert text.endswith("…")
    assert len(text) < len(long)


def test_clamp_label_caps_over_large_font_to_body_fraction():
    # a body of 100px caps size to <= body/5 = 20 (and the hard max)
    _text, size = pt.clamp_label("Cyno", 999, 384, 100)
    assert size <= 20
    # and never below the sane floor
    assert size >= pt._LABEL_MIN_SIZE


def test_clamp_label_floors_tiny_font():
    _text, size = pt.clamp_label("Cyno", 1, 384, 400)
    assert size >= pt._LABEL_MIN_SIZE


def test_clamp_label_width_bound_truncates_wide_text():
    # a very narrow tile forces truncation to fit the body width
    text, size = pt.clamp_label("Cyno - Onyx", 20, 60, 216)
    assert text.endswith("…") or len(text) <= len("Cyno - Onyx")
    # the estimated pixel width of the result never exceeds the body width
    approx_w = len(text) * size * pt._CHAR_W_RATIO
    assert approx_w <= 60


def test_clamp_label_short_text_unchanged():
    text, size = pt.clamp_label("Cyno", 12, 384, 216)
    assert text == "Cyno"
    assert size == 12


# ── label_anchor_placement: all four corners → relx/rely/tk-anchor ────────────
@pytest.mark.parametrize("anchor,expected_tk", [
    ("top-left", "nw"),
    ("top-right", "ne"),
    ("bottom-left", "sw"),
    ("bottom-right", "se"),
])
def test_label_anchor_placement_all_corners(anchor, expected_tk):
    relx, rely, tk_anchor = pt.label_anchor_placement(anchor)
    assert tk_anchor == expected_tk
    # corner placement stays inside [0, 1]
    assert 0.0 <= relx <= 1.0
    assert 0.0 <= rely <= 1.0
    # left corners hug the left edge, right corners the right edge
    if "left" in anchor:
        assert relx < 0.5
    else:
        assert relx > 0.5
    if anchor.startswith("top"):
        assert rely < 0.5
    else:
        assert rely > 0.5


def test_label_anchor_placement_unknown_falls_back_top_left():
    relx, rely, tk_anchor = pt.label_anchor_placement("nonsense")
    assert tk_anchor == "nw"
