"""Shared display helpers for EVE overview preset/tab-name markup.

Overview preset and tab names carry EVE rich-text markup — almost always a
single ``<color=0xAARRGGBB>…</color>`` span wrapping a glyph-prefixed label
(e.g. ``"<color=0xFFFF6666>✜ --- PvX: Basic (+Neut +NPC)</color>"``), sometimes
with ``<b>``/``<i>``/``<u>``. The pack model stores that RAW markup string as
the single source of truth everywhere; these helpers exist purely so the
*display* layer (the pack editor's preset list, its name previews, the tab-name
previews and the overview/bracket comboboxes) can show a name the way the game
does: colored, styled, tags stripped, glyphs intact.

Parsing is delegated to :mod:`motd_markup` — the project's canonical, Tk-free,
tolerant EVE-markup parser (handles nesting, unclosed tags, unknown tags
stripped; never raises). It is the same module :mod:`markup_editor` uses and it
carries no MOTD-specific coupling, so overview reuses it rather than
duplicating a parser. This module only adapts the parsed segments into the
shapes the overview UIs need and renders them into a ``tk.Text``.

The query helpers (:func:`parse_spans`, :func:`display_text`,
:func:`primary_color`, :func:`disambiguate`) are pure and import no Tkinter;
:func:`render_into_text` imports Tkinter lazily so the query helpers stay usable
in a headless context (and so importing this module never needs a display).
"""
from __future__ import annotations

import motd_markup


def parse_spans(s):
    """Parse overview-name markup ``s`` into ``[(text, style), …]`` spans.

    ``style`` is a dict with keys ``color`` (a lowercase ``"#rrggbb"`` string or
    ``None``), ``bold``, ``italic`` and ``underline`` (bools). Adjacent runs of
    identical style are already merged by the parser. A ``<br>`` — vanishingly
    rare inside a name — contributes a literal ``"\\n"`` span so nothing is
    dropped. ``size`` / ``url`` markup keeps its visible text but is not
    reflected in the style dict (overview names never carry them meaningfully).
    Never raises (delegates to the tolerant :func:`motd_markup.parse_markup`).
    """
    spans = []
    for seg in motd_markup.parse_markup(s or ""):
        text = "\n" if seg.newline else seg.text
        if not text:
            continue
        spans.append((text, {
            "color": seg.color,
            "bold": bool(seg.bold),
            "italic": bool(seg.italic),
            "underline": bool(seg.underline),
        }))
    return spans


def display_text(s):
    """Return ``s`` with every markup tag stripped, glyphs/text kept verbatim.

    ``"<color=0xFFFF6666>✜ PvX: FW</color>"`` → ``"✜ PvX: FW"``. A plain name
    passes through unchanged; ``None`` and ``""`` yield ``""``. Never raises."""
    return "".join(text for text, _style in parse_spans(s))


def primary_color(s):
    """Return the first/outermost color of ``s`` as ``"#rrggbb"``, or ``None``.

    Used to tint a preset-list row or a name label with the pack author's chosen
    color. The first colored span wins (overview names are, in practice, a
    single outer color span). Never raises."""
    for _text, style in parse_spans(s):
        if style["color"]:
            return style["color"]
    return None


def disambiguate(names):
    """Map each raw name to a UNIQUE display string.

    :func:`display_text` collapses different raws (e.g. two identically-labeled
    presets in different colors) to the same visible text — but a combobox whose
    options are display strings must be able to round-trip each option back to
    exactly one raw name. This assigns the first occurrence of a display its
    bare form and each later collision a deterministic ``" (2)"``, ``" (3)"``…
    suffix (bumped until free), so every value in the returned ``{raw: display}``
    map is distinct. Order follows ``names``; a repeated raw maps once
    (idempotent). Never raises."""
    out = {}
    used = set()
    for raw in names:
        if raw in out:
            continue
        base = display_text(raw)
        cand = base
        n = 1
        while cand in used:
            n += 1
            cand = f"{base} ({n})"
        used.add(cand)
        out[raw] = cand
    return out


def _font_desc(family, size, style):
    """A Tk font descriptor tuple for a span's style.

    Tk accepts ``(family, size, "bold italic underline")`` directly, so this
    avoids constructing ``tkfont.Font`` objects (and their named-font
    garbage-collection pitfalls) entirely — the descriptor is self-contained."""
    words = []
    if style["bold"]:
        words.append("bold")
    if style["italic"]:
        words.append("italic")
    if style["underline"]:
        words.append("underline")
    return (family, size, " ".join(words)) if words else (family, size)


def render_into_text(widget, s, base_font=None):
    """Render markup ``s`` into a ``tk.Text`` ``widget`` as styled colored spans.

    Clears ``widget`` then inserts each parsed span tagged with a foreground
    color (spans without a color use the widget's own ``fg``) and a font derived
    from ``base_font`` (bold/italic/underline). Intended for the small read-only
    preview strips in the overview editor. ``base_font`` is a ``(family, size)``
    tuple (default ``("Consolas", 9)``) or a ``tkfont.Font``. The widget's prior
    ``state`` (e.g. ``"disabled"``) is restored so a read-only preview stays
    read-only. Imports Tkinter lazily; tolerant of malformed markup (never
    raises on the parse — delegates to :func:`parse_spans`)."""
    import tkinter as tk

    # Normalize the base font to (family, size).
    if base_font is None:
        family, size = "Consolas", 9
    elif isinstance(base_font, (tuple, list)):
        family = base_font[0] if len(base_font) > 0 else "Consolas"
        size = base_font[1] if len(base_font) > 1 else 9
    else:  # a tkfont.Font (or Font-like) — read its actual family/size.
        try:
            family = base_font.actual("family")
            size = base_font.actual("size")
        except Exception:
            family, size = "Consolas", 9

    try:
        prev_state = str(widget.cget("state"))
    except tk.TclError:
        prev_state = "normal"
    try:
        widget.configure(state="normal")
    except tk.TclError:
        pass

    try:
        widget.delete("1.0", "end")
    except tk.TclError:
        return

    for text, style in parse_spans(s):
        tags = []
        color = style["color"]
        if color:
            ctag = "ovm_fg_" + color.lstrip("#")
            widget.tag_configure(ctag, foreground=color)
            tags.append(ctag)
        ftag = "ovm_font_%d%d%d" % (
            style["bold"], style["italic"], style["underline"])
        widget.tag_configure(ftag, font=_font_desc(family, size, style))
        tags.append(ftag)
        widget.insert("end", text, tuple(tags))

    try:
        widget.configure(state=prev_state)
    except tk.TclError:
        pass
