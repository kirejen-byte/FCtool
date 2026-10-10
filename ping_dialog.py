# ping_dialog.py
"""The Discord ping editor: this ping's values, the doctrine's template, a live preview.

The window behind the arrow of the MOTD builder's ``Copy ping`` button. Left, one row per
field of THIS ping (fleet type, FC, location, when, doctrine, comms). Right, the template
of the selected doctrine on the MOTD's own pill canvas. Bottom, exactly what would be
copied right now. There is no Save button: every change is stored as it is made.

What the window is GIVEN, and all it ever touches:

* ``store`` - a ``ping_store.PingStore``. Every read and write of the settings goes
  through it; the window never sees the config.
* ``env()`` - what the app knows this second (``ping_compose.Env``: the selected doctrine,
  the FC, the staging system). Asked again on every tick.
* ``when`` - the session's ``ping_compose.WhenSession``, shared with the one-click button.
* ``copy()`` - the app's ONE copy routine. The window does no clipboard work.
* ``clock()``, ``system_names()``, ``confirm(title, message)`` and the two zone converters.

Two halves, split at the ``Tk shell`` marker (the ``close_clients.py`` shape): the PURE
helpers above it decide wording, choices and what a save must do, and need no display;
the window below it only draws them.

The rules the shell keeps:

* **No input hold.** Both windows here go through ``make_modal(..., grab=False)``: a hold
  would leave every other FCTool window - the client previews included - dead to clicks.
  The opener owns the one-window-at-a-time guard.
* **A template edit never makes a copy by accident.** A doctrine with no template of its
  own shows the basic one. Typing and taking it back must leave that doctrine inheriting,
  so a save compares the canvas with what it LOADED (``save_action``).
* **A save lands where it was loaded from.** The canvas is written under the doctrine it
  was filled for, never under whatever ``env()`` names at that moment; a changed doctrine
  is noticed by ``poll``, which saves the old one first and only then loads the new one.
* **Nothing is saved from a canvas that is nobody's.** A load is all or nothing
  (``_loaded``): until it has finished, and once the canvas widget is gone, ``flush``
  writes nothing, and the next tick loads again. A question (reset, make basic) notes
  the doctrine before it asks and does nothing if the answer comes after a change.
* **Two timers, no more:** the one-second tick and the 400 ms save. ``close`` cancels
  both, and both do nothing once the window is gone. No variable trace is used anywhere.
* **A typed time is read once.** Re-filling the date and time fields (a basis switch, a
  quick button) only re-displays the stored moment; the fields are not read back, because
  inside the repeated hour of a daylight-saving night that would move the ping an hour.

Imports no ``fc_gui`` and nothing that talks to the network.
"""
from __future__ import annotations

import calendar
import functools
import logging
import time
from dataclasses import dataclass

import discord_time
import ping_compose
import ping_doc
from motd_doc import doc_to_json
from ping_store import LIST_KINDS, MAX_ITEMS, STAGING

log = logging.getLogger(__name__)

_DOT = chr(0xB7)                # middle dot
_ELLIPSIS = chr(0x2026)
_DASH = chr(0x2014)             # em dash

TITLE = "Discord ping"
LIMIT = 2000                    # Discord's message limit, in UTF-16 units
WARNINGS_SHOWN = 2
#: Under the preview in "As Discord shows it" mode: it is a likeness, not Discord.
CAPTION = ("Approximation %s times shown for your time zone; each viewer sees their own"
           % _DASH)
BASIC_IS_SHOWN = "This IS the basic template: doctrines without their own use it."
MAKE_BASIC_TIP = "Copy this doctrine's template to the basic template."

WHEN_NOW = "Now"
WHEN_AT = "At a time" + _ELLIPSIS
WHEN_HINT = "Type a time (HH:MM). Until then the ping says Now."
QUICK = (("+15m", 15), ("+30m", 30), ("+1h", 60), ("+2h", 120))
BASIS_LABELS = (("eve", "EVE time"), ("local", "My local time"))

#: pick field -> the shared list its choices come from.
LIST_OF_FIELD = {"fleet_type": "fleet_types", "location": "locations", "comms": "comms"}
LIST_TITLES = {"fleet_types": "Fleet types", "locations": "Locations", "comms": "Comms"}
#: What a row shows while its list is empty.
EMPTY_NUDGE = {"fleet_type": "Add a fleet type", "comms": "Add a comms option"}
NO_FC = "(no character)"

KEEP, WRITE, DROP = "keep", "write", "drop"


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def doctrine_key(doctrine_id):
    """The id as the store keys it: ``None`` for no doctrine (``""`` included)."""
    return None if doctrine_id is None or doctrine_id == "" else doctrine_id


def title_for(env) -> str:
    """``Discord ping . <doctrine>``, or ``. basic template`` with none selected."""
    if doctrine_key(env.doctrine_id) is None:
        name = "basic template"
    else:
        name = _clean(env.doctrine_name) or "(unnamed doctrine)"
    return "%s %s %s" % (TITLE, _DOT, name)


def utf16_units(text) -> int:
    """The length Discord counts: UTF-16 code units. Never raises."""
    if not isinstance(text, str):
        return 0
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def count_text(text) -> tuple:
    """``("312 / 2000", over)`` - ``over`` once the ping is past Discord's limit."""
    units = utf16_units(text)
    return "%d / %d" % (units, LIMIT), units > LIMIT


def warning_line(warnings, limit: int = WARNINGS_SHOWN) -> str:
    """The first ``limit`` warnings on one line, then ``+N more``."""
    found = [str(warning) for warning in warnings or ()]
    shown = found[:limit]
    if len(found) > limit:
        shown.append("+%d more" % (len(found) - limit))
    return ("  %s  " % _DOT).join(shown)


def location_choices(items, staging_name) -> list:
    """``[(label, value)]`` for the Location row: the live staging entry, then the list."""
    staging = "Staging: %s" % (_clean(staging_name) or "(not set)")
    return [(staging, STAGING)] + [(item, item) for item in items]


def doctrine_choices(text, doctrine_name) -> list:
    """``[(label, mode)]`` for the Doctrine row: the text, then the doctrine's own name."""
    return [(text, "text"), (_clean(doctrine_name) or "(no doctrine selected)", "name")]


def index_of(choices, value) -> int:
    """Where ``value`` is among ``[(label, value)]``; the first when absent, -1 when empty."""
    for index, (_label, candidate) in enumerate(choices):
        if candidate == value:
            return index
    return 0 if choices else -1


@functools.lru_cache(maxsize=1)
def _time_prefixes() -> frozenset:
    """Every PROPER prefix of every time ``discord_time.parse_time`` accepts
    (``HH:MM``, ``H:MM``, ``HHMM``) - a few hundred short strings, built once."""
    valid = []
    for hour in range(24):
        for minute in range(60):
            valid.append("%02d:%02d" % (hour, minute))
            valid.append("%02d%02d" % (hour, minute))
            if hour < 10:
                valid.append("%d:%02d" % (hour, minute))
    return frozenset(text[:cut] for text in valid for cut in range(len(text)))


def _date_can_complete(text: str) -> bool:
    """Is ``text`` the start of some date ``discord_time.parse_date`` accepts?"""
    shape = "dddd-dd-dd"
    if len(text) >= len(shape):
        return False
    for char, slot in zip(text, shape):
        if (char not in "0123456789") if slot == "d" else (char != "-"):
            return False
    years = range(discord_time.MIN_YEAR, discord_time.MAX_YEAR + 1)
    year, month, day = text[:4], text[5:7], text[8:10]
    if len(year) < 4:
        return any(str(candidate).startswith(year) for candidate in years)
    if int(year) not in years:
        return False
    if len(month) < 2:
        return any(("%02d" % candidate).startswith(month) for candidate in range(1, 13))
    if not 1 <= int(month) <= 12:
        return False
    last = calendar.monthrange(int(year), int(month))[1]
    return any(("%02d" % candidate).startswith(day) for candidate in range(1, last + 1))


def incomplete(field: str, text) -> bool:
    """Could ``text`` still BECOME a valid entry of ``field`` (``"date"`` / ``"time"``)
    by typing more? ``""`` and ``19:`` could; ``25`` and ``2026-13`` never will. (Asked
    only about an entry that is not valid as it stands.)"""
    text = _clean(text)
    if field == "time":
        return text in _time_prefixes()
    return field == "date" and _date_can_complete(text)


@dataclass(frozen=True)
class WhenEntry:
    """What the typed date and time came to. ``unix`` is None when they cannot be used;
    ``bad`` then names the field(s) at fault (``"date"`` / ``"time"``) and ``message``
    says why, in ``discord_time``'s own words. ``waiting``: a field that is only
    half-typed was let off (``read_when``'s ``lenient``), so the verdict is not final."""
    unix: "int | None"
    bad: tuple
    message: str
    waiting: bool = False


def read_when(date_text, time_text, basis, *, local_to_utc=None, lenient=()) -> WhenEntry:
    """Read the two typed fields. Never raises: ``discord_time`` only ever raises
    ``ValueError``, and each one becomes a ``WhenEntry`` that names its field.

    ``lenient`` names the fields still being typed: an entry there that is merely
    ``incomplete`` is not counted as bad (one that can no longer become valid is)."""
    bad, messages, waiting = [], [], False
    for field, parse, text in (("date", discord_time.parse_date, date_text),
                               ("time", discord_time.parse_time, time_text)):
        try:
            parse(text)
        except ValueError as exc:
            if field in lenient and incomplete(field, text):
                waiting = True
                continue
            bad.append(field)
            messages.append(str(exc))
    if bad:
        return WhenEntry(None, tuple(bad), messages[0], waiting)
    if waiting:
        return WhenEntry(None, (), "", True)
    try:
        unix = discord_time.to_unix(date_text, time_text, basis, local_to_utc=local_to_utc)
    except ValueError as exc:               # the two fields are fine; together they are not
        return WhenEntry(None, ("date", "time"), str(exc))
    return WhenEntry(unix, (), "")


def moved(items, index: int, step: int) -> tuple:
    """``(new list, new index)`` after moving row ``index`` by ``step`` (-1 up, +1 down).
    A move off either end, or of a row that is not there, changes nothing."""
    out = list(items)
    target = index + step
    if not (0 <= index < len(out) and 0 <= target < len(out)):
        return out, index
    out[index], out[target] = out[target], out[index]
    return out, target


def save_action(cur, last, baseline, inheriting: bool, has_own: bool) -> str:
    """What saving the canvas must do to the store: ``KEEP`` / ``WRITE`` / ``DROP``.

    ``cur`` is the canvas now, ``last`` what was last loaded or written, ``baseline`` what
    was LOADED; all three are canvas output, so they compare exactly. ``inheriting``: the
    doctrine had no template of its own when it was loaded (it showed the basic one).
    ``has_own``: it has one in the store right now.

    An inheriting doctrine whose canvas is back at what it loaded must go back to
    inheriting: ``DROP`` the copy an earlier save made, or ``KEEP`` when none was made.
    """
    if cur == last:
        return KEEP
    if inheriting and cur == baseline:
        return DROP if has_own else KEEP
    return WRITE


def reset_wording(doctrine_id) -> tuple:
    """``(button label, question title, question)`` for the reset button."""
    if doctrine_key(doctrine_id) is None:
        return ("Reset to built-in", "Reset the basic template",
                "Throw away your basic template and go back to the built-in one?")
    return ("Reset to basic", "Reset this doctrine's template",
            "Throw away this doctrine's own template? It will use the basic template again.")


def reset_tip(doctrine_id, has_own: bool) -> str:
    """The reset button's tooltip; with nothing to reset it says why the button is off."""
    if has_own:
        return reset_wording(doctrine_id)[2].split("?")[0] + "."
    if doctrine_key(doctrine_id) is None:
        return "Nothing to reset: the built-in basic template is in use."
    return "Nothing to reset: this doctrine uses the basic template."


def make_basic_message(count) -> str:
    """The question before a template becomes the basic one. ``count`` is how many
    doctrines have no template of their own, or None when that is not known."""
    if count is None:
        who = "Every doctrine without a template of its own will use it."
    elif count == 0:
        who = "No doctrine uses the basic template right now."
    elif count == 1:
        who = "1 doctrine has no template of its own and will use it."
    else:
        who = "%d doctrines have no template of their own and will use it." % count
    return "Make this template your basic template? " + who


# -- Tk shell -------------------------------------------------------------------------------
# Nothing above this line uses Tk, and nothing below it is needed by the helpers.
import tkinter as tk                                              # noqa: E402
from tkinter import font as tkfont                                # noqa: E402
from tkinter import messagebox, ttk                               # noqa: E402

from autocomplete import AutocompleteEntry                        # noqa: E402
from discord_preview import MODE_RAW, MODE_RENDERED, DiscordPreview  # noqa: E402
from pill_canvas import PillCanvas                                # noqa: E402
from ui_helpers import attach_tooltip, make_modal, update_tooltip  # noqa: E402
from ui_theme import (                                            # noqa: E402
    BG_DARK, BG_ENTRY, BG_SELECT, BORDER_COLOR, FG_ACCENT, FG_DIM, FG_RED, FG_TEXT,
    FG_WHITE, FG_YELLOW,
)

SAVE_MS = 400                   # the pause after the last template edit before it is stored
TICK_MS = 1000                  # how often the environment is read again

# Sizes are written for 96 dpi and scaled to the display (``scaled``): the fonts are in
# points and grow with it, so a fixed pixel size would starve the layout on a dense screen.
SIZE = (860, 620)
MIN_SIZE = (760, 520)
PANEL_WIDTH = 300               # the values panel; the template takes the rest

# Positive sizes are POINTS, so every font follows ``tk scaling``.
_FONT = ("Consolas", 9)
_BOLD = ("Consolas", 9, "bold")
_SMALL = ("Consolas", 8)

#: The chip glyph of each ping pill (each measured to have a width in the chip font).
KIND_GLYPHS = {
    "fleet_type": chr(0x2691), "fc": chr(0x25C6), "location": chr(0x2726),
    "when": chr(0x25D4), "doctrine": chr(0x25A4), "comms": "#",
}

_SAVED, _SAVING, _NOT_SAVED = "saved", "saving", "failed"


def scaled(widget, pixels) -> int:
    """``pixels`` at 96 dpi, as pixels of ``widget``'s display."""
    try:
        return max(1, int(round(pixels * float(widget.winfo_fpixels("1i")) / 96.0)))
    except (tk.TclError, ValueError):
        return int(pixels)


def glyph_or(widget, glyph: str, fallback: str, font=_FONT) -> str:
    """``glyph`` when the font can draw it, else ``fallback``: a few code points have no
    width at all in the fallback font here, and a button would then show nothing."""
    try:
        width = tkfont.Font(root=widget, font=font).measure(glyph)
    except Exception:
        width = 0
    return glyph if width > 0 else fallback


def _dimmed(widget, colour, towards, share=0.3) -> str:
    """``colour`` mixed ``share`` of the way to ``towards`` (both follow the palette)."""
    try:
        a, b = widget.winfo_rgb(colour), widget.winfo_rgb(towards)
    except tk.TclError:
        return colour
    return "#%02x%02x%02x" % tuple(
        int(round((x * (1 - share) + y * share) / 257.0)) for x, y in zip(a, b))


def _swallow(_event=None):
    # Stock ttk turns a read-only combobox with the wheel: one notch over a row would
    # change a pick unseen. "break" stops the event before the class binding.
    return "break"


def _label(parent, text="", *, font=_FONT, fg=FG_TEXT, **options):
    return tk.Label(parent, text=text, font=font, fg=fg, bg=BG_DARK, **options)


def _mini_button(parent, text, command, *, font=_FONT):
    """A compact button whose size does not depend on the ttk theme."""
    return tk.Button(parent, text=text, command=command, font=font, bg=BG_ENTRY,
                     fg=FG_TEXT, activebackground=BG_SELECT, activeforeground=FG_WHITE,
                     bd=1, relief=tk.RIDGE, cursor="hand2", padx=4, pady=0, takefocus=0)


def _entry(parent, width, cls=tk.Entry, *args):
    return cls(parent, *args, width=width, font=_FONT, bg=BG_ENTRY, fg=FG_WHITE,
               insertbackground=FG_WHITE, relief=tk.FLAT, highlightthickness=1,
               highlightbackground=BORDER_COLOR, highlightcolor=FG_ACCENT)


def _radio(parent, text, value, variable, command):
    return tk.Radiobutton(parent, text=text, value=value, variable=variable,
                          command=command, font=_FONT, fg=FG_TEXT, bg=BG_DARK,
                          selectcolor=BG_ENTRY, activebackground=BG_DARK,
                          activeforeground=FG_ACCENT, bd=0, highlightthickness=0,
                          padx=0, pady=0)


class PingDialog:
    """The ping editor window (module docstring). The app calls:

    ``alive()``            is the window still there?
    ``lift()``             bring it to the front (the opener's single-window guard).
    ``poll()``             one environment tick; also runs every second while open.
    ``flush()``            store a pending template edit now.
    ``focus_field(kind)``  move the keyboard focus to that field's row.
    ``refresh_when()``     read ``when`` again (after a copy used the time up).
    ``close()``            store what is pending, stop the timers, destroy the window.

    ``emoji`` is accepted and not used yet. ``doctrine_ids`` (optional, ``() -> ids``) is
    every doctrine the app has; with it the "basic template" question can say how many
    doctrines inherit, without it the question does not name a number.
    """

    def __init__(self, parent, *, store, env, when, copy,
                 clock=None, system_names=None, confirm=None,
                 emoji=False, local_to_utc=None, utc_to_local=None, doctrine_ids=None):
        self._store = store
        self._env_fn = env
        self._when = when
        self._copy = copy
        self._clock = clock if callable(clock) else (lambda: int(time.time()))
        self._system_names = system_names
        self._confirm = confirm
        self._local_to_utc = local_to_utc
        self._utc_to_local = utc_to_local
        self._doctrine_ids = doctrine_ids

        self._closed = False
        self._env = ping_compose.Env(None, "", "", "")
        self._doctrine_id = None            # the doctrine the canvas was LOADED for
        self._loaded = False                # False while a load is unfinished or failed
        self._values = ping_doc.PingValues()
        self._now_seen = 0
        self._tick_id = None                # the two timers: at most one of each pending
        self._save_id = None
        self._tick_failed = False           # each failure is logged once per window
        self._env_failed = False
        self._buttons_shown = None
        self._last_json = self._baseline = []
        self._inheriting = True
        self._rows_key = None
        self._preview_key = None
        self._warning_shown = None
        self._echo_shown = None
        self._choices = {}                  # pick field -> [(label, value)]
        self._at_time = False
        self._basis = "eve"
        self._when_seen = None              # when.unix as this window last showed it
        self._when_shown = None             # (date, time, basis) last settled or filled in
        self._left = set()                  # the time fields the user has typed in and left
        self._bad = ()
        self._text_entry = None             # the Doctrine text field, while it is open
        self._editor = None                 # the one list editor
        self.combos = {}
        self.pencils = {}
        self.pill_buttons = {}
        self.quick_buttons = {}
        self.basis_buttons = {}
        self.mode_buttons = {}

        self.win = tk.Toplevel(parent)
        self.win.title(TITLE)
        make_modal(self.win, parent, on_cancel=self.close, grab=False, center=True)
        self.win.geometry("%dx%d" % tuple(scaled(self.win, n) for n in SIZE))
        self.win.minsize(*(scaled(self.win, n) for n in MIN_SIZE))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self._build()
            self._load(self._read_env())
            self._sync_when()
            self._refresh_echo()
        except Exception:
            self._closed = True             # never leave a half-built window behind
            self._destroy()
            raise
        # add="+": make_modal's <Escape> lives on the toplevel too.
        self.win.bind("<Destroy>", self._on_destroy, add="+")
        self._arm_tick()

    # -- build ------------------------------------------------------------------------------
    def _build(self):
        win = self.win
        self._pencil = glyph_or(win, chr(0x270E), "Edit")
        self._saved_text = {
            _SAVED: glyph_or(win, "Saved " + chr(0x2713), "Saved"),
            _SAVING: "Saving" + _ELLIPSIS,
            _NOT_SAVED: "Not saved",
        }
        body = tk.Frame(win, bg=BG_DARK, padx=8, pady=6)
        body.pack(fill=tk.BOTH, expand=True)
        body.grid_columnconfigure(0, weight=0, minsize=scaled(win, PANEL_WIDTH))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=2)
        body.grid_rowconfigure(1, weight=1)
        self._build_values(body).grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._build_template(body).grid(row=0, column=1, sticky="nsew")
        self._build_preview(body).grid(row=1, column=0, columnspan=2, sticky="nsew",
                                       pady=(6, 0))
        self._build_buttons(body).grid(row=2, column=0, columnspan=2, sticky="ew",
                                       pady=(6, 0))

    def _build_values(self, parent):
        panel = self.values_panel = tk.Frame(parent, bg=BG_DARK)
        panel.grid_columnconfigure(1, weight=1)
        _label(panel, "THIS PING", font=_BOLD, fg=FG_ACCENT).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))
        row = 1
        for kind in ping_doc.PING_KINDS:
            _label(panel, ping_doc.FIELD_LABELS[kind], width=10, anchor=tk.W).grid(
                row=row, column=0, sticky="w")
            if kind == "fc":
                # width=1: a long name is clipped, it never widens the panel. The label
                # takes the focus so "go to this row" has somewhere to land.
                self.fc_label = _label(
                    panel, width=1, anchor=tk.W, takefocus=1, highlightthickness=1,
                    highlightbackground=BG_DARK, highlightcolor=FG_ACCENT)
                self.fc_label.grid(row=row, column=1, sticky="ew", pady=1)
                attach_tooltip(self.fc_label,
                               "The FC comes from the MOTD's FC / ANCHOR pick.")
                _label(panel, "(MOTD)", font=_SMALL, fg=FG_DIM).grid(row=row, column=2)
            else:
                combo = ttk.Combobox(panel, state="readonly", font=_FONT, width=12)
                combo.grid(row=row, column=1, sticky="ew", pady=1)
                combo.bind("<<ComboboxSelected>>", lambda _e, k=kind: self._on_pick(k))
                combo.bind("<MouseWheel>", _swallow)
                self.combos[kind] = combo
            if kind in LIST_OF_FIELD or kind == "doctrine":
                button = _mini_button(panel, self._pencil,
                                      lambda k=kind: self._on_pencil(k))
                button.grid(row=row, column=2, padx=(4, 0))
                attach_tooltip(button, "Edit the text" if kind == "doctrine"
                               else "Edit the list")
                self.pencils[kind] = button
            row += 1
            if kind == "when":
                self._build_when(panel).grid(row=row, column=0, columnspan=3,
                                             sticky="ew", pady=(0, 2))
                self.when_frame.grid_remove()
                row += 1
        self.combos["when"].configure(values=[WHEN_NOW, WHEN_AT])

        pills = tk.Frame(panel, bg=BG_DARK)
        pills.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        _label(pills, "Add pill:").grid(row=0, column=0, sticky="w")
        for index, kind in enumerate(ping_doc.PING_KINDS):
            button = _mini_button(pills, ping_doc.FIELD_LABELS[kind],
                                  lambda k=kind: self._add_pill(k), font=_SMALL)
            button.grid(row=index // 3, column=1 + index % 3, sticky="ew",
                        padx=(3, 0), pady=1)
            self.pill_buttons[kind] = button
        return panel

    def _build_when(self, parent):
        frame = self.when_frame = tk.Frame(parent, bg=BG_DARK)
        top = tk.Frame(frame, bg=BG_DARK)
        top.pack(fill=tk.X)
        self.date_entry = _entry(top, 10)
        self.date_entry.pack(side=tk.LEFT)
        self.time_entry = _entry(top, 5)
        self.time_entry.pack(side=tk.LEFT, padx=(3, 0))
        for name, entry in (("date", self.date_entry), ("time", self.time_entry)):
            entry.bind("<KeyRelease>", lambda _e, f=name: self._on_when_typed(f), add="+")
            entry.bind("<FocusOut>", lambda _e, f=name: self._on_when_left(f), add="+")
            entry.bind("<Return>", self._on_when_edit, add="+")
        for text, minutes in QUICK:
            button = _mini_button(top, text, lambda m=minutes: self._on_quick(m),
                                  font=_SMALL)
            button.pack(side=tk.LEFT, padx=(3, 0))
            self.quick_buttons[text] = button
        basis = tk.Frame(frame, bg=BG_DARK)
        basis.pack(fill=tk.X)
        self._basis_var = tk.StringVar(frame, value=self._basis)
        for value, text in BASIS_LABELS:
            button = _radio(basis, text, value, self._basis_var, self._on_basis)
            button.pack(side=tk.LEFT, padx=(0, 8))
            self.basis_buttons[value] = button
        self.format_combo = ttk.Combobox(
            frame, state="readonly", font=_FONT, width=12,
            values=[discord_time.PRESET_LABELS[p] for p in discord_time.PRESETS])
        self.format_combo.pack(fill=tk.X, pady=1)
        self.format_combo.bind("<<ComboboxSelected>>", self._on_format)
        self.format_combo.bind("<MouseWheel>", _swallow)
        # Two fixed lines (width=1: the text never sets the panel's width).
        self.echo_label = _label(frame, font=_SMALL, fg=FG_DIM, anchor=tk.NW,
                                 justify=tk.LEFT, width=1, height=2,
                                 wraplength=scaled(frame, PANEL_WIDTH - 16))
        self.echo_label.pack(fill=tk.X)
        return frame

    def _build_template(self, parent):
        frame = tk.Frame(parent, bg=BG_DARK)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)
        _label(frame, "TEMPLATE", font=_BOLD, fg=FG_ACCENT).grid(
            row=0, column=0, sticky="w", pady=(0, 2))
        # on_pill_activate is ALWAYS passed: without it a ping pill would open the
        # canvas's own read-only window, which takes the input hold.
        self.canvas = PillCanvas(
            frame, self._resolve_label, on_change=self._on_template_change, height=8,
            style_toolbar=False, kind_glyphs=dict(KIND_GLYPHS),
            on_pill_activate=self._on_pill_activate)
        self.canvas.text.configure(width=30)    # a request; the column stretches it
        self.canvas.grid(row=1, column=0, sticky="nsew")
        return frame

    def _build_preview(self, parent):
        frame = tk.Frame(parent, bg=BG_DARK)
        head = tk.Frame(frame, bg=BG_DARK)
        head.pack(fill=tk.X)
        _label(head, "PREVIEW", font=_BOLD, fg=FG_ACCENT).pack(side=tk.LEFT)
        self._mode_var = tk.StringVar(frame, value=MODE_RENDERED)
        for mode, text in ((MODE_RENDERED, "As Discord shows it"), (MODE_RAW, "Raw text")):
            button = _radio(head, text, mode, self._mode_var, self._on_mode)
            button.pack(side=tk.LEFT, padx=(10, 0))
            self.mode_buttons[mode] = button
        self.count_label = _label(head, fg=FG_DIM)
        self.count_label.pack(side=tk.RIGHT)
        self.preview = DiscordPreview(frame, utc_to_local=self._utc_to_local)
        self.preview.text.configure(height=3)   # a request; the row stretches it
        self.preview.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        # Shown in the Discord view only (_on_mode); width=1: it never sets the width.
        self.caption_label = _label(frame, CAPTION, font=_SMALL, fg=FG_DIM, anchor=tk.W,
                                    width=1)
        self.caption_label.pack(fill=tk.X)
        self.warning_label = _label(frame, font=_SMALL, anchor=tk.W, width=1,
                                    fg=_dimmed(frame, FG_YELLOW, BG_DARK))
        self.warning_label.pack(fill=tk.X)
        return frame

    def _build_buttons(self, parent):
        row = tk.Frame(parent, bg=BG_DARK)
        self.copy_button = ttk.Button(row, text="Copy ping", style="Yellow.TButton",
                                      command=self._on_copy)
        self.copy_button.pack(side=tk.LEFT)
        self.saved_label = _label(row, fg=FG_DIM, width=10, anchor=tk.W)
        self.saved_label.pack(side=tk.LEFT, padx=(8, 0))
        self.reset_button = ttk.Button(row, text="Reset to basic", style="Dark.TButton",
                                       command=self._on_reset)
        self.reset_button.pack(side=tk.LEFT, padx=(8, 0))
        attach_tooltip(self.reset_button, "")
        self.basic_button = ttk.Button(row, text="Make this my basic template",
                                       style="Dark.TButton", command=self._on_make_basic)
        self.basic_button.pack(side=tk.LEFT, padx=(6, 0))
        attach_tooltip(self.basic_button, MAKE_BASIC_TIP)
        self.close_button = ttk.Button(row, text="Close", style="Dark.TButton",
                                       command=self.close)
        self.close_button.pack(side=tk.RIGHT)
        return row

    # -- the app-facing surface -------------------------------------------------------------
    def alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def lift(self) -> None:
        if not self.alive():
            return
        try:
            if self.win.state() == "iconic":
                self.win.deiconify()
            self.win.lift()
            self.win.focus_set()
        except tk.TclError:
            pass

    def poll(self) -> None:
        """One environment tick. A changed doctrine saves the old template under the OLD
        id and loads the new one; otherwise the rows, the chips, the echo and - only when
        its text or the minute changed - the preview are brought up to date."""
        if not self.alive():
            return
        env = self._read_env()
        if not self._loaded or doctrine_key(env.doctrine_id) != self._doctrine_id:
            self.flush()                    # (refuses while nothing is loaded)
            self._load(env)
            return
        self._env = env
        if self._when.unix != self._when_seen:
            self._sync_when()               # the one-click button used the time up
        self._refresh_rows()
        self._refresh_echo()
        self._refresh()

    def flush(self) -> None:
        """Store the template now if it changed (``save_action``); stops the save timer.

        Writes NOTHING unless the canvas really holds a loaded doctrine: a load that did
        not finish leaves a canvas that belongs to no id, and a canvas whose Text has
        been destroyed answers an EMPTY document - storing either would wreck a template.
        """
        self._cancel("_save_id")
        if not self.alive() or not self._canvas_alive():
            return
        if not self._loaded:
            self._show_saved(_NOT_SAVED)
            return
        key = self._doctrine_id
        cur = doc_to_json(self.canvas.get_doc())
        action = KEEP
        if cur != self._last_json:
            action = save_action(cur, self._last_json, self._baseline, self._inheriting,
                                 self._has_own(key))
        done = True
        if action == WRITE:
            done = self._write(self._store.set_template, key, cur)
        elif action == DROP:
            done = self._write(self._store.reset_template, key)
        if done:
            self._last_json = cur
        self._show_saved(_SAVED if done else _NOT_SAVED)
        if action != KEEP:
            self._refresh_buttons()         # there is, or is no longer, a copy to reset

    def focus_field(self, kind: str) -> None:
        if not self.alive():
            return
        target = self.fc_label if kind == "fc" else self.combos.get(kind)
        if target is None:
            return
        try:
            target.focus_set()
        except tk.TclError:
            pass

    def refresh_when(self) -> None:
        if not self.alive():
            return
        self._sync_when()
        self._refresh_echo()
        self._refresh()

    def close(self) -> None:
        if self._closed:
            return
        # Two guards, not one: a text commit that fails must not cost the template.
        for save in (self._commit_doctrine_text,    # a text still being typed
                     self.flush):                   # a template edit still pending
            try:
                save()
            except Exception:
                log.warning("ping dialog: could not save on close", exc_info=True)
        self._closed = True
        self._cancel("_tick_id")
        self._cancel("_save_id")
        if self._editor is not None:
            self._editor.close()
        self._destroy()

    # -- lifetime ---------------------------------------------------------------------------
    def _destroy(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _on_destroy(self, event):
        # <Destroy> arrives for every child too; only the window's own counts. This is
        # a destroy from OUTSIDE (close() sets the flag first): the widgets are already
        # gone, so nothing can be saved - only the timers are stopped.
        if self._closed or event.widget is not self.win:
            return
        self._closed = True
        self._cancel("_tick_id")
        self._cancel("_save_id")

    def _cancel(self, name):
        job = getattr(self, name)
        setattr(self, name, None)
        if job is not None:
            try:
                self.win.after_cancel(job)
            except (tk.TclError, ValueError):
                pass

    def _arm_tick(self):
        self._tick_id = self.win.after(TICK_MS, self._on_tick)

    def _on_tick(self):
        self._tick_id = None
        if not self.alive():
            return
        self._sync()                        # the tick never raises, and never stops
        if self.alive():
            self._arm_tick()

    def _sync(self) -> bool:
        """``poll`` for the window's own callbacks: nothing escapes into Tk (a failure is
        logged once per window). True when a doctrine is loaded afterwards."""
        try:
            self.poll()
        except Exception:
            if not self._tick_failed:
                self._tick_failed = True
                log.warning("ping dialog: the tick failed", exc_info=True)
        return self._loaded and self.alive()

    def _canvas_alive(self) -> bool:
        try:
            return bool(self.canvas.text.winfo_exists())
        except tk.TclError:
            return False

    def _arm_save(self):
        self._cancel("_save_id")
        self._save_id = self.win.after(SAVE_MS, self._on_save_timer)
        self._show_saved(_SAVING)

    def _on_save_timer(self):
        self._save_id = None
        self.flush()                        # does nothing once the window is gone

    # -- reading the world ------------------------------------------------------------------
    def _read_env(self):
        try:
            env = self._env_fn()
        except Exception:
            if not self._env_failed:        # once per window, not once per tick
                self._env_failed = True
                log.warning("ping dialog: env() failed", exc_info=True)
            return self._env
        return self._env if env is None else env

    def _now(self) -> int:
        try:
            self._now_seen = int(self._clock())
        except Exception:
            pass                            # keep the last reading
        return self._now_seen

    def _has_own(self, key) -> bool:
        """Does doctrine ``key`` have a template of its own in the store right now?
        With no doctrine: is there a basic template (as against the built-in one)?"""
        if key is None:
            return self._store.template(None) is not None
        return bool(self._store.has_own_template(key))

    def _still(self, key) -> bool:
        """Is the window still showing the doctrine it showed when a question was asked?"""
        return self.alive() and self._loaded and self._doctrine_id == key

    def _write(self, call, *args) -> bool:
        try:
            call(*args)
            return True
        except Exception as exc:            # ASCII, and no stored value in the log
            log.warning("ping dialog: could not save (%s)", type(exc).__name__)
            return False

    def _ask(self, title, message) -> bool:
        try:
            if callable(self._confirm):
                return bool(self._confirm(title, message))
            return bool(messagebox.askyesno(title, message, parent=self.win))
        except Exception:
            log.warning("ping dialog: the question failed", exc_info=True)
            return False

    # -- loading and saving the template ----------------------------------------------------
    def _load(self, env):
        """Show ``env``'s doctrine: its template (a silent load) and its picks. Whoever
        wants a pending edit kept calls ``flush`` first; a load throws it away."""
        self._cancel("_save_id")
        # All or nothing. Until the last step below the window counts as NOT loaded:
        # flush() refuses, and the next tick loads again. Were the id set first and
        # set_doc then failed, the old canvas would be saved under the new doctrine.
        self._loaded = False
        key = doctrine_key(env.doctrine_id)
        self.canvas.set_doc(ping_compose.template_for(self._store, env))
        cur = doc_to_json(self.canvas.get_doc())
        inheriting = not self._has_own(key)
        self._env, self._doctrine_id = env, key
        self._last_json = self._baseline = cur
        self._inheriting = inheriting
        self._loaded = True
        self._show_saved(_SAVED)
        self._rows_key = self._preview_key = None
        self._refresh_rows()
        self._refresh_echo()
        self._refresh()

    def _reload(self):
        """Load the shown doctrine again, from a button: a failure is the tick's to retry."""
        try:
            self._load(self._env)
        except Exception:
            log.warning("ping dialog: could not reload the template", exc_info=True)

    def _rebase(self, cur):
        """``cur`` (canvas output) is now what is loaded: later saves compare with it."""
        self._last_json = self._baseline = cur
        self._inheriting = not self._has_own(self._doctrine_id)

    def _show_saved(self, state):
        try:
            self.saved_label.configure(text=self._saved_text[state],
                                       fg=FG_RED if state == _NOT_SAVED else FG_DIM)
        except tk.TclError:
            pass

    def _on_template_change(self):
        # Called by the canvas for every edit (and every key release). Never loads the
        # canvas from here: set_doc inside its own change callback would loop.
        if self._closed:
            return
        self._arm_save()
        self._refresh()

    def _resolve_label(self, run):
        return ping_doc.chip_label(run.kind, self._values)

    def _on_pill_activate(self, kind, _params):
        self.focus_field(kind)

    def _add_pill(self, kind):
        self.canvas.insert_token_at_caret(kind, {})
        try:
            self.canvas.text.focus_set()
        except tk.TclError:
            pass

    # -- the value rows ---------------------------------------------------------------------
    def _refresh_rows(self):
        """Fill the rows from the store and the environment - only when one changed.
        The rows always show what is STORED; a pick the store did not take is undone
        here (``_on_pick`` forgets the key to force it)."""
        env, store, key = self._env, self._store, self._doctrine_id
        self._refresh_buttons()
        picks = store.picks(key)
        lists = {kind: store.items(kind) for kind in LIST_KINDS}
        text = store.text("doctrine_text")
        preset = store.when_format()
        state = (key, env, picks, tuple(tuple(lists[kind]) for kind in LIST_KINDS),
                 text, preset)
        if state == self._rows_key:
            return
        self._rows_key = None               # set again only once every row is filled
        self.win.title(title_for(env))
        fc = _clean(env.fc_name)
        self.fc_label.configure(text=fc or NO_FC, fg=FG_WHITE if fc else FG_DIM)
        self._fill("fleet_type", [(i, i) for i in lists["fleet_types"]], picks.fleet_type)
        self._fill("location", location_choices(lists["locations"], env.staging_name),
                   picks.location)
        self._fill("doctrine", doctrine_choices(text, env.doctrine_name), picks.doctrine)
        self._fill("comms", [(i, i) for i in lists["comms"]], picks.comms)
        if preset in discord_time.PRESETS:
            self.format_combo.current(discord_time.PRESETS.index(preset))
        self.reset_button.configure(text=reset_wording(key)[0])
        self._rows_key = state

    def _refresh_buttons(self):
        """Reset is on only while there is something to reset; "make this my basic
        template" only for a doctrine (with none selected the canvas IS the basic one)."""
        key = self._doctrine_id
        state = (key is None, self._has_own(key))
        if state == self._buttons_shown:
            return
        basic_shown, has_own = state
        self.reset_button.configure(state=tk.NORMAL if has_own else tk.DISABLED)
        update_tooltip(self.reset_button, reset_tip(key, has_own))
        self.basic_button.configure(state=tk.DISABLED if basic_shown else tk.NORMAL)
        update_tooltip(self.basic_button,
                       BASIC_IS_SHOWN if basic_shown else MAKE_BASIC_TIP)
        self._buttons_shown = state

    def _fill(self, field, choices, value):
        combo = self.combos[field]
        self._choices[field] = choices
        combo.configure(values=[label for label, _value in choices])
        if choices:
            combo.current(index_of(choices, value))
        else:
            combo.set(EMPTY_NUDGE.get(field, ""))

    def _on_pick(self, field):
        """A row's combobox was used: store the pick under the doctrine that is SHOWN."""
        if not self.alive():
            return
        combo = self.combos[field]
        try:
            combo.selection_clear()
        except tk.TclError:
            pass
        if field == "when":
            self._on_when_mode()
            return
        index = combo.current()
        choices = self._choices.get(field) or []
        if 0 <= index < len(choices):
            self._write(self._store.set_pick, self._doctrine_id, field, choices[index][1])
        self._rows_key = None               # show what the store holds, taken or not
        self._sync()

    def _on_pencil(self, kind):
        if kind == "doctrine":
            self._edit_doctrine_text()
        else:
            self._open_list_editor(LIST_OF_FIELD[kind])

    def _edit_doctrine_text(self):
        """The Doctrine row's pencil: its combobox turns into a text field in place
        (Enter or leaving the field stores it, Escape drops it)."""
        if self._text_entry is not None:
            self._commit_doctrine_text()
            return
        combo = self.combos["doctrine"]
        info = combo.grid_info()
        entry = self._text_entry = _entry(self.values_panel, 12)
        entry.insert(0, self._store.text("doctrine_text"))
        combo.grid_remove()
        entry.grid(row=info["row"], column=info["column"], sticky="ew", pady=1)
        entry.bind("<Return>", lambda _e: self._commit_doctrine_text())
        entry.bind("<FocusOut>", lambda _e: self._commit_doctrine_text())
        entry.bind("<Escape>", lambda _e: self._end_doctrine_text() or "break")
        entry.select_range(0, tk.END)
        entry.focus_set()

    def _commit_doctrine_text(self):
        entry = self._text_entry
        if entry is None:
            return
        try:
            value = entry.get()
        except tk.TclError:
            value = None
        self._end_doctrine_text()
        if value is not None:
            self._write(self._store.set_text, "doctrine_text", value)
        self._sync()

    def _end_doctrine_text(self):
        entry, self._text_entry = self._text_entry, None
        if entry is None:
            return
        try:
            entry.destroy()
            self.combos["doctrine"].grid()
        except tk.TclError:
            pass

    def _open_list_editor(self, kind):
        editor = self._editor
        if editor is not None and editor.alive():
            if editor.kind == kind:
                editor.lift()
                return
            editor.close()
        self._editor = _ListEditor(self, kind)

    def _list_changed(self):
        self._sync()

    def _completions(self) -> list:
        try:
            return [str(name) for name in (self._system_names() or ())]
        except Exception:
            return []

    # -- When -------------------------------------------------------------------------------
    def _sync_when(self):
        """Show what ``when`` holds: Now, or the fields of its moment (a re-display)."""
        unix = self._when.unix
        self._when_seen = unix
        self._set_bad(())
        self._show_when(unix is not None)
        if unix is not None:
            self._fill_when(unix)

    def _show_when(self, at_time: bool):
        self._at_time = at_time
        self.combos["when"].current(1 if at_time else 0)
        if at_time:
            self.when_frame.grid()
        else:
            self.when_frame.grid_remove()

    def _on_when_mode(self):
        if self.combos["when"].current() != 1:
            self._when.set(None)
            self._sync_when()
        elif not self._at_time:
            self._show_when(True)
            if self._when.unix is None:
                self._prefill_date()
                try:
                    self.time_entry.focus_set()
                except tk.TclError:
                    pass
            else:
                self._fill_when(self._when.unix)
        self._refresh_echo()
        self._refresh()

    def _set_fields(self, date_text, time_text):
        """Put text in the two fields WITHOUT it being read back: what is shown is
        remembered, and ``_read_fields`` only reads fields that differ from it."""
        for entry, text in ((self.date_entry, date_text), (self.time_entry, time_text)):
            entry.delete(0, tk.END)
            entry.insert(0, text)
        self._when_shown = (date_text, time_text, self._basis)
        self._left = set()                  # nothing in them has been typed by the user
        self._set_bad(())

    def _fill_when(self, unix) -> bool:
        try:
            date_text, time_text = discord_time.fields_for(
                unix, self._basis, utc_to_local=self._utc_to_local)
        except ValueError as exc:
            self._echo(str(exc), FG_RED)
            return False
        self._set_fields(date_text, time_text)
        return True

    def _prefill_date(self):
        """No time yet: today's date in the chosen basis, and an empty time to type in."""
        try:
            date_text = discord_time.fields_for(
                self._now(), self._basis, utc_to_local=self._utc_to_local)[0]
        except ValueError:
            date_text = ""
        self._set_fields(date_text, "")

    def _set_bad(self, bad):
        self._bad = tuple(bad)
        for name, entry in (("date", self.date_entry), ("time", self.time_entry)):
            wrong = name in self._bad
            entry.configure(highlightbackground=FG_RED if wrong else BORDER_COLOR,
                            highlightcolor=FG_RED if wrong else FG_ACCENT)

    # A field is outlined when what it holds is WRONG - not while it is only half-typed.
    # Half-typed is forgiven in the field being typed in and in a field the user has not
    # been through yet; once a field is left (or Enter is pressed) it is judged as it
    # stands. An entry that can no longer become valid is outlined at once.
    def _on_when_typed(self, field):
        """A key was released in the date or the time field."""
        self._read_fields(lenient={field} | ({"date", "time"} - self._left))

    def _on_when_left(self, field):
        """The date or the time field lost the focus. A field that was only passed
        through (it still holds what was last settled or filled in) is not judged."""
        entry, index = ((self.date_entry, 0) if field == "date" else (self.time_entry, 1))
        if self._when_shown is None or entry.get() != self._when_shown[index]:
            self._left.add(field)
        self._read_fields(lenient={"date", "time"} - self._left)

    def _on_when_edit(self, _event=None):
        """Enter in either field (and every read asked for from code): both fields are
        judged as they stand."""
        self._left.update(("date", "time"))
        self._read_fields()

    def _read_fields(self, lenient=()):
        """Read the two fields if they changed. A usable entry becomes the pending time
        at once; a bad one is outlined and explained, and the time stays as it was."""
        if not self.alive() or not self._at_time:
            return
        shown = (self.date_entry.get(), self.time_entry.get(), self._basis)
        if shown == self._when_shown:
            return                          # nothing typed since it was settled or filled in
        entry = read_when(*shown, local_to_utc=self._local_to_utc, lenient=lenient)
        if not entry.waiting:
            self._when_shown = shown        # settled; a half-typed entry is read again
        self._set_bad(entry.bad)
        if entry.unix is None:
            if entry.bad:
                self._echo(entry.message, FG_RED)
            else:
                self._refresh_echo()        # nothing is wrong yet: the time in force
            return
        self._when.set(entry.unix)
        self._when_seen = entry.unix
        self._refresh_echo()
        self._refresh()

    def _on_basis(self):
        basis = self._basis_var.get()
        if basis == self._basis or basis not in discord_time.BASES:
            return
        self._basis = basis
        unix = self._when.unix
        if unix is not None:
            self._fill_when(unix)           # the same moment, shown in the other basis
        elif self.time_entry.get().strip():
            self._when_shown = None         # nothing valid yet: read what is typed again
            self._on_when_edit()
        else:
            self._prefill_date()
        self._refresh_echo()
        self._refresh()

    def _on_quick(self, minutes):
        try:
            unix = discord_time.quick_target(self._now(), minutes)
        except ValueError as exc:
            self._echo(str(exc), FG_RED)
            return
        if not self._fill_when(unix):
            return                          # it cannot be shown: the time stays as it was
        self._when.set(unix)
        self._when_seen = unix
        self._refresh_echo()
        self._refresh()

    def _on_format(self, _event=None):
        index = self.format_combo.current()
        try:
            self.format_combo.selection_clear()
        except tk.TclError:
            pass
        if 0 <= index < len(discord_time.PRESETS):
            self._write(self._store.set_when_format, discord_time.PRESETS[index])
        self._rows_key = None               # show what the store holds, taken or not
        self._sync()

    def _echo(self, text, colour):
        if (text, colour) != self._echo_shown:
            self._echo_shown = (text, colour)
            self.echo_label.configure(text=text, fg=colour)

    def _refresh_echo(self):
        """The line under the time fields: the truth about the pending time. Left alone
        while a field is outlined - the reason is showing there."""
        if not self._at_time or self._bad:
            return
        unix = self._when.unix
        if unix is None:
            self._echo(WHEN_HINT, FG_DIM)
            return
        now = self._now()
        try:
            text = "= " + discord_time.describe(unix, now,
                                                utc_to_local=self._utc_to_local)
        except ValueError as exc:
            self._echo(str(exc), FG_RED)
            return
        self._echo(text, FG_YELLOW if unix < now else FG_TEXT)

    # -- chips and preview ------------------------------------------------------------------
    def _refresh(self):
        """Compose the ping as it would be copied; bring the chips, the warning line and -
        only when the text or the minute changed - the preview up to date."""
        if not self.alive() or not self._loaded or not self._canvas_alive():
            return                          # nothing trustworthy to compose from
        now = self._now()
        try:
            composed = ping_compose.compose(self._store, self._env, self._when.unix, now,
                                            doc=self.canvas.get_doc())
        except ValueError as exc:           # a moment discord_time cannot word
            self._warn(str(exc))
            return
        self._values = composed.values
        self.canvas.refresh_pills()
        self._warn(warning_line(composed.warnings))
        key = (composed.text, now // 60)
        if key == self._preview_key:
            return
        self._preview_key = key
        self.preview.render(composed.text, now)
        text, over = count_text(composed.text)
        self.count_label.configure(text=text, fg=FG_YELLOW if over else FG_DIM)

    def _warn(self, text):
        if text != self._warning_shown:
            self._warning_shown = text
            self.warning_label.configure(text=text)

    def _on_mode(self):
        mode = self._mode_var.get()
        try:
            self.preview.set_mode(mode)
            if mode == MODE_RENDERED:       # the caption belongs to the likeness only
                self.caption_label.pack(fill=tk.X, before=self.warning_label)
            else:
                self.caption_label.pack_forget()
        except (ValueError, tk.TclError):
            pass

    # -- the buttons ------------------------------------------------------------------------
    def _on_copy(self):
        """Copy ping: bring the window up to date, store the template, then hand over to
        the app's ONE copy routine. No clipboard work happens here."""
        self._sync()
        self.flush()
        try:
            self._copy()
        except Exception:
            log.warning("ping dialog: copy() failed", exc_info=True)

    # Both questions below can stay up for a while, and the timers keep running under
    # them: the doctrine is noted BEFORE asking, and an answer that arrives after the
    # window moved on to another doctrine does nothing.
    def _on_reset(self):
        if not self._sync():
            return
        key = self._doctrine_id
        if not self._has_own(key):
            return                          # nothing to reset (the button is off)
        _label_text, title, message = reset_wording(key)
        if not self._ask(title, message) or not self._still(key):
            return
        self._write(self._store.reset_template, key)
        self._reload()                      # (drops a pending edit with the template)

    def _on_make_basic(self):
        if not self._sync():
            return
        key = self._doctrine_id
        if key is None:
            return                          # the canvas IS the basic template (button off)
        self.flush()
        if not self._ask("Basic template", make_basic_message(self._inheriting_count())):
            return
        if not self._still(key):
            return
        cur = doc_to_json(self.canvas.get_doc())
        if self._write(self._store.make_basic, cur):
            self._rebase(cur)               # the old basic is no longer what was loaded

    def _inheriting_count(self):
        if not callable(self._doctrine_ids):
            return None
        try:
            return int(self._store.inheriting_count(list(self._doctrine_ids())))
        except Exception:
            return None


class _ListEditor:
    """The small list popup behind a row's pencil: the list, Add, Rename, Remove, Up and
    Down. Every change is stored at once through the owner's store; the owner's rows
    follow. No input hold; the owner keeps one open at a time and closes it with itself."""

    def __init__(self, owner, kind):
        self._owner = owner
        self.kind = kind
        self._closed = False
        self.win = tk.Toplevel(owner.win)
        self.win.title(LIST_TITLES[kind])
        self.win.resizable(False, False)
        make_modal(self.win, owner.win, on_cancel=self.close, grab=False, center=True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.reload()

    def _build(self):
        frame = tk.Frame(self.win, bg=BG_DARK, padx=10, pady=8)
        frame.pack(fill=tk.BOTH, expand=True)
        top = tk.Frame(frame, bg=BG_DARK)
        top.pack(fill=tk.X)
        self.listbox = tk.Listbox(
            top, height=8, width=30, font=_FONT, bg=BG_ENTRY, fg=FG_TEXT,
            selectbackground=BG_SELECT, selectforeground=FG_WHITE, activestyle="none",
            exportselection=False, highlightthickness=1,
            highlightbackground=BORDER_COLOR, highlightcolor=FG_ACCENT, bd=0)
        self.listbox.pack(side=tk.LEFT)
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self._on_select())
        side = tk.Frame(top, bg=BG_DARK)
        side.pack(side=tk.LEFT, anchor=tk.N, padx=(6, 0))
        self.buttons = {}
        for name, command in (("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1)),
                              ("Remove", self.remove)):
            self.buttons[name] = _mini_button(side, name, command)
            self.buttons[name].pack(fill=tk.X, pady=(0, 3))
        row = tk.Frame(frame, bg=BG_DARK)
        row.pack(fill=tk.X, pady=(6, 0))
        if self.kind == "locations":        # system names complete; free text is fine too
            self.entry = _entry(row, 22, AutocompleteEntry, self._owner._completions())
        else:
            self.entry = _entry(row, 22)
        self.entry.pack(side=tk.LEFT)
        # add="+": the autocomplete entry has its own <Return> (take the suggestion).
        self.entry.bind("<Return>", lambda _e: self.add(), add="+")
        # NOT add="+": this replaces the autocomplete entry's own <Escape>, which closed
        # the suggestions and then let the same key close the whole editor.
        self.entry.bind("<Escape>", self._on_escape)
        for name, command in (("Add", self.add), ("Rename", self.rename)):
            self.buttons[name] = _mini_button(row, name, command)
            self.buttons[name].pack(side=tk.LEFT, padx=(4, 0))
        _label(frame, "The first item is what a doctrine with no pick yet gets.",
               font=_SMALL, fg=FG_DIM, anchor=tk.W).pack(fill=tk.X, pady=(6, 0))
        self.status_label = _label(frame, font=_SMALL, fg=FG_YELLOW, anchor=tk.W)
        self.status_label.pack(fill=tk.X)
        self.buttons["Close"] = ttk.Button(frame, text="Close", style="Dark.TButton",
                                           command=self.close)
        self.buttons["Close"].pack(anchor=tk.E, pady=(4, 0))
        self.entry.focus_set()

    def alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def lift(self):
        try:
            self.win.lift()
            self.entry.focus_set()
        except tk.TclError:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def suggesting(self) -> bool:
        """Is the entry's suggestion list up? (Only the Locations entry has one.)"""
        return getattr(self.entry, "_listbox", None) is not None

    def _close_suggestions(self):
        closer = getattr(self.entry, "_close_dropdown", None)
        if callable(closer):
            try:
                closer()
            except tk.TclError:
                pass

    def _on_escape(self, _event=None):
        """Escape in the entry: the first one closes the suggestions and stops there;
        with none showing it goes on to the window, which closes the editor."""
        if self.suggesting():
            self._close_suggestions()
            return "break"
        return None

    def items(self) -> list:
        return self._owner._store.items(self.kind)

    def selected(self):
        """The selected row's index, or None."""
        picked = self.listbox.curselection()
        return int(picked[0]) if picked else None

    def select(self, index) -> None:
        """Select a row and put its name in the field, ready to be renamed. (A selection
        made from code raises no <<ListboxSelect>>, hence the explicit call.)"""
        self.listbox.selection_clear(0, tk.END)
        if index is not None and 0 <= index < self.listbox.size():
            self.listbox.selection_set(index)
            self.listbox.see(index)
        self._on_select()

    def reload(self, select=None) -> None:
        self.listbox.delete(0, tk.END)
        for item in self.items():
            self.listbox.insert(tk.END, item)
        if select is not None and 0 <= select < self.listbox.size():
            self.listbox.selection_set(select)
            self.listbox.see(select)

    def _on_select(self):
        index = self.selected()
        if index is None:
            return
        self.entry.delete(0, tk.END)
        self.entry.insert(0, self.listbox.get(index))
        self._say("")

    def _say(self, text):
        self.status_label.configure(text=text)

    def _store_items(self, items, select=None):
        self._owner._write(self._owner._store.set_items, self.kind, items)
        self.reload(select)
        self._owner._list_changed()

    def add(self) -> None:
        # The suggestions go first: left up, the next Enter would add whichever system
        # happened to be highlighted in them.
        self._close_suggestions()
        text = self.entry.get().strip()
        items = self.items()
        if not text:
            self._say("Type a name first.")
            return
        if text.casefold() in (item.casefold() for item in items):
            self._say("That name is already in the list.")
            return
        if len(items) >= MAX_ITEMS:
            self._say("The list is full (%d items)." % MAX_ITEMS)
            return
        self._say("")
        self._store_items(items + [text], select=len(items))
        if len(self.items()) == len(items):         # the store would not take it
            self._say("That name cannot be used.")
            return
        self.entry.delete(0, tk.END)

    def rename(self) -> None:
        index = self.selected()
        new = self.entry.get().strip()
        if index is None:
            self._say("Pick the item to rename first.")
            return
        if not new:
            self._say("Type the new name first.")
            return
        # The store renames the item AND every doctrine's pick of it, in one save.
        try:
            renamed = self._owner._store.rename_item(self.kind, self.listbox.get(index), new)
        except Exception as exc:
            log.warning("ping dialog: could not rename (%s)", type(exc).__name__)
            renamed = False
        if not renamed:
            self._say("That name is already used.")
            return
        self._say("")
        self.reload(index)
        self._owner._list_changed()

    def remove(self) -> None:
        index = self.selected()
        if index is None:
            self._say("Pick the item to remove first.")
            return
        items = self.items()
        if not 0 <= index < len(items):
            return
        del items[index]
        self._say("")
        self._store_items(items, select=min(index, len(items) - 1) if items else None)

    def move(self, step) -> None:
        index = self.selected()
        if index is None:
            self._say("Pick the item to move first.")
            return
        items, where = moved(self.items(), index, step)
        self._say("")
        self._store_items(items, select=where)
