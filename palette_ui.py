# palette_ui.py
"""Palette editor - a preset list, eight role colours and a live sample.

The editor only RETURNS a choice. It writes no file and changes no colour of
the running app: a palette is applied at startup and nowhere else (see
``palette.py``), so the host that opened the editor decides what to do with the
result - save it and restart, save it for the next launch, or keep it as the
draft of a group file. That is also why the editor's own chrome is drawn in the
LIVE theme (``ui_theme``) while the sample panel alone is drawn in the DRAFT
colours: the sample is the only place the draft can be seen before a restart.

Two halves, split at the ``Tk shell`` marker below:

* PURE (above it): ``sample_rows`` - what the sample panel shows for a set of
  roles - and ``problem_lines``. No ``tkinter``, testable without a display.
* Tk (below it): ``open_editor`` and its controller, hung on the window as
  ``win._pe``.

The window never takes the application input hold (``make_modal(...,
grab=False)``): while one is held every other FCTool window - the FCPreview
tiles included - is dead to clicks. The host owns the one-editor-at-a-time
guard that the hold would otherwise have provided.
"""
from __future__ import annotations

import logging

import palette

log = logging.getLogger(__name__)

# -- Sample model -------------------------------------------------------------
# The words shown for the status colours. A mirror of palette's private label
# table, so the sample and the failing-rule sentences name a colour the same
# way; tests/test_palette_ui.py pins the two against each other.
STATUS_TEXT = {
    "green": "Green",
    "red": "Red",
    "orange": "Orange",
    "yellow": "Yellow",
    "magenta": "Magenta",
    "under": "Soft red",
}

# The failing-rule list is CAPPED at this many lines: the window keeps one size
# whatever the draft is, so no button moves under the pointer mid-edit.
PROBLEM_LINES = 4


def _clean(roles):
    """Anything -> a fresh dict of exactly the eight roles, each a normalised
    colour; a missing or unusable role is its stock colour. Total."""
    if not isinstance(roles, dict):
        return dict(palette.DEFAULT)
    return {role: (palette.normalize_hex(roles.get(role))
                   or palette.DEFAULT[role])
            for role in palette.ROLES}


def sample_rows(roles):
    """The sample panel for ``roles``: a list of ``{"kind", "bg", "fg",
    "text"}`` rows. ``fg`` is the ink, ``bg`` the surface it sits on; the shell
    lays the rows out by ``kind`` and keeps this order within a line.

    One row per surface the app really draws - an accent header, a selected tab
    (Accent on Field), panel text, muted text, a field, a button (Text on
    Field), an accent button (Window on Accent), a border rule (``text`` is
    empty: the rule is a line of ``fg`` on ``bg``), a selected row (white on
    Selection) - then each status colour on Panel (``"status_panel"``) and on
    Field (``"status_field"``).

    Every colour is one of the eight roles, a ``palette.STATUS`` colour or
    ``palette.WHITE``. Total: roles that are not a dict read as the stock
    palette and an unusable role as its stock colour. The rows are new on every
    call.
    """
    c = _clean(roles)
    rows = [
        ("header", c["panel"], c["accent"], "Header"),
        ("tab", c["field"], c["accent"], "Selected tab"),
        ("text", c["panel"], c["text"], "Panel text"),
        ("muted", c["panel"], c["muted"], "Muted text"),
        ("field", c["field"], c["text"], "Field"),
        ("button", c["field"], c["text"], "Button"),
        ("accent_button", c["accent"], c["window"], "Accent button"),
        ("rule", c["panel"], c["border"], ""),
        ("selected", c["selection"], palette.WHITE, "Selected row"),
    ]
    for kind, surface in (("status_panel", "panel"), ("status_field", "field")):
        rows.extend((kind, c[surface], palette.STATUS[name], STATUS_TEXT[name])
                    for name in palette.STATUS)
    return [{"kind": kind, "bg": bg, "fg": fg, "text": text}
            for kind, bg, fg, text in rows]


def problem_lines(problems, limit=PROBLEM_LINES):
    """The failing-rule sentences as at most ``limit`` lines: all of them when
    they fit, else the first ``limit - 1`` and ``"+N more"``."""
    found = [str(line) for line in problems or ()]
    if len(found) <= limit:
        return found
    return found[:limit - 1] + [f"+{len(found) - limit + 1} more"]


# ── Tk shell ─────────────────────────────────────────────────────────────────
# Nothing above this line uses Tk, and nothing below it is needed by the model.
import tkinter as tk                                              # noqa: E402
from tkinter import ttk                                           # noqa: E402

from ui_helpers import (                                          # noqa: E402
    attach_tooltip, make_color_swatch, make_modal,
)
from ui_theme import (                                            # noqa: E402
    BG_DARK, BG_ENTRY, BORDER_COLOR, FG_ACCENT, FG_DIM, FG_ORANGE, FG_TEXT,
    FG_WHITE,
)

TITLE = "Colour palette"
NOTE = "Colours change the next time FCTool starts."
STOCK_PRESET = "Navy (stock)"
_SWATCH_TIP = "Click to choose a colour. Right-click for the stock colour."

# Positive sizes are POINTS, so every font below follows ``tk scaling``.
_LABEL_FONT = ("Consolas", 10)
_HINT_FONT = ("Consolas", 9)
_SAMPLE_FONT = ("Consolas", 9)
_SAMPLE_BOLD = ("Consolas", 9, "bold")

_APPLY_ACTIONS = ("apply_now", "apply_later", "use")
# mode -> its buttons, left to right: (key, label, ttk style). The key is the
# ``finish`` action, except "reset", which only loads the stock colours.
_MODE_BUTTONS = {
    "local": (("apply_now", "Apply and restart", "Green.TButton"),
              ("apply_later", "Apply at next launch", "Dark.TButton"),
              ("reset", "Reset to stock", "Dark.TButton"),
              ("cancel", "Cancel", "Dark.TButton")),
    "draft": (("use", "Use these colours", "Green.TButton"),
              ("reset", "Reset to stock", "Dark.TButton"),
              ("cancel", "Cancel", "Dark.TButton")),
}

# The sample panel, top to bottom: the row kinds that share one line.
_SAMPLE_LINES = (
    ("header", "text", "muted", "tab"),
    ("field", "button", "accent_button"),
    ("rule",),
    ("selected",),
    ("status_panel",),
    ("status_field",),
)
_RAISED = dict(padx=10, pady=1, relief=tk.RAISED, bd=1)
# kind -> (Label options, pack options). "rule" is a 1 px Frame, not a Label.
_SAMPLE_WIDGETS = {
    "header": (dict(font=_SAMPLE_BOLD), dict(side=tk.LEFT)),
    "tab": (dict(padx=10, pady=1), dict(side=tk.RIGHT)),
    "text": ({}, dict(side=tk.LEFT, padx=(16, 0))),
    "muted": ({}, dict(side=tk.LEFT, padx=(16, 0))),
    "field": (dict(width=18, anchor=tk.W, padx=6, pady=1), dict(side=tk.LEFT)),
    "button": (_RAISED, dict(side=tk.LEFT, padx=(8, 0))),
    "accent_button": (dict(_RAISED, font=_SAMPLE_BOLD),
                      dict(side=tk.LEFT, padx=(8, 0))),
    "selected": (dict(anchor=tk.W, padx=6, pady=1), dict(fill=tk.X)),
    "status_panel": ({}, dict(side=tk.LEFT, padx=(0, 10))),
    "status_field": ({}, dict(side=tk.LEFT, padx=(6, 4))),
}


class _Editor:
    """The editor's controller, hung on the window as ``win._pe``.

      ``vars``           role -> ``tk.StringVar``; always a normalised colour
      ``preset_var``     the preset box's text: a preset name or ``"Custom"``
      ``problems``       the failing contrast rules of the draft (list of str)
      ``buttons``        key -> ``ttk.Button``, in display order
      ``apply_buttons``  the apply / use buttons; disabled while ``problems``
      ``rows``           role -> its row's ``label`` / ``swatch`` / ``entry`` /
                         ``hex_var`` / ``hint``
      ``sample_widgets`` one widget per ``sample_rows`` row, same order
      ``set_preset(name)`` / ``set_role(role, value)`` / ``commit_hex(role)``
      ``current()``      the draft, a new eight-role dict
      ``finish(action)`` / ``cancel()``

    EVERY CHANGE GOES THROUGH ``_refresh``. The role vars have three writers -
    a preset, a swatch, a typed hex - and nothing observes them (no variable
    trace), so each writer ends in ``_refresh``, which re-derives everything
    that shows the draft: the hex fields (all but the one being typed in), the
    swatch faces, the preset box, the failing rules, the apply buttons and the
    sample.

    ``on_done`` IS CALLED EXACTLY ONCE, however the window goes: an apply /
    use button, or - all four of them ``cancel`` - the Cancel button, Escape,
    the window X, a destroy from outside. It is called AFTER the window is
    destroyed, so a host that restarts the app or opens something else never
    does so from under a live editor; whatever it raises is logged, not
    re-raised.
    """

    def __init__(self, win, roles, *, on_done, mode, name):
        self.win = win
        self.mode = mode
        self.name = name if isinstance(name, str) else ""
        self._on_done = on_done
        self._done = False
        start = _clean(roles)
        self.vars = {role: tk.StringVar(win, value=start[role])
                     for role in palette.ROLES}
        self.preset_var = tk.StringVar(win, value=palette.preset_name(start))
        self.problems = []
        self.rows = {}
        self.buttons = {}
        self.apply_buttons = []
        self.sample_widgets = []
        self.preset_box = self.sample_frame = None
        self.problems_label = self.note_label = None
        self._sample_panel = self._sample_strip = None
        self._sample_lines = []

    # -- build ----------------------------------------------------------------
    def build(self):
        win = self.win
        frame = tk.Frame(win, bg=BG_DARK, padx=14, pady=12)
        frame.pack(fill=tk.BOTH, expand=True)

        top = tk.Frame(frame, bg=BG_DARK)
        top.pack(fill=tk.X)
        tk.Label(top, text="Preset:", font=_LABEL_FONT, fg=FG_TEXT,
                 bg=BG_DARK).pack(side=tk.LEFT)
        self.preset_box = ttk.Combobox(
            top, textvariable=self.preset_var, state="readonly",
            values=list(palette.PRESETS) + ["Custom"], width=14,
            font=_HINT_FONT)            # the app's combobox font
        self.preset_box.pack(side=tk.LEFT, padx=(8, 0))
        self.preset_box.bind("<<ComboboxSelected>>", self._on_preset_picked)
        self.preset_box.bind("<MouseWheel>", self._on_preset_wheel)
        # The note rides on the preset row, at its right end: a line of its
        # own is height this window does not have to spare.
        self.note_label = tk.Label(top, text=NOTE, font=_HINT_FONT, fg=FG_DIM,
                                   bg=BG_DARK, anchor=tk.E)
        self.note_label.pack(side=tk.RIGHT, padx=(12, 0))

        grid = tk.Frame(frame, bg=BG_DARK)
        grid.pack(fill=tk.X, pady=(8, 0))
        for index, role in enumerate(palette.ROLES):
            self._build_row(grid, index, role)

        self._build_sample(frame)
        self.sample_frame.pack(fill=tk.X, pady=(8, 0))

        # A fixed block of PROBLEM_LINES lines (width=1: the text never sets
        # the window's width either), so the window asks for one size whether
        # the draft passes every rule or fails all of them.
        self.problems_label = tk.Label(
            frame, font=_HINT_FONT, fg=FG_ORANGE, bg=BG_DARK, anchor=tk.NW,
            justify=tk.LEFT, width=1, height=PROBLEM_LINES)
        self.problems_label.pack(fill=tk.X, pady=(6, 0))

        btn_row = tk.Frame(frame, bg=BG_DARK)
        btn_row.pack(fill=tk.X, pady=(8, 0))
        commands = {"reset": self.reset, "cancel": self.cancel}
        for key, label, style in _MODE_BUTTONS[self.mode]:
            command = commands.get(key) or (lambda a=key: self.finish(a))
            btn = ttk.Button(btn_row, text=label, style=style, command=command)
            if key == "cancel":
                btn.pack(side=tk.RIGHT)
            else:
                btn.pack(side=tk.LEFT, padx=(0, 6))
            self.buttons[key] = btn
            if key in _APPLY_ACTIONS:
                self.apply_buttons.append(btn)

        # add="+": make_modal's <Escape> lives on the toplevel too, and a plain
        # bind() replaces every handler on its sequence.
        win.bind("<Destroy>", self._on_destroy, add="+")
        self.preset_box.focus_set()
        self._refresh()

    def _build_row(self, grid, index, role):
        label = palette.ROLE_LABELS[role]
        name = tk.Label(grid, text=label, font=_LABEL_FONT, fg=FG_TEXT,
                        bg=BG_DARK, anchor=tk.W)
        name.grid(row=index, column=0, sticky=tk.W, padx=(0, 10))
        # Right-click on a swatch is "this role's stock colour".
        swatch = make_color_swatch(grid, self.vars[role], palette.DEFAULT[role],
                                   title=f"{label} colour",
                                   on_change=self._refresh)
        swatch.grid(row=index, column=1, padx=(0, 8), pady=0)
        attach_tooltip(swatch, _SWATCH_TIP)
        # The hex field has a var of its OWN: half-typed text must not reach
        # the role var. It is committed on Enter and on leaving the field.
        hex_var = tk.StringVar(grid, value=self.vars[role].get())
        entry = tk.Entry(grid, textvariable=hex_var, width=8, font=_LABEL_FONT,
                         bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
                         relief=tk.FLAT, highlightthickness=1,
                         highlightbackground=BORDER_COLOR,
                         highlightcolor=FG_ACCENT)
        entry.grid(row=index, column=2, padx=(0, 10))
        for sequence in ("<Return>", "<FocusOut>"):
            entry.bind(sequence, lambda _e, r=role: self.commit_hex(r), add="+")
        hint = tk.Label(grid, text=palette.ROLE_HINTS[role], font=_HINT_FONT,
                        fg=FG_DIM, bg=BG_DARK, anchor=tk.W)
        hint.grid(row=index, column=3, sticky=tk.W)
        self.rows[role] = {"label": name, "swatch": swatch, "entry": entry,
                           "hex_var": hex_var, "hint": hint}

    def _build_sample(self, parent):
        """One widget per ``sample_rows`` row, laid out by ``_SAMPLE_LINES``.
        Colours and texts are set by ``_paint_sample``, never here."""
        # The backdrop is the draft Window colour: panels sit on the window.
        self.sample_frame = tk.Frame(parent, padx=8, pady=6)
        self._sample_panel = tk.Frame(self.sample_frame, padx=10, pady=4,
                                      highlightthickness=1)
        self._sample_panel.pack(fill=tk.X)
        line_of = {}
        for kinds in _SAMPLE_LINES:
            line = tk.Frame(self._sample_panel)
            line.pack(fill=tk.X, pady=2)
            self._sample_lines.append(line)
            line_of.update(dict.fromkeys(kinds, line))
        # The status colours on Field sit on a strip of the Field colour.
        self._sample_strip = tk.Frame(line_of["status_field"])
        self._sample_strip.pack(fill=tk.X)
        line_of["status_field"] = self._sample_strip

        for row in sample_rows(self.current()):
            kind = row["kind"]
            if kind == "rule":
                widget = tk.Frame(line_of[kind], height=1)
                widget.pack(fill=tk.X)
            else:
                options, packing = _SAMPLE_WIDGETS[kind]
                widget = tk.Label(line_of[kind],
                                  **{"font": _SAMPLE_FONT, **options})
                widget.pack(**packing)
            self.sample_widgets.append(widget)

    # -- the draft ------------------------------------------------------------
    def current(self):
        """The draft: a new dict of the eight roles."""
        return {role: var.get() for role, var in self.vars.items()}

    def set_preset(self, name):
        """Load the preset called ``name`` into all eight roles. A name that is
        not a preset (``"Custom"`` included) changes no colour; either way the
        preset box ends up naming what the colours really are."""
        if self._done:
            return
        preset = palette.PRESETS.get(name) if isinstance(name, str) else None
        if preset is not None:
            for role, var in self.vars.items():
                var.set(preset[role])
        self._refresh()

    def set_role(self, role, value):
        """Set one role to ``value`` (``#rgb`` / ``#rrggbb``, normalised). A
        value that is not a colour, or a role that is not a role, is ignored."""
        if self._done or role not in self.vars:
            return
        colour = palette.normalize_hex(value)
        if colour is not None:
            self.vars[role].set(colour)
        self._refresh()

    def commit_hex(self, role):
        """Take what is typed in ``role``'s hex field (Enter / focus out). The
        ``#`` may be left out: three or six hex digits are read as a colour.
        Anything that is not a colour puts the field back."""
        if self._done or role not in self.rows:
            return
        hex_var = self.rows[role]["hex_var"]
        text = hex_var.get().strip()
        if not text.startswith("#") and len(text) in (3, 6):
            text = "#" + text           # normalize_hex still checks the digits
        self.set_role(role, text)
        # By hand: _refresh skips the field that holds the focus, and on Enter
        # that is this one.
        try:
            hex_var.set(self.vars[role].get())
        except tk.TclError:
            pass

    def reset(self):
        """"Reset to stock": load the stock colours and nothing else - the user
        then applies them like any other palette."""
        self.set_preset(STOCK_PRESET)

    def _on_preset_picked(self, _event=None):
        self.set_preset(self.preset_var.get())
        try:
            self.preset_box.selection_clear()
        except tk.TclError:
            pass

    def _on_preset_wheel(self, _event=None):
        # Stock ttk scrolls a read-only combobox with the wheel: one notch over
        # the box would replace all eight roles of a custom draft, with no way
        # back. "break" stops the event before ttk's class binding sees it.
        return "break"

    def _focus_owner(self):
        """The widget holding the keyboard focus, or None."""
        try:
            return self.win.focus_get()
        except (tk.TclError, KeyError):  # KeyError: a ttk popdown has it
            return None

    def _refresh(self):
        """Re-derive everything that shows the draft (see the class doc)."""
        if self._done:
            return
        roles = self.current()
        self.problems = palette.problems(roles)
        try:
            # The hex field being typed in is left alone: rewriting it under
            # the caret would throw the half-typed text away. Its own Enter /
            # focus-out (commit_hex) settles it.
            focused = self._focus_owner()
            for role, row in self.rows.items():
                if row["entry"] is not focused:
                    row["hex_var"].set(roles[role])
                row["swatch"].configure(bg=roles[role],
                                        activebackground=roles[role])
            self.preset_var.set(palette.preset_name(roles))
            self.problems_label.configure(
                text="\n".join(problem_lines(self.problems)))
            state = tk.DISABLED if self.problems else tk.NORMAL
            for btn in self.apply_buttons:
                btn.configure(state=state)
            self._paint_sample(roles)
        except tk.TclError:
            pass                        # the window is already gone

    def _paint_sample(self, roles):
        self.sample_frame.configure(bg=roles["window"])
        self._sample_panel.configure(bg=roles["panel"],
                                     highlightbackground=roles["border"],
                                     highlightcolor=roles["border"])
        for line in self._sample_lines:
            line.configure(bg=roles["panel"])
        self._sample_strip.configure(bg=roles["field"])
        for row, widget in zip(sample_rows(roles), self.sample_widgets):
            if row["kind"] == "rule":
                widget.master.configure(bg=row["bg"])
                widget.configure(bg=row["fg"])
            else:
                widget.configure(bg=row["bg"], fg=row["fg"], text=row["text"])

    # -- the ways out ---------------------------------------------------------
    def finish(self, action):
        """Close the editor and report ``action`` to ``on_done`` - once.

        ``"apply_now"`` / ``"apply_later"`` / ``"use"`` carry the draft and its
        preset name, and are REFUSED (nothing happens, the window stays) while
        a contrast rule fails: a palette that cannot be applied never reaches
        the host. ``"cancel"`` carries no roles and the name the editor was
        opened with. Any other action is ignored. The mode decides which
        buttons exist, not which of these actions is accepted here.
        """
        if self._done:
            return
        if action == "cancel":
            roles, name = None, self.name
        elif action in _APPLY_ACTIONS:
            roles = self.current()
            if palette.problems(roles):
                return
            name = palette.preset_name(roles)
        else:
            return
        self._done = True
        self._destroy()
        self._report(action, roles, name)

    def cancel(self):
        """Cancel, Escape and the window X - one path."""
        self.finish("cancel")

    def abandon(self):
        """Drop a window that could not be built, without a report."""
        self._done = True
        self._destroy()

    def _destroy(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _on_destroy(self, event):
        # <Destroy> arrives here for every child too; only the window's own
        # counts, and only when no road above already reported.
        if self._done or str(event.widget) != str(self.win):
            return
        self._done = True
        self._report("cancel", None, self.name)

    def _report(self, action, roles, name):
        if not callable(self._on_done):
            return
        try:
            self._on_done({"action": action, "roles": roles, "name": name})
        except Exception:
            # This runs inside a Tk callback: an escaping exception would cost
            # the user a traceback dialog for a window that is already closed.
            log.warning("palette editor: on_done failed", exc_info=True)


def open_editor(parent, roles, *, on_done, mode="local", name=""):
    """Open the palette editor over ``parent``; returns its Toplevel.

    ``roles``    the colours to start from. Cleaned, never edited: a missing
                 or unusable role starts as its stock colour.
    ``on_done``  called exactly once with ``{"action", "roles", "name"}``:
                 ``action`` is ``"apply_now"`` / ``"apply_later"`` (mode
                 ``"local"``), ``"use"`` (mode ``"draft"``) or ``"cancel"``;
                 ``roles`` is the chosen eight-role dict - always one that
                 passes every contrast rule - or ``None`` for ``cancel``;
                 ``name`` is ``palette.preset_name(roles)``, or for ``cancel``
                 the ``name`` passed in here.
    ``mode``     ``"local"``: Apply and restart / Apply at next launch / Reset
                 to stock / Cancel. ``"draft"``: Use these colours / Reset to
                 stock / Cancel. Anything else raises ``ValueError``.
    ``name``     what the caller calls the palette it is handing in; only ever
                 handed back, on ``cancel``.

    The editor applies nothing itself (module doc). The window is created and
    handed to ``make_modal`` in THIS function - the shape
    ``tests/test_dialog_placement.py`` looks for - without the application
    input hold; the window X is wired to the same cancel as Escape. The caller
    owns the one-editor-at-a-time guard.
    """
    if mode not in _MODE_BUTTONS:
        raise ValueError(f"unknown palette editor mode: {mode!r}")
    win = tk.Toplevel(parent)
    win.title(TITLE)
    win.resizable(False, False)
    ctl = _Editor(win, roles, on_done=on_done, mode=mode, name=name)
    make_modal(win, parent, on_cancel=ctl.cancel, grab=False)
    win.protocol("WM_DELETE_WINDOW", ctl.cancel)
    try:
        ctl.build()
    except Exception:
        ctl.abandon()                   # never leave a half-built window behind
        raise
    win._pe = ctl
    return win
