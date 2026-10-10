"""Discord ping - the app's side: what the yellow ``Copy ping`` split button of the MOTD
builder does (the copy, the editor window, the tooltip) and how a doctrine's ping entry
follows the doctrine library.

``FCToolGUI`` inherits :class:`PingWiringMixin`. The main module keeps the import, the
base class, the button's layout and ttk style, the two session attributes, two one-line
doctrine hooks and the close-path call. This module never imports the main module.

What it needs from its host
---------------------------
``config``                    the app's config dict - always read through ``self.config``
                              at call time, never kept (Save Settings REPLACES the dict)
``_save_config()``            write the config now
``root``                      the main Tk window: the clipboard, the editor's parent
``_motd_fleet_status``        the MOTD builder's status label (``config(text=, fg=)``)
``_motd_selected_doctrine()`` the composer's doctrine, or ``None``
``_motd_selected_fc_auth()``  the FC picked in the composer, or ``None``
``esi_auth``                  the primary character (``character_name``)
``fittings``                  the doctrine library (``list_doctrines()``)
``_system_names``             solar-system names for the editor's completion

What it reads AND the host creates with the MOTD tab: ``_ping_when`` (a
:data:`WhenSession`, the pending time of this session) and ``_ping_dialog`` (the one
editor window, or ``None``).

Rules
-----
* ONE copy routine. The main button and the editor's own button both end in
  ``_copy_discord_ping``; nothing else here touches the clipboard.
* The store is handed ``lambda: self.config``, never the dict or a part of it.
* Nothing fails silently. The shipped exe has no stderr, so a failure writes one status
  line and one log line. The log names the exception's TYPE and the ``file:line`` it was
  raised at (``via`` this module's own line when that is a library's) - never its
  message, which can carry a character's name or template text.
* No input hold, no thread, no timer, no variable trace. The editor window is built by
  ``ping_dialog``; the opener only keeps it to one window.
"""

import logging
import os
import time
from tkinter import messagebox

import ping_compose
import ping_dialog
import ping_store
from ui_theme import FG_GREEN, FG_YELLOW

log = logging.getLogger(__name__)

#: The host builds its session's pending time from this (``self._ping_when``).
WhenSession = ping_compose.WhenSession

_DASH = chr(0x2014)          # em dash, built at runtime: the source stays pure ASCII


def _where(exc) -> str:
    """Where ``exc`` was raised - places, never the message: ``file.py:line`` of the
    deepest frame and, when that frame is not this module's (a ``TclError`` is raised
    inside tkinter), `` via ping_wiring.py:line`` - this module's own deepest line on
    the way there. Base names only: a full path would carry the Windows user name."""
    trace = getattr(exc, "__traceback__", None)
    if trace is None:
        return "?"
    own = None
    while True:
        if trace.tb_frame.f_globals.get("__name__") == __name__:
            own = trace
        if trace.tb_next is None:
            break
        trace = trace.tb_next
    place = "%s:%d" % (os.path.basename(trace.tb_frame.f_code.co_filename), trace.tb_lineno)
    if own is None or own is trace:
        return place
    return "%s via %s:%d" % (place, os.path.basename(own.tb_frame.f_code.co_filename),
                             own.tb_lineno)


class PingWiringMixin:
    """The Discord ping inside the main window (module docstring)."""

    def _ping_store(self):
        """The discord_ping block's reader/writer. It is handed ``lambda:
        self.config``, never the dict: Save Settings REPLACES self.config."""
        return ping_store.PingStore(lambda: self.config, self._save_config)

    def _ping_env(self):
        """What a ping takes from the app right now: doctrine, FC, staging."""
        doctrine = self._motd_selected_doctrine()
        auth = self._motd_selected_fc_auth() or getattr(self, "esi_auth", None)
        zkill = self.config.get("zkillboard")       # hand-edited config: any shape
        staging = zkill.get("staging_system") if isinstance(zkill, dict) else None
        return ping_compose.Env(
            getattr(doctrine, "id", None), getattr(doctrine, "name", None) or "",
            getattr(auth, "character_name", None) or "",
            staging.strip() if isinstance(staging, str) else "")

    def _ping_preview_text(self):
        """The main button's tooltip: what a click would copy. Never raises."""
        try:
            when = self._ping_when.unix
            return ping_compose.tooltip_text(
                ping_compose.compose(self._ping_store(), self._ping_env(), when,
                                     int(time.time())), pending=when is not None)
        except Exception:
            return ""

    def _copy_discord_ping(self):
        """THE one ping copy - the main button and the editor's button both end here.
        A pending time is used up by a copy that SUCCEEDED, never else. A failure is
        never silent: the status line says it and the log says where."""
        dialog, status = getattr(self, "_ping_dialog", None), self._motd_fleet_status
        copied = said = False
        try:
            editing = dialog is not None and dialog.alive()
            if editing:
                dialog.flush()      # a template edit still pending is copied too
            now = int(time.time())
            composed = ping_compose.compose(self._ping_store(), self._ping_env(),
                                            self._ping_when.unix, now)
            consumed = None
            if composed.text:
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(composed.text)
                except Exception as e:
                    messagebox.showerror("Copy failed", f"Could not copy the ping:\n{e}",
                                         parent=getattr(self, "root", None))
                    return
                copied = True       # set BEFORE anything else can fail
                consumed = self._ping_when.consume()
            text, level = ping_compose.summary(composed, consumed_when=consumed, now_unix=now)
            status.config(text=text, fg=FG_GREEN if level == "ok" else FG_YELLOW)
            said = True             # said FIRST: a refresh that fails costs no confirmation
            if copied and editing:
                dialog.refresh_when()
        except Exception as exc:
            log.warning("ping: the copy routine failed (%s at %s, copied=%s)",
                        type(exc).__name__, _where(exc), copied)
            if not said:
                status.config(fg=FG_YELLOW, text="Ping copied" if copied else (
                    "Ping not copied %s an internal error (%s); see fctool.log"
                    % (_DASH, type(exc).__name__)))

    def _open_ping_dialog(self):
        """The arrow: open the ping editor, or lift the one already open."""
        try:
            dialog = getattr(self, "_ping_dialog", None)
            if dialog is not None and dialog.alive():
                dialog.lift()
                return
            self._ping_dialog = ping_dialog.PingDialog(
                self.root, store=self._ping_store(), env=self._ping_env,
                when=self._ping_when, copy=self._copy_discord_ping,
                system_names=lambda: list(getattr(self, "_system_names", []) or []),
                confirm=lambda title, message: bool(messagebox.askyesno(
                    title, message, parent=getattr(self._ping_dialog, "win", self.root))),
                emoji=True,     # a flag: the window builds and owns the emoji itself
                doctrine_ids=lambda: [d.id for d in self.fittings.list_doctrines()])
        except Exception as exc:    # never silent; self._ping_dialog stays as it was
            log.warning("ping: the editor could not be opened (%s at %s)",
                        type(exc).__name__, _where(exc))
            self._motd_fleet_status.config(
                text="The ping editor could not be opened; see fctool.log", fg=FG_YELLOW)

    def _ping_doctrine_changed(self, op, *ids):
        """Duplicate / Delete: keep the ping entry in step (``op`` = the PingStore
        method). An open editor stores its pending edit FIRST - saved later, it
        would bring a dropped entry back. Never blocks the doctrine operation."""
        try:
            try:
                editor = getattr(self, "_ping_dialog", None)
                if editor is not None and editor.alive():
                    editor.flush()
            finally:
                getattr(self._ping_store(), op)(*ids)
        except Exception as exc:
            log.warning("ping: %s did not go through cleanly (%s at %s)",
                        op, type(exc).__name__, _where(exc))
