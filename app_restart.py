"""Start FCTool again - a plain relaunch of the same program after a clean close.

This is NOT the self-updater. ``self_update.relaunch_and_watch`` starts a NEW build, watches
it boot and rolls the swap back when it dies; it owns the update marker and the ``.old`` /
``.new`` files. A plain restart starts the SAME program once and walks away: no marker, no
watchdog, nothing written. It exists for changes that are only read at start-up (a staged
group configuration, a colour palette).

Who calls it: ``fc_gui.main()``, after the main loop has returned from a normal close and
``logging.shutdown()`` has run - ``FCToolGUI._restart_app`` only sets a flag and closes the
window. The old process is still alive for a moment while the new one starts; whatever the
new one must not do before the old one is gone waits for it by itself (the group import
waits for the process that staged it).

``relaunch_argv`` - what to start, and in which folder:

* frozen (``sys.frozen``): the exe alone, in the exe's folder;
* from source: the interpreter plus the ABSOLUTE path of the script, in the script's folder.
  That path is worked out ONCE, when this module is imported (``SCRIPT``): ``FCTool.bat``
  starts ``python fc_gui.py``, and a relative name resolved at relaunch time would follow
  whatever the working folder is by then - to a file that is not there, started all the
  same. The argument list is handed over as a list, so a path with spaces is one argument.

``relaunch`` hands that to ``popen`` detached (``self_update.CREATION_FLAGS``), and a frozen
child gets ``self_update.clean_child_env()``: a onefile exe passes its extraction folder to
its children through ``_PYI_*`` variables, and a child started with them would run out of
THIS process's temporary folder, which is deleted when this process exits. A child started
from source inherits the environment unchanged.

It never raises: a restart that could not be started is ``False``, and the app has closed
normally either way. From source that includes a script that is no file (``python -c``
leaves ``-c`` as the script; a checkout can be moved while the app runs): the interpreter
would start, find nothing to run and exit, so nothing is started. A frozen build starts
its exe and is never asked for a script.

The module imports only the standard library at the top - fc_gui imports it at start-up;
``self_update`` is fetched when a relaunch actually happens.
"""

import os
import subprocess
import sys


def _text(value, what) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError("%s is not a path" % what)
    return value


def _script_at_import():
    """The absolute path of the script this process was started with (``sys.argv[0]``), or
    ``None`` when there is none to name. Never raises."""
    try:
        argv = sys.argv
        if not isinstance(argv, list) or not argv:
            return None
        return os.path.abspath(_text(argv[0], "the script"))
    except Exception:
        return None


#: The script to start again, absolute, as the working folder was when this module was
#: imported (fc_gui imports it at start-up). ``None``: this process has no script.
SCRIPT = _script_at_import()


def relaunch_argv(*, frozen=None, executable=None, script=None):
    """``(argv, cwd)`` that starts this program again.

    ``frozen`` defaults to ``sys.frozen``, ``executable`` to ``sys.executable``, ``script``
    to ``SCRIPT`` - the script of this process, resolved when the module was imported. A
    ``script`` that is handed in is made absolute now. ``cwd`` is always an absolute folder.
    Raises ``TypeError`` for a path that is no text; ``relaunch`` turns that into ``False``.
    """
    if frozen is None:
        frozen = getattr(sys, "frozen", False)
    program = _text(sys.executable if executable is None else executable, "the program")
    if frozen:
        return [program], os.path.dirname(os.path.abspath(program))
    if script is None:
        source = _text(SCRIPT, "the script")
    else:
        source = os.path.abspath(_text(script, "the script"))
    return [program, source], os.path.dirname(source)


def relaunch(*, popen=subprocess.Popen, frozen=None, executable=None, script=None) -> bool:
    """Start this program again; ``True`` when ``popen`` returned, ``False`` on any failure
    - and, from source, when the script (``SCRIPT``, or ``script``) is no file: ``popen`` is
    not called then. Never raises. Call it only after the window closed normally."""
    try:
        import self_update

        if frozen is None:
            frozen = getattr(sys, "frozen", False)
        frozen = bool(frozen)
        argv, cwd = relaunch_argv(frozen=frozen, executable=executable, script=script)
        if not frozen and not os.path.isfile(argv[1]):
            return False                # a command that cannot start is not a restart
        popen(argv, cwd=cwd, close_fds=True, creationflags=self_update.CREATION_FLAGS,
              env=self_update.clean_child_env() if frozen else None)
        return True
    except Exception:
        return False
