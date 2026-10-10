"""The MOTD composer's editor widget: live-token *pills* over a rich-text canvas.

:class:`PillCanvas` subclasses :class:`markup_editor.MarkupEditor` — inheriting its
formatting toolbar (colour swatches, B/I/U, size, clear) and its ``tk.Text`` body —
and layers on **pills**: atomic embedded-window chips that stand in for a
:class:`motd_doc.TokenRun` and re-resolve (label / colour / delta / stale-state) on
demand via an injected ``resolve_label`` callable. The document model
(``motd_doc``) is the single source of truth: :meth:`get_doc` reconstructs the
ordered run list by walking ``text.dump`` and mapping every embedded window back
through the :attr:`_pills` registry.

Load-bearing Tk facts this module is built around (each verified before coding):

* **Native undo destroys embedded windows.** The inherited Text is reconfigured
  ``undo=False`` and this class keeps its OWN snapshot stack
  (``doc_to_json`` + caret) with debounced text-edit coalescing.
* **The clipboard cannot carry embedded windows.** ``<<Copy>>``/``<<Cut>>``/
  ``<<Paste>>`` are intercepted: copy writes ``⟦kind:label⟧`` placeholders to the
  system clipboard AND stashes the run-list JSON internally; paste prefers the
  internal fragment when the system clipboard still matches what we wrote.
* **``dump`` splits text at tag boundaries and reports windows by pathname**, so a
  per-chunk :meth:`_style_at` yields the run's style and the window value keys the
  registry. A :meth:`_reconcile_registry` pass keeps the registry byte-consistent
  with the widget after every mutation (Tk delivers ``<Destroy>`` asynchronously,
  so explicit reconciliation — not the ``<Destroy>`` bind alone — is authoritative).

The palette (Task 4) is never imported here: it drives drags into
:meth:`drag_feedback` / :meth:`drop_at_pointer` / :meth:`cancel_drag_feedback` and
inline triggers into the ``on_trigger`` callback, then calls
:meth:`accept_trigger`. All colours come from :mod:`ui_theme`; dialogs use
:func:`ui_helpers.make_modal`; tooltips use :func:`ui_helpers.attach_tooltip`.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

import markup_editor
import motd_markup
from markup_editor import MarkupEditor
from motd_doc import (TextRun, TokenRun, doc_to_json, doc_from_json,
                      CHANNEL_LINE_DEFAULT_LABEL)
from ui_theme import (
    BG_DARK, BG_PANEL, BG_ENTRY,
    FG_TEXT, FG_DIM, FG_WHITE, FG_ACCENT, FG_GREEN, FG_RED, FG_YELLOW,
    BORDER_COLOR,
)
from ui_helpers import make_modal, attach_tooltip
from autocomplete import AutocompleteEntry

try:  # app_log is a leaf logger; degrade to a stdlib logger if unavailable.
    from app_log import get_logger as _get_logger
    _LOG = _get_logger("pill_canvas")
except Exception:  # pragma: no cover - defensive
    import logging
    _LOG = logging.getLogger("pill_canvas")


# Chip glyphs per token kind (item glyphs mirror the palette legend, spec §6;
# line kinds share ☰, the block kind uses ▤).
GLYPHS = {
    "char": "◆", "fit": "▣", "system": "✦", "channel": "#",
    "fc_line": "☰", "staging_line": "☰", "doctrine_line": "☰",
    "tag_line": "☰", "channel_line": "☰", "doctrine_block": "▤",
}
_DEFAULT_GLYPH = "◆"

# The kinds :meth:`PillCanvas._build_param_body` builds a real editor for. Every
# other kind (fit / char / the doctrine kinds / a kind this module has never
# heard of) falls through to its read-only info body - and is the set an
# ``on_pill_activate`` callback takes over. Kept equal to that method's
# ``if kind ...`` chain by tests/test_pill_canvas_seams.py (AST drift guard).
PARAM_EDITOR_KINDS = frozenset({
    "tag_line", "staging_line", "system", "channel", "channel_line", "fc_line",
})

_BASE_FAMILY = "Consolas"
_BASE_SIZE = 11              # mirrors markup_editor._BASE_SIZE (the "no size tag" size)
_CHIP_FONT = ("Consolas", 9)
_UNDO_CAP = 100
_TEXT_BURST_MS = 400        # debounce window that coalesces a typing burst -> 1 undo

# Caret-navigation keysyms (they carry no printable char) that dismiss a pill
# selection — once the user navigates the caret away, the accent highlight must
# not linger. "Prior"/"Next" are PageUp/PageDown.
_CARET_NAV_KEYS = frozenset({
    "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
})


def _post_menu_at(menu, x_root, y_root) -> None:
    """Post a minted-per-click ``menu`` at the pointer and OWN its lifetime.

    The pointer-anchored twin of :func:`doctrine_refits.post_menu_under` (that
    one anchors under a button, so its shape does not fit a right-click menu; the
    teardown is the same, deliberately). The chip menu is built fresh on every
    right-click, so without a teardown each click leaked one Tk menu for the life
    of the canvas. Two details are load-bearing, both from fc_gui's cured
    ``_post_menu``: the destroy hangs off ``<Unmap>`` (fired when Tk unposts the
    menu), is armed BEFORE the popup, and is DEFERRED 100 ms - an immediate
    destroy races Tk's idle-scheduled command invocation and swallows the click;
    and ``grab_release`` runs in a ``finally``, so a menu that fails to post never
    leaves the pointer grabbed. Do NOT pass a long-lived menu.
    """
    menu.bind("<Unmap>", lambda ev: menu.after(100, menu.destroy))
    try:
        menu.tk_popup(x_root, y_root)
    finally:
        menu.grab_release()


# -- emoji (the opt-in ``emoji=`` provider; see PillCanvas) ---------------------
# An emoji the provider has a tile for is DRAWN as one embedded image whose name
# spells its sequence, so reading the widget back needs no registry: the name is
# the data. Tk suffixes a repeated name with "#N"; the separator is "_" because
# "-" and "+" start an index modifier.
_EMOJI_NAME = "emoji:"
_EMOJI_MARK = "pc_emoji"            # scratch mark: where the next piece is drawn
_EMOJI_WINDOW = 24                  # most elements one re-cluster looks back over
_ZWJ = chr(0x200D)
# Tk state bits that mean "not typing" (Control, Alt) - the one shared definition.
_KEY_NOT_TYPING = markup_editor.KEY_NOT_TYPING


def _emoji_name(seq: str) -> str:
    """The embedded-image name for ``seq``: its code points in hex."""
    return _EMOJI_NAME + "_".join("%x" % ord(ch) for ch in seq)


def _emoji_seq(name) -> "str | None":
    """The sequence an embedded image's name spells; None if it is not ours."""
    base = str(name).partition("#")[0]
    if not base.startswith(_EMOJI_NAME):
        return None
    try:
        return "".join(chr(int(part, 16))
                       for part in base[len(_EMOJI_NAME):].split("_"))
    except (ValueError, OverflowError):
        return None


def _emoji_joinable(text: str) -> bool:
    """True when ``text`` is only characters that CONTINUE an emoji sequence:
    ZWJ, VS16, the keycap, a skin-tone modifier, a regional indicator, a tag
    character."""
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        if not (cp in (0x200D, 0xFE0F, 0x20E3) or 0x1F3FB <= cp <= 0x1F3FF
                or 0x1F1E6 <= cp <= 0x1F1FF or 0xE0020 <= cp <= 0xE007F):
            return False
    return True


class PillCanvas(MarkupEditor):
    """A :class:`MarkupEditor` with embedded live-token pills + a model + own undo.

    See the module docstring for the architecture. The frozen public surface used
    by the Task-4 wiring is: :meth:`get_doc` / :meth:`set_doc`,
    :meth:`insert_token_at_caret` / :meth:`insert_text_at_caret`,
    :meth:`refresh_pills`, :meth:`drag_feedback` / :meth:`drop_at_pointer` /
    :meth:`cancel_drag_feedback`, :meth:`undo_model` / :meth:`redo_model`,
    :meth:`accept_trigger` / :meth:`dismiss_trigger`, and the
    :meth:`get_markup` / :meth:`set_markup` legacy-compat overrides.

    Opt-in seams (keyword-only; the defaults ARE the MOTD composer's behaviour,
    which passes none of them) for a second host that edits its own pill kinds:

    * ``style_toolbar=False`` - build none of the inherited colour / B / I / U /
      size / clear controls. The undo / redo buttons are still built.
    * ``kind_glyphs`` - a ``{kind: glyph}`` overlay consulted BEFORE the module
      :data:`GLYPHS` for this instance only. Copied at construction; the module
      table is never written.
    * ``on_pill_activate(kind, params)`` - called (with a copy of the params) when
      the user activates a pill whose kind has no built-in parameter editor
      (:data:`PARAM_EDITOR_KINDS`): double-click, Return with the caret against
      the selected pill, or the chip menu's Edit entry. Replaces the read-only
      info window for those kinds; kinds WITH an editor are unaffected.
    * ``param_editors=False`` - NO kind opens a built-in parameter editor (it
      takes a Tk grab): every activation goes to ``on_pill_activate``, or does
      nothing when there is none, and a freshly inserted incomplete pill is not
      prompted for.
    * ``emoji`` - a provider with ``split(text) -> [(is_emoji, chunk), ...]``
      (lossless), ``image(seq) -> PhotoImage | None`` and
      ``from_shortcode(name) -> str | None`` (duck-typed; this module imports
      no emoji module). Each sequence that has an image is drawn as ONE inline
      image, typed pieces are re-clustered, and a typed ``:name:`` converts.
      The MODEL does not change - an emoji is still plain characters in a
      ``TextRun``. A falsy value (None, False) means no provider. See the
      ``emoji`` section near the end of the class.
    """

    def __init__(self, master, resolve_label: Callable,
                 system_completions: Callable = lambda: [],
                 system_labels: Callable = lambda: {},
                 channel_completions: Callable = lambda: [],
                 doctrine_tags: Callable = lambda: [],
                 on_change=None,
                 on_trigger: Callable = None,
                 height=14, *,
                 # -- opt-in host seams (see the class docstring) --
                 style_toolbar: bool = True,
                 kind_glyphs: dict | None = None,
                 on_pill_activate: Callable[[str, dict], None] | None = None,
                 param_editors: bool = True,
                 emoji=None,
                 **kw):
        super().__init__(master, height=height, on_change=on_change,
                         style_toolbar=style_toolbar, **kw)
        self._resolve_label = resolve_label
        self._system_completions = system_completions
        self._system_labels = system_labels
        self._channel_completions = channel_completions
        self._doctrine_tags = doctrine_tags
        self._on_trigger = on_trigger

        # Opt-in host seams. Empty overlay + no callback = the MOTD composer.
        self._kind_glyphs = dict(kind_glyphs or {})
        self._on_pill_activate = on_pill_activate
        self._param_editors = param_editors
        # The emoji provider (None = no emoji code runs; any falsy argument is
        # None), the images this canvas has embedded (image NAME -> PhotoImage,
        # kept referenced), the sequences Tk refused to embed, whether a
        # provider fault has been logged, and the typed path's post-class bindtag.
        self._emoji = emoji or None
        self._emoji_imgs: dict = {}
        self._emoji_bad: set = set()
        self._emoji_logged = False
        self._emoji_tag = None

        # name (embedded-window pathname) -> TokenRun / chip Frame
        self._pills: dict = {}
        self._pill_frames: dict = {}
        self._selected_pill = None

        # Own undo/redo: each entry is (doc_json, caret) - the caret a Tk index,
        # or with an emoji provider a count of model characters (_emoji_caret).
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._burst_open = False           # a text-typing burst is in progress
        self._burst_after = None           # after-id of the debounce timer
        self._destroyed = False            # set by the canvas's own <Destroy>

        # Drag / drop-caret state.
        self._chip_drag = None
        self._pre_drag_caret = None
        self._pre_drag_insertwidth = None

        # Clipboard fragment (internal, carries pills that the system clipboard
        # cannot) + the exact placeholder string we last wrote.
        self._clip_fragment = None
        self._clip_string = None

        self._trigger_state = None
        self._param_win = None             # most-recent param editor (test hook)
        self._loading = False              # True during set_doc rebuild → silent

        # Tk native undo destroys embedded windows — we own undo instead (§5).
        try:
            self.text.config(undo=False)
        except tk.TclError:
            pass

        self._add_undo_buttons()
        self._bind_canvas_events()
        if self._emoji is not None:
            self._emoji_bind()

    # ── construction ────────────────────────────────────────────────────────

    def _add_undo_buttons(self):
        """Add ⟲/⟳ buttons to the inherited toolbar, packed after "clear"."""
        # The inherited strip - or, under style_toolbar=False, a fresh one that
        # holds these two buttons alone. A fresh strip is born AFTER the Text, so
        # it is lowered to the front of the stacking order (= keyboard traversal
        # order, as on a default canvas) and its first button drops the 6 px gap
        # that separates it from "clear". Neither applies to the inherited strip.
        lone = self._toolbar is None
        bar = self._ensure_toolbar()
        if lone:
            bar.lower()
        common = dict(font=_CHIP_FONT, bg=self._bg_entry, fg=self._fg_text,
                      activebackground=self._fg_accent, activeforeground=BG_DARK,
                      bd=1, relief=tk.RIDGE, cursor="hand2", width=2)
        self._undo_btn = tk.Button(bar, text="⟲", command=self.undo_model, **common)
        self._undo_btn.pack(side=tk.LEFT, padx=1 if lone else (6, 1))
        attach_tooltip(self._undo_btn, "Undo (Ctrl+Z)")
        self._redo_btn = tk.Button(bar, text="⟳", command=self.redo_model, **common)
        self._redo_btn.pack(side=tk.LEFT, padx=1)
        attach_tooltip(self._redo_btn, "Redo (Ctrl+Y)")

    def _bind_canvas_events(self):
        # The canvas's own teardown: cancels the pending burst timer.
        self.bind("<Destroy>", self._on_canvas_destroy, add="+")
        t = self.text
        t.bind("<Button-1>", self._on_text_click, add="+")
        t.bind("<KeyPress>", self._on_keypress, add="+")
        t.bind("<KeyRelease>", self._on_keyrelease_trigger, add="+")
        t.bind("<Escape>", self._on_escape, add="+")
        t.bind("<Return>", self._on_return, add="+")
        t.bind("<Control-z>", self._on_ctrl_z, add="+")
        t.bind("<Control-y>", self._on_ctrl_y, add="+")
        t.bind("<Control-Shift-Z>", self._on_ctrl_shift_z, add="+")
        t.bind("<<Copy>>", self._on_copy, add="+")
        t.bind("<<Cut>>", self._on_cut, add="+")
        t.bind("<<Paste>>", self._on_paste, add="+")

    # ── model reconstruction (source of truth) ──────────────────────────────

    def get_doc(self) -> list:
        """Reconstruct the ordered run list by walking ``text.dump``.

        Text chunks (split by dump at tag boundaries) carry the style read via the
        inherited :meth:`_style_at`; window markers map through :attr:`_pills`.
        Adjacent same-style :class:`TextRun`\\ s are merged so the model is a
        canonical, rebuild-stable form.
        """
        runs: list = []
        try:
            dump = self.text.dump("1.0", "end-1c", text=True, tag=True, window=True,
                                  image=self._emoji is not None)
        except tk.TclError:
            return []
        if self._emoji is not None:
            dump = self._emoji_as_text(dump)
        for key, value, index in dump:
            if key == "text":
                color, bold, italic, underline, size = self._style_at(index)
                runs.append(TextRun(value, color=color, bold=bold, italic=italic,
                                    underline=underline, size=size))
            elif key == "window":
                run = self._pills.get(value)
                if run is not None:
                    runs.append(TokenRun(run.kind, dict(run.params)))
                else:  # orphan marker (desync bug case) — drop it, log, don't raise
                    _LOG.warning("orphan window marker %r dropped from get_doc", value)
        return self._merge_textruns(runs)

    @staticmethod
    def _same_style(a: TextRun, b: TextRun) -> bool:
        return (a.color == b.color and a.bold == b.bold and a.italic == b.italic
                and a.underline == b.underline and a.size == b.size)

    def _merge_textruns(self, runs: list) -> list:
        out: list = []
        for r in runs:
            if (isinstance(r, TextRun) and out and isinstance(out[-1], TextRun)
                    and self._same_style(out[-1], r)):
                prev = out[-1]
                out[-1] = TextRun(prev.text + r.text, color=prev.color,
                                  bold=prev.bold, italic=prev.italic,
                                  underline=prev.underline, size=prev.size)
            else:
                out.append(r)
        return out

    def _runs_in_range(self, start, end) -> list:
        """The runs contained in ``[start, end)`` (used by copy / cut)."""
        runs: list = []
        try:
            dump = self.text.dump(start, end, text=True, tag=True, window=True,
                                  image=self._emoji is not None)
        except tk.TclError:
            return []
        if self._emoji is not None:
            dump = self._emoji_as_text(dump)
        for key, value, index in dump:
            if key == "text":
                color, bold, italic, underline, size = self._style_at(index)
                runs.append(TextRun(value, color=color, bold=bold, italic=italic,
                                    underline=underline, size=size))
            elif key == "window":
                r = self._pills.get(value)
                if r is not None:
                    runs.append(TokenRun(r.kind, dict(r.params)))
        return self._merge_textruns(runs)

    def sync_check(self) -> list:
        """Assert model↔widget consistency; return the current doc.

        Verifies (1) the model is JSON round-trip idempotent, (2) the pill registry
        keys match exactly the window markers currently in the widget, and (3) no
        window marker is duplicated. Tests call this after every mutation path.
        """
        doc = self.get_doc()
        j = doc_to_json(doc)
        assert doc_to_json(doc_from_json(j)) == j, "doc JSON is not idempotent"
        dump_windows = [v for k, v, _i
                        in self.text.dump("1.0", "end-1c", window=True)
                        if k == "window"]
        assert len(dump_windows) == len(set(dump_windows)), "duplicate window marker"
        assert set(dump_windows) == set(self._pills.keys()), \
            "pill registry out of sync with the widget"
        return doc

    def _reconcile_registry(self):
        """Drop registry entries whose window is gone; destroy orphaned frames.

        Tk delivers ``<Destroy>`` asynchronously, so after any deletion the
        registry can briefly hold stale entries — this makes the pruning
        synchronous so :meth:`get_doc` / :meth:`sync_check` are correct immediately.
        """
        try:
            live = {v for k, v, _i in self.text.dump("1.0", "end-1c", window=True)
                    if k == "window"}
        except tk.TclError:
            return
        for name in list(self._pills.keys()):
            if name not in live:
                self._pills.pop(name, None)
                frame = self._pill_frames.pop(name, None)
                if frame is not None:
                    try:
                        frame.destroy()
                    except tk.TclError:
                        pass
                if self._selected_pill == name:
                    self._selected_pill = None

    # ── pills (chip creation + styling) ─────────────────────────────────────

    def _make_chip(self, run: TokenRun) -> tk.Frame:
        """Build a chip Frame (the embedded window) for ``run`` and wire its events.

        Structure: a bordered Frame containing a main Label (glyph + label text)
        and a second Label for the coloured delta suffix — Tk Labels are
        single-styled, hence two Labels in a one-row Frame.
        """
        frame = tk.Frame(self.text, bg=BG_ENTRY, highlightthickness=1,
                         highlightbackground=BORDER_COLOR, cursor="hand2")
        name = str(frame)
        frame._pill_name = name
        main = tk.Label(frame, bg=BG_ENTRY, font=_CHIP_FONT, padx=5)
        main.pack(side=tk.LEFT)
        delta = tk.Label(frame, bg=BG_ENTRY, font=_CHIP_FONT)
        delta.pack(side=tk.LEFT)
        frame._pill_main = main
        frame._pill_delta = delta
        frame._pill_label = None
        for w in (frame, main, delta):
            w.bind("<Button-1>", lambda e, n=name: self._chip_press(e, n), add="+")
            w.bind("<B1-Motion>", self._chip_motion, add="+")
            w.bind("<ButtonRelease-1>", self._chip_release, add="+")
            w.bind("<Double-Button-1>",
                   lambda e, n=name: self._chip_double(e, n), add="+")
            w.bind("<Button-3>", lambda e, n=name: self._chip_menu(e, n), add="+")
        # The tooltip is attached inside _style_chip (guarded on text change) so a
        # freshly-built chip gets exactly one attach.
        self._style_chip(frame, run, self._resolve_label(run))
        return frame

    def _glyph_for(self, kind) -> str:
        """The chip glyph for ``kind``: this instance's ``kind_glyphs`` overlay
        first, then the module :data:`GLYPHS`, then the default."""
        overlay = self._kind_glyphs
        if kind in overlay:
            return overlay[kind]
        return GLYPHS.get(kind, _DEFAULT_GLYPH)

    def _style_chip(self, frame, run, label):
        """Apply ``label`` (a TokenLabel) to an existing chip — no widget churn."""
        glyph = self._glyph_for(run.kind)
        prefix = "" if label.resolved else "!"
        frame._pill_main.config(text=f"{prefix}{glyph} {label.text}",
                                fg=FG_WHITE if label.resolved else FG_DIM)
        d = label.delta or 0
        if d > 0:
            frame._pill_delta.config(text=f" +{d}", fg=FG_GREEN)
        elif d < 0:
            frame._pill_delta.config(text=f" −{abs(d)}", fg=FG_RED)
        else:
            frame._pill_delta.config(text="")
        if self._selected_pill == frame._pill_name:
            border = FG_ACCENT
        else:
            border = BORDER_COLOR if label.resolved else FG_YELLOW
        frame.config(highlightbackground=border, highlightcolor=border)
        # attach_tooltip is append-only (binds <Enter>/<Leave>/<Destroy> with
        # add="+"), so re-attach ONLY when the tooltip text actually changed —
        # otherwise a restyle storm (e.g. an oscillating delta) leaks binds.
        if getattr(frame, "_tooltip_text", None) != label.tooltip:
            attach_tooltip(frame, label.tooltip)
        frame._pill_label = label

    def _insert_token_widget(self, index, run: TokenRun):
        """Embed a chip for ``run`` at ``index``, register it, and return its name."""
        frame = self._make_chip(run)
        self.text.window_create(index, window=frame, align="center", padx=1)
        name = str(frame)
        self._pills[name] = run
        self._pill_frames[name] = frame
        frame.bind("<Destroy>",
                   lambda e, n=name: self._on_pill_destroyed(n), add="+")
        return name

    def _on_pill_destroyed(self, name):
        self._pills.pop(name, None)
        self._pill_frames.pop(name, None)
        if self._selected_pill == name:
            self._selected_pill = None

    def refresh_pills(self) -> None:
        """Re-query ``resolve_label`` for every pill; restyle changed ones in place.

        Flicker-free: no widget is destroyed or recreated, so ``id()`` of each chip
        stays stable. A pill whose resolved label is byte-identical is left
        untouched (cheap diff).
        """
        for name in list(self._pills.keys()):
            self._refresh_pill(name)

    def _refresh_pill(self, name, force=False):
        run = self._pills.get(name)
        frame = self._pill_frames.get(name)
        if run is None or frame is None:
            return
        label = self._resolve_label(run)
        if not force and frame._pill_label == label:
            return
        self._style_chip(frame, run, label)

    # ── selection ───────────────────────────────────────────────────────────

    def _select_pill(self, name):
        if self._selected_pill and self._selected_pill != name:
            self._deselect_current()
        self._selected_pill = name
        frame = self._pill_frames.get(name)
        if frame is not None:
            frame.config(highlightbackground=FG_ACCENT, highlightcolor=FG_ACCENT)
        try:
            idx = self.text.index(name)
            self.text.mark_set("insert", f"{idx}+1c")
            self.text.focus_set()
        except tk.TclError:
            pass

    def _deselect_current(self):
        name = self._selected_pill
        self._selected_pill = None
        frame = self._pill_frames.get(name) if name else None
        if frame is not None:
            label = frame._pill_label
            resolved = label.resolved if label is not None else True
            border = BORDER_COLOR if resolved else FG_YELLOW
            try:
                frame.config(highlightbackground=border, highlightcolor=border)
            except tk.TclError:
                pass

    def _clear_pill_selection(self):
        if self._selected_pill:
            self._deselect_current()

    def _on_text_click(self, event=None):
        """A Button-1 that reaches the Text body clears any active pill selection.

        Clicking in the text is text-editing intent, so it must drop the pill
        selection (and restyle the chip border) — otherwise a later Return, meant
        as a newline, hits the "edit selected pill" shortcut (owner bug). A click
        that lands on a chip is delivered to the chip's own embedded window and
        does NOT bubble to the Text's bindtags in Tk (verified), so this handler
        never fires for a chip-select click — chip selection survives. Returns
        ``None`` so the default Text binding still positions the caret / starts a
        drag-select normally.
        """
        self._clear_pill_selection()
        return None

    def _insert_adjacent_to(self, name) -> bool:
        """True when the insert caret sits immediately before or after ``name``'s
        embedded-window index — i.e. the selection is still "live" at the caret.

        ``_select_pill`` parks the caret just after the chip, so this holds right
        after a select; it goes False once the caret navigates away. It gates the
        Return edit shortcut so a stale selection can't hijack a newline.
        """
        try:
            pill_idx = self.text.index(name)
            return (self.text.compare("insert", "==", pill_idx)
                    or self.text.compare("insert", "==", f"{pill_idx}+1c"))
        except tk.TclError:
            return False

    # ── insert-at-caret API ─────────────────────────────────────────────────

    def insert_token_at_caret(self, kind: str, params: dict) -> None:
        self._snapshot()
        self._clear_pill_selection()
        name = self._insert_token_widget("insert", TokenRun(kind, dict(params or {})))
        self._after_mutation()
        self._prompt_editor_for_incomplete(name)

    def insert_text_at_caret(self, text: str) -> None:
        self._snapshot()
        self._put_text("insert", text)
        self._after_mutation()

    def _prompt_editor_for_incomplete(self, name) -> None:
        """The single choke every USER token-insert funnels through (click / Enter
        via :meth:`insert_token_at_caret`, drag-drop via :meth:`drop_at_pointer`,
        inline-trigger accept via :meth:`accept_trigger`): open the pill's own param
        editor when the token is incomplete and would otherwise be a dead pill.

        Today the only such case is a ``channel_line`` with no channel name — the
        Lines & blocks "Channel line" building block defaults to
        ``{label:CHANNEL_LINE_DEFAULT_LABEL, name:""}`` and resolves to nothing
        (originally the owner's stale "Logi:" line — the default label was "Logi"
        until the 2026-07-22 amendment renamed it to "Channel" to stop colliding
        with the "Logi:" fits line; the empty-name prompt behavior below predates
        and is independent of that rename). Prompting immediately lets the user
        pick the channel instead of being handed a dead pill. An actual channel
        item (``kind="channel"``) or a ``channel_line`` that already carries a
        name is complete and never prompts. Document-load paths (``set_doc`` /
        undo / redo / paste) go through ``_rebuild`` / ``_insert_runs_at_caret``,
        not here, so they never prompt. Nor does a canvas built with
        ``param_editors=False``: it opens no built-in editor at all."""
        if name is None:
            return
        run = self._pills.get(name)
        if run is None:
            return
        if (run.kind == "channel_line" and not (run.params or {}).get("name")
                and self._param_editors):
            self._open_param_editor(name, fresh_insert=True)

    def _cancel_fresh_insert(self, name) -> None:
        """Undo a freshly-inserted incomplete pill to net-zero: pop its insert
        snapshot and restore that exact pre-insert state, so the dead pill is gone
        AND Ctrl+Z cannot resurrect it (the insert snapshot captured the state
        BEFORE the pill, and nothing pushed the empty-pill state, so it lives in
        neither undo nor redo — as if the insert never happened). Only the
        ``_prompt_editor_for_incomplete`` prompt routes here; editing an EXISTING
        pill keeps the normal cancel-leaves-it semantics."""
        if self._undo_stack:
            cap = self._undo_stack.pop()              # discard the insert snapshot
            self._rebuild_from_capture(cap)           # restore pre-insert doc+caret
            self._reconcile_registry()
        else:                                         # defensive: nothing to pop
            try:
                self.text.delete(self.text.index(name))
            except tk.TclError:
                pass
            self._reconcile_registry()
        self._fire_change()

    def _fill_fresh_insert(self, name, new_params) -> None:
        """Complete a freshly-inserted pill IN PLACE (no new snapshot): the
        insert's own snapshot already stands for the whole 'add a channel' step, so
        one Ctrl+Z removes the finished pill instead of stepping back onto an empty
        one."""
        run = self._pills.get(name)
        if run is None:
            return
        run.params = dict(new_params)
        self._refresh_pill(name, force=True)
        self._after_mutation()

    def _insert_runs_at_caret(self, runs: list):
        for run in runs:
            if isinstance(run, TokenRun):
                self._insert_token_widget("insert", run)
            else:
                self._put_text("insert", run.text, self._style_tags(run))

    # ── document rebuild (set_doc / undo / redo) ────────────────────────────

    def _style_tags(self, run: TextRun) -> tuple:
        tags = []
        if run.color:
            tags.append(self._fg_tag(run.color))
        if run.bold:
            tags.append("bold")
        if run.italic:
            tags.append("italic")
        if run.underline:
            tags.append("underline")
        if run.size is not None and run.size != _BASE_SIZE:
            tag = f"size_{run.size}"
            if tag not in self.text.tag_names():
                self.text.tag_configure(
                    tag, font=tkfont.Font(family=_BASE_FAMILY, size=run.size))
            tags.append(tag)
        return tuple(tags)

    def _rebuild(self, doc: list):
        """Replace the whole widget content from ``doc`` (destroys/recreates pills).

        Low-level: does NOT touch the undo/redo stacks — :meth:`set_doc` clears
        them, undo/redo preserve them.
        """
        self._selected_pill = None
        for _name, frame in list(self._pill_frames.items()):
            try:
                frame.destroy()
            except tk.TclError:
                pass
        self._pills.clear()
        self._pill_frames.clear()
        try:
            self.text.config(state=tk.NORMAL)
            self.text.delete("1.0", tk.END)
        except tk.TclError:
            return
        if self._emoji is not None:
            self._emoji_imgs.clear()        # every image is redrawn below
        for run in doc:
            if isinstance(run, TokenRun):
                self._insert_token_widget(tk.END, run)
            else:
                # No re-cluster on a load: each run is split whole, and a
                # sequence is never joined across two runs' styles.
                self._put_text(tk.END, run.text, self._style_tags(run),
                               recluster=False)
        try:
            self.text.edit_modified(False)
        except tk.TclError:
            pass

    def set_doc(self, doc: list) -> None:
        """Rebuild the canvas from ``doc``; clears undo (not a user change).

        Silent load: no ``on_change`` fires during the rebuild (``_loading`` guards
        the change-fire path; a final ``edit_modified(False)`` neutralises the
        queued ``<<Modified>>`` events, mirroring the base ``set_markup`` discipline).
        """
        self._flush_burst()
        self._loading = True
        try:
            self._rebuild([r for r in doc])
        finally:
            self._loading = False
        try:
            self.text.edit_modified(False)
        except tk.TclError:
            pass
        self._undo_stack.clear()
        self._redo_stack.clear()

    # ── undo / redo (own snapshot stack) ────────────────────────────────────

    def _capture(self):
        try:
            caret = self.text.index("insert")
        except tk.TclError:
            caret = "1.0"
        if self._emoji is not None:
            # A Tk index does not survive a rebuild that regroups emoji: the
            # caret travels as a count of MODEL characters instead.
            caret = self._emoji_caret()
        return (doc_to_json(self.get_doc()), caret)

    def _push_undo(self, cap):
        self._undo_stack.append(cap)
        if len(self._undo_stack) > _UNDO_CAP:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _snapshot(self):
        """Push the CURRENT (pre-mutation) state; called before every structural
        mutation. Any pending text burst is closed first so it stays a distinct
        undo step."""
        self._flush_burst()
        self._push_undo(self._capture())

    def _rebuild_from_capture(self, cap):
        doc_json, caret = cap
        self._rebuild(doc_from_json(doc_json))
        if self._emoji is not None:
            caret = self._emoji_caret_index(caret)
        try:
            self.text.mark_set("insert", caret)
            self.text.see("insert")
        except tk.TclError:
            pass

    def undo_model(self) -> None:
        self._flush_burst()
        if not self._undo_stack:
            return
        cur = self._capture()
        prev = self._undo_stack.pop()
        self._redo_stack.append(cur)
        self._rebuild_from_capture(prev)
        self._reconcile_registry()
        self._fire_change()

    def redo_model(self) -> None:
        self._flush_burst()
        if not self._redo_stack:
            return
        cur = self._capture()
        nxt = self._redo_stack.pop()
        self._undo_stack.append(cur)
        self._rebuild_from_capture(nxt)
        self._reconcile_registry()
        self._fire_change()

    # ── text-edit burst coalescing ──────────────────────────────────────────

    def _maybe_start_burst(self):
        """Leading-edge capture: on the first key of a burst, snapshot the
        pre-edit state (KeyPress fires BEFORE the char is inserted)."""
        if self._burst_open:
            self._reset_burst_timer()
            return
        self._push_undo(self._capture())
        self._burst_open = True
        self._reset_burst_timer()

    def _reset_burst_timer(self):
        if self._burst_after is not None:
            try:
                self.after_cancel(self._burst_after)
            except Exception:
                pass
            self._burst_after = None
        if self._destroyed:
            return                       # a dead canvas never arms a timer
        try:
            self._burst_after = self.after(_TEXT_BURST_MS, self._close_burst)
        except tk.TclError:
            self._burst_after = None

    def _close_burst(self):
        if self._destroyed:
            return                       # fired against a dead canvas: no-op
        self._burst_open = False
        self._burst_after = None

    def _on_canvas_destroy(self, event=None):
        """The canvas's own ``<Destroy>``: cancel the pending burst timer.

        Without this a canvas destroyed within ``_TEXT_BURST_MS`` of a keystroke
        left its ``after`` id pending, to fire later against a dead widget. Only
        the event for the canvas ITSELF counts - a ``<Destroy>`` that reaches this
        binding for any other widget (a chip, a child, a host re-binding) must not
        end a live burst."""
        if event is not None and getattr(event, "widget", None) is not self:
            return
        self._destroyed = True
        self._flush_burst()

    def _flush_burst(self):
        if self._burst_after is not None:
            try:
                self.after_cancel(self._burst_after)
            except Exception:
                pass
        self._burst_after = None
        self._burst_open = False

    # ── mutation epilogue ───────────────────────────────────────────────────

    def _after_mutation(self):
        self._reconcile_registry()
        try:
            self.text.edit_modified(False)   # swallow the pending <<Modified>>
        except tk.TclError:
            pass
        self._fire_change()

    def _fire_change(self):
        # Suppress change notification while set_doc is loading a document
        # ("loading is not a user change" — the wiring's dirty flag depends on it).
        if self._loading:
            return
        super()._fire_change()

    # ── key handling: atomic delete, burst capture, selection ───────────────

    @staticmethod
    def _is_printable_insert(event) -> bool:
        return (bool(event.char) and len(event.char) == 1
                and event.char.isprintable()
                and not (event.state & _KEY_NOT_TYPING))

    def _on_keypress(self, event):
        ks = event.keysym
        if ks == "BackSpace":
            return self._handle_backspace(event)
        if ks == "Delete":
            return self._handle_delete(event)
        if ks in _CARET_NAV_KEYS:
            # Navigating the caret away from a selected pill drops the selection so
            # the accent highlight doesn't linger (and a later Return becomes a
            # plain newline, not the edit shortcut). Return None so the default
            # binding still performs the caret movement.
            self._clear_pill_selection()
            return None
        if self._is_printable_insert(event) or self._emoji_joiner_typed(event):
            # A real insert dismisses pill selection (caret already sits after it)
            # and opens/extends a text-edit burst for undo.
            self._clear_pill_selection()
            self._maybe_start_burst()
        return None

    def _handle_backspace(self, event):
        if self._selected_pill:
            self._remove_pill(self._selected_pill)
            return "break"
        if self._has_selection():
            self._delete_selection()
            return "break"
        if self.text.compare("insert", "==", "1.0"):
            return None                                  # nothing before the caret
        name = self._window_name_at("insert-1c")
        if name is not None:
            self._snapshot()
            self.text.delete(self.text.index("insert-1c"))
            self._after_mutation()
            return "break"
        self._maybe_start_burst()
        return None

    def _handle_delete(self, event):
        if self._selected_pill:
            self._remove_pill(self._selected_pill)
            return "break"
        if self._has_selection():
            self._delete_selection()
            return "break"
        if self.text.compare("insert", ">=", "end-1c"):
            return None                                  # nothing after the caret
        name = self._window_name_at("insert")
        if name is not None:
            self._snapshot()
            self.text.delete(self.text.index("insert"))
            self._after_mutation()
            return "break"
        self._maybe_start_burst()
        return None

    def _has_selection(self) -> bool:
        return self._selection_range() is not None

    def _delete_range(self, start, end):
        """Delete ``[start, end)`` and synchronously prune any pills it held.

        The shared model-delete primitive (no snapshot of its own) used by the
        Backspace/Delete selection path and by <<Paste>>-replaces-selection.
        """
        self.text.delete(start, end)
        self._reconcile_registry()

    def _delete_selection(self):
        rng = self._selection_range()
        if rng is None:
            return
        self._snapshot()
        self._delete_range(rng[0], rng[1])
        self._after_mutation()

    def _window_name_at(self, index):
        try:
            idx = self.text.index(index)
            dump = self.text.dump(idx, f"{idx}+1c", window=True)
        except tk.TclError:
            return None
        for key, value, _i in dump:
            if key == "window":
                return value
        return None

    # ── pill commands (remove / convert / edit) ─────────────────────────────

    def _remove_pill(self, name):
        try:
            idx = self.text.index(name)
        except tk.TclError:
            idx = None
        self._snapshot()
        if idx is not None:
            self.text.delete(idx)
        if self._selected_pill == name:
            self._selected_pill = None
        self._after_mutation()

    def _convert_to_text(self, name):
        run = self._pills.get(name)
        if run is None:
            return
        label = self._resolve_label(run)
        try:
            idx = self.text.index(name)
        except tk.TclError:
            return
        self._snapshot()
        self.text.delete(idx)
        self._put_text(idx, label.text)
        self._clear_pill_selection()
        self._after_mutation()

    def _apply_params(self, name, new_params):
        run = self._pills.get(name)
        if run is None:
            return
        self._snapshot()
        run.params = dict(new_params)
        self._refresh_pill(name, force=True)
        self._after_mutation()

    # ── chip mouse interactions (select / drag / menu / editor) ──────────────

    def _chip_press(self, event, name):
        self._chip_drag = {"name": name, "x0": event.x_root, "y0": event.y_root,
                           "moved": False, "ghost": None}
        return "break"

    def _chip_motion(self, event):
        d = self._chip_drag
        if not d:
            return
        if not d["moved"]:
            if (abs(event.x_root - d["x0"]) <= 5
                    and abs(event.y_root - d["y0"]) <= 5):
                return
            d["moved"] = True
            d["ghost"] = self._create_ghost(d["name"])
        self._move_ghost(d["ghost"], event.x_root, event.y_root)
        self.drag_feedback(event.x_root, event.y_root)
        return "break"

    def _chip_release(self, event):
        d = self._chip_drag
        self._chip_drag = None
        if d is None:
            return
        if not d["moved"]:
            self._select_pill(d["name"])           # plain click = select
            return "break"
        self._destroy_ghost(d.get("ghost"))
        self._drop_chip_move(d["name"], event.x_root, event.y_root)
        return "break"

    def _activate_pill(self, name):
        """The ONE dispatch for "the user activated pill ``name``" - double-click,
        Return with the caret against the selected pill, the chip menu's Edit entry.

        With no ``on_pill_activate`` callback (the MOTD composer) this is exactly
        :meth:`_open_param_editor`. With one, a pill whose kind has no built-in
        editor (not in :data:`PARAM_EDITOR_KINDS`) goes to the callback as
        ``(kind, dict(params))`` - a copy, so the host cannot edit the document
        through it - and no window is opened; every other pill still gets its
        editor. A raising callback is logged, never propagated into the Tk
        binding (the ``on_trigger`` precedent).

        Built with ``param_editors=False`` the canvas opens NO editor: every
        kind goes to the callback, and with no callback nothing happens."""
        if not self._param_editors:
            return self._activate_without_editor(name)
        callback = self._on_pill_activate
        if callback is not None:
            run = self._pills.get(name)
            if run is not None and run.kind not in PARAM_EDITOR_KINDS:
                try:
                    callback(run.kind, dict(run.params or {}))
                except Exception:
                    _LOG.exception("on_pill_activate callback raised")
                return None
        return self._open_param_editor(name)

    def _activate_without_editor(self, name):
        """:meth:`_activate_pill` for a ``param_editors=False`` canvas."""
        callback = self._on_pill_activate
        run = self._pills.get(name)
        if callback is not None and run is not None:
            try:
                callback(run.kind, dict(run.params or {}))
            except Exception:
                _LOG.exception("on_pill_activate callback raised")
        return None

    def _chip_double(self, event, name):
        self._chip_drag = None
        self._activate_pill(name)
        return "break"

    def _chip_menu(self, event, name):
        self._chip_drag = None
        self._select_pill(name)
        menu = tk.Menu(self, tearoff=0, bg=BG_PANEL, fg=FG_TEXT,
                       activebackground=FG_ACCENT, activeforeground=BG_DARK,
                       bd=1, relief=tk.FLAT)
        menu.add_command(label="Edit…",
                         command=lambda: self._activate_pill(name))
        menu.add_command(label="Convert to plain text",
                         command=lambda: self._convert_to_text(name))
        menu.add_command(label="Remove", command=lambda: self._remove_pill(name))
        _post_menu_at(menu, event.x_root, event.y_root)
        return "break"

    def _create_ghost(self, name):
        frame = self._pill_frames.get(name)
        text = frame._pill_main.cget("text") if frame is not None else ""
        try:
            ghost = tk.Toplevel(self)
            ghost.overrideredirect(True)
            try:
                ghost.attributes("-topmost", True)
                ghost.attributes("-alpha", 0.85)
            except tk.TclError:
                pass
            tk.Label(ghost, text=text, bg=FG_ACCENT, fg=BG_DARK, font=_CHIP_FONT,
                     padx=6, pady=2, relief=tk.SOLID, borderwidth=1).pack()
            return ghost
        except tk.TclError:
            return None

    @staticmethod
    def _move_ghost(ghost, x_root, y_root):
        if ghost is not None:
            try:
                ghost.geometry(f"+{x_root + 12}+{y_root + 10}")
            except tk.TclError:
                pass

    @staticmethod
    def _destroy_ghost(ghost):
        if ghost is not None:
            try:
                ghost.destroy()
            except tk.TclError:
                pass

    def _drop_chip_move(self, name, x_root, y_root):
        if not self._pointer_inside(x_root, y_root):
            self._restore_caret()
            return
        run = self._pills.get(name)
        if run is None:
            self._restore_caret()
            return
        x = x_root - self.text.winfo_rootx()
        y = y_root - self.text.winfo_rooty()
        drop_idx = self.text.index(f"@{x},{y}")
        old_idx = self.text.index(name)
        self._snapshot()                       # ONE snapshot for the delete+reinsert
        # Park the insert mark at the drop point; deleting the old window shifts
        # marks after it left by one automatically, so the reinsert lands right.
        self.text.mark_set("insert", drop_idx)
        self.text.delete(old_idx)
        self._insert_token_widget("insert", TokenRun(run.kind, dict(run.params)))
        self._restore_insertwidth_only()
        self._after_mutation()

    # ── drop-caret (palette-driven drag, §10) ───────────────────────────────

    def _pointer_inside(self, x_root, y_root) -> bool:
        x = x_root - self.text.winfo_rootx()
        y = y_root - self.text.winfo_rooty()
        return (0 <= x < self.text.winfo_width()
                and 0 <= y < self.text.winfo_height())

    def drag_feedback(self, x_root: int, y_root: int) -> None:
        """Move the real insert caret to the pointer and widen it as a drop marker
        while over the canvas; restore the pre-drag caret when the pointer leaves."""
        if self._pointer_inside(x_root, y_root):
            if self._pre_drag_caret is None:
                try:
                    self._pre_drag_caret = self.text.index("insert")
                    self._pre_drag_insertwidth = self.text.cget("insertwidth")
                except tk.TclError:
                    self._pre_drag_caret = None
            x = x_root - self.text.winfo_rootx()
            y = y_root - self.text.winfo_rooty()
            try:
                self.text.mark_set("insert", self.text.index(f"@{x},{y}"))
                self.text.config(insertwidth=3)
                self.text.focus_set()
            except tk.TclError:
                pass
        else:
            self._restore_caret()

    def drop_at_pointer(self, kind: str, params: dict,
                        x_root: int, y_root: int) -> bool:
        """Insert a ``kind`` pill at the pointer. Returns False (caller keeps the
        dragged item) when the pointer is outside the canvas."""
        if not self._pointer_inside(x_root, y_root):
            self._restore_caret()
            return False
        x = x_root - self.text.winfo_rootx()
        y = y_root - self.text.winfo_rooty()
        idx = self.text.index(f"@{x},{y}")
        self._snapshot()
        self.text.mark_set("insert", idx)
        name = self._insert_token_widget("insert", TokenRun(kind, dict(params or {})))
        self._restore_insertwidth_only()
        self._after_mutation()
        self._prompt_editor_for_incomplete(name)
        return True

    def cancel_drag_feedback(self) -> None:
        self._restore_caret()

    def _restore_caret(self):
        try:
            if self._pre_drag_caret is not None:
                self.text.mark_set("insert", self._pre_drag_caret)
            if self._pre_drag_insertwidth is not None:
                self.text.config(insertwidth=self._pre_drag_insertwidth)
        except tk.TclError:
            pass
        self._pre_drag_caret = None
        self._pre_drag_insertwidth = None

    def _restore_insertwidth_only(self):
        try:
            if self._pre_drag_insertwidth is not None:
                self.text.config(insertwidth=self._pre_drag_insertwidth)
        except tk.TclError:
            pass
        self._pre_drag_insertwidth = None
        self._pre_drag_caret = None

    # ── clipboard (§9) ──────────────────────────────────────────────────────

    def _on_copy(self, event=None):
        self._do_copy(cut=False)
        return "break"

    def _on_cut(self, event=None):
        self._do_copy(cut=True)
        return "break"

    def _do_copy(self, cut: bool):
        rng = self._selection_range()
        if rng is None:
            return
        start, end = rng
        runs = self._runs_in_range(start, end)
        if not runs:
            return
        parts = []
        for run in runs:
            if isinstance(run, TokenRun):
                parts.append(f"⟦{run.kind}:{self._resolve_label(run).text}⟧")
            else:
                parts.append(run.text)
        clip_str = "".join(parts)
        self._clip_fragment = doc_to_json(runs)
        self._clip_string = clip_str
        try:
            self.clipboard_clear()
            self.clipboard_append(clip_str)
        except tk.TclError:
            pass
        if cut:
            self._snapshot()
            self.text.delete(start, end)
            self._after_mutation()

    def _on_paste(self, event=None):
        try:
            clip = self.clipboard_get()
        except tk.TclError:
            clip = ""
        internal = (bool(clip) and clip == self._clip_string
                    and self._clip_fragment is not None)
        if not internal and not clip:
            return "break"
        # Match the Windows/Tk-native <<Paste>> convention: an active selection is
        # replaced by the pasted content, as ONE undo step. Snapshot once, delete
        # the selection through the registry-pruning model-delete, then insert at
        # the collapsed caret — shared by both the internal-fragment and
        # plain-text paths.
        self._snapshot()
        rng = self._selection_range()
        if rng is not None:
            self._delete_range(rng[0], rng[1])
            try:
                self.text.mark_set("insert", rng[0])
            except tk.TclError:
                pass
        if internal:
            self._insert_runs_at_caret(doc_from_json(self._clip_fragment))
        else:
            self._put_text("insert", clip)
        self._after_mutation()
        return "break"

    # ── inline triggers (§12) ───────────────────────────────────────────────

    def _on_keyrelease_trigger(self, event):
        if self._on_trigger is None:
            return
        ch = getattr(event, "char", "")
        if ch not in ("@", "/"):
            return
        try:
            caret = self.text.index("insert")
            trig = self.text.index("insert-1c")
            if self.text.get(trig, caret) != ch:
                return
        except tk.TclError:
            return
        col = int(trig.split(".")[1])
        if col == 0:
            at_start = True
        else:
            try:
                before = self.text.get(f"{trig}-1c", trig)
            except tk.TclError:
                return
            at_start = before.isspace()
        if not at_start:
            return
        bbox = self.text.bbox(trig)
        if bbox is None:
            anchor = (self.text.winfo_rootx(), self.text.winfo_rooty())
        else:
            bx, by, _bw, bh = bbox
            anchor = (self.text.winfo_rootx() + bx,
                      self.text.winfo_rooty() + by + bh)
        mode = "entity" if ch == "@" else "block"
        self._trigger_state = {"start": trig}
        try:
            self._on_trigger(mode, anchor, (trig, caret))
        except Exception:
            _LOG.exception("on_trigger callback raised")

    def accept_trigger(self, query_range, kind: str, params: dict) -> None:
        """Delete the trigger char + typed query, then insert the chosen pill
        (one undo snapshot for the pair)."""
        start, _end = query_range
        self._snapshot()
        try:
            self.text.delete(start, "insert")
            self.text.mark_set("insert", start)
        except tk.TclError:
            pass
        name = self._insert_token_widget("insert", TokenRun(kind, dict(params or {})))
        self._trigger_state = None
        self._after_mutation()
        self._prompt_editor_for_incomplete(name)

    def dismiss_trigger(self) -> None:
        self._trigger_state = None

    # ── keyboard shortcuts ──────────────────────────────────────────────────

    def _on_return(self, event=None):
        # The edit shortcut fires ONLY when the caret still sits against the
        # selected pill (immediately before or after its embedded-window index).
        # If the caret has wandered off — e.g. a navigation path that failed to
        # clear the flag — the selection is stale: drop it and let Return be a
        # plain newline (defense at the source for the owner's "move the cursor
        # and try to make a new line" bug).
        if self._selected_pill:
            if self._insert_adjacent_to(self._selected_pill):
                self._activate_pill(self._selected_pill)
                return "break"
            self._clear_pill_selection()
        # A newline is a text edit; open/extend the undo burst (Return is more
        # specific than <KeyPress>, so _on_keypress never sees it).
        self._maybe_start_burst()
        return None

    def _on_escape(self, event=None):
        if self._chip_drag is not None:
            d = self._chip_drag
            self._chip_drag = None
            self._destroy_ghost(d.get("ghost"))
            self._restore_caret()
            return "break"
        if self._selected_pill:
            self._clear_pill_selection()
            return "break"
        return None

    def _on_ctrl_z(self, event=None):
        self.undo_model()
        return "break"

    def _on_ctrl_y(self, event=None):
        self.redo_model()
        return "break"

    def _on_ctrl_shift_z(self, event=None):
        self.redo_model()
        return "break"

    # ── param editors (§7) ──────────────────────────────────────────────────

    def _open_param_editor(self, name, fresh_insert=False):
        """Open the per-kind param editor for pill ``name``.

        ``fresh_insert`` marks the ``_prompt_editor_for_incomplete`` flow — the pill
        was just inserted and is still incomplete (an empty-name channel_line). In
        that mode the dialog is a commit/abort gate rather than an edit: Cancel /
        Escape / close, AND Apply while the channel is still empty, all REMOVE the
        pill net-zero (:meth:`_cancel_fresh_insert`); Apply with a real channel
        completes it in place under the insert's own snapshot
        (:meth:`_fill_fresh_insert`). Editing an EXISTING pill (``fresh_insert``
        False — double-click / menu / Return) keeps the normal edit semantics
        (Cancel leaves the pill, Apply pushes its own undo step)."""
        run = self._pills.get(name)
        if run is None:
            return
        parent = self.winfo_toplevel()
        win = tk.Toplevel(parent)
        win.title(f"Edit {run.kind}")

        def cancel():
            if fresh_insert:
                self._cancel_fresh_insert(name)   # backing out a fresh pill = remove it
            win.destroy()

        make_modal(win, parent, on_cancel=cancel)
        # The title-bar X (WM_DELETE_WINDOW) shares the one cancel path, so closing
        # a fresh-insert prompt removes the pill net-zero instead of stranding it.
        win.protocol("WM_DELETE_WINDOW", cancel)
        reader = self._build_param_body(win, run)
        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(fill="x", padx=8, pady=(2, 8))
        btn_kw = dict(font=_CHIP_FONT, bg=self._bg_entry, fg=self._fg_text,
                      activebackground=self._fg_accent, activeforeground=BG_DARK,
                      bd=1, relief=tk.RIDGE, cursor="hand2")

        def apply():
            new_params = reader()
            if new_params is None:
                return                       # invalid (e.g. bad int) — keep open
            if fresh_insert and not (new_params or {}).get("name"):
                self._cancel_fresh_insert(name)   # applied still-empty = remove it
                win.destroy()
                return
            if fresh_insert:
                self._fill_fresh_insert(name, new_params)   # complete in place
                win.destroy()
                return
            self._apply_params(name, new_params)
            win.destroy()

        tk.Button(btns, text="Apply", command=apply, **btn_kw).pack(
            side=tk.RIGHT, padx=2)
        tk.Button(btns, text="Cancel", command=cancel, **btn_kw).pack(
            side=tk.RIGHT, padx=2)
        self._param_win = win
        return win

    def _entry(self, parent, value=""):
        e = tk.Entry(parent, bg=BG_ENTRY, fg=FG_TEXT, insertbackground=FG_WHITE,
                     relief=tk.RIDGE, bd=1)
        if value:
            e.insert(0, value)
        return e

    def _build_param_body(self, win, run):
        """Build the per-kind editor body; return a reader ``() -> params|None``."""
        kind, p = run.kind, run.params
        body = tk.Frame(win, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=8, pady=8)
        body.columnconfigure(1, weight=1)

        def label(text, row):
            tk.Label(body, text=text, bg=BG_DARK, fg=FG_TEXT, font=_CHIP_FONT
                     ).grid(row=row, column=0, sticky="w", padx=(0, 6), pady=2)

        if kind == "tag_line":
            label("Tag:", 0)
            try:
                tags = list(self._doctrine_tags() or [])
            except Exception:
                tags = []
            # Editable combobox (state="normal" → free text still allowed) seeded
            # with the selected doctrine's tags, so a stale/migrated tag_line can be
            # re-pointed at the doctrine's real tag (e.g. "Logistics" → "Logi") from
            # a dropdown. App-wide dark TCombobox styling applies (fc_gui startup).
            combo = ttk.Combobox(body, values=tags)
            combo.set(p.get("tag", ""))
            combo.grid(row=0, column=1, sticky="ew")
            return lambda: {"tag": combo.get().strip()}

        if kind in ("staging_line", "system"):
            label("System:", 0)
            e = AutocompleteEntry(body, self._system_completions(),
                                  labels=self._system_labels(), bg=BG_ENTRY,
                                  fg=FG_TEXT, insertbackground=FG_WHITE,
                                  relief=tk.RIDGE, bd=1)
            e.grid(row=0, column=1, sticky="ew")
            if p.get("name"):
                e.insert(0, p.get("name"))
            return lambda: {"name": e.get().strip()}

        if kind == "channel":
            label("Channel:", 0)
            e = AutocompleteEntry(body, self._channel_completions(), bg=BG_ENTRY,
                                  fg=FG_TEXT, insertbackground=FG_WHITE,
                                  relief=tk.RIDGE, bd=1)
            e.grid(row=0, column=1, sticky="ew")
            if p.get("name"):
                e.insert(0, p.get("name"))
            return lambda: {"name": e.get().strip()}

        if kind == "channel_line":
            label("Label:", 0)
            # Owner amendment 2026-07-22 (spec §4.2): default "Channel", not
            # "Logi" — see CHANNEL_LINE_DEFAULT_LABEL. from_legacy_fields-migrated
            # pills still carry an explicit "Logi" params label and are unaffected
            # (this default only fires when no label key is present at all).
            lab = self._entry(body, p.get("label", CHANNEL_LINE_DEFAULT_LABEL))
            lab.grid(row=0, column=1, sticky="ew")
            label("Channel:", 1)
            e = AutocompleteEntry(body, self._channel_completions(), bg=BG_ENTRY,
                                  fg=FG_TEXT, insertbackground=FG_WHITE,
                                  relief=tk.RIDGE, bd=1)
            e.grid(row=1, column=1, sticky="ew")
            if p.get("name"):
                e.insert(0, p.get("name"))
            return lambda: {"label": lab.get().strip() or CHANNEL_LINE_DEFAULT_LABEL,
                            "name": e.get().strip()}

        if kind == "fc_line":
            src = tk.StringVar(value=p.get("source", "selected"))
            radio_kw = dict(bg=BG_DARK, fg=FG_TEXT, selectcolor=BG_ENTRY,
                            activebackground=BG_DARK, activeforeground=FG_ACCENT,
                            font=_CHIP_FONT, anchor="w")
            tk.Radiobutton(body, text="Follow selected FC", variable=src,
                           value="selected", **radio_kw).grid(
                row=0, column=0, columnspan=2, sticky="w")
            tk.Radiobutton(body, text="Pinned character", variable=src,
                           value="pinned", **radio_kw).grid(
                row=1, column=0, columnspan=2, sticky="w")
            label("Name:", 2)
            name_e = self._entry(body, p.get("name") or "")
            name_e.grid(row=2, column=1, sticky="ew")
            label("Char ID:", 3)
            id_e = self._entry(body, "" if p.get("id") is None else str(p.get("id")))
            id_e.grid(row=3, column=1, sticky="ew")

            def reader():
                if src.get() == "selected":
                    return {"source": "selected"}
                try:
                    cid = int(id_e.get().strip())
                except ValueError:
                    try:
                        win.bell()
                    except tk.TclError:
                        pass
                    return None
                return {"source": "pinned", "id": cid,
                        "name": name_e.get().strip()}
            return reader

        # fit / char / unknown → read-only info; params unchanged.
        info = f"{kind}: " + ", ".join(f"{k}={v}" for k, v in p.items())
        tk.Label(body, text=info or kind, bg=BG_DARK, fg=FG_TEXT, font=_CHIP_FONT,
                 justify="left", wraplength=360).grid(
            row=0, column=0, columnspan=2, sticky="w")
        return lambda: dict(p)

    # -- emoji (the opt-in ``emoji=`` provider) --------------------------------
    #
    # Display only. The model never changes: an emoji is plain characters in a
    # TextRun; here each sequence the provider has a tile for is DRAWN as one
    # embedded image (one index unit: the caret cannot split it, one BackSpace
    # removes it) and read back as its characters. Nothing below runs, and no
    # image exists, when ``self._emoji`` is None.

    def _put_text(self, index, text, *tags, recluster=True):
        """Insert ``text`` at ``index`` - the ONE call behind every text entry
        point. ``tags`` is nothing (the text takes the tags around it, as a bare
        ``Text.insert`` does) or one tuple of tag names (exactly those). Without
        an emoji provider this IS that ``Text.insert`` call."""
        if self._emoji is None:
            self.text.insert(index, text, *tags)
            return
        self._emoji_draw(index, text, tags[0] if tags else None)
        if recluster:
            self._emoji_recluster(_EMOJI_MARK)

    def _emoji_pieces(self, text) -> list:
        """``text`` as ``[(image | None, chunk), ...]``, chunks without an image
        merged. A provider that raises, or whose split is not lossless, costs
        the pictures and never a character: the text comes back as one chunk."""
        provider = self._emoji
        bad = self._emoji_bad
        try:
            pieces = [(provider.image(chunk)
                       if is_emoji and chunk not in bad else None, chunk)
                      for is_emoji, chunk in provider.split(text)]
            if "".join(chunk for _img, chunk in pieces) != text:
                raise ValueError("emoji split lost or changed text")
        except Exception:
            self._emoji_fault("emoji provider failed; text kept as characters")
            return [(None, text)] if text else []
        out: list = []
        for img, chunk in pieces:
            if img is None and out and out[-1][0] is None:
                out[-1] = (None, out[-1][1] + chunk)
            elif chunk:
                out.append((img, chunk))
        return out

    def _emoji_fault(self, message):
        """Log a provider fault ONCE per canvas (it would otherwise repeat on
        every key press). Call from inside the ``except`` block."""
        if not self._emoji_logged:
            self._emoji_logged = True
            _LOG.exception(message)

    def _emoji_draw(self, index, text, tags=None, pieces=None):
        """Insert ``text`` at ``index``, each emoji that has a tile as an image.
        Leaves :data:`_EMOJI_MARK` just after what was inserted."""
        t = self.text
        t.mark_set(_EMOJI_MARK, index)
        if t.compare(_EMOJI_MARK, "==", "end"):
            # Tk inserts before the final newline, but a mark parked after it
            # would not follow what is inserted.
            t.mark_set(_EMOJI_MARK, "end-1c")
        for img, chunk in (self._emoji_pieces(text) if pieces is None else pieces):
            if img is not None:
                try:
                    name = t.image_create(_EMOJI_MARK, image=img,
                                          name=_emoji_name(chunk))
                except tk.TclError:
                    # Not an image Tk will embed: this sequence is text from
                    # now on - never a raise that truncates the document.
                    self._emoji_bad.add(chunk)
                    self._emoji_fault("emoji image refused by Tk; drawn as text")
                    img = None
            if img is None:
                if tags is None:
                    t.insert(_EMOJI_MARK, chunk)
                else:
                    t.insert(_EMOJI_MARK, chunk, tags)
                continue
            # Keyed by the element's own name (Tk's "#N" included): a provider
            # may answer a new object per call, and Tk shows a collected image
            # blank.
            self._emoji_imgs[name] = img
            if tags is not None:                # an image takes the tags AROUND it
                at = t.index(_EMOJI_MARK + "-1c")
                for tag in t.tag_names(at):
                    if tag not in tags:
                        t.tag_remove(tag, at)
                for tag in tags:
                    t.tag_add(tag, at)

    def _emoji_caret(self) -> int:
        """The caret as a count of MODEL characters before it (a pill = 1)."""
        count = 0
        try:
            for key, value, _index in self.text.dump(
                    "1.0", "insert", text=True, window=True, image=True):
                if key == "text":
                    count += len(value)
                elif key == "window":
                    count += 1
                else:
                    count += len(_emoji_seq(value) or "")
        except tk.TclError:
            pass
        return count

    def _emoji_caret_index(self, count) -> str:
        """The Tk index that has ``count`` model characters before it in the
        widget AS IT IS NOW - always an element boundary, so the insert mark is
        never put between the two halves of a raw astral character (a mark
        there makes ``get_doc`` return lone surrogates). A count that falls
        inside an image lands after it."""
        t = self.text
        try:
            if not isinstance(count, int):      # defensive: an old-style index
                return t.index("%s +1c -1c" % count)
            seen = 0
            for key, value, index in t.dump("1.0", "end-1c", text=True,
                                            window=True, image=True):
                if key == "text":
                    size = len(value)
                elif key == "window":
                    size = 1
                else:
                    size = len(_emoji_seq(value) or "")
                if seen + size >= count:
                    if key == "text":
                        return t.index("%s + %d chars" % (index, count - seen))
                    return index if count <= seen else t.index(index + " +1c")
                seen += size
            return t.index("end-1c")
        except tk.TclError:
            return "1.0"

    @staticmethod
    def _emoji_as_text(dump) -> list:
        """A ``dump`` with every emoji image turned back into a ``text`` item
        holding its characters, so the model walks need no image branch."""
        out = []
        for key, value, index in dump:
            if key == "image":
                value = _emoji_seq(value)
                if value is None:
                    continue                    # not an image this class drew
                key = "text"
            out.append((key, value, index))
        return out

    def _emoji_tags_at(self, index) -> tuple:
        return tuple(tag for tag in self.text.tag_names(index) if tag != "sel")

    def _emoji_recluster(self, index="insert"):
        """Regroup the emoji neighbourhood that ends at ``index``.

        The window is the contiguous run of emoji images and joinable raw
        characters ending there, plus the ONE raw character directly before
        that run (a text heart or a digit is plain text until its VS16 / keycap
        arrives). A raw character directly preceded by a ZWJ does NOT end the
        window: it is glued to what comes before it (every "person ZWJ symbol
        VS16" and hair form of the real table). The window is flattened,
        re-split and redrawn only if the split changed - so a flag typed as two
        letters, a thumb followed by a tone or a family arriving code point by
        code point ends up as ONE image. A pill or a change of style ends the
        window, and it never holds more than :data:`_EMOJI_WINDOW` elements:
        regrouping never alters the model (the same characters in the same
        styles before and after) and its cost per key press is bounded."""
        t = self.text
        try:
            end = start = t.index(index)
            parts: list = []            # newest first: (is_image, chars, selected)
            tags = None
            while len(parts) < _EMOJI_WINDOW:
                prev = t.index(start + "-1c")
                if t.compare(prev, "==", start):
                    break                       # the start of the document
                item = t.dump(prev, start, text=True, window=True, image=True)
                if len(item) != 1 or item[0][0] == "window":
                    break
                is_image = item[0][0] == "image"
                chars = _emoji_seq(item[0][1]) if is_image else item[0][1]
                if not chars:
                    break
                names = t.tag_names(prev)
                here = tuple(tag for tag in names if tag != "sel")
                if tags is None:
                    tags = here
                elif here != tags:
                    break
                parts.append((is_image, chars, "sel" in names))
                start = prev
                if (not (is_image or _emoji_joinable(chars))
                        and t.get(start + "-1c", start) != _ZWJ):
                    break                       # the one raw character before the run
            if not parts:
                return
            parts.reverse()
            drawn: list = []
            for is_image, chars, _selected in parts:
                if not is_image and drawn and not drawn[-1][0]:
                    drawn[-1] = (False, drawn[-1][1] + chars)
                else:
                    drawn.append((is_image, chars))
            flat = "".join(chars for _is_image, chars, _selected in parts)
            pieces = self._emoji_pieces(flat)
            if [(img is not None, chunk) for img, chunk in pieces] == drawn:
                return
            held = self._emoji_as_text(t.dump(start, end, text=True, window=True,
                                              image=True))
            if "".join(value for _key, value, _index in held) != flat:
                return                          # never delete what is not put back
            t.delete(start, end)
            self._emoji_draw(start, flat, tags, pieces)
            if any(selected for _is_image, _chars, selected in parts):
                self._emoji_reselect(start, [
                    selected for _is_image, chars, selected in parts for _ch in chars])
            self._emoji_forget_gone()
        except tk.TclError:
            pass

    def _emoji_reselect(self, start, flags):
        """Put the selection highlight back on what a redraw just replaced
        (``start`` .. :data:`_EMOJI_MARK`); ``flags`` says, per character of the
        redrawn text, whether it was selected. An image is one element: it is
        selected when any of its characters was."""
        t = self.text
        at = 0
        for key, value, index in t.dump(start, _EMOJI_MARK, text=True, image=True):
            if key == "image":
                size = len(_emoji_seq(value) or "")
                if any(flags[at:at + size]):
                    t.tag_add("sel", index)
            else:
                size = len(value)
                for n in range(size):
                    if flags[at + n:at + n + 1] == [True]:
                        t.tag_add("sel", "%s + %d chars" % (index, n))
            at += size

    def _emoji_forget_gone(self):
        """Drop the references to images no longer embedded (a redraw replaces
        elements, and Tk never reuses a ``#N`` name)."""
        live = {value for key, value, _index
                in self.text.dump("1.0", "end-1c", image=True) if key == "image"}
        for name in [name for name in self._emoji_imgs if name not in live]:
            del self._emoji_imgs[name]

    def _emoji_bind(self):
        """Hook typed input: a bindtag directly AFTER Tk's ``Text`` class, so the
        hook runs once the class binding has inserted the character - in the
        same key press, with no timer. The command belongs to this widget (Tk
        deletes it with the canvas); the tag's binding is cleared on destroy."""
        tag = "PillEmoji" + str(self.text)
        self._emoji_tag = tag
        tags = list(self.text.bindtags())
        tags.insert(tags.index("Text") + 1 if "Text" in tags else len(tags), tag)
        self.text.bindtags(tuple(tags))
        self.tk.call("bind", tag, "<KeyPress>",
                     self.register(self._emoji_after_key) + " %A %s")
        self.bind("<Destroy>", self._emoji_unbind, add="+")
        # Middle-click and the Insert key paste Tk's PRIMARY selection, whose
        # text omits every image: both are routed through the canvas instead.
        self.text.bind("<<PasteSelection>>", self._emoji_paste_selection, add="+")
        self.text.bind("<Insert>", lambda event: self._emoji_paste_selection(),
                       add="+")

    def _emoji_paste_selection(self, click=None):
        """``<<PasteSelection>>`` (middle-click; ``click`` is its event) and the
        Insert key, with a provider. The canvas's OWN selection is pasted as
        model runs (emoji as characters, pills as pills); a selection owned by
        another widget as text through the splitter. One undo step; always
        ``"break"``, so Tk's class binding never inserts the image-less text."""
        t = self.text
        if click is not None:
            try:        # Tk's own rule: a middle-button drag scans, it does not paste
                if int(self.tk.call("set", "::tk::Priv(mouseMoved)")):
                    return "break"
            except (tk.TclError, TypeError, ValueError):
                pass
        rng = self._selection_range()
        runs, text = [], ""
        try:
            if rng is not None:
                runs = self._runs_in_range(rng[0], rng[1])
            else:
                text = t.selection_get()
        except tk.TclError:
            pass
        if not runs and not text:
            return "break"
        where = "insert"
        if click is not None:
            try:
                where = self.tk.call("tk::TextClosestGap", t, click.x, click.y)
            except (tk.TclError, AttributeError):
                where = "insert"
        self._snapshot()
        try:
            t.mark_set("insert", where)
        except tk.TclError:
            pass
        if runs:
            self._insert_runs_at_caret(runs)
        else:
            self._put_text("insert", text)
        self._after_mutation()
        if click is not None:
            try:
                t.focus_set()
            except tk.TclError:
                pass
        return "break"

    def _emoji_unbind(self, event=None):
        if event is not None and getattr(event, "widget", None) is not self:
            return
        try:
            self.tk.call("bind", self._emoji_tag, "<KeyPress>", "")
        except tk.TclError:
            pass
        self._emoji_imgs.clear()

    def _emoji_joiner_typed(self, event) -> bool:
        """A typed ZWJ or tag character is not "printable", yet Tk inserts it:
        with a provider it opens the undo burst like any other typed character."""
        if self._emoji is None:
            return False
        char = getattr(event, "char", "")
        return (_emoji_joinable(char)
                and not (getattr(event, "state", 0) & _KEY_NOT_TYPING))

    def _emoji_after_key(self, char="", state=0):
        """The post-class ``<KeyPress>`` hook (``%A``, ``%s``): what was just
        typed goes through the splitter, its neighbourhood is re-clustered, and
        a closing colon may complete a shortcode."""
        if self._destroyed or not char:
            return
        try:
            state = int(state)
        except (TypeError, ValueError):
            state = 0
        code = ord(char[0])
        if code < 0x20 or code == 0x7F or state & _KEY_NOT_TYPING:
            return                              # not a key Tk inserts
        t = self.text
        try:
            start = t.index("insert-%dc" % len(char))
            if t.get(start, "insert") != char:
                return                          # nothing was inserted
            pieces = self._emoji_pieces(char)
            if any(img is not None for img, _chunk in pieces):
                tags = self._emoji_tags_at(start)
                t.delete(start, "insert")
                self._emoji_draw("insert", char, tags, pieces)
            self._emoji_recluster("insert")
            if char == ":":
                self._emoji_shortcode()
            if self._pending_armed:
                # MarkupEditor applies an armed typing style at key RELEASE,
                # from the index it noted at key press. A redraw above may have
                # moved everything: re-aim it at the element just typed, so the
                # tag never starts inside a raw astral character.
                self._pending_press_idx = t.index("insert-1c")
        except tk.TclError:
            pass

    def _emoji_shortcode(self):
        """The caret follows a just-typed ``:``. Replace ``:name:`` by its emoji
        (one undo step) when the name has no whitespace and no ``<`` / ``>``,
        the opening colon is at the line start or NOT preceded by a letter,
        digit or underscore (an image or a pill before it is none of those),
        the span is not inside a ``<...>`` token being typed, and the provider
        knows the name. A token holds no whitespace, so an unclosed ``<``
        followed by whitespace ("ETA <10 min", "a < b") is not one - the rule
        of ``emoji_data.replace_shortcodes``."""
        t = self.text
        close = t.index("insert-1c")
        line = t.index("insert linestart")
        opening = t.search(":", close, stopindex=line, backwards=True)
        if not opening:
            return
        name = t.get(opening + "+1c", close)
        if not name or any(ch.isspace() or ch in "<>" for ch in name):
            return
        if t.dump(opening, close, window=True, image=True):
            return                              # a pill or an image in the name
        before = t.get(opening + "-1c", opening)
        if before.isalnum() or before == "_":
            return
        head = t.get(line, opening)
        opened = head.rfind("<")
        if (opened > head.rfind(">")
                and not any(ch.isspace() for ch in head[opened:])):
            return                              # inside a <...> token
        try:
            seq = self._emoji.from_shortcode(name)
        except Exception:
            self._emoji_fault("emoji provider failed; shortcode left as typed")
            return
        if not seq or not isinstance(seq, str):
            return
        tags = self._emoji_tags_at(opening)
        self._snapshot()                        # the typed text is the undo step
        t.delete(opening, "insert")
        self._emoji_draw("insert", seq, tags)
        self._emoji_recluster("insert")
        self._after_mutation()

    # ── legacy MarkupEditor overrides (§14) ─────────────────────────────────

    def get_markup(self) -> str:
        """Serialise the TEXT RUNS ONLY (pills excluded) to EVE markup.

        Legacy-compat: the base :meth:`MarkupEditor.get_markup` walks the Text
        char-by-char and would mis-align styles across embedded windows, so this
        model-backed override replaces it. Used by no new code path — it just
        keeps the MarkupEditor contract non-crashing for any legacy caller.
        """
        segs: list = []
        for run in self.get_doc():
            if not isinstance(run, TextRun):
                continue
            for i, piece in enumerate(run.text.split("\n")):
                if i:
                    segs.append(motd_markup.Segment(newline=True))
                if piece:
                    segs.append(motd_markup.Segment(
                        text=piece, color=run.color, bold=run.bold,
                        italic=run.italic, underline=run.underline, size=run.size))
        return motd_markup.segments_to_markup(segs)

    def set_markup(self, markup: str) -> None:
        """Replace content with styled TextRuns parsed from ``markup`` (no pills).

        Legacy-compat: mirrors the base contract (a plain styled load) but through
        the model-rebuild path so the pill registry stays clean. Not a user change.
        """
        runs: list = []
        for seg in motd_markup.parse_markup(markup or ""):
            if seg.newline:
                runs.append(TextRun("\n"))
            elif seg.text:
                runs.append(TextRun(seg.text, color=seg.color, bold=seg.bold,
                                    italic=seg.italic, underline=seg.underline,
                                    size=seg.size))
        self._rebuild(self._merge_textruns(runs))
