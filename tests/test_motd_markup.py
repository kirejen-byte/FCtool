"""Tests for motd_markup: parsing EVE rich-text markup into styled segments.

EVE in-game markup is NOT valid XML, so the parser uses regex/string ops and is
tolerant of malformed/unclosed tags (it must never raise). These tests pin the
Segment shape, the style-stack composition of nested tags, the link/newline
handling, and the colour helpers (including their round-trip).
"""
from motd_markup import (
    Segment,
    parse_markup,
    eve_color_to_hex,
    hex_to_eve_color,
    segments_to_markup,
)


# --- Segment dataclass defaults -------------------------------------------

def test_segment_defaults():
    seg = Segment()
    assert seg.text == ""
    assert seg.color is None
    assert seg.bold is False
    assert seg.italic is False
    assert seg.underline is False
    assert seg.size is None
    assert seg.link is None
    assert seg.newline is False


# --- plain text -----------------------------------------------------------

def test_plain_text_single_segment_all_defaults():
    segs = parse_markup("hello world")
    assert len(segs) == 1
    seg = segs[0]
    assert seg.text == "hello world"
    assert seg.color is None
    assert seg.bold is False
    assert seg.italic is False
    assert seg.underline is False
    assert seg.size is None
    assert seg.link is None
    assert seg.newline is False


def test_empty_string_yields_no_segments():
    assert parse_markup("") == []


# --- line breaks ----------------------------------------------------------

def test_br_emits_newline_segment_between_text():
    segs = parse_markup("a<br>b")
    assert len(segs) == 3
    assert segs[0].text == "a" and segs[0].newline is False
    assert segs[1].newline is True and segs[1].text == ""
    assert segs[2].text == "b" and segs[2].newline is False


def test_br_self_closing_form():
    segs = parse_markup("a<br/>b")
    assert len(segs) == 3
    assert segs[1].newline is True


def test_multiple_br_emit_multiple_newlines():
    segs = parse_markup("a<br><br>b")
    newline_count = sum(1 for s in segs if s.newline)
    assert newline_count == 2


# --- bold / italic / underline --------------------------------------------

def test_bold_then_normal():
    segs = parse_markup("<b>bold</b> normal")
    assert len(segs) == 2
    assert segs[0].text == "bold" and segs[0].bold is True
    assert segs[1].text == " normal" and segs[1].bold is False


def test_italic_and_underline():
    segs = parse_markup("<i>it</i><u>un</u>")
    assert len(segs) == 2
    assert segs[0].text == "it" and segs[0].italic is True and segs[0].underline is False
    assert segs[1].text == "un" and segs[1].underline is True and segs[1].italic is False


# --- nested style composition (style stack) -------------------------------

def test_nested_color_bold_compose():
    segs = parse_markup("<color=0xffff0000><b>x</b>y</color>z")
    assert len(segs) == 3
    x, y, z = segs
    assert x.text == "x" and x.color == "#ff0000" and x.bold is True
    assert y.text == "y" and y.color == "#ff0000" and y.bold is False
    assert z.text == "z" and z.color is None and z.bold is False


# --- font size ------------------------------------------------------------

def test_font_size_attribute_form():
    segs = parse_markup("<font size=18>big</font>")
    assert len(segs) == 1
    assert segs[0].text == "big" and segs[0].size == 18


def test_fontsize_tag_form():
    segs = parse_markup("<fontsize=14>med</fontsize>")
    assert len(segs) == 1
    assert segs[0].text == "med" and segs[0].size == 14


# --- links ----------------------------------------------------------------

def test_url_fitting_link():
    segs = parse_markup("<url=fitting:670::>Pod</url>")
    assert len(segs) == 1
    assert segs[0].text == "Pod"
    assert segs[0].link == "fitting:670::"


def test_url_showinfo_link():
    segs = parse_markup("<url=showinfo:5//30000142>Jita</url>")
    assert len(segs) == 1
    assert segs[0].text == "Jita"
    assert segs[0].link == "showinfo:5//30000142"


def test_link_then_plain_text_resets_link():
    segs = parse_markup("<url=fitting:670::>Pod</url> next")
    assert len(segs) == 2
    assert segs[0].link == "fitting:670::"
    assert segs[1].text == " next" and segs[1].link is None


# --- malformed / unclosed (tolerance) -------------------------------------

def test_unclosed_color_persists_to_end():
    segs = parse_markup("<color=0xff00ff00>green forever")
    assert len(segs) == 1
    assert segs[0].text == "green forever"
    assert segs[0].color == "#00ff00"


def test_unknown_tag_stripped_inner_kept():
    segs = parse_markup("<foo>bar</foo>")
    assert len(segs) == 1
    assert segs[0].text == "bar"
    assert segs[0].color is None and segs[0].bold is False


def test_loc_tag_strips_keeps_inner():
    segs = parse_markup("<loc>Translated</loc>")
    assert len(segs) == 1
    assert segs[0].text == "Translated"


def test_stray_closing_tag_does_not_raise():
    # A closing tag with no matching open must be tolerated (no underflow).
    segs = parse_markup("text</b>more")
    assert "".join(s.text for s in segs) == "textmore"


def test_adjacent_identical_style_merges():
    # Two color spans of the same colour, back to back, merge into one segment.
    segs = parse_markup("<color=0xffff0000>a</color><color=0xffff0000>b</color>")
    assert len(segs) == 1
    assert segs[0].text == "ab" and segs[0].color == "#ff0000"


def test_six_hex_color_in_markup():
    segs = parse_markup("<color=0x00ff00>g</color>")
    assert len(segs) == 1
    assert segs[0].color == "#00ff00"


# --- colour helpers -------------------------------------------------------

def test_eve_color_to_hex_argb():
    assert eve_color_to_hex("0xffff0000") == "#ff0000"


def test_eve_color_to_hex_six_hex():
    assert eve_color_to_hex("0x00ff00") == "#00ff00"


def test_eve_color_to_hex_without_prefix():
    assert eve_color_to_hex("ffff0000") == "#ff0000"
    assert eve_color_to_hex("00ff00") == "#00ff00"


def test_eve_color_to_hex_bad_input_is_none():
    assert eve_color_to_hex("not-a-color") is None
    assert eve_color_to_hex("") is None
    assert eve_color_to_hex("0xZZ") is None
    assert eve_color_to_hex(None) is None  # type: ignore[arg-type]


def test_hex_to_eve_color():
    assert hex_to_eve_color("#ff0000") == "0xffFF0000"


def test_hex_to_eve_color_custom_alpha():
    assert hex_to_eve_color("#00ff00", alpha="80") == "0x8000FF00"


def test_color_round_trip():
    assert eve_color_to_hex(hex_to_eve_color("#ff0000")) == "#ff0000"
    assert eve_color_to_hex(hex_to_eve_color("#1a2b3c")) == "#1a2b3c"


# --- segments_to_markup round-trip ----------------------------------------

def test_segments_to_markup_round_trip_styles():
    src = "<color=0xffff0000><b>x</b>y</color>z"
    segs = parse_markup(src)
    out = segments_to_markup(segs)
    # Re-parsing the regenerated markup yields equivalent segments.
    assert parse_markup(out) == segs


def test_segments_to_markup_round_trip_link_and_break():
    src = "<url=fitting:670::>Pod</url><br>plain"
    segs = parse_markup(src)
    assert parse_markup(segments_to_markup(segs)) == segs
