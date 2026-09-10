# update_dialog.py
"""The one-click update window — a Tk shell over a pure state machine.

What this module is
-------------------
The FC clicks "↑ v5.7.0 available" in the title bar and gets THIS window: the
version pair, the release notes as plain text, a progress bar, a status line
and five buttons. It is the whole user-facing surface of the self-updater, and
it contains none of the updater: ``self_update`` downloads, verifies, extracts
and swaps; ``fc_gui`` owns the worker thread that runs those steps. This module
decides only what the user sees and which button is usable when.

The split is deliberate and load-bearing:

* **The dialog owns NO threads.** Pressing *Download & install* calls
  ``host._start_update_install(info, self.set_progress, self.on_result)``. The
  host spawns the worker and marshals both callbacks back onto the Tk thread
  (``_post_ui``). Nothing in this file may be called from a worker, and nothing
  in this file may block — a Tk callback that blocks freezes the app, and this
  window is open while a 53 MB download runs.
* **The host is duck-typed, not imported.** ``update_dialog`` sits BELOW
  ``fc_gui`` in the import order (``app_version <- update_check <-
  self_update <- update_dialog <- fc_gui``), so it can be built and tested with
  a ``SimpleNamespace`` carrying ``root``, ``_start_update_install``,
  ``_cancel_update_install``, ``_request_restart`` and ``_open_release_page``.
  A source-text test enforces the direction.
* **The pure core is separable.** ``format_notes`` and ``buttons_for`` are
  plain functions with no Tk in sight, which is what makes the notes formatter
  and every row of the button table assertable without a display.

Two rules that are NOT style preferences
----------------------------------------
1. **This window never grabs** (``make_modal(..., grab=False)``). A Tk
   ``grab_set`` is application-wide: while it is held, every other Toplevel
   this process owns stops receiving input — including the FCPreview client
   tiles, which are how an FC switches EVE clients mid-fight. The tiles keep
   animating (DWM composites them at the OS level) while dead to clicks, so the
   symptom reads as "the previews froze", never as "that dialog did it". v4.1.0
   shipped exactly that regression from a different window. A source-text test
   asserts the token is absent from this file.
2. **Every Tk call is guarded against ``TclError``.** The host marshals
   ``set_progress`` / ``on_result`` with ``_post_ui``, so both can land AFTER
   the user closed the window; an unguarded call would raise inside the UI
   dispatcher rather than in the caller that could handle it.

Closing while an install runs
-----------------------------
The window manager's X and the *Cancel* button share ONE flag, so the worker is
told to stop exactly once per attempt (and the flag re-arms when a new attempt
starts). Cancel does not close the window: the worker still owes a result, and
the user should see what came of it.
"""
from __future__ import annotations

import logging
import re
import tkinter as tk
from enum import Enum
from tkinter import ttk

from app_version import APP_VERSION
# Containment-safe leaves only. ui_theme is the single source of the palette
# (never re-declare a colour literal here); ui_helpers carries the house dialog
# contract, whose ``grab=False`` branch is the whole reason this window can be
# a normal themed dialog without deafening the previews.
from ui_helpers import make_modal
from ui_theme import (
    BG_DARK, BG_ENTRY, BG_PANEL,
    BORDER_COLOR,
    FG_ACCENT, FG_DIM, FG_RED, FG_TEXT,
)

log = logging.getLogger(__name__)

#: Shown instead of an empty notes box, which a user cannot tell apart from a
#: release whose notes simply failed to arrive.
NO_NOTES = "No release notes."

#: The refusal text when the host says "not now" without saying why.
CANNOT_START_NOW = "FCTool cannot start an update right now."

#: The fallback for a result that carries no message of its own.
GENERIC_FAILURE = "The update did not finish. Nothing was changed."


class DialogState(str, Enum):
    """Where one install attempt has got to.

    A ``str`` Enum so a value that made a round trip through a host attribute
    or a log line still resolves (``DialogState("downloading")``).

    ``BLOCKED`` is not a failure: it is a release this install cannot take —
    running from source, a read-only exe folder, a release with no usable zip —
    and the honest answer to it is the release page, not a disabled button with
    no explanation.
    """

    AVAILABLE = "available"
    BLOCKED = "blocked"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    INSTALLING = "installing"
    READY_TO_RESTART = "ready_to_restart"
    FAILED = "failed"


#: The states during which the worker is alive and closing must cancel it.
BUSY_STATES = (DialogState.DOWNLOADING, DialogState.VERIFYING,
               DialogState.INSTALLING)

#: Every button this window owns, in footer order.
BUTTON_KEYS = ("install", "restart", "release_page", "later", "cancel")

# ── The enablement table ─────────────────────────────────────────────────────
# All five buttons exist in every state; the state decides which are usable, so
# the footer never reflows mid-download (a moving Cancel button under a moving
# mouse is how a user cancels by accident).
#
# Two decisions worth naming:
#   * VERIFYING and INSTALLING show Cancel DISABLED. Hashing 53 MB and two
#     same-volume renames are seconds long, and neither can be interrupted
#     without leaving staging files behind — an enabled Cancel there would be
#     a button that does nothing, which is worse than a greyed one.
#   * FAILED re-enables Install as the RETRY. Every failure path in
#     ``self_update.run_install`` cleans up after itself, so a second attempt
#     always starts from a clean staging directory.
_BUTTONS = {
    DialogState.AVAILABLE:
        dict(install=True,  restart=False, release_page=True,  later=True,  cancel=False),
    DialogState.BLOCKED:
        dict(install=False, restart=False, release_page=True,  later=True,  cancel=False),
    DialogState.DOWNLOADING:
        dict(install=False, restart=False, release_page=False, later=False, cancel=True),
    DialogState.VERIFYING:
        dict(install=False, restart=False, release_page=False, later=False, cancel=False),
    DialogState.INSTALLING:
        dict(install=False, restart=False, release_page=False, later=False, cancel=False),
    DialogState.READY_TO_RESTART:
        dict(install=False, restart=True,  release_page=False, later=True,  cancel=False),
    DialogState.FAILED:
        dict(install=True,  restart=False, release_page=True,  later=True,  cancel=False),
}

#: What the status line says when the caller passes no message of its own.
_DEFAULT_STATUS = {
    DialogState.AVAILABLE: "",
    DialogState.BLOCKED: "This update cannot be installed automatically.",
    DialogState.DOWNLOADING: "Downloading…",
    DialogState.VERIFYING: "Checking the download…",
    DialogState.INSTALLING: "Installing…",
    DialogState.READY_TO_RESTART: "Installed — restart FCTool to run it.",
    DialogState.FAILED: GENERIC_FAILURE,
}

#: A host that is mid-install (or already finished one) when the window opens.
#: Anything not listed — "idle", "", a value from a future version — means
#: "nothing in flight", which is AVAILABLE.
_HOST_STATE = {
    "ready": DialogState.READY_TO_RESTART,
    "installing": DialogState.DOWNLOADING,
    "running": DialogState.DOWNLOADING,
    "busy": DialogState.DOWNLOADING,
}

_BUTTON_LABELS = {
    # Tk has no ampersand mnemonics (that is -underline), so a single & here
    # is exactly what the user reads.
    "install": "Download & install",
    "restart": "Restart now",
    "release_page": "Open release page",
    "later": "Later",
    "cancel": "Cancel",
}

# Heading markers: one to six leading hashes FOLLOWED BY whitespace or the end
# of the line. "#hashtag" and "code # comment" are not headings and stay.
_HEADING_RE = re.compile(r"^#{1,6}(?:[ \t]+|$)")
# Four or more newlines = three or more blank lines; leave exactly two.
_BLANK_RUN_RE = re.compile(r"\n{4,}")


def format_notes(body) -> str:
    """Render a GitHub release body for a plain, read-only ``Text`` box.

    There is no markdown renderer here on purpose: the notes are read, never
    interacted with, and a renderer would be a dependency plus a whole class of
    escaping bugs for a paragraph of text. What is worth doing is dropping the
    three markers that read as noise unrendered — heading hashes, ``**`` bold
    stars and backticks — and collapsing the blank-line piles markdown authors
    leave behind. Everything else (bullets, indentation, links, punctuation) is
    verbatim.

    Total: anything that is not a non-blank string returns :data:`NO_NOTES`.
    """
    try:
        if not isinstance(body, str) or not body.strip():
            return NO_NOTES
        text = body.replace("\r\n", "\n").replace("\r", "\n")
        lines = [_HEADING_RE.sub("", line) for line in text.split("\n")]
        text = "\n".join(lines).replace("**", "").replace("`", "")
        text = _BLANK_RUN_RE.sub("\n\n\n", text)
        return text if text.strip() else NO_NOTES
    except Exception as exc:                      # pragma: no cover - defensive
        log.debug("format_notes failed: %s", exc)
        return NO_NOTES


def buttons_for(state) -> dict:
    """The enablement map for ``state``: ``{key: bool}`` over
    :data:`BUTTON_KEYS`.

    Total by design — an unrecognised state enables NOTHING, so a caller that
    loses track of where it is can never offer an install by accident. Returns
    a fresh dict every call; the table above is not handed out.
    """
    try:
        row = _BUTTONS.get(DialogState(state))
    except (ValueError, TypeError):
        row = None
    if row is None:
        return {key: False for key in BUTTON_KEYS}
    return dict(row)


def _clamp(value, low, high):
    return low if value < low else (high if value > high else value)


class UpdateDialog(tk.Toplevel):
    """The window itself. Owns widgets and state; owns no work.

    ``host`` is duck-typed (see the module docstring). ``info`` is an
    :class:`update_check.UpdateInfo` — read for ``tag``, ``url`` and ``notes``
    only, so any object with those attributes will do. ``block_reason``, when
    given, opens the window in :attr:`DialogState.BLOCKED` showing that reason:
    the HOST decides what is installable (frozen build, writable folder, a
    release with exactly one usable zip), because only the host knows.
    """

    def __init__(self, host, info, *, block_reason=None):
        super().__init__(getattr(host, "root", None))
        self._host = host
        self._info = info
        # The X and the Cancel button share this, so one attempt is stopped
        # once; a new attempt re-arms it.
        self._cancel_sent = False
        self.dialog_state = DialogState.AVAILABLE
        self.buttons = {}

        try:
            self.title("FCTool update")
        except tk.TclError:
            pass
        make_modal(self, getattr(host, "root", None), on_cancel=self.close,
                   grab=False)                     # NEVER a grab — see module doc
        self._build()
        try:
            self.protocol("WM_DELETE_WINDOW", self.close)
        except tk.TclError:
            pass
        self._place_near_parent()

        if isinstance(block_reason, str) and block_reason.strip():
            self.set_state(DialogState.BLOCKED, block_reason)
        else:
            self.set_state(_HOST_STATE.get(
                getattr(host, "_update_install_state", None),
                DialogState.AVAILABLE))

    # ── construction ────────────────────────────────────────────────────────

    def _build(self):
        tag = str(getattr(self._info, "tag", "") or "")
        pad = dict(padx=12)

        self.header = tk.Label(
            self, bg=BG_DARK, fg=FG_ACCENT, anchor="w", justify="left",
            font=("Segoe UI", 11, "bold"),
            text=f"FCTool {tag} is available "
                 f"(you are running {APP_VERSION})")
        self.header.pack(fill=tk.X, pady=(12, 6), **pad)

        notes_frame = tk.Frame(self, bg=BG_DARK, highlightthickness=1,
                               highlightbackground=BORDER_COLOR)
        notes_frame.pack(fill=tk.BOTH, expand=True, **pad)
        scroll = ttk.Scrollbar(notes_frame, orient=tk.VERTICAL)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.notes = tk.Text(
            notes_frame, wrap="word", height=14, width=68, bd=0,
            relief=tk.FLAT, bg=BG_ENTRY, fg=FG_TEXT, padx=8, pady=6,
            yscrollcommand=scroll.set)
        self.notes.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.configure(command=self.notes.yview)
        self.notes.insert("1.0", format_notes(getattr(self._info, "notes", "")))
        # Read-only, not merely uneditable-by-convention: a NORMAL Text invites
        # typing into the release notes.
        self.notes.configure(state=tk.DISABLED)

        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100,
                                        value=0)
        self.progress.pack(fill=tk.X, pady=(10, 2), **pad)

        self.progress_text = tk.Label(self, bg=BG_DARK, fg=FG_DIM, anchor="w",
                                      justify="left", text="")
        self.progress_text.pack(fill=tk.X, **pad)

        self.status = tk.Label(self, bg=BG_DARK, fg=FG_TEXT, anchor="w",
                               justify="left", wraplength=520, text="")
        self.status.pack(fill=tk.X, pady=(2, 8), **pad)

        footer = tk.Frame(self, bg=BG_PANEL)
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        commands = {
            "install": self._on_install,
            "restart": self._on_restart,
            "release_page": self._on_release_page,
            "later": self._on_later,
            "cancel": self._on_cancel,
        }
        styles = {"install": "Green.TButton", "restart": "Green.TButton"}
        for key in BUTTON_KEYS:
            btn = ttk.Button(footer, text=_BUTTON_LABELS[key],
                             style=styles.get(key, "Dark.TButton"),
                             command=commands[key])
            btn.pack(side=tk.LEFT, padx=(8 if key == "install" else 6, 0),
                     pady=8)
            self.buttons[key] = btn

        try:
            self.minsize(560, 420)
        except tk.TclError:
            pass

    def _place_near_parent(self):
        """Offset from the main window so the dialog does not open under it.

        Deliberately no ``update_idletasks``/centring: the window is not laid
        out yet, and forcing a layout here would make every caller pay for a
        geometry pass on the Tk thread.
        """
        try:
            root = getattr(self._host, "root", None)
            if root is not None:
                self.geometry("+%d+%d" % (root.winfo_rootx() + 60,
                                          root.winfo_rooty() + 60))
        except (tk.TclError, AttributeError, TypeError):
            pass

    # ── the three entry points the host calls ───────────────────────────────

    def set_progress(self, done, total):
        """Paint download progress. Called on the Tk thread via ``_post_ui``.

        Total by design: no total yet (``0``/``None`` — a response with no
        ``Content-Length``), garbage, or a server that sends more than it
        promised all resolve to a sane bar rather than an exception inside the
        UI dispatcher.
        """
        pct = 0.0
        try:
            total_f = float(total)
            done_f = float(done)
            if total_f > 0:
                pct = _clamp(done_f / total_f * 100.0, 0.0, 100.0)
        except (TypeError, ValueError):
            pct = 0.0
            done_f = total_f = 0.0
        try:
            self.progress.configure(value=pct)
            # Displayed "done" is clamped to "total" too: a server that sends
            # more than it promised must not read as "57.2 MB of 53.0 MB".
            done_disp = _clamp(done_f, 0.0, total_f) if total_f > 0 else done_f
            self.progress_text.configure(
                text=("%.1f MB of %.1f MB (%d%%)"
                      % (done_disp / 1048576.0, total_f / 1048576.0, pct))
                if total_f > 0 else "")
        except (tk.TclError, AttributeError):
            pass                                   # the window is already gone

    def set_state(self, state, message=""):
        """Move to ``state`` and repaint the footer and the status line.

        An unrecognised state is ignored rather than applied: leaving the
        window where it was is always safer than blanking every button.
        """
        try:
            state = DialogState(state)
        except (ValueError, TypeError):
            log.debug("update dialog: ignoring unknown state %r", state)
            return
        self.dialog_state = state
        text = (message if isinstance(message, str) and message.strip()
                else _DEFAULT_STATUS.get(state, ""))
        try:
            self.status.configure(
                text=text,
                fg=FG_RED if state is DialogState.FAILED else FG_TEXT)
            for key, enabled in buttons_for(state).items():
                self.buttons[key].configure(
                    state=tk.NORMAL if enabled else tk.DISABLED)
            if state in (DialogState.AVAILABLE, DialogState.BLOCKED):
                self.progress.configure(value=0)
                self.progress_text.configure(text="")
        except (tk.TclError, AttributeError, KeyError):
            pass                                   # the window is already gone

    def on_result(self, result):
        """Consume one ``self_update.InstallResult``. Called via ``_post_ui``.

        ``stage`` is deliberately NOT interpreted: ``run_install`` already
        phrases every failure for a human, and a stage vocabulary that grows in
        ``self_update`` must never be able to break this window.
        """
        ok = False
        message = ""
        try:
            ok = bool(getattr(result, "ok", False))
            raw = getattr(result, "message", "")
            message = raw if isinstance(raw, str) else ""
        except Exception as exc:                  # pragma: no cover - defensive
            log.debug("update dialog: odd result %r (%s)", result, exc)
        self.set_state(DialogState.READY_TO_RESTART if ok
                       else DialogState.FAILED, message)

    # ── buttons ─────────────────────────────────────────────────────────────

    def _on_install(self):
        if self.dialog_state not in (DialogState.AVAILABLE, DialogState.FAILED):
            return
        self._cancel_sent = False
        # Move first: a host that calls back synchronously must find the
        # window already in the state its callbacks belong to.
        self.set_state(DialogState.DOWNLOADING)
        started = self._call_host("_start_update_install", self._info,
                                  self.set_progress, self.on_result)
        if not started:
            self.set_state(DialogState.AVAILABLE, self._refusal_reason())

    def _on_restart(self):
        self._call_host("_request_restart")

    def _on_release_page(self):
        self._call_host("_open_release_page", getattr(self._info, "url", ""))

    def _on_later(self):
        self.close()

    def _on_cancel(self):
        self._cancel_once()

    # ── closing ─────────────────────────────────────────────────────────────

    def close(self):
        """The WM_DELETE_WINDOW handler, the Escape binding and *Later*.

        Closing mid-install stops the worker (once); closing at rest just
        closes. Either way the window goes: an install that is already past the
        swap is finished on disk, and one that is not has been told to stop.
        """
        if self.dialog_state in BUSY_STATES:
            self._cancel_once()
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _cancel_once(self):
        if self._cancel_sent:
            return
        self._cancel_sent = True
        self._call_host("_cancel_update_install")

    # ── host plumbing ───────────────────────────────────────────────────────

    def _call_host(self, name, *args):
        """Call one host method, swallowing anything it throws.

        This runs inside a Tk callback: an exception here escapes into the Tk
        event loop, where it is printed and dropped, and the window is left in
        whatever half-painted state it reached. Logging it and carrying on is
        strictly better.
        """
        fn = getattr(self._host, name, None)
        if not callable(fn):
            log.debug("update dialog: host has no %s", name)
            return None
        try:
            return fn(*args)
        except Exception as exc:
            log.debug("update dialog: host %s failed: %s", name, exc)
            return None

    def _refusal_reason(self):
        """Why the host said no, if it says. Accepts a string or a callable."""
        reason = getattr(self._host, "_update_install_reason", None)
        if callable(reason):
            try:
                reason = reason()
            except Exception as exc:
                log.debug("update dialog: refusal reason failed: %s", exc)
                reason = None
        if isinstance(reason, str) and reason.strip():
            return reason
        return CANNOT_START_NOW


__all__ = [
    "BUSY_STATES", "BUTTON_KEYS", "CANNOT_START_NOW", "DialogState",
    "GENERIC_FAILURE", "NO_NOTES", "UpdateDialog", "buttons_for",
    "format_notes",
]
