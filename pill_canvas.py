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
from typing import Callable

import markup_editor
import motd_markup
from markup_editor import MarkupEditor
from motd_doc import TextRun, TokenRun, doc_to_json, doc_from_json
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

_BASE_FAMILY = "Consolas"
_BASE_SIZE = 11              # mirrors markup_editor._BASE_SIZE (the "no size tag" size)
_CHIP_FONT = ("Consolas", 9)
_UNDO_CAP = 100
_TEXT_BURST_MS = 400        # debounce window that coalesces a typing burst -> 1 undo


class PillCanvas(MarkupEditor):
    """A :class:`MarkupEditor` with embedded live-token pills + a model + own undo.

    See the module docstring for the architecture. The frozen public surface used
    by the Task-4 wiring is: :meth:`get_doc` / :meth:`set_doc`,
    :meth:`insert_token_at_caret` / :meth:`insert_text_at_caret`,
    :meth:`refresh_pills`, :meth:`drag_feedback` / :meth:`drop_at_pointer` /
    :meth:`cancel_drag_feedback`, :meth:`undo_model` / :meth:`redo_model`,
    :meth:`accept_trigger` / :meth:`dismiss_trigger`, and the
    :meth:`get_markup` / :meth:`set_markup` legacy-compat overrides.
    """

    def __init__(self, master, resolve_label: Callable,
                 system_completions: Callable = lambda: [],
                 system_labels: Callable = lambda: {},
                 channel_completions: Callable = lambda: [],
                 on_change=None,
                 on_trigger: Callable = None,
                 height=14, **kw):
        super().__init__(master, height=height, on_change=on_change, **kw)
        self._resolve_label = resolve_label
        self._system_completions = system_completions
        self._system_labels = system_labels
        self._channel_completions = channel_completions
        self._on_trigger = on_trigger

        # name (embedded-window pathname) -> TokenRun / chip Frame
        self._pills: dict = {}
        self._pill_frames: dict = {}
        self._selected_pill = None

        # Own undo/redo: each entry is (doc_json, caret_index).
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._burst_open = False           # a text-typing burst is in progress
        self._burst_after = None           # after-id of the debounce timer

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

    # ── construction ────────────────────────────────────────────────────────

    def _add_undo_buttons(self):
        """Add ⟲/⟳ buttons to the inherited toolbar, packed after "clear"."""
        slaves = self.grid_slaves(row=0, column=0)
        if not slaves:
            return
        bar = slaves[0]
        common = dict(font=_CHIP_FONT, bg=self._bg_entry, fg=self._fg_text,
                      activebackground=self._fg_accent, activeforeground=BG_DARK,
                      bd=1, relief=tk.RIDGE, cursor="hand2", width=2)
        self._undo_btn = tk.Button(bar, text="⟲", command=self.undo_model, **common)
        self._undo_btn.pack(side=tk.LEFT, padx=(6, 1))
        attach_tooltip(self._undo_btn, "Undo (Ctrl+Z)")
        self._redo_btn = tk.Button(bar, text="⟳", command=self.redo_model, **common)
        self._redo_btn.pack(side=tk.LEFT, padx=1)
        attach_tooltip(self._redo_btn, "Redo (Ctrl+Y)")

    def _bind_canvas_events(self):
        t = self.text
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
            dump = self.text.dump("1.0", "end-1c", text=True, tag=True, window=True)
        except tk.TclError:
            return []
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
            dump = self.text.dump(start, end, text=True, tag=True, window=True)
        except tk.TclError:
            return []
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

    def _style_chip(self, frame, run, label):
        """Apply ``label`` (a TokenLabel) to an existing chip — no widget churn."""
        glyph = GLYPHS.get(run.kind, _DEFAULT_GLYPH)
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
        """Embed a chip for ``run`` at ``index`` and register it."""
        frame = self._make_chip(run)
        self.text.window_create(index, window=frame, align="center", padx=1)
        name = str(frame)
        self._pills[name] = run
        self._pill_frames[name] = frame
        frame.bind("<Destroy>",
                   lambda e, n=name: self._on_pill_destroyed(n), add="+")

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

    # ── insert-at-caret API ─────────────────────────────────────────────────

    def insert_token_at_caret(self, kind: str, params: dict) -> None:
        self._snapshot()
        self._clear_pill_selection()
        self._insert_token_widget("insert", TokenRun(kind, dict(params or {})))
        self._after_mutation()

    def insert_text_at_caret(self, text: str) -> None:
        self._snapshot()
        self.text.insert("insert", text)
        self._after_mutation()

    def _insert_runs_at_caret(self, runs: list):
        for run in runs:
            if isinstance(run, TokenRun):
                self._insert_token_widget("insert", run)
            else:
                self.text.insert("insert", run.text, self._style_tags(run))

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
        for run in doc:
            if isinstance(run, TokenRun):
                self._insert_token_widget(tk.END, run)
            else:
                self.text.insert(tk.END, run.text, self._style_tags(run))
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
        try:
            self._burst_after = self.after(_TEXT_BURST_MS, self._close_burst)
        except tk.TclError:
            self._burst_after = None

    def _close_burst(self):
        self._burst_open = False
        self._burst_after = None

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
                and not (event.state & (0x4 | 0x8 | 0x20000)))

    def _on_keypress(self, event):
        ks = event.keysym
        if ks == "BackSpace":
            return self._handle_backspace(event)
        if ks == "Delete":
            return self._handle_delete(event)
        if self._is_printable_insert(event):
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
        self.text.insert(idx, label.text)
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

    def _chip_double(self, event, name):
        self._chip_drag = None
        self._open_param_editor(name)
        return "break"

    def _chip_menu(self, event, name):
        self._chip_drag = None
        self._select_pill(name)
        menu = tk.Menu(self, tearoff=0, bg=BG_PANEL, fg=FG_TEXT,
                       activebackground=FG_ACCENT, activeforeground=BG_DARK,
                       bd=1, relief=tk.FLAT)
        menu.add_command(label="Edit…",
                         command=lambda: self._open_param_editor(name))
        menu.add_command(label="Convert to plain text",
                         command=lambda: self._convert_to_text(name))
        menu.add_command(label="Remove", command=lambda: self._remove_pill(name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
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
        self._insert_token_widget("insert", TokenRun(kind, dict(params or {})))
        self._restore_insertwidth_only()
        self._after_mutation()
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
            self.text.insert("insert", clip)
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
        self._insert_token_widget("insert", TokenRun(kind, dict(params or {})))
        self._trigger_state = None
        self._after_mutation()

    def dismiss_trigger(self) -> None:
        self._trigger_state = None

    # ── keyboard shortcuts ──────────────────────────────────────────────────

    def _on_return(self, event=None):
        if self._selected_pill:
            self._open_param_editor(self._selected_pill)
            return "break"
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

    def _open_param_editor(self, name):
        run = self._pills.get(name)
        if run is None:
            return
        parent = self.winfo_toplevel()
        win = tk.Toplevel(parent)
        win.title(f"Edit {run.kind}")
        make_modal(win, parent, on_cancel=win.destroy)
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
            self._apply_params(name, new_params)
            win.destroy()

        tk.Button(btns, text="Apply", command=apply, **btn_kw).pack(
            side=tk.RIGHT, padx=2)
        tk.Button(btns, text="Cancel", command=win.destroy, **btn_kw).pack(
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
            e = self._entry(body, p.get("tag", ""))
            e.grid(row=0, column=1, sticky="ew")
            return lambda: {"tag": e.get().strip()}

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
            lab = self._entry(body, p.get("label", "Logi"))
            lab.grid(row=0, column=1, sticky="ew")
            label("Channel:", 1)
            e = AutocompleteEntry(body, self._channel_completions(), bg=BG_ENTRY,
                                  fg=FG_TEXT, insertbackground=FG_WHITE,
                                  relief=tk.RIDGE, bd=1)
            e.grid(row=1, column=1, sticky="ew")
            if p.get("name"):
                e.insert(0, p.get("name"))
            return lambda: {"label": lab.get().strip() or "Logi",
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
