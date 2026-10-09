"""Group configuration: the boot hook.

``fc_gui.py`` runs it with three lines, right after its console-encoding block and before
its first application import::

    import group_boot
    if __name__ == "__main__":
        group_boot.run()

So ``run`` works before the app exists: no window, no store, no monitor - and no module
that has read a colour from ``ui_theme``. It does three things, in this order:

1. **Stale bundle.** A frozen process that inherited another build's extraction folder
   (``self_update.inherited_stale_meipass()``) is only a stub - ``main()`` starts a clean
   copy and that copy does the work. The stub touches nothing: palette ``"skipped"``, and
   one log line that says so. NOT when ``FCTOOL_REEXEC=1`` is set: then
   ``self_update.reexec_if_inherited()`` starts nothing more and ``main()`` goes on as the
   app in this very process - there is no clean copy to leave the work to, so it is done
   here (one log line as well). One case is known and accepted: a stub whose clean
   re-exec fails to spawn carries on as the app with the hook skipped - stock colours for
   that session, a staged import kept and applied at the next start.
2. **The staged import.** Only when the pending or the applying file is there:
   ``group_apply.apply_pending(folder)``. Everything that boots afterwards reads files that
   already hold the import. (One start can spend seconds here when another FCTool is still
   closing, or is applying the same import - ``apply_pending`` waits for it.)

   One process is never waited for, and no staged work is begun while it lives: the
   updater's watcher. After an update the OLD build's process stays alive until this build reports
   that it booted - which it does seconds after its window is up, long after this hook.
   Its leftover threads can still save settings, over whatever is applied now; and work
   that process staged itself would make this start sit for ``apply_pending``'s whole wait
   before any window and then be told "another FCTool window is still open". So when the
   update marker is this start's own (below), the hook looks at that process ONCE, where
   ``apply_pending`` asks whether the process that staged the work is gone - whoever that
   was: still alive -> ``deferred`` at once, with ``MSG_AFTER_UPDATE`` as the message and
   a log line; the work stays staged for the next start. Gone -> the apply goes on as on
   any other start, its own wait for the process that staged the work included. (That
   question is the only place the hook has a say: a run that was interrupted and left its
   applying file behind is finished or undone by ``apply_pending`` before it is asked.)

   The marker is believed only when it says ``launched``, names another process, names the
   version that is running (compared the way the updater compares it) and was stamped
   within the updater's watch plus ``_MARKER_MARGIN_S``. Anything else is a leftover or
   somebody else's - an update to another version, a marker that is days old and whose pid
   belongs to an unrelated process by now - and this start is an ordinary one.
3. **The palette.** ``palette.load(folder)`` and ``palette.install(ui_theme, roles)`` - also
   when step 2 failed or raised. ``FCTOOL_DEFAULT_PALETTE=1`` in the environment skips it
   (the way back from a palette someone cannot read; one log line). ``ui_theme`` is imported
   HERE and not earlier, so every later ``from ui_theme import X`` picks the palette up by
   itself.

``run`` NEVER raises and never shows anything: whatever goes wrong - a module that cannot be
imported, a broken file, a full disk, an ``apply_pending`` that raises although it promises
not to - the app still starts, on the stock colours if need be. Each failure, and each step
that was skipped, is ONE line in the result's ``log`` (ASCII, counts, type names and module
names only, never a name from a file). ``run`` itself writes to no logger; the app writes
those lines to its own log once its window is being built (``log_lines``).

The result, also kept as ``LAST``::

    {"report": <the report dict of apply_pending, or None>,
     "palette": "stock" | "custom" | "invalid" | "skipped",
     "log": [str, ...]}

``palette`` says which colours are in use: ``custom`` - a stored palette was installed;
``stock`` - there is none; ``invalid`` - there is one and it could not be used (stock colours
are in use); ``skipped`` - the palette step did not run (the switch, the stub, no folder).

A plain start does not pay for the feature. Importing this module imports nothing but ``os``
and ``sys`` and does nothing - every test imports ``fc_gui``, and the hook must not run
then. ``run`` fetches what a step needs when the step needs it: ``group_apply`` - and with
it ``group_state``, the whole engine and every item - ONLY when one of the two protocol
files is there. That is why the two names are held here a second time (``_PENDING_FILE``,
``_APPLYING_FILE``): importing ``group_state`` for them cost 51-65 ms on every start.
``tests/test_group_boot.py`` pins both against ``group_state``. A plain start asks whether
the two files are there, asks for the palette file, and is done.

Files (owner rule A2): this module opens no file itself. What its steps open:
``palette.load`` reads the palette file; ``apply_pending`` reaches the five state files, the
three protocol files and ``backups``; and - only in a frozen build, only when something is
staged - ``self_update.read_marker`` reads the updater's marker. One more file is opened on
the way, by nobody's intent: ``import self_update`` (step 1) pulls in ``update_check``,
whose module-level logger sets logging up and opens the app's log. The app's first import
would do the same a moment later. No sign-in file, no character map, no cache.
"""

import os
import sys


#: The result of the one ``run`` of this process; ``None`` until the hook ran - and for good
#: wherever ``fc_gui`` is imported as a module instead of being the program.
LAST = None

#: Set to ``1`` to start on the stock colours whatever the stored palette holds.
PALETTE_ENV = "FCTOOL_DEFAULT_PALETTE"

#: What the report says when the start that follows an update leaves the staged work for
#: the next start (module docstring, step 2): for an import, for a restore.
MSG_AFTER_UPDATE = ("FCTool was just updated. Your group import is applied the next time "
                    "FCTool starts.",
                    "FCTool was just updated. Your previous settings are restored the next "
                    "time FCTool starts.")

#: Mirrors of ``group_state.PENDING_FILE`` / ``group_state.APPLYING_FILE`` (see the module
#: docstring; a test holds them equal). Looked for in THIS order - pending first: a claim
#: renames pending -> applying, so a start that misses the one finds the other.
_PENDING_FILE = "group_import_pending.json"
_APPLYING_FILE = "group_import_applying.json"
_STAGED = (_PENDING_FILE, _APPLYING_FILE)

#: How long past the updater's watch (``self_update.RELAUNCH_WATCH_S``) a ``launched``
#: marker is still taken for this start's own. Generous: a cold start of a new build takes
#: half a minute; after that the pid in it may be anybody's.
_MARKER_MARGIN_S = 600.0

_MAX_MODULES = 8        # _note_missing_items: how many module names its one line carries
_MAX_NAME = 40          # ... and how long one of them may be

_TAKEN = False          # take_report: the report of LAST was handed out


def run(app_dir=None, *, theme=None, environ=None) -> dict:
    """Apply a staged import, then install the palette (the module docstring has the steps).

    ``app_dir`` defaults to ``app_path.app_dir()``, ``theme`` to the ``ui_theme`` module,
    ``environ`` to ``os.environ``. Returns the result dict and keeps it as ``LAST``.
    Never raises.
    """
    global LAST, _TAKEN
    result = {"report": None, "palette": "skipped", "log": []}
    try:
        _run(app_dir, theme, environ, result)
    except Exception as exc:            # nothing below should get here; the app starts anyway
        _note(result, "group boot: %s" % type(exc).__name__)
    LAST, _TAKEN = result, False
    return result


def _note(result, line) -> None:
    try:
        result["log"].append(line)
    except Exception:
        pass


def _env(environ, name):
    """``environ[name]`` (``os.environ`` when ``environ`` is ``None``), or ``None`` - also
    for an ``environ`` that is no mapping."""
    try:
        source = os.environ if environ is None else environ
        return source.get(name)
    except Exception:
        return None


def _run(app_dir, theme, environ, result) -> None:
    if _stale_bundle(environ, result):                                  # step 1
        return
    root = _folder(app_dir, result)
    if root is None:
        return
    result["report"] = _apply(root, result)                             # step 2
    result["palette"] = _install_palette(root, theme, environ, result)  # step 3


def _stale_bundle(environ, result) -> bool:
    """Is this the updater's inherited-mode stub, about to hand over to a clean copy? A
    check that fails is "no": a start that does nothing because a check broke would be
    worse than the stub doing the work. "No" as well for the process that was itself
    started by such a stub (``FCTOOL_REEXEC=1``) and still runs out of a stale bundle:
    nothing is started after it, it IS the app - one log line, because it does the work
    out of a doomed folder. A healthy process that merely inherited the variable (the
    updater's ``clean_child_env`` hands it on to every descendant) says nothing.

    The stub's own line is kept as well, but a genuine stub leaves in ``main()`` before
    anything writes these lines to the app's log. The line gets there in ONE case: the
    stub's clean re-exec failed to spawn, so the stub carries on as the app with the
    hook skipped (module docstring, step 1)."""
    try:
        import self_update

        if self_update.inherited_stale_meipass() is not True:
            return False
        if _env(environ, self_update.REEXEC_ENV) == "1":
            _note(result, "group boot: stale bundle, but no clean copy follows this process "
                          "- doing the start work")
            return False
        _note(result, "group boot: skipped - this process only starts a clean copy of FCTool")
        return True
    except Exception as exc:
        _note(result, "group boot: stale-bundle check failed (%s)" % type(exc).__name__)
        return False


def _folder(app_dir, result):
    """The data folder as text, or ``None`` (one log line) when there is none to work in.
    An empty name is refused: it would mean "whatever folder the process happens to be in"."""
    try:
        if app_dir is None:
            import app_path

            app_dir = app_path.app_dir()
        root = os.fspath(app_dir)
        if not isinstance(root, str):
            raise TypeError("not text")
        if not root.strip() or "\0" in root:
            raise ValueError("not a folder name")
        return root
    except Exception as exc:
        _note(result, "group boot: no data folder (%s)" % type(exc).__name__)
        return None


def _apply(root, result):
    """Step 2 -> the report, or ``None`` when nothing was staged or nothing usable came
    back. Nothing of the feature is imported unless one of the two files is there."""
    met = []                            # _watcher_wait: the updater's watcher is still alive
    try:
        if not any(_there(os.path.join(root, name)) for name in _STAGED):
            return None
        import group_apply

        wait = _wait_after_update(result, met)
        if wait is None:
            report = group_apply.apply_pending(root)
        else:
            report = group_apply.apply_pending(root, wait=wait)
    except Exception as exc:
        _note(result, "group import: %s" % type(exc).__name__)
        return None
    _note_missing_items(result)
    if report is None:
        return None
    if not isinstance(report, dict):
        _note(result, "group import: no usable report")
        return None
    for line in _clean_lines(report.get("log")):
        _note(result, line)
    if met and report.get("status") == "deferred":
        restore = report.get("kind") == "restore"
        report = dict(report, message=MSG_AFTER_UPDATE[1 if restore else 0])
        _note(result, "%s FCTool was just updated - applied at the next start"
              % _prefix(report))
    _note(result, _summary(report))
    return report


def _there(path) -> bool:
    """Is something at ``path``? Only "no such file" is a no: a look that is refused (a
    scanner holding the folder for a moment) must not make a staged import invisible -
    ``apply_pending`` reads properly and decides."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def _prefix(report) -> str:
    """How a log line about this report begins: by what was staged."""
    return "group restore:" if report.get("kind") == "restore" else "group import:"


def _summary(report) -> str:
    """One line for the app's log: the outcome and counts - never a name from the file."""
    status = report.get("status")
    status = status if isinstance(status, str) and status.isascii() and status.isidentifier() \
        else "unknown"
    items = report.get("items")
    items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    failed = sum(1 for item in items if item.get("status") == "failed")
    return "%s %s (%d items, %d failed)" % (_prefix(report), status, len(items), failed)


def _note_missing_items(result) -> None:
    """One log line when item modules of this build failed to load (a broken frozen build).
    The engine REFUSES to apply on such a build: the staged import ends ``failed``, it is
    cleared, nothing is changed, and the report's sentence is
    ``group_apply.MSG_INCOMPLETE``. This line is the log's half of that: it names the
    missing modules by their code names. ``group_items`` is only looked up - a start never
    imports the engine to ask this."""
    try:
        failures = getattr(sys.modules.get("group_items"), "LOAD_FAILURES", None)
        if not isinstance(failures, (list, tuple)):
            return
        names = set()
        for entry in failures:
            name = entry[0] if isinstance(entry, (list, tuple)) and entry else None
            if isinstance(name, str) and name.isascii() and name.isidentifier() \
                    and len(name) <= _MAX_NAME:
                names.add(name)             # a module's code name, nothing out of a file
        if not names:
            return
        names = sorted(names)
        more = len(names) - _MAX_MODULES
        _note(result, "group import: this build is missing item modules: %s%s"
              % (", ".join(names[:_MAX_MODULES]), " (+%d more)" % more if more > 0 else ""))
    except Exception:
        pass


# -- the start that follows an update (module docstring, step 2) ---------------

def _staging_dir() -> str:
    """Where the updater keeps its marker: ``<exe folder>/updates`` for a frozen build, else
    ``""`` - from source there is no updater. The rule of ``FCToolGUI._update_staging_dir``;
    a test holds the two together."""
    if not getattr(sys, "frozen", False):
        return ""
    return os.path.join(os.path.dirname(sys.executable), "updates")


def _update_watcher(result):
    """The pid of the updater's watcher - the OLD build's process, alive until this build
    reports that it booted - or ``None`` when this start does not follow an update: no
    marker, a marker in another state than ``launched``, one that names no other process,
    one for another version than the running one, or one that is not fresh (module
    docstring, step 2). A marker that cannot be looked for is ``None`` and one log line."""
    try:
        staging = _staging_dir()
        if not staging:
            return None
        import self_update

        marker = self_update.read_marker(self_update.marker_path(staging))
        if marker is None or marker.state != "launched":
            return None
        pid = marker.old_pid
        if type(pid) is not int or pid <= 0 or pid == os.getpid():
            return None
        import app_version              # loaded already: self_update imports it

        # The updater's own comparison (mark_booted, startup_housekeeping): "v1.2" is 1.2.0.
        if self_update._same_version(marker.expected_tag, app_version.APP_VERSION) is not True:
            return None
        return pid if _fresh(marker.ts, self_update.RELAUNCH_WATCH_S) else None
    except Exception as exc:
        _note(result, "group boot: update marker not read (%s)" % type(exc).__name__)
        return None


def _fresh(stamp, watch_s) -> bool:
    """Was the marker stamped (``time.time()``) while its watcher can still be watching -
    within ``watch_s`` plus ``_MARKER_MARGIN_S`` before now? A stamp that is no number, not
    finite, or in the future is not fresh."""
    import time

    if type(stamp) not in (int, float) or type(watch_s) not in (int, float):
        return False
    age = time.time() - stamp
    return 0.0 <= age <= watch_s + _MARKER_MARGIN_S    # False for NaN and both infinities


def _wait_after_update(result, met):
    """The ``wait`` to hand to ``apply_pending`` at the start that follows an update, or
    ``None`` - then ``apply_pending`` is called exactly as on any other start."""
    watcher = _update_watcher(result)
    if watcher is None:
        return None
    # group_apply is loaded by now, and group_state with it: looked up, not imported.
    wait_for_exit = getattr(sys.modules.get("group_state"), "wait_for_exit", None)
    if not callable(wait_for_exit):
        return None
    return _watcher_wait(watcher, wait_for_exit, met)


def _watcher_wait(watcher, wait_for_exit, met):
    """``wait(pid, started) -> bool`` ("the process that staged the work is gone") for
    ``apply_pending``. Whoever staged it, ``watcher`` gets ONE look and no waiting first:
    alive is "not gone" at once, and its pid is appended to ``met`` - no staged work is
    begun while the old build lives. Only when the watcher is gone is the question the engine
    asked answered, by the engine's own ``wait_for_exit`` (not asked again for the watcher
    itself: gone is gone).

    The look is at the pid alone: the marker records no start time for it. That is what
    ``_fresh`` is for - a pid is only believed for minutes."""
    def wait(pid, started=None):
        if not wait_for_exit(watcher, None, 0):
            met.append(watcher)
            return False
        if type(pid) is int and pid == watcher:
            return True
        return wait_for_exit(pid, started)

    return wait


def _install_palette(root, theme, environ, result) -> str:
    """Step 3 -> the palette status. Nothing is installed unless everything worked."""
    if _env(environ, PALETTE_ENV) == "1":
        _note(result, "group boot: stock colours - %s is set" % PALETTE_ENV)
        return "skipped"
    try:
        import palette

        roles, status = palette.load(root)
        if status == "stock" and roles is None:
            return "stock"
        if status != "custom" or roles is None:
            _note(result, "group boot: the stored palette is not usable - stock colours")
            return "invalid"
        if theme is None:
            import ui_theme as theme    # here, and never earlier (module docstring, step 3)
        if not palette.install(theme, roles):
            _note(result, "group boot: the stored palette was refused - stock colours")
            return "invalid"
        return "custom"
    except Exception as exc:
        _note(result, "group boot: palette not installed (%s)" % type(exc).__name__)
        return "invalid"


def _clean_lines(lines) -> list:
    """Only what may go to the log: one-line ASCII text."""
    if not isinstance(lines, (list, tuple)):
        return []
    return [line for line in lines
            if isinstance(line, str) and line and line.isascii() and line.isprintable()]


# -- what the app reads afterwards ---------------------------------------------

def boot_flag(name) -> bool:
    """Did the import applied at THIS start raise the boot flag ``name``
    (``coalitions_seeded``, ``motd_disarm``)? True only for a report whose status is
    ``applied`` and whose flag is ``True``. Never raises."""
    try:
        report = LAST["report"]
        return report["status"] == "applied" and report["flags"][name] is True
    except Exception:
        return False


def take_report():
    """The report of this start's ``run`` - handed out ONCE, then ``None`` (the window that
    shows it is opened once). The boot flags stay readable for the rest of the process.
    Never raises."""
    global _TAKEN
    try:
        report = LAST["report"]
    except Exception:
        return None
    if _TAKEN or not isinstance(report, dict):
        return None
    _TAKEN = True
    return report


def log_lines() -> list:
    """The lines ``run`` kept for the app's log, for the app to write once it has a logger."""
    try:
        return _clean_lines(LAST["log"])
    except Exception:
        return []
