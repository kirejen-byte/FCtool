"""Group configuration - the app's side: the Settings section, the first-run offer, the
"by <tag>" line under the logo, the attention marks and the check of a group's market
structure (plan Task 16; spec 9.1, 10.1, 10.4, 13).

``FCToolGUI`` inherits :class:`GroupConfigMixin`. Everything the feature does inside the
main window lives HERE; the main module keeps the import, the base class and one-line calls
in the methods it already has. This module never imports the main module.

What it needs from its host
---------------------------
Every one of these is read with ``getattr`` (or guarded) where a bare test host - the
Settings tab is built on ``object.__new__`` hosts in tests - may lack it:

``root``                 the main Tk window: parent of the windows opened here, ``after``
``notebook``             the main ``ttk.Notebook``: the offer strip is packed before it,
                         the Settings tab's text carries the attention mark
``config``               the app's config dict - READ only (``tracked_character``)
``_save_config()``       write the config now (the group window asks before it reads)
``_restart_app()``       plain restart -> ``False`` while an update is installing
``_themed_modal(title, message, *, buttons, parent)``   the app's own question dialog
``_post_ui(fn, *args)``  the one road from a worker thread to the Tk thread
``esi_auth``             the primary character's ESI client (``is_authenticated``,
                         ``has_market_structure_scope()``, ``esi_get_ex(path)``)
``esi_accounts``         the signed-in characters (``character_name``)
``_update_install_state``  the updater's state word
``_group_boot_report``   this start's import report, or ``None``
``_add_section(parent, title, toc_title=)``   a Settings section header + its TOC entry
``_settings_toc_anchors``  ``[(TOC title, anchor widget)]``;  ``_settings_toc_rows``
                         ``[(row label, anchor widget)]``;  ``_settings_toc_panel``,
                         ``_settings_toc_tab``, ``_settings_toc_canvas``,
                         ``_settings_toc_scroll_frame``;  ``_settings_toc_jump(canvas,
                         scroll_frame, anchor)``
``_save_status``         the Settings tab's status label (one sentence after the palette)
``_title_logo``          the frame that holds the "FCTool" label, its first pack slave
``_market_persist_keys(**keys)``, ``_market_set_id_var(key, value)``,
``_market_clear_station_selection()``, ``_market_restore_saved_pickers()``,
``_refresh_market_status_labels()``, ``_auto_market_rescan_if_needed()``
                         the market pickers' own persist path

What it keeps on the host: ``_group_window``, ``_palette_window``,
``_group_welcome_strip``, ``_group_status_label``, ``_group_tag_var``, ``_group_tag_entry``,
``_title_tag_label``, ``_group_banners``, ``_group_toc_flagged``, ``_group_toc_spacer``,
``_group_verify_inflight``.

Rules
-----
* Nothing here takes the application input hold and nothing waits: the two windows it opens
  are built by their own modules (``group_config_ui``, ``palette_ui``), the offer is a frame
  in the main window, and a question goes through the host's own dialog with a parent.
* Those two modules are imported when a window is first opened, never with this module
  (``_window_module``, ``_palette_ui``): an ordinary start does not pay for a window
  nobody opened.
* The data folder is written only when the user DID something (typed a tag, closed the
  offer, dismissed a mark, applied colours) or a structure check ended. Building the
  section, the offer, the tag line and the marks only reads the feature's own side file -
  and looks for the staged file by NAME; it is never opened here.
* The worker (``_group_verify_worker``) touches no widget and no Tk variable; its one
  result comes back through ``_post_ui``.
* No variable is traced. Every binding adds to what is bound already.
* Own character names (``own_names``) are handed to the window and never logged.
* The hooks at the bottom (:func:`market_picked`, :func:`staging_saved`,
  :func:`signed_in`, :func:`toc_idle_fg`) are what the app's EXISTING methods call. Tests
  bind those methods onto hosts that are not this class, so each hook takes the host as an
  argument, never raises, and costs an app without a flag or a task two small reads.
"""

import logging
import os
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

import group_apply
import group_state
import group_verify
import palette
from app_path import app_dir, app_dir_decision
from app_version import APP_VERSION
from group_bundle import MAX_TAG, clean_tag
from ui_helpers import attach_tooltip, make_glyph_button, update_tooltip
from ui_theme import (
    BG_DARK, BG_ENTRY, BG_PANEL, FG_ACCENT, FG_DIM, FG_GREEN, FG_ORANGE, FG_TEXT, FG_WHITE,
)

log = logging.getLogger(__name__)

# -- the Settings section ------------------------------------------------------
SECTION_TITLE = "Group configuration"
TOC_TITLE = "Group"
CREATE_LABEL = "Create a group file..."
IMPORT_LABEL = "Import a group file..."
PALETTE_LABEL = "Colour palette..."
STATUS_NONE = "No group configuration imported."
STATUS_PENDING = "Import pending - restart to apply."
TAG_LABEL = "Group tag"
TAG_HINT = 'shown as "FCTool by ..." at the top left (blank = none)'
TAG_ENTRY_CHARS = 12
# Two sentences of the window modules, written out: those modules are not imported until
# a window is opened (see _window_module). A test keeps each equal to its original.
MSG_INSTALLING = "An update is installing - try again in a moment."    # group_config_ui
MSG_PALETTE_LATER = "Colours change the next time FCTool starts."      # palette_ui.NOTE
MSG_PALETTE_FAILED = "The colour palette could not be saved."
MSG_NO_RESTART = ("FCTool could not restart - close it and start it again to see the new "
                  "colours.")
MSG_TAG_NOT_SAVED = "The group tag could not be saved - try again in a moment."

# -- the first-run offer -------------------------------------------------------
WELCOME_TEXT = ("New here? If your group gave you a settings file, import it to set FCTool "
                "up in one step.")
WELCOME_HIDE = "Hide this"

# -- the tag under the logo ----------------------------------------------------
TAG_FONT = ("Consolas", 8)
TAG_PREFIX = "by "
TAG_TIP = "Group tag (Settings > Group configuration)"
ELLIPSIS = chr(0x2026)

# -- attention -----------------------------------------------------------------
MARK = chr(0x26A0)                      # the warning sign
SETTINGS_TAB = "  Settings  "
# The sign takes the place of three of the four blanks: the tab strip is already wider
# than the smallest window at a large text size, and the mark must not add to that
# (measured, real styles: +4 px at tk scaling 1.33, +5 px at 1.51; a test holds it to 6).
SETTINGS_TAB_MARKED = MARK + " Settings "
SECTION_MARKET = "Market"
SECTION_TRACKING = "Tracking"
# The sections an item of a group file can flag (every ``Item.sections`` of the registry;
# a test keeps the two equal). The TOC keeps room for the sign on these rows only.
FLAGGABLE_SECTIONS = (SECTION_TRACKING, SECTION_MARKET)
MARKET_ITEM = "settings.market_staging"     # the item whose structure is checked
DISMISS_TIP = "Dismiss"
CLOSE_GLYPH = chr(0x2715)

# -- timing ----------------------------------------------------------------------
BOOT_REPORT_MS = 1500                   # the result of this start's import opens then
VERIFY_BOOT_MS = 6000                   # the structure check runs then, and after a sign-in
WRITE_RETRY_MS = 1000                   # a refused side-file write is tried once more then

_LABEL_FONT = ("Consolas", 10)
_HINT_FONT = ("Consolas", 9)
_WRAP_CHARS = 70                        # a status line and a reason wrap at this many
_WRAP_FALLBACK_PX = 480
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_POSITIVE_STYLE, _PLAIN_STYLE = "Green.TButton", "Dark.TButton"


# -- pure decisions ------------------------------------------------------------

def _quiet(call, *args, default=None):
    """``call(*args)``, or ``default`` when it raises."""
    try:
        return call(*args)
    except Exception:
        return default


def status_text(pending, applied) -> str:
    """The section's status line. ``pending``: work is staged for the next start;
    ``applied``: ``(name, revision, day)`` of the import in use, or ``None``."""
    if pending:
        return STATUS_PENDING
    if not applied:
        return STATUS_NONE
    name, revision, day = applied
    what = "'%s'" % (name,) if name else "a group file"
    text = "Using %s r%d" % (what, revision)
    return text + (", imported %s." % (day,) if day else ".")


def tag_entry_ok(text) -> bool:
    """The tag entry's validator: at most ``group_bundle.MAX_TAG`` characters."""
    return isinstance(text, str) and len(text) <= MAX_TAG


def fit_tag_text(tag, measure, room) -> str:
    """What the line under the logo shows for ``tag``, or ``""`` for no line at all.

    ``measure(text) -> pixels`` in the line's font; ``room`` is the width the word
    "FCTool" leaves. "by <tag>" when it fits; otherwise characters are dropped from the
    END of the tag and an ellipsis added until it does. No line when there is no tag,
    when the tag draws with no width at all (a few code points have no advance in the
    font - the cleaner cannot know fonts), and when not even one character fits. Total.
    """
    if not isinstance(tag, str) or not tag:
        return ""
    try:
        room = int(room)
        if room <= 0 or measure(tag) <= 0:
            return ""
        text = TAG_PREFIX + tag
        if measure(text) <= room:
            return text
        for kept in range(len(tag) - 1, 0, -1):
            head = tag[:kept].rstrip()
            if not head or measure(head) <= 0:
                continue
            text = TAG_PREFIX + head + ELLIPSIS
            if measure(text) <= room:
                return text
    except Exception:
        return ""
    return ""


def flagged_sections(flags) -> list:
    """The Settings sections (TOC titles) that carry a flag, each once, oldest first."""
    found = []
    for flag in flags if isinstance(flags, (list, tuple)) else ():
        section = flag.get("section") if isinstance(flag, dict) else None
        if isinstance(section, str) and section and section not in found:
            found.append(section)
    return found


def settings_tab_text(flags) -> str:
    """The Settings tab's text: marked while any flag exists."""
    return SETTINGS_TAB_MARKED if flagged_sections(flags) else SETTINGS_TAB


def toc_row_text(title, flagged) -> str:
    """A TOC row's text: the title, behind the mark while its section is flagged."""
    return MARK + " " + title if flagged else title


def attention_text(flags, section) -> str:
    """The reasons of ``section``'s flags, one line each, each once; ``""`` for none."""
    lines = []
    for flag in flags if isinstance(flags, (list, tuple)) else ():
        if not isinstance(flag, dict) or flag.get("section") != section:
            continue
        reason = flag.get("reason")
        if isinstance(reason, str) and reason.strip() and reason not in lines:
            lines.append(reason)
    return "\n".join(lines)


def confirm_buttons(buttons) -> list:
    """The window's ``[(label, value)]`` as the app's dialog takes them: ``[(label,
    style, value)]`` in the SAME order, the first positive action (a value that is neither
    ``None`` nor ``False``) in the green style. Entries that are no pair are left out."""
    mapped, positive_seen = [], False
    for entry in buttons if isinstance(buttons, (list, tuple)) else ():
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        label, value = entry
        positive = value is not None and value is not False
        style = _POSITIVE_STYLE if positive and not positive_seen else _PLAIN_STYLE
        positive_seen = positive_seen or positive
        mapped.append((str(label), style, value))
    return mapped


def own_names(accounts, config) -> list:
    """The user's own character names: who is signed in, then the tracked character.
    Each once. Never logged by this module."""
    names = []
    for account in accounts if isinstance(accounts, (list, tuple)) else ():
        names.append(getattr(account, "character_name", None))
    if isinstance(config, dict):
        names.append(config.get("tracked_character"))
    found = []
    for name in names:
        if isinstance(name, str) and name.strip() and name not in found:
            found.append(name)
    return found


def _is_verify(task) -> bool:
    return isinstance(task, dict) and task.get("kind") == group_apply.VERIFY_TASK


def verify_task(tasks):
    """The queued structure check, or ``None``."""
    for task in tasks if isinstance(tasks, (list, tuple)) else ():
        if _is_verify(task):
            return task
    return None


def without_verify_tasks(tasks) -> list:
    """``tasks`` without any structure check."""
    return [task for task in tasks if not _is_verify(task)] \
        if isinstance(tasks, (list, tuple)) else []


def _same_check(queued, task) -> bool:
    """Whether ``task`` is still the structure check that is queued."""
    current = verify_task(queued)
    return current is not None and isinstance(task, dict) \
        and current.get("structure_id") == task.get("structure_id")


# -- the two window modules, on demand -----------------------------------------
# An ordinary start opens neither window, so neither module is imported with this one:
# each is imported by the first call that needs it. Everything in this module reaches
# them through these two functions and nowhere else.

def _window_module():
    """``group_config_ui`` - the Group configuration window and its ``GroupHost``."""
    import group_config_ui
    return group_config_ui


def _palette_ui():
    """``palette_ui`` - the colour palette editor."""
    import palette_ui
    return palette_ui


# -- small Tk tools ------------------------------------------------------------

def _alive(widget):
    """``widget`` while it exists, else ``None``."""
    try:
        return widget if widget is not None and widget.winfo_exists() else None
    except Exception:
        return None


def _chars_px(widget, font, chars) -> int:
    """The width of ``chars`` characters of ``font``, in pixels."""
    try:
        return max(1, int(tkfont.Font(root=widget, font=font).measure("0" * chars)))
    except Exception:
        return _WRAP_FALLBACK_PX


def _tag_measure(widget):
    """``measure(text) -> pixels`` in the tag line's font (a seam for tests)."""
    return tkfont.Font(root=widget, font=TAG_FONT).measure


def _label_inset(label) -> int:
    """Pixels from a label's left edge to its text."""
    return sum(int(label.winfo_pixels(str(label.cget(option))))
               for option in ("borderwidth", "padx", "highlightthickness"))


def toc_reserve(panel, rows):
    """Make the TOC panel as wide as it is with every one of ``rows`` marked, so that a
    mark which appears later never changes the width the panel asks for (the Settings tab
    decides from that width whether the TOC fits beside the content).

    ``rows`` is ``[(row label, anchor)]``. Each row is measured with the mark in front of
    its title and left as it was; an invisible strut of the widest result goes into the
    panel. -> the strut, or ``None`` when nothing could be measured. Never raises."""
    try:
        widest = 0
        for row, _anchor in rows:
            shown = str(row.cget("text"))
            title = shown[len(MARK) + 1:] if shown.startswith(MARK + " ") else shown
            row.configure(text=toc_row_text(title, True))
            widest = max(widest, int(row.winfo_reqwidth()))
            row.configure(text=shown)
        if widest <= 0:
            return None
        strut = tk.Frame(panel, width=widest, height=1, bg=panel.cget("bg"))
        strut.pack(anchor=tk.W)
        return strut
    except Exception:
        return None


class GroupConfigMixin:
    """The group configuration feature inside the main window (module doc)."""

    _group_window = None                # the one Group configuration window
    _palette_window = None              # the one palette editor
    _group_welcome_strip = None
    _group_status_label = None
    _group_tag_var = None
    _group_tag_entry = None
    _group_tag_shown = ""               # the text the field was last known to hold
    _title_tag_label = None
    _group_toc_flagged = ()             # the TOC rows that carry a mark
    _group_toc_spacer = None
    _group_verify_inflight = False
    _group_start_seen = None            # the start's one snapshot of the side file
    # SESSION memory of the structure check - what the side file may have failed to take:
    _group_market_user_set = False      # the user set the market themselves: adopt nothing
    _group_reason_shown = None          # (structure id, reason) of the line raised last

    # -- small tools --------------------------------------------------------------------
    def _group_dir(self) -> str:
        """The data folder."""
        return str(app_dir())

    def _group_call(self, name, *args, **kwargs):
        """``self.<name>(...)`` when the host has it; a host method that raises ends as a
        log line - a result in hand is not lost to a picker that could not repaint."""
        method = getattr(self, name, None)
        if not callable(method):
            return None
        try:
            return method(*args, **kwargs)
        except Exception:
            log.warning("group configuration: %s failed", name, exc_info=True)
            return None

    def _group_say(self, text, colour=FG_ORANGE) -> None:
        """One sentence in the Settings tab's status label."""
        label = getattr(self, "_save_status", None)
        if label is None:
            return
        try:
            label.configure(text=text, fg=colour)
        except tk.TclError:
            pass

    def _group_unsay(self, text) -> None:
        """Take ``text`` out of that label again - only while it is what the label shows."""
        label = getattr(self, "_save_status", None)
        if label is None:
            return
        try:
            if str(label.cget("text")) == text:
                label.configure(text="")
        except tk.TclError:
            pass

    # -- the Settings section ------------------------------------------------------------
    def _group_overview(self):
        """ONE read of the side file for everything a start asks of it - the engine's
        ``overview``: tag, attention, tasks, the import in effect (with its stamp), and
        whether the install is fresh. ``None`` when the engine beside this module has no
        such function (or it failed): every reader then asks its single helper."""
        ask = getattr(group_apply, "overview", None)
        seen = _quiet(ask, self._group_dir(), getattr(self, "config", None)) \
            if callable(ask) else None
        return seen if isinstance(seen, dict) else None

    def _group_applied(self, seen=None):
        """The import in effect - ``{"name", "revision", ...}``, with ``at`` when it is
        known - or ``None``."""
        if seen is None:
            seen = self._group_overview()
        applied = seen.get("applied") if seen is not None \
            else _quiet(group_apply.applied_bundle, self._group_dir())
        return applied if isinstance(applied, dict) else None

    def _group_status_text(self, seen=None) -> str:
        """Nothing imported / the import in use / work staged for the next start.

        Reads the side file (once; not at all with a snapshot ``seen``). The staged file
        can hold a whole group file: it is looked for by name and never opened."""
        if _quiet(os.path.isfile, os.path.join(self._group_dir(), group_state.PENDING_FILE),
                  default=False):
            return status_text(True, None)
        applied = self._group_applied(seen)
        if applied is None:
            return status_text(False, None)
        name, revision, stamp = applied.get("name"), applied.get("revision"), applied.get("at")
        day = stamp[:10] if isinstance(stamp, str) and _ISO_DAY.fullmatch(stamp[:10]) else ""
        return status_text(False, (name if isinstance(name, str) else "",
                                   revision if type(revision) is int else 0, day))

    def _group_status_refresh(self) -> None:
        label = _alive(self._group_status_label)
        if label is None:
            return
        try:
            label.configure(text=self._group_status_text())
        except tk.TclError:
            pass

    def _build_group_settings(self, parent) -> None:
        """The first Settings section: a status line, three buttons, the tag field.

        It runs inside the app's start, so it reads nothing but the side file and a
        failure ends as a log line, never as an app that does not start."""
        try:
            self._group_build_section(parent)
        except Exception:
            log.exception("group configuration: the Settings section could not be built")

    def _group_build_section(self, parent) -> None:
        # The start's ONE read of the side file. This section is built inside the main
        # window's own build; the pass right after it (_group_after_build_ui) takes the
        # same snapshot for the offer, the tag line, the marks and the queued check.
        seen = self._group_start_seen = self._group_overview()
        self._add_section(parent, SECTION_TITLE, toc_title=TOC_TITLE)
        body = tk.Frame(parent, bg=BG_DARK)
        body.pack(fill=tk.X, padx=20, pady=2)
        status = tk.Label(body, text=self._group_status_text(seen), font=_LABEL_FONT, fg=FG_TEXT,
                          bg=BG_DARK, justify=tk.LEFT, anchor=tk.W,
                          wraplength=_chars_px(body, _LABEL_FONT, _WRAP_CHARS))
        status.pack(anchor=tk.W)
        self._group_status_label = status

        buttons = tk.Frame(body, bg=BG_DARK)
        buttons.pack(anchor=tk.W, pady=(4, 2))
        for label, command in (
                (CREATE_LABEL, lambda: self._open_group_window("create")),
                (IMPORT_LABEL, lambda: self._open_group_window("import", pick=True)),
                (PALETTE_LABEL, lambda: self._open_palette_editor())):
            ttk.Button(buttons, text=label, style=_PLAIN_STYLE, command=command
                       ).pack(side=tk.LEFT, padx=(0, 6))

        row = tk.Frame(body, bg=BG_DARK)
        row.pack(anchor=tk.W, pady=(2, 0))
        tk.Label(row, text=TAG_LABEL, font=_LABEL_FONT, fg=FG_TEXT, bg=BG_DARK
                 ).pack(side=tk.LEFT)
        stored = seen.get("tag") if seen is not None \
            else _quiet(group_apply.get_tag, self._group_dir(), default="")
        var = tk.StringVar(master=parent, value=stored if isinstance(stored, str) else "")
        entry = tk.Entry(row, textvariable=var, width=TAG_ENTRY_CHARS, font=_LABEL_FONT,
                         bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE, borderwidth=1,
                         relief=tk.RIDGE)
        # The command belongs to the entry and goes with it. Typing is refused past the
        # limit; what is stored is cleaned again by the one owner of the tag rule.
        entry.configure(validate="key", validatecommand=(entry.register(tag_entry_ok), "%P"))
        entry.pack(side=tk.LEFT, padx=(8, 0))
        entry.bind("<Return>", lambda _event: self._group_tag_commit(), add="+")
        entry.bind("<FocusOut>", lambda _event: self._group_tag_commit(), add="+")
        tk.Label(row, text=TAG_HINT, font=_HINT_FONT, fg=FG_DIM, bg=BG_DARK
                 ).pack(side=tk.LEFT, padx=(8, 0))
        self._group_tag_var, self._group_tag_entry = var, entry
        self._group_tag_shown = var.get()

    # -- the host of the window ----------------------------------------------------------
    def _group_own_names(self) -> list:
        return own_names(getattr(self, "esi_accounts", None), getattr(self, "config", None))

    def _group_confirm(self, title, message, buttons):
        """The window's question, asked with the app's own dialog - over the group window
        while there is one: parented to the main window it would open behind it."""
        parent = _alive(self._group_window) or getattr(self, "root", None)
        return self._themed_modal(title, message, buttons=confirm_buttons(buttons),
                                  parent=parent)

    def _group_edit_palette(self, parent, roles, on_done):
        """The palette editor on a draft of the group window. One editor at a time: while
        one is open - the local one, or an earlier draft - it is brought to the front, and
        THIS request is answered as cancelled, so that whoever asked is not left waiting
        for an editor that never opened."""
        if self._group_raise(self._palette_window) is not None:
            try:
                on_done({"action": "cancel", "roles": None,
                         "name": palette.preset_name(roles)})
            except Exception:
                log.warning("colour palette: the caller's on_done failed", exc_info=True)
            return
        self._group_track_palette(
            _palette_ui().open_editor(parent, roles, on_done=on_done, mode="draft"))

    def _group_host(self):
        """Everything the Group configuration window needs from the app. What can change
        while the window is open is read when the window asks, not now."""
        return _window_module().GroupHost(
            app_dir=self._group_dir(),
            app_version=APP_VERSION,
            flush_config=lambda: self._group_call("_save_config"),
            restart=lambda: self._restart_app(),
            confirm=self._group_confirm,
            own_names=self._group_own_names,
            open_settings=self._group_open_settings,
            update_state=lambda: getattr(self, "_update_install_state", "idle"),
            writable=lambda: bool(app_dir_decision().get("writable", True)),
            edit_palette=self._group_edit_palette,
            has_characters=lambda: bool(getattr(self, "esi_accounts", None)),
        )

    # -- the one window ------------------------------------------------------------------
    @staticmethod
    def _group_raise(win):
        """Bring a window of ours back in front; ``None`` when it is gone."""
        win = _alive(win)
        if win is None:
            return None
        try:
            if str(win.state()) == "iconic":
                win.deiconify()
            win.lift()
        except tk.TclError:
            pass
        return win

    def _open_group_window(self, view="create", report=None, pick=False):
        """Open the Group configuration window - or, when it is open, raise it and show
        ``view`` there. ``pick`` asks for the file to import at once; ``report`` (with
        ``view="history"``) shows the result of this start's import. -> the window, or
        ``None`` when it could not be built."""
        win = self._group_raise(self._group_window)
        if win is not None:
            try:
                controller = win._gc
                if isinstance(report, dict) and view == "history":
                    controller.show_report(report)      # show_page alone marks nothing seen
                else:
                    controller.show_page(view)
                if pick:
                    controller.choose_file()            # it asks one question at a time
            except Exception:
                log.warning("group window: the page could not be shown", exc_info=True)
            return win
        try:
            win = _window_module().open_window(getattr(self, "root", None),
                                               self._group_host(), view=view,
                                               report=report, pick=pick)
        except Exception:
            log.warning("group window: it could not be opened", exc_info=True)
            return None
        self._group_window = win

        def _forget(event, win=win):    # <Destroy> arrives for every child as well
            if event.widget is win:
                self._group_window_closed(win)
        try:
            win.bind("<Destroy>", _forget, add="+")
        except tk.TclError:
            pass
        return win

    def _group_window_closed(self, win) -> None:
        """The window is gone: what it may have changed shows in the main window now."""
        if self._group_window is win:
            self._group_window = None
        for step in (self._group_status_refresh, self._group_attention_refresh,
                     self._group_welcome_recheck):
            try:
                step()
            except Exception:           # the app itself may be closing
                pass

    # -- the first-run offer -------------------------------------------------------------
    def _build_group_welcome(self, seen=None) -> None:
        """One strip above the tabs while the install is fresh (spec 9.1): a frame in the
        main window. Showing it writes nothing. ``seen``: the start's snapshot."""
        notebook = getattr(self, "notebook", None)
        if notebook is None or _alive(self._group_welcome_strip) is not None:
            return
        fresh = seen.get("fresh") if seen is not None \
            else _quiet(group_apply.is_fresh, self._group_dir(),
                        getattr(self, "config", None), default=False)
        if fresh is not True:
            return
        strip = tk.Frame(notebook.master, bg=BG_PANEL)
        try:
            # Pack order is who keeps its room in a narrow window: the two controls, then
            # the sentence (docs: a row that outgrows its cavity starves what came last).
            make_glyph_button(strip, CLOSE_GLYPH, WELCOME_HIDE, self._group_welcome_dismiss
                              ).pack(side=tk.RIGHT, padx=(6, 12))
            ttk.Button(strip, text=IMPORT_LABEL, style=_PLAIN_STYLE,
                       command=lambda: self._open_group_window("import", pick=True)
                       ).pack(side=tk.RIGHT, padx=(6, 0), pady=4)
            tk.Label(strip, text=WELCOME_TEXT, font=_LABEL_FONT, fg=FG_TEXT, bg=BG_PANEL,
                     anchor=tk.W).pack(side=tk.LEFT, padx=(15, 0))
            strip.pack(fill=tk.X, before=notebook)
        except tk.TclError:
            strip.destroy()
            return
        self._group_welcome_strip = strip

    def _group_welcome_drop(self) -> None:
        strip, self._group_welcome_strip = self._group_welcome_strip, None
        if strip is not None:
            try:
                strip.destroy()
            except tk.TclError:
                pass

    def _group_welcome_dismiss(self) -> None:
        """The strip's close glyph: it stays away for good."""
        if self._group_welcome_strip is None:
            return
        _quiet(group_apply.dismiss_welcome, self._group_dir())
        self._group_welcome_drop()

    def _group_welcome_recheck(self) -> None:
        """An import ends the offer as well: no write, the strip just goes."""
        if self._group_welcome_strip is not None and not _quiet(
                group_apply.is_fresh, self._group_dir(), getattr(self, "config", None),
                default=True):
            self._group_welcome_drop()

    # -- the tag under the logo ----------------------------------------------------------
    def _group_tag_drop(self) -> None:
        label, self._title_tag_label = self._title_tag_label, None
        if label is not None:
            try:
                label.destroy()
            except tk.TclError:
                pass

    def _group_tag_tip(self, seen=None) -> str:
        bundle = seen.get("applied") if seen is not None \
            else _quiet(group_apply.applied_bundle, self._group_dir())
        if isinstance(bundle, dict) and bundle.get("name") \
                and type(bundle.get("revision")) is int:
            return "Group configuration: %s r%d" % (bundle["name"], bundle["revision"])
        return TAG_TIP

    def _group_tag_refresh(self, seen=None) -> None:
        """Draw, redraw or remove the "by <tag>" line under the logo (spec 10.4).

        The line never makes the logo block wider than the word "FCTool": the title row
        measures its slaves, and with no tag the block must be exactly the bare label.
        ``seen``: the start's snapshot; without it the side file is read."""
        logo = _alive(getattr(self, "_title_logo", None))
        if logo is None:
            return
        try:
            label = _alive(self._title_tag_label)
            tag = seen.get("tag") if seen is not None \
                else _quiet(group_apply.get_tag, self._group_dir(), default="")
            brand = next((child for child in logo.pack_slaves() if child is not label), None)
            text = ""
            if isinstance(tag, str) and tag and brand is not None:
                inset = _label_inset(brand)             # the line starts under the "F"
                room = int(brand.winfo_reqwidth()) - 2 * inset
                text = fit_tag_text(tag, _tag_measure(logo), room)
            if not text:
                self._group_tag_drop()
                return
            if label is None:
                label = tk.Label(logo, font=TAG_FONT, fg=FG_DIM, bg=BG_DARK, anchor=tk.W,
                                 borderwidth=0, highlightthickness=0, padx=inset, pady=0)
                label.pack(side=tk.TOP, fill=tk.X)
                attach_tooltip(label, TAG_TIP)          # once; the text follows below
                self._title_tag_label = label
            label.configure(text=text)
            if int(label.winfo_reqwidth()) > int(brand.winfo_reqwidth()):
                self._group_tag_drop()                  # never wider, whatever the font did
                return
            update_tooltip(label, self._group_tag_tip(seen))
        except tk.TclError:
            return

    def _group_tag_commit(self) -> None:
        """The Settings field was left or confirmed: store the tag, show what is stored.

        The tag lives in the feature's side file, not in the app's config - Save Settings
        does not read this field. ``set_tag`` answers the tag that IS stored: anything but
        the cleaned new one means it was not saved (the side file is in use). Then the
        field keeps what was typed - theirs to try again with - and the status label says
        so; the line under the logo shows the stored tag either way. Only a CHANGE is
        stored: a field left as it was shown never writes (it may not show what is stored)."""
        var = self._group_tag_var
        if var is None:
            return
        try:
            typed = var.get()
        except tk.TclError:
            return
        if typed == self._group_tag_shown:
            return
        wanted = clean_tag(typed)
        stored = _quiet(group_apply.set_tag, self._group_dir(), typed, default=False)
        if isinstance(stored, str) and stored == wanted:
            try:
                if typed != stored:
                    var.set(stored)     # as it was cleaned
            except tk.TclError:
                pass
            self._group_tag_shown = stored
            self._group_unsay(MSG_TAG_NOT_SAVED)
        else:
            self._group_say(MSG_TAG_NOT_SAVED)
        self._group_tag_refresh()

    # -- the palette editor --------------------------------------------------------------
    def _group_track_palette(self, win) -> None:
        self._palette_window = win

        def _forget(event, win=win):
            if event.widget is win and self._palette_window is win:
                self._palette_window = None
        try:
            win.bind("<Destroy>", _forget, add="+")
        except (AttributeError, tk.TclError):
            pass

    def _open_palette_editor(self):
        """The colour palette of THIS install. The file is read only at start-up, so
        what is chosen shows after a restart - at once, or at the next launch."""
        win = self._group_raise(self._palette_window)
        if win is not None:
            return win
        loaded = _quiet(palette.load, self._group_dir())
        stored = loaded[0] if isinstance(loaded, tuple) and loaded else None
        roles = dict(stored) if isinstance(stored, dict) else dict(palette.DEFAULT)
        try:
            win = _palette_ui().open_editor(getattr(self, "root", None), roles,
                                            on_done=self._group_palette_done, mode="local",
                                            name=palette.preset_name(roles))
        except Exception:
            log.warning("colour palette: the editor could not be opened", exc_info=True)
            return None
        self._group_track_palette(win)
        return win

    def _group_palette_done(self, result) -> None:
        action = result.get("action") if isinstance(result, dict) else None
        if action not in ("apply_now", "apply_later"):
            return
        checked = _quiet(palette.validate, result.get("roles"))
        roles = checked[0] if isinstance(checked, tuple) and checked else None
        if not isinstance(roles, dict):
            self._group_say(MSG_PALETTE_FAILED)
            return
        folder = self._group_dir()
        try:
            if roles == palette.DEFAULT:
                palette.reset(folder)
            else:
                palette.save(folder, roles, palette.preset_name(roles))
        except Exception as exc:
            log.warning("colour palette: not saved (%s)", type(exc).__name__)
            self._group_say(MSG_PALETTE_FAILED)
            return
        if action == "apply_later":
            self._group_say(MSG_PALETTE_LATER, FG_GREEN)
            return
        # True: the window is closing. False: the app refused (an update is installing).
        # Anything else - no answer, or it raised - is a restart that did not happen.
        answer = self._group_call("_restart_app")
        if answer is not True:
            self._group_say(MSG_INSTALLING if answer is False else MSG_NO_RESTART)

    # -- after the start -----------------------------------------------------------------
    def _group_open_boot_report(self) -> None:
        self._open_group_window("history", getattr(self, "_group_boot_report", None))

    def _group_show_boot_report(self) -> None:
        """The result of the import applied at this start opens by itself, once the main
        window is up - unless it was seen."""
        report = getattr(self, "_group_boot_report", None)
        root = getattr(self, "root", None)
        if root is not None and isinstance(report, dict) and not report.get("seen"):
            root.after(BOOT_REPORT_MS, self._group_open_boot_report)

    def _group_verify_schedule(self, seen=None) -> None:
        """The structure check, a few seconds into the session - when one is queued. (A
        check is only ever queued by an import, and that was applied before this start;
        a sign-in asks on its own.)"""
        queued = seen.get("tasks") if seen is not None \
            else _quiet(group_apply.tasks, self._group_dir())
        if verify_task(queued) is not None:
            self.root.after(VERIFY_BOOT_MS, self._group_verify_kick)

    def _group_after_build_ui(self) -> None:
        """Called once, right after the main window is built: the offer, the tag line, the
        marks, the result of this start's import, the structure check. No step can stop
        the app from starting.

        The side file is read ONCE for a whole start: the snapshot the Settings section
        took while the window was built serves every step here (a start that built no
        section takes its own). It is dropped afterwards - what happens later in the
        session reads the file through the single helpers, as it is then."""
        seen, self._group_start_seen = self._group_start_seen, None
        if seen is None:
            seen = self._group_overview()
        for step, wants_seen in (("_build_group_welcome", True), ("_group_tag_refresh", True),
                                 ("_group_attention_refresh", True),
                                 ("_group_show_boot_report", False),
                                 ("_group_verify_schedule", True)):
            try:
                if wants_seen:
                    getattr(self, step)(seen)
                else:
                    getattr(self, step)()
            except Exception:
                log.exception("group configuration: %s failed", step)

    # -- attention -----------------------------------------------------------------------
    def _group_open_settings(self, section) -> None:
        """Show the Settings tab, scrolled to the section with that TOC title. A section
        the tab does not have only selects the tab."""
        notebook = getattr(self, "notebook", None)
        tab = getattr(self, "_settings_toc_tab", None)
        if notebook is None or tab is None:
            return
        try:
            notebook.select(tab)
        except tk.TclError:
            return
        for title, anchor in getattr(self, "_settings_toc_anchors", None) or ():
            if title == section:
                self._group_call("_settings_toc_jump",
                                 getattr(self, "_settings_toc_canvas", None),
                                 getattr(self, "_settings_toc_scroll_frame", None), anchor)
                return

    def _group_banner_drop(self, section) -> None:
        banner = self._group_banners.pop(section, None)
        if banner is not None:
            try:
                banner.destroy()
            except tk.TclError:
                pass

    def _group_banner_show(self, section, anchor, text) -> None:
        """The orange line of a flagged section, packed right AFTER the section's anchor -
        its header, or its first row where a section has no header. Never before it:
        "Open in Settings" scrolls the anchor to the top of the view, and a line above it
        would be scrolled out by that very jump."""
        banner = _alive(self._group_banners.get(section))
        if banner is not None:
            banner._group_reason.configure(text=text)
            return
        banner = tk.Frame(anchor.master, bg=BG_DARK)
        try:
            reason = tk.Label(banner, text=text, font=_HINT_FONT, fg=FG_ORANGE, bg=BG_DARK,
                              justify=tk.LEFT, anchor=tk.W,
                              wraplength=_chars_px(banner, _HINT_FONT, _WRAP_CHARS))
            reason.pack(side=tk.LEFT)
            make_glyph_button(banner, CLOSE_GLYPH, DISMISS_TIP,
                              lambda section=section: self._group_attention_clear(section),
                              palette={"BG_PANEL": BG_DARK}
                              ).pack(side=tk.LEFT, padx=(8, 0), anchor=tk.N)
            banner.pack(fill=tk.X, padx=20, pady=(0, 4), after=anchor)
        except tk.TclError:
            banner.destroy()
            return
        banner._group_reason = reason
        self._group_banners[section] = banner

    def _group_attention_refresh(self, seen=None) -> None:
        """Show the attention flags of the side file: the mark on the Settings tab, the
        mark and the orange colour on the flagged TOC rows, one orange line per flagged
        section. Reads only; what is not built yet is skipped. ``seen``: the start's
        snapshot; without it the flags are read."""
        flags = seen.get("attention") if seen is not None \
            else _quiet(group_apply.attention, self._group_dir())
        flags = flags if isinstance(flags, list) else []
        sections = flagged_sections(flags)
        if not isinstance(getattr(self, "_group_banners", None), dict):
            self._group_banners = {}

        notebook = getattr(self, "notebook", None)
        tab = getattr(self, "_settings_toc_tab", None)
        if notebook is not None and tab is not None:
            try:
                text = settings_tab_text(flags)
                if str(notebook.tab(tab, "text")) != text:
                    notebook.tab(tab, text=text)
            except tk.TclError:
                pass

        anchors = list(getattr(self, "_settings_toc_anchors", None) or ())
        rows = [(row, next((title for title, anchor in anchors if anchor is widget), None))
                for row, widget in getattr(self, "_settings_toc_rows", None) or ()]
        panel = getattr(self, "_settings_toc_panel", None)
        strut = _alive(self._group_toc_spacer)
        if panel is not None and (strut is None or strut.master is not panel):
            # Before the first mark - and only for the rows a mark can land on: room for
            # every row would widen the panel for everyone (measured: +21 px, enough to
            # fold the TOC away in the smallest window).
            markable = [(row, None) for row, title in rows if title in FLAGGABLE_SECTIONS]
            self._group_toc_spacer = strut = toc_reserve(panel, markable)
        marked = []
        for row, title in rows:
            if title is None:
                continue
            flagged = title in sections
            try:
                # The sign needs the room reserved above; a row without it turns orange.
                row.configure(text=toc_row_text(
                    title, flagged and strut is not None and title in FLAGGABLE_SECTIONS))
                if str(row.cget("fg")).lower() != FG_ACCENT.lower():    # not the one in view
                    row.configure(fg=FG_ORANGE if flagged else FG_DIM)
            except tk.TclError:
                continue
            if flagged:
                marked.append(row)
        self._group_toc_flagged = marked

        placed = {title: anchor for title, anchor in anchors}
        for section in list(self._group_banners):
            if section not in sections or section not in placed:
                self._group_banner_drop(section)
        for section in sections:
            anchor = placed.get(section)
            if anchor is None:
                continue
            try:
                self._group_banner_show(section, anchor, attention_text(flags, section))
            except tk.TclError:
                pass

    def _group_attention_clear(self, section) -> None:
        """The user set the value, or dismissed the line: the section's flags go."""
        _quiet(group_apply.clear_attention, self._group_dir(), section)
        self._group_attention_refresh()

    def _group_has_flag(self, section) -> bool:
        flags = _quiet(group_apply.attention, self._group_dir())
        return section in flagged_sections(flags)

    def _group_market_picked(self) -> None:
        """The user picked a market structure or station: the group's own structure is
        not asked about any more, and the Market flag goes.

        The session remembers the pick on its own: dropping the check can fail (the side
        file in use), and its answer must not replace what the user just chose."""
        self._group_market_user_set = True
        self._group_write(self._group_drop_check)
        if self._group_has_flag(SECTION_MARKET):
            self._group_attention_clear(SECTION_MARKET)

    # -- writes to the side file that may be refused -------------------------------------
    def _group_write(self, write) -> bool:
        """Run ``write()`` - one change of the side file through an engine helper, which
        answers ``None`` / ``True`` (stored) or an explicit ``False`` (not stored: the file
        is in use). -> whether it was stored.

        A refusal gets ONE more try about a second later (``_group_write_again``), and
        that try's answer is final: never a loop. ``write`` decides by what is true when
        it RUNS, so a retry never puts back what the user changed meanwhile. Without a
        window, or with one that is closing, no retry is owed."""
        if _quiet(write, default=False) is not False:
            return True
        root = getattr(self, "root", None)
        try:
            if root is not None:
                root.after(WRITE_RETRY_MS, self._group_write_again, write)
        except Exception:               # the app is closing: the timer was refused
            pass
        return False

    def _group_write_again(self, write) -> None:
        """The one retry of a refused write; what it stored shows at once."""
        if _alive(getattr(self, "root", None)) is None:
            return
        if _quiet(write, default=False) is not False:
            try:
                self._group_attention_refresh()
            except Exception:
                pass

    def _group_drop_check(self):
        """Take the structure check off the queue, by what is queued NOW. -> the helper's
        answer; ``None`` when no check is queued (nothing was written)."""
        folder = self._group_dir()
        queued = _quiet(group_apply.tasks, folder)
        queued = queued if isinstance(queued, list) else []
        kept = without_verify_tasks(queued)
        if len(kept) == len(queued):
            return None
        return group_apply.set_tasks(folder, kept)

    def _group_staging_picked(self) -> None:
        """The user set the staging system."""
        if self._group_has_flag(SECTION_TRACKING):
            self._group_attention_clear(SECTION_TRACKING)

    # -- the structure check -------------------------------------------------------------
    def _group_verify_kick(self) -> None:
        """Start the check of the group's market structure - 6 s after the start and after
        every sign-in. Nothing happens while one runs, when none is queued, or once the
        user set the market themselves in this session."""
        if self._group_verify_inflight or self._group_market_user_set:
            return
        task = verify_task(_quiet(group_apply.tasks, self._group_dir()))
        if task is None:
            return
        self._group_verify_inflight = True
        try:
            threading.Thread(target=self._group_verify_worker, args=(dict(task),),
                             daemon=True).start()
        except Exception:
            self._group_verify_inflight = False

    def _group_verify_worker(self, task) -> None:
        """WORKER THREAD. One ESI call - can the primary character see the structure? -
        and one verdict handed to the main thread. No widget, no variable, no timer.

        A failure of this method itself is "not answered", through the module's own
        choice: at every start it would otherwise be retried for ever."""
        try:
            auth = getattr(self, "esi_auth", None)
            authed = bool(auth is not None and auth.is_authenticated)
            has_scope = bool(authed and auth.has_market_structure_scope())
            verdict = group_verify.verify_structure(
                task, authed=authed, has_scope=has_scope,
                get_structure=lambda sid: auth.esi_get_ex(f"/universe/structures/{sid}/")[:2])
        except Exception:
            verdict = group_verify.unanswered(task)
        try:
            self._post_ui(self._group_verify_done, task, verdict)
        except Exception:
            self._group_verify_inflight = False

    def _group_verify_done(self, task, verdict) -> None:
        """Tk thread: act on the verdict of one structure check (plan rule 14).

        The user's own market always stands. A verdict is dropped whole - nothing
        persisted, no line raised - when its task is not queued any more (a station or
        structure pick dropped it), when the user picked one in this session (even if the
        task could not be dropped then), and, for a reachable structure, when the app's
        market system is no longer the one the group file set: a system pick, or an id
        typed into the advanced fields, drops no task, and the group's structure under
        another system is a market that does not exist.

        A line the user dismissed stays away: the task remembers the reason it showed
        (``reason_shown``), and the same reason is not raised a second time once its line
        is gone. Another outcome raises its own line."""
        self._group_verify_inflight = False
        outcome = verdict.outcome if isinstance(verdict, group_verify.Verdict) else None
        word = outcome if isinstance(outcome, str) and outcome in group_verify.OUTCOMES \
            else "unknown"
        folder = self._group_dir()
        queued = _quiet(group_apply.tasks, folder)
        queued = queued if isinstance(queued, list) else []
        current = _same_check(queued, task)
        if self._group_market_user_set or not current:
            if current:                 # it could not be dropped at the pick: once more
                self._group_write(self._group_drop_check)
            log.info("group structure check: dropped, %s", word)
            return
        # int(): the stored id may be a whole float after a hand edit.
        structure_id = _quiet(int, task.get("structure_id"), default=0)
        reachable = word in ("ok", "renamed") and structure_id > 0
        if reachable and self._group_market_moved(task):
            self._group_market_user_set = True
            self._group_write(self._group_drop_check)
            shown = self._group_shown_reason(task, structure_id)
            if shown and self._group_line_showing(folder, shown):
                # "... will be set once your character is signed in": no longer true
                self._group_attention_clear(SECTION_MARKET)
            log.info("group structure check: dropped, %s", word)
            return
        if reachable:
            self._group_call("_market_persist_keys",
                             staging_structure_id=structure_id,
                             staging_structure_name=verdict.name,
                             staging_station_id=0, staging_station_name="")
            self._group_call("_market_set_id_var", "staging_structure_id", structure_id)
            self._group_call("_market_set_id_var", "staging_station_id", 0)
            self._group_call("_market_clear_station_selection")     # what the picker shows
            self._group_call("_market_restore_saved_pickers")
            self._group_call("_refresh_market_status_labels")
            self._group_call("_auto_market_rescan_if_needed")
        left = group_verify.after_verdict(task, verdict)
        reason = group_verify.attention_reason(task, verdict)

        # The two writes, each as a function of what is true when it RUNS: a write that is
        # refused (the side file in use) is tried once more a second later, and by then
        # the user may have picked a market of their own.
        def put_flag():
            if self._group_market_user_set:
                return None
            answer = group_apply.add_attention(folder, SECTION_MARKET, MARKET_ITEM, reason)
            if answer is not False:
                self._group_reason_shown = (structure_id, reason)
            return answer

        read_already = [queued]

        def put_task():
            now = read_already.pop() if read_already else _quiet(group_apply.tasks, folder)
            now = now if isinstance(now, list) else []
            if self._group_market_user_set or not _same_check(now, task):
                return None
            remaining = [left if _is_verify(entry) else entry for entry in now]
            return group_apply.set_tasks(folder, [entry for entry in remaining
                                                  if entry is not None])

        if reason:
            dismissed = self._group_shown_reason(task, structure_id) == reason \
                and not self._group_line_showing(folder, reason)
            # Only an explicit False means "not stored" (the helper answers None or a bool):
            # a line nobody saw must not count as shown.
            if dismissed or self._group_write(put_flag):
                self._group_reason_shown = (structure_id, reason)
                if _is_verify(left) and left.get("reason_shown") != reason:
                    left = dict(left, reason_shown=reason)
        elif reachable:
            _quiet(group_apply.clear_attention, folder, SECTION_MARKET)
        self._group_write(put_task)
        self._group_attention_refresh()
        log.info("group structure check: %s", word)

    def _group_market_moved(self, task) -> bool:
        """Whether the app's market system is no longer the one the task was queued for.
        A task that names no system has nothing to be compared with; anything in the
        config that is not that id - another system, none, junk - counts as moved."""
        wanted = task.get("system_id") if isinstance(task, dict) else None
        if type(wanted) is not int or wanted <= 0:
            return False
        config = getattr(self, "config", None)
        market = config.get("market") if isinstance(config, dict) else None
        stored = market.get("staging_system_id") if isinstance(market, dict) else None
        try:
            return int(stored or 0) != wanted
        except (TypeError, ValueError, OverflowError):
            return True

    def _group_shown_reason(self, task, structure_id) -> str:
        """The reason whose line this check raised last: kept in the task, and for the
        session where the task could not be written."""
        shown = task.get("reason_shown") if isinstance(task, dict) else None
        if isinstance(shown, str) and shown:
            return shown
        memory = self._group_reason_shown
        return memory[1] if memory is not None and memory[0] == structure_id else ""

    def _group_line_showing(self, folder, reason) -> bool:
        """Whether a Market flag with exactly this reason exists."""
        flags = _quiet(group_apply.attention, folder)
        return any(isinstance(flag, dict) and flag.get("section") == SECTION_MARKET
                   and flag.get("reason") == reason
                   for flag in (flags if isinstance(flags, list) else ()))


# -- the hooks the app's existing methods call ---------------------------------

def _hook(host, name, *args) -> None:
    """``host.<name>(*args)`` for a host that has it. Never raises."""
    try:
        method = getattr(host, name, None)
        if callable(method):
            method(*args)
    except Exception:
        log.warning("group configuration: %s failed", name, exc_info=True)


def market_picked(host, chosen=True) -> None:
    """A market structure or station was picked in Settings (``chosen``: its id; the
    "(none)" row is no pick)."""
    if chosen:
        _hook(host, "_group_market_picked")


def staging_saved(host, name) -> None:
    """The staging system was saved; a blank name sets nothing."""
    if isinstance(name, str) and name.strip():
        _hook(host, "_group_staging_picked")


def signed_in(host) -> None:
    """A character signed in: the structure check can be asked again."""
    _hook(host, "_group_verify_kick")


def toc_idle_fg(host, row) -> str:
    """The colour of a TOC row that is not the section in view: orange while its section
    is flagged, else dim. Total - any host, any row."""
    try:
        flagged = getattr(host, "_group_toc_flagged", None) or ()
        return FG_ORANGE if any(row is marked for marked in flagged) else FG_DIM
    except Exception:
        return FG_DIM
