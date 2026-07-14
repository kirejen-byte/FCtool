"""A compact, reusable WYSIWYG editor for EVE in-game rich-text markup.

:class:`MarkupEditor` is a ``tk.Frame`` containing a small formatting toolbar and
a multi-line ``tk.Text``. The user types and formats text directly (colours,
bold/italic/underline, font size); the widget serialises that to EVE MOTD/chat
markup via :func:`get_markup`, and restores formatting from markup via
:func:`set_markup`. It is the free-text intro/outro editor for the MOTD writer,
instantiated once per field.

Formatting is represented with Tk text tags:

* ``fg_<hex>`` — a foreground colour (``hex`` is ``rrggbb``, no ``#``). At most
  one ``fg_*`` tag is active on any character (applying a colour first strips any
  other ``fg_*`` from the range).
* ``bold`` / ``italic`` / ``underline`` — configured-font toggles.
* ``size_<n>`` — Consolas at point size ``n`` (one ``size_*`` per character).

Serialisation reuses :mod:`motd_markup` (the Tk-free parse/serialise module) so
the editor and the rendered preview agree on the markup dialect: ``get_markup``
builds a list of :class:`motd_markup.Segment` from the Text + tags and feeds it to
:func:`motd_markup.segments_to_markup`; ``set_markup`` feeds markup through
:func:`motd_markup.parse_markup` and applies the matching tags. Round-trip holds
at the styled-run level (not byte-for-byte).

This module imports Tkinter but no project modules other than :mod:`motd_markup`;
it carries its own theme defaults so it can be dropped into any dark-themed frame.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import colorchooser

import motd_markup
from ui_helpers import attach_tooltip


# Dark-theme defaults — imports the shared ui_theme palette (a stdlib-only,
# containment-safe leaf) so the editor blends into the app's navy scheme. The
# caller may override any of these via constructor kwargs; kept as module-private
# aliases so the rest of the module is untouched.
from ui_theme import (
    BG_PANEL as _BG_PANEL,
    BG_ENTRY as _BG_ENTRY,
    BG_DARK as _BG_DARK,
    FG_TEXT as _FG_TEXT,
    FG_WHITE as _FG_WHITE,
    FG_ACCENT as _FG_ACCENT,
    BORDER_COLOR as _BORDER,
)

# Toolbar colour swatches: (label-for-tooltip, "#rrggbb"). White first so it maps
# to the MOTD's default text colour.
_PALETTE = [
    ("White", "#ffffff"),
    ("Red", "#ff4444"),
    ("Orange", "#ff8c00"),
    ("Yellow", "#ffdd00"),
    ("Green", "#00ff88"),
    ("Cyan", "#00d4ff"),
]

# Font-size presets offered by the size dropdown (mirrors the EVE preset range).
_SIZES = (10, 12, 14, 18)

# Base font for the editor body (also the size the "(size)" placeholder maps to).
_BASE_FAMILY = "Consolas"
_BASE_SIZE = 11


class MarkupEditor(tk.Frame):
    """A toolbar + ``tk.Text`` widget that edits EVE markup as formatted text.

    Public API:

    * :meth:`get_markup` → the EVE markup string for the current content.
    * :meth:`set_markup` — replace the content from an EVE markup string.
    * :meth:`on_change` — register a callback fired (debounced by the caller) on
      any content or formatting change.
    """

    def __init__(self, master, height=3, on_change=None,
                 bg_panel=_BG_PANEL, bg_entry=_BG_ENTRY, fg_text=_FG_TEXT,
                 fg_white=_FG_WHITE, fg_accent=_FG_ACCENT, border=_BORDER,
                 button_style="Dark.TButton", **kwargs):
        super().__init__(master, bg=bg_panel, **kwargs)
        self._bg_panel = bg_panel
        self._bg_entry = bg_entry
        self._fg_text = fg_text
        self._fg_white = fg_white
        self._fg_accent = fg_accent
        self._border = border
        self._button_style = button_style
        self._change_cbs = []
        if on_change is not None:
            self._change_cbs.append(on_change)

        # Track which colour tags we have configured so set_markup can register
        # arbitrary colours on the fly (fg_<hex>).
        self._configured_fg = set()
        self._pending_tags = set()      # Tk tag names to apply to the next typed chars
        self._pending_press_idx = None  # insert index captured on a self-inserting KeyPress
        self._pending_armed = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_toolbar()

        self.text = tk.Text(
            self, height=height, wrap=tk.WORD,
            bg=bg_entry, fg=fg_text, insertbackground=fg_white,
            borderwidth=1, relief=tk.RIDGE,
            font=(_BASE_FAMILY, _BASE_SIZE),
            selectbackground=fg_accent, selectforeground=_BG_DARK,
            undo=True)
        self.text.grid(row=1, column=0, sticky="nsew")

        self._configure_static_tags()

        # Change notification: <<Modified>> (reset the flag each fire) + key/mouse.
        # NOTE: the KeyRelease change-fire MUST pass add="+" so the pending
        # typing-style handlers below do not clobber it (bind() without add
        # REPLACES the prior callback).
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<KeyRelease>", lambda e: self._fire_change(), add="+")
        self.text.bind("<KeyPress>", self._pending_keypress, add="+")
        self.text.bind("<KeyRelease>", self._pending_keyrelease, add="+")
        self.text.bind("<ButtonRelease-1>", lambda e: None)

    # ── construction helpers ────────────────────────────────────────────────

    def _build_toolbar(self):
        """Build the compact formatting toolbar (row 0)."""
        bar = tk.Frame(self, bg=self._bg_panel)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 2))

        # Colour swatches.
        for label, hexcolor in _PALETTE:
            sw = tk.Frame(bar, bg=hexcolor, width=16, height=16,
                          highlightbackground=self._border, highlightthickness=1,
                          cursor="hand2")
            sw.pack(side=tk.LEFT, padx=1)
            sw.pack_propagate(False)
            sw.bind("<Button-1>",
                    lambda e, c=hexcolor: self.apply_color(c))
            attach_tooltip(sw, f"Colour: {label}")

        # Custom colour picker.
        custom = tk.Label(bar, text="…", bg=self._bg_entry, fg=self._fg_white,
                          font=(_BASE_FAMILY, 9, "bold"), width=2,
                          cursor="hand2",
                          highlightbackground=self._border, highlightthickness=1)
        custom.pack(side=tk.LEFT, padx=(1, 6))
        custom.bind("<Button-1>", lambda e: self._pick_custom_color())
        attach_tooltip(custom, "Custom colour…")

        # B / I / U toggle buttons (tk.Button so we can style them compactly).
        self._mk_toggle(bar, "B", ("Consolas", 9, "bold"), self.toggle_bold)
        self._mk_toggle(bar, "I", ("Consolas", 9, "italic"), self.toggle_italic)
        self._mk_toggle(bar, "U", ("Consolas", 9, "underline"),
                        self.toggle_underline)

        # Size dropdown.
        self._size_var = tk.StringVar(value="size")
        size_menu = tk.OptionMenu(bar, self._size_var, *[str(s) for s in _SIZES],
                                  command=self._on_size_pick)
        size_menu.config(bg=self._bg_entry, fg=self._fg_white,
                         activebackground=self._bg_entry,
                         activeforeground=self._fg_accent,
                         highlightthickness=0, bd=1, relief=tk.RIDGE,
                         font=("Consolas", 8), width=4, anchor="w",
                         cursor="hand2")
        size_menu["menu"].config(bg=self._bg_entry, fg=self._fg_white,
                                 activebackground=self._fg_accent,
                                 activeforeground=_BG_DARK)
        size_menu.pack(side=tk.LEFT, padx=(6, 1))

        # Clear-formatting button.
        clr = tk.Button(bar, text="clear", font=("Consolas", 8),
                        bg=self._bg_entry, fg=self._fg_text,
                        activebackground=self._fg_accent,
                        activeforeground=_BG_DARK, bd=1, relief=tk.RIDGE,
                        cursor="hand2", command=self.clear_formatting)
        clr.pack(side=tk.LEFT, padx=(6, 1))

    def _mk_toggle(self, bar, label, font, command):
        btn = tk.Button(bar, text=label, font=font, width=2,
                        bg=self._bg_entry, fg=self._fg_white,
                        activebackground=self._fg_accent,
                        activeforeground=_BG_DARK, bd=1, relief=tk.RIDGE,
                        cursor="hand2", command=command)
        btn.pack(side=tk.LEFT, padx=1)
        return btn

    def _configure_static_tags(self):
        """Configure the fixed (non-colour) tags' fonts once."""
        base = tkfont.Font(family=_BASE_FAMILY, size=_BASE_SIZE)
        bold = tkfont.Font(family=_BASE_FAMILY, size=_BASE_SIZE, weight="bold")
        italic = tkfont.Font(family=_BASE_FAMILY, size=_BASE_SIZE, slant="italic")
        self.text.tag_configure("bold", font=bold)
        self.text.tag_configure("italic", font=italic)
        self.text.tag_configure("underline", underline=True)
        for n in _SIZES:
            self.text.tag_configure(
                f"size_{n}", font=tkfont.Font(family=_BASE_FAMILY, size=n))
        # Keep a reference so the Font objects are not garbage-collected.
        self._fonts = (base, bold, italic)

    def _fg_tag(self, hexcolor: str) -> str:
        """Return (configuring if needed) the ``fg_<hex>`` tag for ``hexcolor``."""
        key = hexcolor.lstrip("#").lower()
        tag = f"fg_{key}"
        if tag not in self._configured_fg:
            self.text.tag_configure(tag, foreground="#" + key)
            self._configured_fg.add(tag)
        return tag

    # ── change notification ─────────────────────────────────────────────────

    def on_change(self, callback):
        """Register ``callback`` to be invoked on any content/format change.

        Multiple callbacks may be registered; the caller is responsible for any
        debouncing (the MOTD writer debounces its preview rebuild)."""
        if callback is not None:
            self._change_cbs.append(callback)

    def _on_modified(self, event=None):
        # <<Modified>> fires on programmatic edits too; reset the flag so it can
        # fire again, then notify (guarding against re-entrancy).
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        self._fire_change()

    def _fire_change(self):
        for cb in list(self._change_cbs):
            try:
                cb()
            except Exception:
                pass

    # ── selection helpers ───────────────────────────────────────────────────

    def _selection_range(self):
        """Return ``(start, end)`` index strings for the current selection, or
        ``None`` when there is no selection."""
        try:
            return self.text.index("sel.first"), self.text.index("sel.last")
        except tk.TclError:
            return None

    # ── pending (typing-style) helpers ──────────────────────────────────────

    def _set_pending(self, tag, exclusive_prefix=None):
        """Arm a pending typing style. With exclusive_prefix (e.g. 'fg_'/'size_'),
        drop any other pending tag with that prefix first (one colour / one size)."""
        if exclusive_prefix:
            self._pending_tags = {t for t in self._pending_tags
                                  if not t.startswith(exclusive_prefix)}
        if tag:
            self._pending_tags.add(tag)

    def _toggle_pending(self, tag):
        if tag in self._pending_tags:
            self._pending_tags.discard(tag)
        else:
            self._pending_tags.add(tag)

    def _clear_pending(self):
        self._pending_tags.clear()

    def _pending_keypress(self, e):
        # Arm only when a style is pending AND this key is a real self-inserting
        # printable char (not a modifier/navigation/control combo). e.state bits:
        # Control=0x4, Mod1/Alt(X11)=0x8, Alt(Windows)=0x20000.
        if not self._pending_tags:
            self._pending_armed = False
            return
        is_insert = (bool(e.char) and len(e.char) == 1 and e.char.isprintable()
                     and not (e.state & (0x4 | 0x8 | 0x20000)))
        if is_insert:
            self._pending_press_idx = self.text.index("insert")
            self._pending_armed = True
        else:
            self._pending_armed = False

    def _pending_keyrelease(self, e):
        if not (self._pending_armed and self._pending_tags):
            return
        self._pending_armed = False
        start = self._pending_press_idx
        cur = self.text.index("insert")
        if start is not None and self.text.compare(cur, ">", start):
            for tag in self._pending_tags:
                self.text.tag_add(tag, start, cur)

    # ── toolbar actions ─────────────────────────────────────────────────────

    def apply_color(self, hexcolor: str):
        """Apply ``hexcolor`` (``#rrggbb``) to the selection, replacing any
        other foreground colour on that range (one colour per run)."""
        rng = self._selection_range()
        if rng is None:
            # No selection: arm a pending colour for the next typed chars.
            self._set_pending(self._fg_tag(hexcolor), exclusive_prefix="fg_")
            self._fire_change()
            return
        start, end = rng
        # Strip any existing fg_* tag from the range first.
        for tag in self.text.tag_names():
            if tag.startswith("fg_"):
                self.text.tag_remove(tag, start, end)
        self.text.tag_add(self._fg_tag(hexcolor), start, end)
        self._fire_change()

    def _pick_custom_color(self):
        # Always open the dialog (a selection is no longer required); a chosen
        # colour applies to the selection if any, else arms a pending colour.
        rng = self._selection_range()
        try:
            _rgb, hexval = colorchooser.askcolor(parent=self,
                                                 title="Pick a colour")
        except Exception:
            hexval = None
        if not hexval:
            return
        if rng is not None:
            self.apply_color(hexval)
        else:
            self._set_pending(self._fg_tag(hexval), exclusive_prefix="fg_")
            self._fire_change()

    def _toggle_tag(self, tag: str):
        """Toggle ``tag`` over the selection: remove it if the *whole* selection
        already carries it, otherwise add it."""
        rng = self._selection_range()
        if rng is None:
            # No selection: toggle the pending style for the next typed chars.
            self._toggle_pending(tag)
            self._fire_change()
            return
        start, end = rng
        if self._range_fully_tagged(tag, start, end):
            self.text.tag_remove(tag, start, end)
        else:
            self.text.tag_add(tag, start, end)
        self._fire_change()

    def _range_fully_tagged(self, tag: str, start: str, end: str) -> bool:
        """True when every character in ``[start, end)`` carries ``tag``.

        Walks the tag's ranges and confirms they cover the whole selection."""
        # tag_ranges returns a flat list of (start, end) index pairs.
        ranges = self.text.tag_ranges(tag)
        covered = []
        for i in range(0, len(ranges), 2):
            covered.append((str(ranges[i]), str(ranges[i + 1])))
        # Check coverage by scanning each character boundary in the selection.
        idx = start
        while self.text.compare(idx, "<", end):
            nxt = self.text.index(f"{idx} +1c")
            inside = any(
                self.text.compare(rs, "<=", idx)
                and self.text.compare(idx, "<", re)
                for rs, re in covered)
            if not inside:
                return False
            idx = nxt
        return True

    def toggle_bold(self):
        self._toggle_tag("bold")

    def toggle_italic(self):
        self._toggle_tag("italic")

    def toggle_underline(self):
        self._toggle_tag("underline")

    def _on_size_pick(self, value):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return
        self.apply_size(n)
        # Reset the dropdown label back to its placeholder.
        self._size_var.set("size")

    def apply_size(self, n: int):
        """Apply font size ``n`` to the selection, replacing any other size."""
        rng = self._selection_range()
        if rng is None:
            # No selection: arm a pending size for the next typed chars (base
            # size drops any pending size; otherwise configure the size_n tag
            # ad-hoc — like set_markup — before it is applied).
            if n == _BASE_SIZE:
                self._set_pending(None, exclusive_prefix="size_")
            else:
                tag = f"size_{n}"
                if tag not in self.text.tag_names():
                    self.text.tag_configure(
                        tag, font=tkfont.Font(family=_BASE_FAMILY, size=n))
                self._set_pending(tag, exclusive_prefix="size_")
            self._fire_change()
            return
        start, end = rng
        for tag in self.text.tag_names():
            if tag.startswith("size_"):
                self.text.tag_remove(tag, start, end)
        if n != _BASE_SIZE:
            self.text.tag_add(f"size_{n}", start, end)
        self._fire_change()

    def clear_formatting(self):
        """Remove every formatting tag from the selection (plain text)."""
        rng = self._selection_range()
        if rng is None:
            # No selection: drop any armed pending styles.
            self._clear_pending()
            self._fire_change()
            return
        start, end = rng
        for tag in self.text.tag_names():
            if (tag.startswith("fg_") or tag.startswith("size_")
                    or tag in ("bold", "italic", "underline")):
                self.text.tag_remove(tag, start, end)
        self._fire_change()

    # ── serialisation ───────────────────────────────────────────────────────

    def get_markup(self) -> str:
        """Serialise the editor content + tags into EVE markup.

        Walks the Text character by character, groups maximal runs of identical
        active style into :class:`motd_markup.Segment` objects (newlines become
        dedicated newline segments), and serialises via
        :func:`motd_markup.segments_to_markup`. The Text widget's implicit
        trailing newline is stripped. An empty editor yields ``""``."""
        # Full content sans the implicit trailing newline Tk appends.
        content = self.text.get("1.0", "end-1c")
        if not content:
            return ""

        segments: list[motd_markup.Segment] = []
        run_chars: list[str] = []
        run_style = None

        def _flush():
            if run_chars:
                color, bold, italic, underline, size = run_style
                segments.append(motd_markup.Segment(
                    text="".join(run_chars), color=color, bold=bold,
                    italic=italic, underline=underline, size=size))
            run_chars.clear()

        idx = "1.0"
        for ch in content:
            if ch == "\n":
                _flush()
                run_style = None
                segments.append(motd_markup.Segment(newline=True))
                idx = self.text.index(f"{idx} +1c")
                continue
            style = self._style_at(idx)
            if run_chars and style != run_style:
                _flush()
            if not run_chars:
                run_style = style
            run_chars.append(ch)
            idx = self.text.index(f"{idx} +1c")
        _flush()

        return motd_markup.segments_to_markup(segments)

    def _style_at(self, index: str):
        """Return the active style tuple ``(color, bold, italic, underline,
        size)`` at character ``index`` from the tags applied there."""
        color = None
        bold = italic = underline = False
        size = None
        for tag in self.text.tag_names(index):
            if tag.startswith("fg_"):
                color = "#" + tag[3:]
            elif tag == "bold":
                bold = True
            elif tag == "italic":
                italic = True
            elif tag == "underline":
                underline = True
            elif tag.startswith("size_"):
                try:
                    size = int(tag[5:])
                except ValueError:
                    pass
        return (color, bold, italic, underline, size)

    def set_markup(self, markup: str):
        """Replace the editor content from an EVE markup string.

        Clears the Text, parses ``markup`` via :func:`motd_markup.parse_markup`,
        and inserts each segment's text with the matching tags (colour →
        ``fg_<hex>``, bold/italic/underline, ``size_<n>``); a newline segment
        inserts ``"\\n"``. Restores formatting so a loaded MOTD shows styled.
        Does not fire the change callback (loading is not a user edit)."""
        self.text.config(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        for seg in motd_markup.parse_markup(markup or ""):
            if seg.newline:
                self.text.insert(tk.END, "\n")
                continue
            if not seg.text:
                continue
            tags = []
            if seg.color:
                tags.append(self._fg_tag(seg.color))
            if seg.bold:
                tags.append("bold")
            if seg.italic:
                tags.append("italic")
            if seg.underline:
                tags.append("underline")
            if seg.size is not None and seg.size != _BASE_SIZE:
                # Configure an ad-hoc size tag if it is outside the presets.
                tag = f"size_{seg.size}"
                if tag not in self.text.tag_names():
                    self.text.tag_configure(
                        tag, font=tkfont.Font(family=_BASE_FAMILY,
                                              size=seg.size))
                tags.append(tag)
            self.text.insert(tk.END, seg.text, tuple(tags))
        # Reset the modified flag so the programmatic load does not surface as a
        # user change on the next <<Modified>>.
        self.text.edit_modified(False)

