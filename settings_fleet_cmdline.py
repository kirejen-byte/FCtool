"""Settings > "Fleet Command Line": the section builder + a generic pill row.

The section that used to be "Fleet Management" grew one flat column of
fleet-chat command controls. It now reads:

    Fleet channel name: [Fleet      ]     <- always visible; every command
    [X-up] [Range check] [Refit] [Form] [Help]   listens on this channel
    <exactly ONE panel: a dim "what you type / what it does" line, then
     that command's own controls>

Which pill is showing is SESSION-only view state (no config key): it is a
view, not a setting. Nothing about any option changes here - the same vars,
the same ``_on_*_toggle`` commands, the same config keys and tooltips; only
which row each control sits on. Every var / widget the rest of fc_gui reads
is set on the APP under its old name (``_setting_entries['xup_*']``,
``_range_check_enabled_var``, ``_range_check_keyword_var``,
``_range_check_keyword_entry``, ``_range_check_hint``,
``_refit_command_enabled_var``, ``_form_command_enabled_var``) plus the new
``_help_command_enabled_var``. A widget in an un-packed panel still exists,
so Save Settings still collects it.

A command that is switched off gets a dim `` . off`` suffix on its pill
(``refresh_pills``), read through each feature's OWN ``is_enabled`` - the
range check also counts as off on a blank keyword, as its engine and
``help_command.build_lines`` treat it. Never the green check mark: that means
"this mode is ON" elsewhere in Settings.

``CategoryPills`` is the generic part (tkinter + ui_theme only): buttons,
reveal-one-frame, restyle, per-pill suffix. It knows nothing about fleet
commands. The FCPreview settings categories are deliberately NOT migrated
onto it (a released, heavily-tested panel).

Imports no fc_gui. Source stays pure ASCII: the middle dot is ``chr(0xB7)``.

Spec: docs/superpowers/specs/2026-11-22-fleet-chat-help-command-design.md
"""
from __future__ import annotations

import tkinter as tk

import form_command
import help_command
import range_check
import refit_command
from ui_helpers import attach_tooltip
from ui_theme import BG_DARK, BG_ENTRY, FG_ACCENT, FG_DIM, FG_TEXT, FG_WHITE

#: (internal key, pill label), in display order. The first is the fallback.
CATEGORIES = (
    ("xup", "X-up"),
    ("range", "Range check"),
    ("refit", "Refit"),
    ("form", "Form"),
    ("help", "Help"),
)

#: The switched-off tell appended to a pill label.
OFF_SUFFIX = " " + chr(0xB7) + " off"

_FONT = ("Consolas", 10)
_DIM_FONT = ("Consolas", 9)


# -- the generic helper ---------------------------------------------------------

class CategoryPills:
    """A row of category buttons that reveals exactly ONE panel frame.

    ``categories`` is ``((key, label), ...)``. Packs two frames into
    ``parent``: ``row`` (the buttons) and ``body`` (the panels' container, so
    a panel revealed later still lands directly under the row, never at the
    end of ``parent``). ``panels[key]`` is created UNPACKED for the caller to
    fill; ``select`` packs exactly one. An unknown key falls back to the first
    category - never an empty body. Styling mirrors the FCPreview categories:
    active = SUNKEN on the accent, others RIDGE on the entry colour; a pill
    marked ``dim`` (see ``set_suffix``) draws its label in the dim colour
    while not active. The first category is shown from construction on (the
    body is never empty); ``on_select(key)`` is called after every LATER
    select, not that initial one. A select that hides the panel holding the
    keyboard focus moves the focus to the pill row first, so that widget's
    ``<FocusOut>`` (an entry's autosave) still fires."""

    def __init__(self, parent, categories, *, on_select=None, padx=20):
        self.categories = tuple(categories)
        if not self.categories:
            raise ValueError("CategoryPills needs at least one category")
        self._labels = dict(self.categories)
        self._suffix = {}
        self._dim = set()
        self._on_select = on_select
        self.current = self.categories[0][0]
        self.row = tk.Frame(parent, bg=BG_DARK)
        self.row.pack(fill=tk.X, padx=padx, pady=(4, 4))
        self.body = tk.Frame(parent, bg=BG_DARK)
        self.body.pack(fill=tk.X)
        self.buttons = {}
        self.panels = {}
        for key, label in self.categories:
            btn = tk.Button(
                self.row, text=label, font=_FONT,
                relief=tk.RIDGE, bd=1, padx=8, pady=1, cursor="hand2",
                bg=BG_ENTRY, fg=FG_TEXT,
                command=lambda k=key: self.select(k))
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self.buttons[key] = btn
            self.panels[key] = tk.Frame(self.body, bg=BG_DARK)
        self._on_select = None               # the initial show is silent
        self.select(self.current)
        self._on_select = on_select

    def _release_focus(self, keep):
        """If the focus sits inside a panel other than ``keep``'s (about to be
        hidden), move it to the pill row: a focused widget in an un-packed
        frame never sees ``<FocusOut>`` until the user clicks elsewhere, so an
        entry's autosave (and its pill refresh) would silently wait."""
        try:
            widget = self.row.focus_get()
        except (KeyError, tk.TclError):      # the ttk combobox popdown quirk
            return
        hidden = [f for k, f in self.panels.items() if k != keep]
        while widget is not None:
            if any(widget is f for f in hidden):
                try:
                    self.row.focus_set()
                except tk.TclError:
                    pass
                return
            widget = getattr(widget, "master", None)

    def select(self, key):
        """Reveal ``key``'s panel (unknown -> the first) and restyle."""
        if key not in self._labels:
            key = self.categories[0][0]
        self._release_focus(key)
        self.current = key
        for frame in self.panels.values():
            try:
                frame.pack_forget()
            except tk.TclError:
                pass
        try:
            self.panels[key].pack(fill=tk.X)
        except tk.TclError:
            pass
        self.restyle()
        if self._on_select is not None:
            self._on_select(key)

    def set_suffix(self, key, suffix="", *, dim=False):
        """Append ``suffix`` to ``key``'s label ("" clears it); ``dim`` draws
        the label in the dim colour while the pill is not active."""
        if key not in self._labels:
            return
        self._suffix[key] = str(suffix or "")
        if dim:
            self._dim.add(key)
        else:
            self._dim.discard(key)
        try:
            self.buttons[key].configure(
                text=self._labels[key] + self._suffix[key])
        except tk.TclError:
            pass
        self.restyle()

    def restyle(self):
        for key, btn in self.buttons.items():
            try:
                if key == self.current:
                    btn.configure(relief=tk.SUNKEN, bg=FG_ACCENT, fg=BG_DARK,
                                  activebackground=FG_ACCENT,
                                  activeforeground=BG_DARK)
                else:
                    fg = FG_DIM if key in self._dim else FG_TEXT
                    btn.configure(relief=tk.RIDGE, bg=BG_ENTRY, fg=fg,
                                  activebackground=BG_ENTRY,
                                  activeforeground=fg)
            except tk.TclError:
                pass


# -- the off state --------------------------------------------------------------

def off_states(config) -> dict:
    """``{pill key: switched off?}`` from a RAW config, through each feature's
    own gate. X-up has no switch: it is off when a trigger/clear word is blank
    (``help_command.xup_is_off``). Never raises on junk blocks."""
    cfg = config if isinstance(config, dict) else {}
    rng_block = cfg.get("range_check")
    rng = range_check.normalize_config(rng_block)
    return {
        "xup": help_command.xup_is_off(cfg.get("xup")),
        "range": (not range_check.is_enabled(rng_block)) or not rng["keyword"],
        "refit": not refit_command.is_enabled(cfg.get("refit_command")),
        "form": not form_command.is_enabled(cfg.get("form_command")),
        "help": not help_command.is_enabled(cfg.get("help_command")),
    }


def refresh_pills(app):
    """Recompute every pill's `` . off`` suffix from ``app.config``. A no-op
    before the section is built (``app._fleet_cmdline_pills`` absent), so the
    toggle / collect methods may call it from any host."""
    pills = getattr(app, "_fleet_cmdline_pills", None)
    if pills is None:
        return
    for key, off in off_states(getattr(app, "config", None)).items():
        pills.set_suffix(key, OFF_SUFFIX if off else "", dim=off)


# -- the section ----------------------------------------------------------------

def _dim(parent, text, pady=(2, 2)):
    lbl = tk.Label(parent, text=text, font=_DIM_FONT, fg=FG_DIM, bg=BG_DARK,
                   justify=tk.LEFT, anchor=tk.W)
    lbl.pack(anchor=tk.W, padx=20, pady=pady)
    return lbl


def _check(parent, text, variable, command):
    frame = tk.Frame(parent, bg=BG_DARK)
    frame.pack(fill=tk.X, padx=20, pady=2)
    box = tk.Checkbutton(
        frame, text=text, variable=variable,
        font=_FONT, fg=FG_TEXT, bg=BG_DARK,
        selectcolor=BG_ENTRY, activebackground=BG_DARK,
        activeforeground=FG_TEXT, command=command)
    box.pack(side=tk.LEFT)
    return box


def build_section(app, parent):
    """Build the whole "Fleet Command Line" section into ``parent`` (the
    Settings scroll frame) and store the pill row on
    ``app._fleet_cmdline_pills``. Tk thread, at Settings build."""
    app._add_section(parent, "Fleet Command Line", toc_title="Fleet Cmd Line")
    xup = app.config.get("xup", {})
    if not isinstance(xup, dict):
        xup = {}

    # Created HERE (the first _add_setting of the tab): Save Settings reads
    # the three x-up keys out of it.
    app._setting_entries = {}
    app._add_setting(parent, "Fleet channel name", "xup_channel",
                     xup.get("channel_name", "Fleet"),
                     tooltip=("The fleet chat channel every command below "
                              "listens on: x-up, range check, refit, form "
                              "and help."))

    def _remember(key):
        # Session memory of the shown pill. The Settings tab is built once
        # per run today, so this only matters if a future in-session rebuild
        # calls build_section again; the select() below honours it then.
        app._fleet_cmdline_category = key

    pills = CategoryPills(parent, CATEGORIES, on_select=_remember)
    app._fleet_cmdline_pills = None          # no refresh until fully built
    _build_xup_panel(app, pills.panels["xup"], xup)
    _build_range_panel(app, pills.panels["range"])
    _build_refit_panel(app, pills.panels["refit"])
    _build_form_panel(app, pills.panels["form"])
    _build_help_panel(app, pills.panels["help"])
    app._fleet_cmdline_pills = pills
    refresh_pills(app)
    pills.select(getattr(app, "_fleet_cmdline_category", None))
    return pills


def _build_xup_panel(app, panel, xup):
    _dim(panel, "Anyone in fleet: type the trigger word to x-up (each pilot "
                "counts once),\nthe clear word to reset the count (read from "
                "the tracked character's\nfleet chat).",
         pady=(4, 2))
    app._add_setting(panel, "Trigger Word", "xup_trigger",
                     xup.get("trigger_word", "x"),
                     tooltip=("The word a pilot types in the fleet channel "
                              "to x-up. Each pilot is counted once; an alert "
                              "fires when the count reaches the threshold."))
    app._add_setting(panel, "Clear Word", "xup_fire",
                     xup.get("fire_word", "FIRE"),
                     tooltip=("Resets the x-up tally to zero, clearing "
                              "everyone who has x'd up, so you can start a "
                              "fresh count."))
    _dim(panel, "The x-up threshold is on the Fleet Management tab.")


def _build_range_panel(app, panel):
    # -- Fleet-chat range check (range_check.py; default ON) ------------------
    # Both controls read their initial state through the module: is_enabled
    # is THE master gate (the chat hook gates on the same call, so a ticked
    # box can never sit over a feature that cannot fire) and normalize_config
    # owns the keyword default. No default value is typed here.
    _dim(panel, "Type the keyword in fleet chat (optionally naming systems): "
                "a short-lived\nwindow over that client lists the jump "
                "ranges from you.", pady=(4, 2))
    _rc_block = app.config.get("range_check")
    app._range_check_enabled_var = tk.BooleanVar(
        value=range_check.is_enabled(_rc_block))
    rc_check = _check(panel, "Range check on fleet chat",
                      app._range_check_enabled_var,
                      command=app._on_range_check_toggle)
    attach_tooltip(
        rc_check,
        "Type the keyword below in Fleet chat and a short-lived window "
        "pops up over that character's EVE client, showing which staging "
        "systems are in Titan, Capital, Command Carrier and Blops jump "
        "range of them (JDC 5).\n\n"
        "Only YOUR OWN logged-in characters can trigger it - a fleet "
        "member saying the keyword is ignored. Naming systems in the "
        "message replaces the staging list for that check.\n\n"
        "NEEDS FCPreview or the Eve-O overlay for full coverage: only "
        "those poll every logged-in character's location. With previews "
        "OFF (the default) just the signed-in PRIMARY character's own "
        "location is known, so a check typed by any other character "
        "answers \"Location unknown\" instead of a range table.\n\n"
        "A BLANK keyword below also switches it off - a ticked box over an "
        "empty keyword field matches nothing and does nothing.")

    rc_kw_frame = tk.Frame(panel, bg=BG_DARK)
    rc_kw_frame.pack(fill=tk.X, padx=20, pady=2)
    rc_kw_label = tk.Label(rc_kw_frame, text="Range check keyword:",
                           font=_FONT, fg=FG_TEXT, bg=BG_DARK,
                           width=28, anchor=tk.W)
    rc_kw_label.pack(side=tk.LEFT)
    app._range_check_keyword_var = tk.StringVar(
        value=range_check.normalize_config(_rc_block).get("keyword", ""))
    app._range_check_keyword_entry = tk.Entry(
        rc_kw_frame, textvariable=app._range_check_keyword_var,
        font=_FONT, bg=BG_ENTRY, fg=FG_WHITE,
        insertbackground=FG_WHITE, width=40, borderwidth=1, relief=tk.RIDGE)
    app._range_check_keyword_entry.pack(side=tk.LEFT, padx=5)
    # The dim tell for the OTHER off switch. A ticked box over a blank
    # keyword is a dead feature and the checkbox cannot show that; the
    # keyword tooltip says so but a tooltip has to be hovered to be read.
    app._range_check_hint = tk.Label(
        rc_kw_frame, text="", font=_DIM_FONT, fg=FG_DIM, bg=BG_DARK)
    app._range_check_hint.pack(side=tk.LEFT, padx=5)
    app._refresh_range_check_hint()
    # Autosave on leaving the field / pressing Enter, the same instant-apply
    # the Staging System entry uses (_autosave_staging_system). Save Settings
    # collects it too, so a value still being typed is never lost.
    app._range_check_keyword_entry.bind(
        "<FocusOut>", app._autosave_range_check_keyword, add="+")
    app._range_check_keyword_entry.bind(
        "<Return>", app._autosave_range_check_keyword, add="+")
    for _w in (rc_kw_label, app._range_check_keyword_entry):
        attach_tooltip(
            _w,
            "Case-insensitive; matches anywhere in the message, so "
            "\"range check on me\" fires too.\n\n"
            "Leave it BLANK to switch the feature off - a blank keyword "
            "matches nothing rather than every line.")
    _dim(panel, "Only your own characters can trigger it, from any of them.\n"
                "Needs FCPreview or the Eve-O overlay to know every "
                "character's location;\nwithout them only the primary "
                "character's own location is known.")


def _build_refit_panel(app, panel):
    # -- Fleet-chat refit command (refit_command.py; default ON) --------------
    # ONE control: the master gate, read through the module's own predicate
    # for the same reason the range tick is. No keyword entry - the config
    # key exists for power users, but a second free-text field here would
    # be a second thing to get wrong for a feature nobody renames.
    # The intro and its usage lines are ONE label above the tick, so the
    # intro's ":" leads straight into what it introduces.
    _dim(panel, "In fleet chat, from one of your own characters (read from "
                "the tracked\ncharacter's fleet chat):\n"
                "  refit <ship or role> <number|name>   e.g. refit dps 2 / "
                "refit muninn ac\n"
                "  refit <name>    when only one ship has refits\n"
                "  refit reset     every slot back to its default",
         pady=(4, 2))
    app._refit_command_enabled_var = tk.BooleanVar(
        value=refit_command.is_enabled(app.config.get("refit_command")))
    rf_check = _check(panel, "Refit commands on fleet chat",
                      app._refit_command_enabled_var,
                      command=app._on_refit_command_toggle)
    attach_tooltip(
        rf_check,
        "In fleet chat, from one of your own characters:\n"
        "refit <ship or role> <number|name>   e.g. refit dps 2 / "
        "refit muninn ac\n"
        "refit <name>   when only one ship has refits\n"
        "refit reset   back to defaults")


def _build_form_panel(app, panel):
    # -- Fleet-chat form command (form_command.py; default ON) ----------------
    # The refit tick's twin: ONE control, seeded through the module's own
    # master gate; the keyword stays config-only for the same reason.
    _dim(panel, "form <doctrine name>   e.g. form svipuls - selects that "
                "doctrine and pushes\nits MOTD (read from the tracked "
                "character's fleet chat).", pady=(4, 2))
    app._form_command_enabled_var = tk.BooleanVar(
        value=form_command.is_enabled(app.config.get("form_command")))
    fm_check = _check(panel, "Form commands on fleet chat",
                      app._form_command_enabled_var,
                      command=app._on_form_command_toggle)
    attach_tooltip(
        fm_check,
        "In fleet chat, from one of your own characters:\n"
        "form <doctrine name>   e.g. form svipuls\n"
        "selects that doctrine, loads its top MOTD template,\n"
        "resets its refits to default and pushes the MOTD.")


def _build_help_panel(app, panel):
    # -- Fleet-chat help command (help_command.py; default ON) ----------------
    # Same one-control shape, seeded through help_command.is_enabled.
    secs = help_command.TOAST_SECONDS
    if float(secs).is_integer():
        secs = int(secs)
    _dim(panel, "Type \"help FCT\" (or \"help FCTool\") in fleet chat from any "
                "of your characters:\na %s s list of every fleet-chat command."
                % secs, pady=(4, 2))
    app._help_command_enabled_var = tk.BooleanVar(
        value=help_command.is_enabled(app.config.get("help_command")))
    hp_check = _check(panel, "help FCT command on fleet chat",
                      app._help_command_enabled_var,
                      command=app._on_help_command_toggle)
    attach_tooltip(
        hp_check,
        "Type help FCT in fleet chat from any of your own characters and a "
        "toast over that client lists every fleet-chat command FCTool "
        "reacts to, with the live keywords; switched-off ones say (off).")
