"""Pure, Tk-free parsing of EVE in-game rich-text markup into styled segments.

EVE's MOTD / chat markup is rich text built from a small set of tag-like
constructs that are *not* valid XML (attributes are unquoted, tags may be
unclosed, casing is loose). This module turns such markup into a flat list of
:class:`Segment` objects — each a maximal run of text under one constant style —
so a renderer (the MOTD preview) or a WYSIWYG editor can walk segments without
re-implementing the parse. It has no Tkinter, no network, and no dependency on
other project modules; it works purely on strings and is safe from any thread.

The parser is deliberately *tolerant*: it never raises on malformed input.
Unclosed tags simply let their style persist to the end; stray closing tags are
ignored; unknown tags are stripped with their inner text kept as plain.

Supported markup subset (mirrors what :mod:`motd_builder` emits, plus the
formatting tags the in-game editor produces):

    Colour        <color=0xAARRGGBB>…</color>  (ARGB; alpha ignored for display)
                  <color=0xRRGGBB>…</color>    (6-hex tolerated)
                  <fontcolor=…>…</fontcolor>   (treated as colour)
    Bold/Italic   <b>…</b>  <i>…</i>
    Underline     <u>…</u>   (and <s>…</s> strike → treated as plain)
    Font size     <font size=14>…</font>  or  <fontsize=14>…</fontsize>
    Line break    <br>  (and <br/>)
    Link          <url=TARGET>visible</url>  (fitting:/showinfo:/joinChannel:…)
    Localised     <loc>…</loc>  → inner text kept, tag stripped
    Anything else → stripped, inner text kept as plain.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Segment:
    """One maximal run of text rendered under a single constant style.

    A *newline* segment has :attr:`newline` ``True`` and empty :attr:`text`; a
    renderer should emit a line break for it and ignore its other fields. All
    other segments carry visible :attr:`text` plus the active style:

    * :attr:`color` — a lowercase ``"#rrggbb"`` hex string, or ``None``.
    * :attr:`bold`, :attr:`italic`, :attr:`underline` — formatting flags.
    * :attr:`size` — font size as an ``int``, or ``None`` for the default.
    * :attr:`link` — the raw ``url=`` target (e.g. ``"fitting:670::"``), or
      ``None`` when the run is not part of a link.
    """

    text: str = ""
    color: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    size: int | None = None
    link: str | None = None
    newline: bool = False


# --- colour helpers -------------------------------------------------------

# A run of 6 or 8 hex digits, optionally prefixed with "0x"/"0X" or "#".
_HEX_COLOR_RE = re.compile(r"^(?:0x|0X|#)?([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def eve_color_to_hex(eve: str) -> str | None:
    """Convert an EVE colour token to a CSS-style ``"#rrggbb"`` (lowercase).

    Accepts ``"0xAARRGGBB"``/``"0xRRGGBB"``/``"AARRGGBB"``/``"RRGGBB"`` and the
    ``"#"``-prefixed forms. For an 8-digit ARGB value the leading alpha pair is
    dropped (alpha is ignored for display). Returns ``None`` for anything that is
    not a clean 6- or 8-digit hex colour (including ``None`` or empty input).

    Examples::

        eve_color_to_hex("0xffff0000") -> "#ff0000"
        eve_color_to_hex("0x00ff00")   -> "#00ff00"
        eve_color_to_hex("garbage")    -> None
    """
    if not eve:
        return None
    m = _HEX_COLOR_RE.match(eve.strip())
    if m is None:
        return None
    digits = m.group(1).lower()
    if len(digits) == 8:  # AARRGGBB → drop the alpha pair.
        digits = digits[2:]
    return "#" + digits


def hex_to_eve_color(hex_str: str, alpha: str = "ff") -> str:
    """Convert a ``"#rrggbb"`` colour to an EVE ``"0xAARRGGBB"`` token.

    The RGB part is upper-cased and prefixed with the given ``alpha`` pair
    (default ``"ff"``, fully opaque). The inverse of :func:`eve_color_to_hex`
    for the colour value (alpha is synthesised here, dropped there)::

        hex_to_eve_color("#ff0000")            -> "0xffFF0000"
        hex_to_eve_color("#00ff00", alpha="80") -> "0x8000FF00"
    """
    rgb = hex_str.lstrip("#").upper()
    return f"0x{alpha}{rgb}"


# --- tokenizer ------------------------------------------------------------

# One token: either a tag "<...>" (captured raw, sans angle brackets) or a run of
# non-"<" text. Greedy up to the first ">" keeps malformed input from swallowing
# the rest of the string. Text runs never contain "<".
_TOKEN_RE = re.compile(r"<([^>]*)>|([^<]+)")

# Parsed tag forms (matched case-insensitively against the inner tag text).
_COLOR_OPEN_RE = re.compile(r"^(?:font)?color\s*=\s*(.+)$", re.IGNORECASE)
_FONT_SIZE_RE = re.compile(r"^font\s+size\s*=\s*(\d+)\s*$", re.IGNORECASE)
_FONTSIZE_RE = re.compile(r"^fontsize\s*=\s*(\d+)\s*$", re.IGNORECASE)
_URL_OPEN_RE = re.compile(r"^url\s*=\s*(.+)$", re.IGNORECASE)


def _tag_kind(inner: str):
    """Classify a raw tag body into ``(kind, payload)`` or ``None`` to ignore.

    ``kind`` is one of: ``"open"`` (style push, payload is a partial Segment-style
    dict), ``"close"`` (style pop, payload is the style key to pop), ``"br"``
    (line break, no payload), or ``None`` for tags we strip without effect
    (unknown tags, ``<loc>``, ``<s>`` strike, stray whitespace).
    """
    body = inner.strip()
    if not body:
        return None

    # Closing tag: "</name>".
    if body.startswith("/"):
        name = body[1:].strip().lower()
        # Normalise the few multi-attr close tags to their style key.
        if name in ("color", "fontcolor"):
            return ("close", "color")
        if name in ("font", "fontsize"):
            return ("close", "size")
        if name == "url":
            return ("close", "link")
        if name in ("b", "i", "u"):
            return ("close", {"b": "bold", "i": "italic", "u": "underline"}[name])
        # Unknown / strike / loc close → no effect.
        return None

    # Line break (with or without a trailing slash).
    low = body.lower().rstrip("/").strip()
    if low == "br":
        return ("br", None)

    # Colour / fontcolor (must be checked before bare letters).
    m = _COLOR_OPEN_RE.match(body)
    if m is not None:
        hexcolor = eve_color_to_hex(m.group(1).strip())
        # Even an unparseable colour pushes a (no-op) frame so its matching
        # close tag balances correctly; color stays None in that case.
        return ("open", {"color": hexcolor}, "color")

    # Font size, either spelling.
    m = _FONT_SIZE_RE.match(body) or _FONTSIZE_RE.match(body)
    if m is not None:
        return ("open", {"size": int(m.group(1))}, "size")

    # Link.
    m = _URL_OPEN_RE.match(body)
    if m is not None:
        return ("open", {"link": m.group(1).strip()}, "link")

    # Simple formatting tags.
    name = body.lower()
    if name == "b":
        return ("open", {"bold": True}, "bold")
    if name == "i":
        return ("open", {"italic": True}, "italic")
    if name == "u":
        return ("open", {"underline": True}, "underline")

    # Unknown tag (incl. <s>, <loc>, attributes we do not model) → strip, no
    # style change; the inner text is kept by virtue of being its own token.
    return None


def _active_style(stack: list[tuple[str, dict]]) -> dict:
    """Collapse the open-tag ``stack`` into the currently active style dict.

    Later frames override earlier ones for the same key (so a nested ``<color>``
    wins), giving the composed style for the text run that follows.
    """
    style: dict = {
        "color": None,
        "bold": False,
        "italic": False,
        "underline": False,
        "size": None,
        "link": None,
    }
    for _key, attrs in stack:
        for k, v in attrs.items():
            style[k] = v
    return style


def parse_markup(markup: str) -> list[Segment]:
    """Parse EVE rich-text ``markup`` into a flat list of :class:`Segment`.

    The string is tokenized into tags and text runs. A style stack tracks open
    tags so nested formatting composes (e.g.
    ``<color=0xffff0000><b>x</b>y</color>`` → ``x`` is red+bold, ``y`` red).
    Each closing tag pops the most recent matching open frame; unclosed tags let
    their style persist to the end; stray closing tags and unknown tags are
    ignored (their inner text is still kept as plain). ``<br>`` emits a dedicated
    ``Segment(newline=True)``. Adjacent runs with identical style are merged into
    a single segment. Never raises.
    """
    if not markup:
        return []

    stack: list[tuple[str, dict]] = []
    segments: list[Segment] = []

    def _emit_text(text: str) -> None:
        if not text:
            return
        style = _active_style(stack)
        seg = Segment(text=text, **style)
        # Merge with the previous segment when it carries the same style (and is
        # not a newline marker).
        if segments and not segments[-1].newline and _same_style(segments[-1], seg):
            segments[-1].text += text
        else:
            segments.append(seg)

    for tag, text in _TOKEN_RE.findall(markup):
        if text:  # A plain-text run.
            _emit_text(text)
            continue

        kind = _tag_kind(tag)
        if kind is None:
            continue
        if kind[0] == "br":
            segments.append(Segment(newline=True))
        elif kind[0] == "open":
            _, attrs, key = kind
            stack.append((key, attrs))
        elif kind[0] == "close":
            key = kind[1]
            # Pop the nearest matching open frame; tolerate no match.
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == key:
                    del stack[i]
                    break

    return segments


def _same_style(a: Segment, b: Segment) -> bool:
    """True when two non-newline segments share every style attribute."""
    return (
        a.color == b.color
        and a.bold == b.bold
        and a.italic == b.italic
        and a.underline == b.underline
        and a.size == b.size
        and a.link == b.link
    )


# --- serialisation (round-trip) -------------------------------------------

def segments_to_markup(segments: list[Segment]) -> str:
    """Serialise ``segments`` back into EVE markup (inverse of parse_markup).

    Each segment is wrapped in the tags implied by its style — link, colour,
    size, then bold/italic/underline (closed in reverse) — so that re-parsing the
    output yields an equivalent segment list. Newline segments become ``<br>``.
    The exact tag *text* need not match the original input byte-for-byte; only
    the parsed result round-trips.
    """
    parts: list[str] = []
    for seg in segments:
        if seg.newline:
            parts.append("<br>")
            continue

        open_tags: list[str] = []
        close_tags: list[str] = []

        if seg.link is not None:
            open_tags.append(f"<url={seg.link}>")
            close_tags.append("</url>")
        if seg.color is not None:
            open_tags.append(f"<color={hex_to_eve_color(seg.color)}>")
            close_tags.append("</color>")
        if seg.size is not None:
            open_tags.append(f"<fontsize={seg.size}>")
            close_tags.append("</fontsize>")
        if seg.bold:
            open_tags.append("<b>")
            close_tags.append("</b>")
        if seg.italic:
            open_tags.append("<i>")
            close_tags.append("</i>")
        if seg.underline:
            open_tags.append("<u>")
            close_tags.append("</u>")

        parts.append("".join(open_tags) + seg.text + "".join(reversed(close_tags)))

    return "".join(parts)
