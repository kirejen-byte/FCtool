"""Discord-look preview of a ping: a pure markdown parser + a read-only widget.

The Discord ping editor shows, under the template, what will be copied. Two
modes: ``raw`` is the clipboard string exactly as copied, in a monospace font;
``rendered`` is an APPROXIMATION of how Discord draws it. This module is that
preview and nothing else - it copies nothing and sends nothing.

``parse`` (pure, above the ``Tk shell`` seam) turns the text into ``Line`` s of
``Span`` s. What it understands, and only this:

* Block prefixes - ``# ``, ``## ``, ``### `` (headers), ``-# `` (subtext) and
  ``> `` (quote): only at the very start of a line, only when followed by a
  space, and only with something after them (a bare ``# `` is text).
* Inline - ``**bold**``, ``*italic*`` / ``_italic_``, ``__underline__``,
  ``~~strike~~`` and `` `code` `` (its content is literal). Markers nest; a
  marker that is never closed is ordinary text. Two of Discord's own rules
  keep prose from turning italic by accident: an underscore is a marker only
  at a WORD EDGE (``_snake_case_name_`` is italic and keeps its inner
  underscores, a bare ``snake_case_name`` is left alone), and an opening
  ``*`` needs a non-space right after it (``2 * 3 * 4`` is arithmetic).
* ``<t:DIGITS>`` / ``<t:DIGITS:STYLE>`` with a ``discord_time`` style - a
  timestamp span (no style = ``f``). The digits are matched with an explicit
  ``[0-9]`` class and capped in length, so ``int()`` cannot refuse them; a
  value past Discord's own ceiling is text.
* ``@everyone`` / ``@here`` at a line start or after whitespace, and
  ``<@&DIGITS>`` (shown as ``@role``) - mention spans.
* Emoji - only through the optional ``split_emoji`` hook. Each emoji it names
  becomes ONE span and is atomic: a marker character inside one (the keycap
  asterisk starts with ``*``) is never markup. Without the hook, or when the
  hook misbehaves, emoji are ordinary text.

Shown literally, on purpose: fenced code blocks (the fence lines and
everything between them), lists, spoilers, masked links, backslash escapes,
user and channel mentions.

``DiscordPreview`` (below the seam) draws that on Discord's dark background.
It is READ-ONLY - text can be selected and copied, never edited - and it never
raises, whatever string it is handed: the editor re-renders it on a timer. A
render that would draw exactly what is already on screen rewrites nothing, so
a selection survives the timer.

No network, no Pillow (images are ``tk.PhotoImage`` objects handed in by the
``emoji_image`` hook), no ``fc_gui`` / ``ui_theme`` import.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from discord_time import STYLES

MODE_RENDERED = "rendered"
MODE_RAW = "raw"
MODES = (MODE_RENDERED, MODE_RAW)

BLOCKS = ("p", "h1", "h2", "h3", "subtext", "quote")
KINDS = ("text", "emoji", "timestamp", "mention")

#: Discord's style for a tag written without one: ``<t:1791658800>``.
DEFAULT_STYLE = "f"
#: The largest timestamp Discord accepts (the JavaScript date ceiling, in
#: seconds). Anything larger is not a timestamp to Discord either.
MAX_UNIX = 8640000000000
#: What a role mention shows as: the role's name is not known here.
ROLE_TEXT = "@role"


@dataclass(frozen=True)
class Span:
    """One run of a line.

    ``kind``   ``"text"`` | ``"emoji"`` | ``"timestamp"`` | ``"mention"``
    ``text``   text: the characters. emoji: the sequence. mention: what is
               displayed (``@everyone`` / ``@here`` / ``@role``). timestamp:
               the tag exactly as written - what the widget shows when the
               time cannot be worded.
    ``unix`` / ``style``   timestamp only.
    """
    kind: str
    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    code: bool = False
    unix: int = 0
    style: str = ""


@dataclass(frozen=True)
class Line:
    """One line of the message: its spans (the block prefix is not part of
    them) and its ``block`` - one of ``BLOCKS``. A blank line has no spans."""
    spans: tuple
    block: str


# -- parsing -----------------------------------------------------------------
# Longest prefix first: "### " must be tried before "## " and "# ".
_BLOCK_PREFIXES = (("### ", "h3"), ("## ", "h2"), ("# ", "h1"),
                   ("-# ", "subtext"), ("> ", "quote"))
_FENCE = "```"

# Explicit ASCII digits: these go to int(), which str.isdigit() does not
# guarantee to accept (tests/test_no_isdigit_int.py). 13 digits reaches
# MAX_UNIX, so a longer run never reaches int() at all.
_TIME_RE = re.compile(r"<t:([0-9]{1,13})(?::([A-Za-z]))?>")
_ROLE_RE = re.compile(r"<@&[0-9]+>")
_MENTIONS = ("@everyone", "@here")

# A span's emphasis, as (bold, italic, underline, strike) - the order of the
# Span fields, so it can be splatted into the constructor.
_PLAIN = (False, False, False, False)
_BOLD, _ITALIC, _UNDERLINE, _STRIKE = range(4)
# Two-character markers first: "**" must be tried before "*".
_MARKERS = (("**", _BOLD), ("__", _UNDERLINE), ("~~", _STRIKE),
            ("*", _ITALIC), ("_", _ITALIC))
# Every character that can START something other than plain text.
_SPECIAL = frozenset("`<@*_~")
# Markers deeper than this are text. Real nesting is three or four deep; the
# cap only bounds the recursion on hostile input.
_MAX_DEPTH = 8


def _block_of(line: str) -> tuple:
    """``(block, index where the content starts)`` for one line."""
    for prefix, block in _BLOCK_PREFIXES:
        if line.startswith(prefix):
            start = len(prefix)
            while start < len(line) and line[start] == " ":
                start += 1
            if line[start:].strip():
                return block, start
            break                   # a bare prefix heads nothing: text
    return "p", 0


def _fenced(rows: list) -> list:
    """Per row: is it part of a fenced code block (fence lines included)?

    A fence opens on a row with an odd number of triple backticks and closes
    on the next such row. One that never closes is not a block - Discord shows
    those rows as ordinary text, and so does this.
    """
    literal = [False] * len(rows)
    opened = None
    for index, row in enumerate(rows):
        marks = row.count(_FENCE)
        if opened is None:
            if marks % 2:
                opened = index
            elif marks:
                literal[index] = True           # opened and closed on the row
        elif marks % 2:
            for inside in range(opened, index + 1):
                literal[inside] = True
            opened = None
    return literal


class _Inline:
    """The inline pass over ONE line (indices are into the whole line, so
    "what comes before this character" is always answerable)."""

    def __init__(self, line: str, start: int, split_emoji):
        self.line = line
        self.emoji = {}                     # first index of an emoji -> end
        self.atomic = bytearray(len(line))  # 1 = inside an emoji
        # (marker, end) -> the lowest index from which no closer was found.
        # A closer depends on its own neighbours, never on where the search
        # began, so a later search of the same stretch cannot succeed either;
        # remembering that keeps a line of unclosed markers linear.
        self.dead = {}
        if split_emoji is not None:
            self._find_emoji(split_emoji, start)

    def _find_emoji(self, split_emoji, start: int) -> None:
        """Ask the hook once for the whole line. Its answer is used only when
        the chunks rejoin to exactly the text it was given; a hook that
        raises or answers anything else leaves emoji as text."""
        line = self.line
        found = {}
        try:
            pos = start
            for is_emoji, chunk in split_emoji(line[start:]):
                if not line.startswith(chunk, pos):
                    return
                if is_emoji and chunk:
                    found[pos] = pos + len(chunk)
                pos += len(chunk)
            if pos != len(line):
                return
        except Exception:
            return
        self.emoji = found
        for first, end in found.items():
            self.atomic[first:end] = b"\x01" * (end - first)

    def run(self, i: int, end: int, style=_PLAIN, depth: int = 0) -> list:
        """The spans of ``line[i:end]`` under ``style``."""
        line = self.line
        atomic = self.atomic
        out = []
        pending = i                         # start of the text not emitted yet
        while i < end:
            if line[i] not in _SPECIAL and not atomic[i]:
                i += 1
                continue
            spans, after = self._piece(i, end, style, depth)
            if spans is None:               # literal: stays in the pending text
                i = after
                continue
            if i > pending:
                out.append(Span("text", line[pending:i], *style))
            out.extend(spans)
            i = pending = after
        if end > pending:
            out.append(Span("text", line[pending:end], *style))
        return out

    def _piece(self, i: int, end: int, style, depth: int) -> tuple:
        """``(spans, index after)`` for whatever starts at ``i``, or
        ``(None, index after)`` when it is just text."""
        line = self.line
        if self.atomic[i]:
            stop = self.emoji.get(i)
            if stop is not None and stop <= end:
                return [Span("emoji", line[i:stop], *style)], stop
            return None, i + 1
        ch = line[i]
        if ch == "`":
            inner, stop, after = self._code(i, end)
            if inner is None:
                return None, after
            return [Span("text", line[inner:stop], *style, True)], after
        if ch == "<":
            match = _TIME_RE.match(line, i, end)
            if match is not None:
                letter = match.group(2) or DEFAULT_STYLE
                unix = int(match.group(1))
                if letter in STYLES and unix <= MAX_UNIX:
                    return [Span("timestamp", match.group(0), *style, False,
                                 unix, letter)], match.end()
            match = _ROLE_RE.match(line, i, end)
            if match is not None:
                return [Span("mention", ROLE_TEXT, *style)], match.end()
            return None, i + 1
        if ch == "@":
            if i == 0 or line[i - 1].isspace():
                for word in _MENTIONS:
                    if line.startswith(word, i, end):
                        return [Span("mention", word, *style)], i + len(word)
            return None, i + 1
        if depth < _MAX_DEPTH:              # ch is one of * _ ~
            for marker, slot in _MARKERS:
                if not line.startswith(marker, i, end):
                    continue
                inner = i + len(marker)
                if not self._opens(marker, i, inner, end):
                    continue
                stop = self._closer(marker, inner, end)
                if stop < 0:
                    continue
                nested = style[:slot] + (True,) + style[slot + 1:]
                return (self.run(inner, stop, nested, depth + 1),
                        stop + len(marker))
        return None, i + 1

    def _code(self, i: int, end: int) -> tuple:
        """The code span opened by the backtick run at ``i``:
        ``(content start, content end, index after)``, or ``(None, None,
        index after the run)`` when the run opens nothing.

        One or two backticks open a span that the next run of the SAME width
        closes. Three or more are a fence, shown literally.
        """
        line = self.line
        inner = i
        while inner < end and line[inner] == "`":
            inner += 1
        width = inner - i
        if width <= 2:
            k = inner
            while True:
                stop = line.find("`", k, end)
                if stop < 0:
                    break
                k = stop
                while k < end and line[k] == "`":
                    k += 1
                if k - stop == width and stop > inner:
                    return inner, stop, k
        return None, None, inner

    def _opens(self, marker: str, i: int, inner: int, end: int) -> bool:
        line = self.line
        if marker == "_":                   # only at a word edge
            return not (i > 0 and line[i - 1].isalnum())
        if marker == "*":                   # "2 * 3" is not emphasis
            return inner < end and not line[inner].isspace()
        return True

    def _closes(self, marker: str, pos: int) -> bool:
        line = self.line
        if marker == "_":                   # only at a word edge
            return not (pos + 1 < len(line) and line[pos + 1].isalnum())
        if marker == "*":
            return not line[pos - 1].isspace()
        return True

    def _closer(self, marker: str, start: int, end: int) -> int:
        """The index of the marker that closes one opened just before
        ``start``, or -1. Code spans and emoji are stepped over.

        Runs of the marker character decide who owns what: a two-character
        marker closes on the LAST two of a run of two or more, a one-character
        marker on the last of a run of one or of three and more (a run of
        exactly two is somebody's bold / underline). So ``***both***`` is
        bold around italic, and ``**a *b* c**`` nests.
        """
        key = (marker, end)
        known = self.dead.get(key)
        if known is not None and known <= start:
            return -1
        line = self.line
        atomic = self.atomic
        ch = marker[0]
        width = len(marker)
        k = start
        while k < end:
            c = line[k]
            if atomic[k]:
                k += 1
                continue
            if c == "`":
                k = self._code(k, end)[2]
                continue
            if c != ch:
                k += 1
                continue
            run_end = k + 1
            while (run_end < end and line[run_end] == ch
                   and not atomic[run_end]):
                run_end += 1
            run = run_end - k
            if width == 2:
                pos = run_end - 2 if run >= 2 else -1
            else:
                pos = run_end - 1 if run != 2 else -1
            # The content must hold more than marker characters ("****").
            if (pos > start and self._closes(marker, pos)
                    and line[start:pos].strip(ch)):
                return pos
            k = run_end
        self.dead[key] = start if known is None else min(known, start)
        return -1


def _merged(spans: list) -> tuple:
    """Adjacent text spans that look the same become one."""
    out = []
    for span in spans:
        last = out[-1] if out else None
        if (last is not None and span.kind == "text" == last.kind
                and replace(span, text="") == replace(last, text="")):
            out[-1] = replace(last, text=last.text + span.text)
        else:
            out.append(span)
    return tuple(out)


def parse(text: str, *, split_emoji=None) -> list:
    """``text`` as a list of ``Line`` - one per line of the message.

    Pure, and total over strings: any string parses, nothing raises (anything
    that is not a string is no lines at all). Lines split on ``\\n``; a
    ``\\r`` left before one by a Windows line ending is dropped.

    ``split_emoji(text) -> [(is_emoji, chunk), ...]`` is optional. With it,
    each emoji it names is one ``"emoji"`` span; without it emoji stay text.
    """
    if not isinstance(text, str):
        return []
    rows = [row[:-1] if row.endswith("\r") else row
            for row in text.split("\n")]
    lines = []
    for row, literal in zip(rows, _fenced(rows)):
        if literal:
            lines.append(Line((Span("text", row),) if row else (), "p"))
            continue
        block, start = _block_of(row)
        spans = _Inline(row, start, split_emoji).run(start, len(row))
        lines.append(Line(_merged(spans), block))
    return lines


# -- Tk shell ----------------------------------------------------------------
# Nothing above this line uses Tk, and nothing below it is needed by parse().
# The widget's imports are kept down here with it so the boundary stays
# visible in the file (the close_clients.py shape).
import logging                                                    # noqa: E402
import tkinter as tk                                              # noqa: E402
from tkinter import ttk                                           # noqa: E402

from discord_time import render_style                             # noqa: E402

_LOG = logging.getLogger(__name__)

# Discord's own dark theme, approximated. These are FIXED on purpose and are
# deliberately NOT ui_theme / palette colours: the point of the preview is to
# show what the READER will see in Discord, and Discord does not follow
# FCTool's palette. Recolouring them with the app would make the preview lie.
DISCORD_BG = "#313338"          # the chat background
DISCORD_TEXT = "#dbdee1"        # body text
DISCORD_HEADER = "#f2f3f5"      # "#" headers (and selected text)
DISCORD_MUTED = "#949ba4"       # "-#" subtext
MENTION_BG = "#3d4270"          # the mention chip: blurple at 30 % over the bg
MENTION_FG = "#c9cdfb"
TIMESTAMP_BG = "#3f4147"        # the grey chip under a <t:...> time
CODE_BG = "#2b2d31"             # inline code
QUOTE_BAR = "#4e5058"           # the bar left of a "> " quote
SELECT_BG = "#4752c4"           # selection

# Positive sizes are POINTS, so everything follows ``tk scaling``. 12 pt is
# Discord's 16 px body at 96 dpi; its line is 21 px there, so a 22 px emoji
# tile makes its own line 1 px taller and no more (measured on a mapped
# window: tests/test_discord_preview.py::test_mapped_layout).
_UI_FONT = "Segoe UI"
_CODE_FONT = "Consolas"
_BODY_PT = 12
_BLOCK_PT = {"h1": 18, "h2": 15, "h3": 12, "subtext": 10}
_HEADERS = ("h1", "h2", "h3")
_RAW_FONT = (_CODE_FONT, 10)

# Block -> the tags every character of such a line carries.
_BLOCK_TAGS = {
    "h1": ("block-h1", "fg-header"),
    "h2": ("block-h2", "fg-header"),
    "h3": ("block-h3", "fg-header"),
    "subtext": ("fg-subtext",),
    "quote": ("block-quote",),
}


class DiscordPreview(tk.Frame):
    """The preview box: a read-only ``tk.Text`` (``.text``) and its scrollbar
    (``.scrollbar``) on Discord's dark background.

    ``render(text, now_unix)``  show ``text``; ``now_unix`` is what relative
                                timestamps count from.
    ``set_mode(mode)``          ``"rendered"`` (the default) or ``"raw"``.
    ``mode()``                  the current mode.

    RAW shows ``text`` exactly: ``.text.get("1.0", "end-1c") == text``.
    RENDERED shows ``parse(text)`` - markers hidden, headers / subtext /
    quotes styled, mentions and timestamps on chips. A timestamp is worded by
    ``discord_time.render_style`` for this PC's zone (``utc_to_local`` is
    passed through to it); one it cannot word is shown as the tag itself.

    Emoji arrive through two optional hooks. ``split_emoji`` goes to
    ``parse``; ``emoji_image(seq) -> tk.PhotoImage | None`` supplies the tile
    for one emoji, and None (or no hook) leaves its characters. The widget
    keeps a reference to every image for as long as it is on screen. Text
    copied out of the RENDERED view does not include image emoji - RAW is the
    exact string.

    READ-ONLY: the Text is ``state="disabled"``, which on Windows still takes
    the focus on a click, shows a selection and copies it.

    NEVER RAISES from ``render``: a hook that raises is treated as "no
    image" / "no emoji", and a drawing that fails is drawn again more simply
    (without images, then as plain text), with one WARNING in the log.

    A render that would put exactly the same thing on screen changes nothing,
    so the caller may re-render on a timer without wiping the selection or
    the scroll position.
    """

    def __init__(self, master, *, split_emoji=None, emoji_image=None,
                 utc_to_local=None):
        super().__init__(master, bg=DISCORD_BG)
        self._split_emoji = split_emoji
        self._emoji_image = emoji_image
        self._utc_to_local = utc_to_local
        self._mode = MODE_RENDERED
        self._source = ""
        self._now = 0
        self._shown = None          # the drawing plan last put on screen
        self._images = []           # every PhotoImage now in the Text
        self._bars = []             # every quote-bar window now in the Text
        self._font_tags = set()
        self._warned = False

        self.text = tk.Text(
            self, wrap="word", width=48, height=8,
            font=(_UI_FONT, _BODY_PT), bg=DISCORD_BG, fg=DISCORD_TEXT,
            borderwidth=0, highlightthickness=0, padx=12, pady=8,
            # No spacing between lines: the quote bar is as tall as its line,
            # so the bars of consecutive quoted lines join into one.
            spacing1=0, spacing3=0, undo=False, exportselection=False,
            selectbackground=SELECT_BG, selectforeground=DISCORD_HEADER,
            inactiveselectbackground=SELECT_BG, state="disabled")
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL,
                                       command=self.text.yview)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")

        # The quote bar, in pixels that follow the display scaling.
        self._bar_px = max(2, int(round(self.winfo_fpixels("3p"))))
        self._quote_px = max(self._bar_px + 4,
                             int(round(self.winfo_fpixels("9p"))))
        self._configure_tags()

    # -- the public surface ---------------------------------------------------
    def render(self, text: str, now_unix: int) -> None:
        self._source = text if isinstance(text, str) else ""
        self._now = now_unix
        self._redraw()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("Unknown preview mode %r: expected 'rendered' "
                             "or 'raw'" % (mode,))
        if mode != self._mode:
            self._mode = mode
            self._redraw()

    def mode(self) -> str:
        return self._mode

    def destroy(self):
        # Let go of the emoji tiles with the widget: their owner may want to
        # free the sheet as soon as the window closes.
        self._shown = None
        self._images = []
        self._bars = []
        super().destroy()

    # -- tags -----------------------------------------------------------------
    def _configure_tags(self) -> None:
        """Creation order is priority order: a later tag wins. The chips come
        after the block colours so a mention in a header keeps its own
        colour; font tags (made on demand) set nothing but the font."""
        view = self.text
        view.tag_configure("raw", font=_RAW_FONT)
        view.tag_configure("block-h1", spacing1="8p", spacing3="3p")
        view.tag_configure("block-h2", spacing1="6p", spacing3="3p")
        view.tag_configure("block-h3", spacing1="4p", spacing3="2p")
        view.tag_configure("block-quote", lmargin2=self._quote_px)
        view.tag_configure("fg-header", foreground=DISCORD_HEADER)
        view.tag_configure("fg-subtext", foreground=DISCORD_MUTED)
        view.tag_configure("code", background=CODE_BG)
        view.tag_configure("timestamp", background=TIMESTAMP_BG)
        view.tag_configure("mention", background=MENTION_BG,
                           foreground=MENTION_FG)
        view.tag_configure("underline", underline=True)
        view.tag_configure("strike", overstrike=True)
        view.tag_raise("sel")       # a selection stays readable over a chip

    def _font_tag(self, block: str, span) -> str:
        """The tag carrying the font for ``span`` on a ``block`` line."""
        size = _BLOCK_PT.get(block, _BODY_PT)
        bold = bool(span.bold or block in _HEADERS)
        italic = bool(span.italic)
        if span.code:
            family, size, label = _CODE_FONT, size - 1, "code"
        else:
            family, label = _UI_FONT, "ui"
        name = "font-%s-%d-%d%d" % (label, size, bold, italic)
        if name not in self._font_tags:
            font = (family, size)
            if bold:
                font += ("bold",)
            if italic:
                font += ("italic",)
            self.text.tag_configure(name, font=font)
            # A tag made at runtime outranks "sel" until it is raised again.
            self.text.tag_raise("sel")
            self._font_tags.add(name)
        return name

    # -- the drawing plan -----------------------------------------------------
    # A plan is a tuple of operations: ("text", chars, tags), ("image",
    # photo, tags) or ("bar",). Building one touches no text; comparing two
    # is how "nothing changed" is known.
    def _raw_plan(self, tags=("raw",)) -> tuple:
        return (("text", self._source, tags),) if self._source else ()

    def _rendered_plan(self, use_images: bool = True) -> tuple:
        plan = []
        newline_tags = None
        for line in parse(self._source, split_emoji=self._split_emoji):
            block_tags = _BLOCK_TAGS.get(line.block, ())
            if newline_tags is not None:
                # The newline belongs to the line it ends.
                plan.append(("text", "\n", newline_tags))
            newline_tags = block_tags
            if line.block == "quote":
                plan.append(("bar",))
            for span in line.spans:
                tags = (self._font_tag(line.block, span),) + block_tags
                if span.underline:
                    tags += ("underline",)
                if span.strike:
                    tags += ("strike",)
                shown = span.text
                if span.kind == "emoji":
                    image = self._image_for(shown) if use_images else None
                    if image is not None:
                        plan.append(("image", image, tags))
                        continue
                elif span.kind == "timestamp":
                    wording = self._wording(span)
                    if wording is not None:
                        shown = wording
                        tags += ("timestamp",)
                elif span.kind == "mention":
                    tags += ("mention",)
                elif span.code:
                    tags += ("code",)
                if shown:
                    plan.append(("text", shown, tags))
        return tuple(plan)

    def _image_for(self, seq: str):
        if self._emoji_image is None:
            return None
        try:
            return self._emoji_image(seq)
        except Exception:
            return None

    def _wording(self, span):
        """What Discord shows for a timestamp here and now, or None when it
        cannot be worded (the tag is then shown as written)."""
        try:
            wording = render_style(span.unix, span.style, now_unix=self._now,
                                   utc_to_local=self._utc_to_local)
        except Exception:
            return None
        return wording if isinstance(wording, str) and wording else None

    # -- drawing --------------------------------------------------------------
    def _redraw(self) -> None:
        """Draw the current text in the current mode - or, when that fails,
        something simpler. Never raises."""
        raw = self._mode == MODE_RAW
        attempts = (
            self._raw_plan if raw else self._rendered_plan,
            self._raw_plan if raw else (lambda: self._rendered_plan(False)),
            lambda: self._raw_plan(()),
        )
        for attempt, build in enumerate(attempts):
            try:
                plan = build()
            except Exception as exc:
                if attempt == 0:
                    self._shown = None      # what is on screen is a fallback
                self._note_failure(exc)
                continue
            if attempt == 0:
                if plan == self._shown:
                    return                  # already on screen: touch nothing
                # Recorded BEFORE drawing: a plan that cannot be drawn is not
                # tried again on every tick.
                self._shown = plan
            try:
                self._draw(plan)
                return
            except Exception as exc:
                self._note_failure(exc)

    def _note_failure(self, exc) -> None:
        if self._warned:
            return
        self._warned = True
        try:                        # ASCII, and the exception TYPE only
            _LOG.warning("discord_preview: drawing failed (%s); showing a "
                         "simpler preview", type(exc).__name__)
        except Exception:
            pass

    def _draw(self, plan: tuple) -> None:
        view = self.text
        top = view.yview()[0]
        for bar in self._bars:
            bar.destroy()
        self._bars = []
        images = []
        # Filled while NORMAL: insert / delete on a disabled Text are silent
        # no-ops, so the toggle around a refill is load-bearing.
        view.configure(state="normal")
        try:
            view.delete("1.0", "end")
            for op in plan:
                if op[0] == "text":
                    view.insert("end", op[1], op[2])
                elif op[0] == "image":
                    index = view.index("end-1c")
                    view.image_create(index, image=op[1], padx=1)
                    images.append(op[1])
                    for tag in op[2]:
                        view.tag_add(tag, index)
                else:
                    self._add_bar()
        finally:
            view.configure(state="disabled")
            # The old images are let go only now, after their elements went.
            self._images = images
        view.yview_moveto(top)

    def _add_bar(self) -> None:
        """The quote bar: a thin embedded window at the start of the line,
        stretched to the line's height - a bar, then a gap, as Discord draws
        it. (A wrapped quote keeps its indent; the bar marks its first row.)"""
        view = self.text
        index = view.index("end-1c")
        holder = tk.Frame(view, width=self._quote_px, height=1, bg=DISCORD_BG)
        tk.Frame(holder, bg=QUOTE_BAR).place(x=0, y=0, width=self._bar_px,
                                             relheight=1)
        self._bars.append(holder)
        view.window_create(index, window=holder, stretch=1)
        view.tag_add("block-quote", index)
