"""Group configuration: the boot hook.

``fc_gui.py`` runs it with three lines, right after its console-encoding block and before
its first application import::

    import group_boot
    if __name__ == "__main__":
        group_boot.run()

So ``run`` works before the app exists: no window, no logger, no store, no monitor - and no
module that has read a colour from ``ui_theme``. It does three things, in this order:

1. **Stale bundle.** A frozen process that inherited another build's extraction folder
   (``self_update.inherited_stale_meipass()``) is only a stub - ``main()`` starts a clean
   copy and that copy does the work. The stub touches nothing: palette ``"skipped"``.
2. **The staged import.** Only when the pending or the applying file is there:
   ``group_apply.apply_pending(folder)``. Everything that boots afterwards reads files that
   already hold the import. (One start can spend seconds here when another FCTool is still
   closing, or is applying the same import - ``apply_pending`` waits for it.)
3. **The palette.** ``palette.load(folder)`` and ``palette.install(ui_theme, roles)`` - also
   when step 2 failed or raised. ``FCTOOL_DEFAULT_PALETTE=1`` in the environment skips it
   (the way back from a palette someone cannot read). ``ui_theme`` is imported HERE and not
   earlier, so every later ``from ui_theme import X`` picks the palette up by itself.

``run`` NEVER raises and never shows anything: whatever goes wrong - a module that cannot be
imported, a broken file, a full disk, an ``apply_pending`` that raises although it promises
not to - the app still starts, on the stock colours if need be. Each failure is ONE line in
the result's ``log`` (ASCII, counts and type names only, never a name from a file); the app
writes those lines to its own log once it has one (``log_lines``).

The result, also kept as ``LAST``::

    {"report": <the report dict of apply_pending, or None>,
     "palette": "stock" | "custom" | "invalid" | "skipped",
     "log": [str, ...]}

``palette`` says which colours are in use: ``custom`` - a stored palette was installed;
``stock`` - there is none; ``invalid`` - there is one and it could not be used (stock colours
are in use); ``skipped`` - the palette step did not run (the switch, the stub, no folder).

A plain start does not pay for the feature. Importing this module imports nothing but ``os``
and does nothing - every test imports ``fc_gui``, and the hook must not run then. ``run``
fetches what a step needs when the step needs it: ``group_apply`` - and with it
``group_state``, the whole engine and every item - ONLY when one of the two protocol files
is there. That is why the two names are held here a second time (``_PENDING_FILE``,
``_APPLYING_FILE``): importing ``group_state`` for them cost 51-65 ms on every start.
``tests/test_group_boot.py`` pins both against ``group_state``. A plain start asks whether
the two files are there, asks for the palette file, and is done.

Files (owner rule A2): nothing in this module opens a file. ``palette.load`` reads the
palette file; ``apply_pending`` reaches the five state files, the three protocol files and
``backups``. No sign-in file, no character map, no cache.
"""

import os


#: The result of the one ``run`` of this process; ``None`` until the hook ran - and for good
#: wherever ``fc_gui`` is imported as a module instead of being the program.
LAST = None

#: Set to ``1`` to start on the stock colours whatever the stored palette holds.
PALETTE_ENV = "FCTOOL_DEFAULT_PALETTE"

#: Mirrors of ``group_state.PENDING_FILE`` / ``group_state.APPLYING_FILE`` (see the module
#: docstring; a test holds them equal). Looked for in THIS order - pending first: a claim
#: renames pending -> applying, so a start that misses the one finds the other.
_PENDING_FILE = "group_import_pending.json"
_APPLYING_FILE = "group_import_applying.json"
_STAGED = (_PENDING_FILE, _APPLYING_FILE)

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


def _run(app_dir, theme, environ, result) -> None:
    if _stale_bundle(result):                                           # step 1
        return
    root = _folder(app_dir, result)
    if root is None:
        return
    result["report"] = _apply(root, result)                             # step 2
    result["palette"] = _install_palette(root, theme, environ, result)  # step 3


def _stale_bundle(result) -> bool:
    """Is this the updater's inherited-mode stub? A check that fails is "no": a start that
    does nothing because a check broke would be worse than the stub doing the work."""
    try:
        import self_update

        return self_update.inherited_stale_meipass() is True
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
    try:
        if not any(_there(os.path.join(root, name)) for name in _STAGED):
            return None
        import group_apply

        report = group_apply.apply_pending(root)
    except Exception as exc:
        _note(result, "group import: %s" % type(exc).__name__)
        return None
    if report is None:
        return None
    if not isinstance(report, dict):
        _note(result, "group import: no usable report")
        return None
    for line in _clean_lines(report.get("log")):
        _note(result, line)
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


def _summary(report) -> str:
    """One line for the app's log: the outcome and counts - never a name from the file."""
    status = report.get("status")
    status = status if isinstance(status, str) and status.isascii() and status.isidentifier() \
        else "unknown"
    items = report.get("items")
    items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    failed = sum(1 for item in items if item.get("status") == "failed")
    return "group import: %s (%d items, %d failed)" % (status, len(items), failed)


def _install_palette(root, theme, environ, result) -> str:
    """Step 3 -> the palette status. Nothing is installed unless everything worked."""
    if _stock_palette_wanted(environ):
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


def _stock_palette_wanted(environ) -> bool:
    try:
        source = os.environ if environ is None else environ
        return source.get(PALETTE_ENV) == "1"
    except Exception:
        return False


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

