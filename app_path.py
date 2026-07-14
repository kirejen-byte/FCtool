"""
Application path helper for PyInstaller compatibility.
When frozen as an EXE, __file__ points to a temp extraction dir.
Data files (config, caches) should live next to the EXE instead.
Bundled assets (sounds, templates) live in sys._MEIPASS.
"""

import os
import sys


_APP_DATA_SUBDIR = "FCTool"
_resolved_app_dir = None   # cached: the writability probe is wasteful per-call


def _is_dir_writable(path: str) -> bool:
    """True if we can create the dir (if needed) and write+remove a probe file."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".fctool_write_test")
        with open(probe, "w") as fh:
            fh.write("")
        os.remove(probe)
        return True
    except Exception:
        return False


def _user_data_dir() -> str:
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or os.path.expanduser("~"))
    return os.path.join(base, _APP_DATA_SUBDIR)


def app_dir() -> str:
    """Return the directory for the app's WRITABLE data (config, ESI tokens,
    caches, chat-monitor state).

    - Frozen exe in a WRITABLE folder (portable install): the folder next to the
      .exe — preserves the existing portable layout (no migration for current
      users).
    - Frozen exe in a READ-ONLY folder (e.g. C:\\Program Files): a per-user dir
      under %LOCALAPPDATA%\\FCTool, created on demand, so token/config saves do
      not fail with 'Permission denied'.
    - Running from source: the directory containing this module.

    Resolved once and cached (the location cannot change during a run, and a
    writability probe on every call would be wasteful)."""
    global _resolved_app_dir
    if _resolved_app_dir is not None:
        return _resolved_app_dir
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        if _is_dir_writable(exe_dir):
            _resolved_app_dir = exe_dir
        else:
            data = _user_data_dir()
            os.makedirs(data, exist_ok=True)
            _resolved_app_dir = data
    else:
        _resolved_app_dir = os.path.dirname(os.path.abspath(__file__))
    return _resolved_app_dir


def bundle_dir() -> str:
    """Return the directory where bundled read-only assets live.

    - Frozen (PyInstaller --onefile): sys._MEIPASS (temp extraction dir)
    - Normal Python: same as app_dir()
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resolve_data_file(name: str, prefer: str = "writable") -> str | None:
    """Resolve the on-disk path of a data file that ships bundled and/or lives
    next to the app's writable data, trying candidate locations in the order
    that fits the KIND of file. One shared implementation for the four
    call-sites that used to hand-roll (and silently diverge on) this lookup.

    ``prefer="writable"`` (default) — try :func:`app_dir` FIRST, then
        :func:`bundle_dir`. Use for data the user may legitimately override or
        that the app itself refreshes: caches, or generated tables a newer copy
        of which can be dropped next to the exe to shadow the shipped one (e.g.
        ``system_coords.json``, the star-map layout). A writable-dir copy WINS.

    ``prefer="bundle"`` — try :func:`bundle_dir` FIRST, then this module's own
        directory. Use for PRISTINE shipped tables that must always read the
        packaged copy and never be shadowed by a stray file in the writable dir
        (e.g. the SDE ``inv_groups``/``fit_types`` tables, overview starter
        tables). The bundled copy WINS; the module-dir entry is only a
        source-checkout fallback (unfrozen, ``bundle_dir()`` and this module's
        directory are the same folder).

    Returns the first candidate path that exists, or ``None`` when the file is
    found in none of the candidate locations (callers decide whether that is
    fatal — most degrade gracefully). Pure lookup: never creates or writes.
    """
    if prefer == "bundle":
        candidates = (
            os.path.join(bundle_dir(), name),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
        )
    else:  # "writable" (default)
        candidates = (
            os.path.join(app_dir(), name),
            os.path.join(bundle_dir(), name),
        )
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None
