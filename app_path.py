"""
Application path helper for PyInstaller compatibility.
When frozen as an EXE, __file__ points to a temp extraction dir.
Data files (config, caches) should live next to the EXE instead.
Bundled assets (sounds, templates) live in sys._MEIPASS.
"""

import os
import sys
import time


_APP_DATA_SUBDIR = "FCTool"
_resolved_app_dir = None   # cached: the writability probe is wasteful per-call
_decision = None           # cached: HOW app_dir() chose (see app_dir_decision)
_last_probe_error = None   # text of the last write-probe failure, or None

#: A single failed probe is not proof of a read-only folder — see
#: :func:`_is_dir_writable`.
_PROBE_ATTEMPTS = 3
_PROBE_BACKOFF_S = 0.2

#: Indirection so tests can neutralise the backoff without touching `time`.
_sleep = time.sleep

#: Files that make a folder OURS. A folder holding either of these is a data
#: folder we (or a previous run) already wrote, whatever a probe says today.
_CONFIG_NAME = "config.json"
_TOKENS_PREFIX = "esi_tokens_"
_TOKENS_SUFFIX = ".json"
#: The pre-multi-character token file (``esi_auth.TOKEN_FILE``). Still the only
#: sign-in an install upgraded from an old version may have, so a folder holding
#: it is just as much OUR folder as one holding ``esi_tokens_<name>.json``.
_LEGACY_TOKENS_NAME = "esi_tokens.json"

#: Name prefix of the write-probe file (see :func:`_probe_once`). Everything
#: starting with it is ours and disposable — including v6.2.0's fixed-name
#: ``.fctool_write_test``, left behind whenever a crash beat ``os.remove``.
_PROBE_PREFIX = ".fctool_write_test"


def _has_user_data(path: str) -> bool:
    """True if ``path`` already holds this app's user data — ``config.json``,
    the legacy ``esi_tokens.json`` or any ``esi_tokens_*.json``. Never raises:
    an unreadable or missing directory is simply 'no data here'."""
    try:
        if os.path.isfile(os.path.join(path, _CONFIG_NAME)):
            return True
        with os.scandir(path) as entries:
            for entry in entries:
                name = entry.name
                if name == _LEGACY_TOKENS_NAME:
                    return True
                if (name.startswith(_TOKENS_PREFIX)
                        and name.endswith(_TOKENS_SUFFIX)):
                    return True
    except Exception:
        return False
    return False


def _sweep_stale_probes(path: str) -> None:
    """Best-effort removal of probe files an earlier run left behind (a crash
    or a kill between the write and the ``os.remove``, or v6.2.0's fixed-name
    probe). Purely cosmetic — the app works with them there — so every failure
    is swallowed: this must never turn a writable folder into a failed run."""
    try:
        mine = "%s.%d" % (_PROBE_PREFIX, os.getpid())
        with os.scandir(path) as entries:
            for entry in entries:
                name = entry.name
                if name == mine or not name.startswith(_PROBE_PREFIX):
                    continue
                try:
                    os.remove(os.path.join(path, name))
                except Exception:
                    pass
    except Exception:
        pass


def _probe_once(path: str) -> None:
    """One write probe: create the dir if needed, then write and remove a probe
    file. Raises on any failure — the caller decides whether one failure is
    decisive. The probe name carries this process's pid so a stale probe left
    by a crashed run can never collide with ours."""
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, "%s.%d" % (_PROBE_PREFIX, os.getpid()))
    with open(probe, "w") as fh:
        fh.write("")
    os.remove(probe)


def _is_dir_writable(path: str, attempts: int = _PROBE_ATTEMPTS,
                     sleep=None) -> bool:
    """True if we can create the dir (if needed) and write+remove a probe file.

    RETRIED, because one failure is not proof of a read-only folder: Windows
    Controlled Folder Access sizing up a brand-new unrecognised exe, an AV
    first-run scan still holding the probe file when ``os.remove`` runs, or a
    OneDrive lock all fail once and succeed a moment later — and treating that
    as "read-only" used to strand the user's whole data set (see
    :func:`app_dir`). Returns True on the FIRST success; the text of the last
    failure is kept in ``_last_probe_error`` for the startup log line.
    """
    global _last_probe_error
    _last_probe_error = None
    if sleep is None:
        sleep = _sleep
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        try:
            _probe_once(path)
            _sweep_stale_probes(path)
            return True
        except Exception as exc:
            _last_probe_error = "%s: %s" % (type(exc).__name__, exc)
        if attempt + 1 < attempts:
            try:
                sleep(_PROBE_BACKOFF_S)
            except Exception:
                pass
    return False


def _user_data_dir() -> str:
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or os.path.expanduser("~"))
    return os.path.join(base, _APP_DATA_SUBDIR)


def app_dir() -> str:
    """Return the directory for the app's WRITABLE data (config, ESI tokens,
    caches, chat-monitor state).

    Frozen exe, in order — **data wins wherever it lives**, and only a pair of
    empty folders is settled by a write probe:

    1. **The exe's folder when it already holds our data** (``config.json``,
       ``esi_tokens.json`` or any ``esi_tokens_*.json``) — no conditions. A
       folder holding our data IS our folder; a save that later fails surfaces
       through the normal save-failure path instead of silently forking the data
       set onto a second, empty copy. (Field bug, v6.2.0: one failed probe sent
       users to an empty ``%LOCALAPPDATA%\\FCTool`` — default config, no tokens,
       and the log that could have explained it written there too.)
    2. **The ``%LOCALAPPDATA%\\FCTool`` fallback when IT holds our data** and the
       exe's folder holds none. This is the same field bug through the other
       door: a first run that fell back (probe blocked) and then signed in has
       the user's ONLY config and tokens down there, so a probe that succeeds
       today must not send them to the empty exe folder and lose the lot a
       second time.

       Both data rules still RUN the retried probe on the folder they picked,
       purely to report it (``writable``/``probe_error``): the probe can no
       longer move us, but a folder that is PERMANENTLY blocked — Controlled
       Folder Access until the user allows the exe — reads the tokens fine and
       then fails every save with nothing in any log to say so, because
       ``fctool.log`` lives in that same folder.
    3. **The exe's folder when a RETRIED write probe succeeds** (portable
       install) — both folders are empty, so this is a fresh install and the
       portable layout is preserved with no migration.
    4. **Otherwise** a per-user dir under ``%LOCALAPPDATA%\\FCTool``, created on
       demand, so a genuinely read-only install (e.g. ``C:\\Program Files``)
       still saves tokens and config.

    When BOTH folders hold data the exe's folder wins (rule 1) and
    ``fallback_has_data`` flags the second, ignored set for the startup log.

    Running from source: the directory containing this module.

    Resolved once and cached (the location cannot change during a run, and a
    writability probe on every call would be wasteful). How it chose is
    available from :func:`app_dir_decision`."""
    global _resolved_app_dir, _decision
    if _resolved_app_dir is not None:
        return _resolved_app_dir
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        data = _user_data_dir()
        if _has_user_data(exe_dir):
            # Both may hold data; the exe dir wins and the flag names the
            # second set so the startup log can warn about it. The probe runs
            # anyway — it cannot change the choice, only REPORT it: a folder
            # that is permanently blocked (Controlled Folder Access) reads the
            # tokens fine and fails every save silently otherwise.
            _resolved_app_dir = exe_dir
            # Probe FIRST, into a local: `_last_probe_error` is only meaningful
            # once the probe has run.
            writable = _is_dir_writable(exe_dir)
            _decision = _make_decision(exe_dir, "exe-dir-has-data", exe_dir,
                                       data,
                                       probe_error=_last_probe_error,
                                       fallback_has_data=_has_user_data(data),
                                       writable=writable)
        elif _has_user_data(data):
            # The exe dir is empty and the fallback is not: this user's data
            # lives down there. The probe cannot move us — same report-only
            # role as above.
            _resolved_app_dir = data
            writable = _is_dir_writable(data)
            _decision = _make_decision(data, "fallback-has-data", exe_dir,
                                       data, probe_error=_last_probe_error,
                                       fallback_has_data=True,
                                       writable=writable)
        elif _is_dir_writable(exe_dir):
            _resolved_app_dir = exe_dir
            _decision = _make_decision(exe_dir, "exe-dir-writable", exe_dir,
                                       data, probe_error=_last_probe_error,
                                       fallback_has_data=False, writable=True)
        else:
            try:
                os.makedirs(data, exist_ok=True)
            except Exception:
                pass
            _resolved_app_dir = data
            # `writable` describes the CHOSEN dir and the fallback was never
            # probed, so it stays True: the failure that matters here is the
            # exe dir's, and it already has its own warning.
            _decision = _make_decision(data, "exe-dir-read-only", exe_dir,
                                       data, probe_error=_last_probe_error,
                                       fallback_has_data=False, writable=True)
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        _resolved_app_dir = here
        _decision = _make_decision(here, "source", "", "", probe_error=None,
                                   fallback_has_data=False, writable=True)
    return _resolved_app_dir


def _make_decision(path: str, reason: str, exe_dir: str, fallback_dir: str,
                   probe_error, fallback_has_data: bool,
                   writable: bool = True) -> dict:
    return {
        "dir": path,
        "reason": reason,
        "probe_error": probe_error,
        "fallback_has_data": bool(fallback_has_data),
        "exe_dir": exe_dir,
        "fallback_dir": fallback_dir,
        "writable": bool(writable),
    }


def app_dir_decision() -> dict:
    """Return a COPY of how :func:`app_dir` chose its folder, resolving it if
    that has not happened yet. Keys:

    - ``dir``: the chosen directory (identical to ``app_dir()``).
    - ``reason``: ``"exe-dir-has-data"`` | ``"fallback-has-data"`` |
      ``"exe-dir-writable"`` | ``"exe-dir-read-only"`` | ``"source"``.
    - ``probe_error``: text of the last write-probe failure, or None. Set on a
      read-only verdict, on a RECOVERED failure (the retry succeeded — a near
      miss worth having in the log), and on a data-branch folder that failed its
      report-only probe.
    - ``writable``: the CHOSEN folder passed its retried write probe. False only
      where the chosen folder was probed and failed — i.e. a data branch whose
      folder we can read but not write (the app runs, nothing ever saves). True
      for ``"source"`` and for ``"exe-dir-read-only"``, where the chosen folder
      was never probed (there the exe dir's failure is the one that matters, and
      it has ``probe_error`` and its own warning).
    - ``fallback_has_data``: the ``%LOCALAPPDATA%`` dir holds config/tokens.
      True for ``"fallback-has-data"`` (where it MADE the choice) and for an
      ``"exe-dir-has-data"`` run where both folders hold a set — there the exe
      dir wins and this flags the second, ignored copy for the log.
    - ``exe_dir``: the exe's folder (empty when running from source).
    - ``fallback_dir``: the ``%LOCALAPPDATA%\\FCTool`` path, chosen or not
      (empty when running from source), so the log line can name both folders.

    A copy, so a caller that stashes it cannot mutate the module's record.
    Nothing here logs — this module runs at import, before logging exists; the
    one caller (fc_gui, right after ``get_logger``) writes the line."""
    if _resolved_app_dir is None:
        app_dir()
    if _decision is None:
        # The dir was resolved without a decision being recorded (a test reset,
        # or a future caller that sets the cache directly). Never hand back an
        # empty dict — the log line would read `dir=None` and name no folder at
        # all, which is exactly the blindness this record exists to cure.
        return _make_decision(_resolved_app_dir or "", "unknown", "", "",
                              probe_error=None, fallback_has_data=False)
    return dict(_decision)


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
