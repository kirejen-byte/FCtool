r"""Runtime facts about a Wine/Proton host, read from inside the PE process.

Verified facts this module rests on (2026-09-12, do not re-derive):
- Wine's ntdll exports `wine_get_version` (cdecl, `const char*`),
  `wine_get_build_id` and `wine_get_host_version(const char **sysname,
  const char **release)`; real Windows exports none of them, and a ctypes
  attribute lookup on a WinDLL IS GetProcAddress (0 -> AttributeError).
- `kernel32.wine_get_unix_file_name(LPCWSTR) -> LPSTR` returns a HeapAlloc'd
  string that must be released with `HeapFree(GetProcessHeap(), 0, p)`.
- `user32.GetPropW(hwnd, "__wine_x11_whole_window")` yields the top-level X
  window id of any window in the prefix (wineserver-global property).
- Wine passes `DISPLAY`/`XAUTHORITY` through verbatim but renames `HOME`,
  `PATH` and `XDG_*` to `WINE_HOST_*` and hides `WAYLAND_DISPLAY`.
- A Unix path can be probed from the PE side through either spelling:
  `\\?\unix<path>` or `Z:<path with backslashes>`; on real Windows neither
  exists, so every probe here is dead code off Wine.

Design rules: every ctypes touch happens lazily INSIDE a function (importing
this module on Linux or at PyInstaller boot must load no DLL), every entry
point is injectable for tests, and every function is total - it returns
None/False/[] instead of raising. All strings are ASCII (cp1252 log trap).
"""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass, field
from typing import Optional, Tuple

#: Ordered Unix interpreters the helper may run under (Steam runtime first).
PYTHON_CANDIDATES = (
    "/usr/bin/python3",
    "/usr/bin/python3.13",
    "/usr/bin/python3.12",
    "/usr/bin/python3.11",
    "/usr/bin/python3.10",
    "/usr/bin/python3.9",
    "/usr/local/bin/python3",
    "/bin/python3",
)

#: The wineserver-global window property carrying the X11 window id.
X_WINDOW_PROP = "__wine_x11_whole_window"

LAUNCH_KINDS = ("protontricks-bwrap", "protontricks-host", "proton", "umu",
                "wine", "none")

_IS_WINE = None  # type: Optional[bool]


def reset_cache() -> None:
    """Forget the cached ``is_wine()`` answer (test hook)."""
    global _IS_WINE
    _IS_WINE = None


# --- ctypes plumbing (lazy, injectable, total) ------------------------------

def _load(name):
    """Load a system DLL, or return None on any failure (incl. off-Windows)."""
    try:
        return ctypes.WinDLL(name)
    except Exception:
        return None


def _export(dll, name):
    """GetProcAddress-equivalent: the function object, or None when absent."""
    if dll is None:
        return None
    try:
        return getattr(dll, name)
    except Exception:
        return None


def _ascii(value, default="-"):
    """Collapse any value to an ASCII-safe one-line string for logs."""
    if value is None:
        return default
    try:
        text = str(value)
    except Exception:
        return default
    text = text.encode("ascii", "replace").decode("ascii")
    return text.replace("\r", " ").replace("\n", " ").strip() or default


# --- detection --------------------------------------------------------------

def is_wine(ntdll=None) -> bool:
    """True iff this process runs under Wine/Proton.

    Decided by whether ntdll exports ``wine_get_version``.  The answer is
    cached for the process (the host cannot change mid-run); tests inject a
    fake ``ntdll`` and clear the cache with :func:`reset_cache`.  A transient
    loader failure is cached the same way, as False - call
    :func:`reset_cache` to force a re-probe.
    """
    global _IS_WINE
    if _IS_WINE is None:
        dll = ntdll if ntdll is not None else _load("ntdll")
        _IS_WINE = _export(dll, "wine_get_version") is not None
    return _IS_WINE


def wine_version(ntdll=None) -> Optional[str]:
    """Wine's version string (e.g. ``"11.17"``), or None off Wine."""
    fn = _export(ntdll if ntdll is not None else _load("ntdll"),
                 "wine_get_version")
    if fn is None:
        return None
    try:
        fn.argtypes = []
        fn.restype = ctypes.c_char_p
        raw = fn()
    except Exception:
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        return raw.decode("ascii", "replace")
    return _ascii(raw, default=None)


def wine_host(ntdll=None) -> Optional[Tuple[str, str]]:
    """``(sysname, release)`` of the Unix host, or None off Wine."""
    fn = _export(ntdll if ntdll is not None else _load("ntdll"),
                 "wine_get_host_version")
    if fn is None:
        return None
    try:
        sysname = ctypes.c_char_p()
        release = ctypes.c_char_p()
        fn.argtypes = [ctypes.POINTER(ctypes.c_char_p),
                       ctypes.POINTER(ctypes.c_char_p)]
        fn.restype = None
        fn(ctypes.pointer(sysname), ctypes.pointer(release))
    except Exception:
        return None
    if not sysname.value or not release.value:
        return None
    return (sysname.value.decode("ascii", "replace"),
            release.value.decode("ascii", "replace"))


def launch_kind(environ=None) -> str:
    """Classify how this prefix was launched.

    Decided in order from the Windows-side environment Wine exposes:
    ``PROTONTRICKS_STEAM_RUNTIME`` (``bwrap`` -> ``protontricks-bwrap``,
    ``legacy``/``off`` -> ``protontricks-host``; an unknown value falls back
    on ``PROTONTRICKS_INSIDE_STEAM_RUNTIME``), then ``STEAM_COMPAT_DATA_PATH``
    (``proton``), then ``UMU_ID``/``GAMEID`` with ``PROTONPATH`` (``umu``),
    then ``WINEPREFIX`` (``wine``), else ``none``.  Blank values count as
    absent.  Never raises; returns one of :data:`LAUNCH_KINDS`.
    """
    try:
        env = os.environ if environ is None else environ

        def get(key):
            try:
                return str(env.get(key, "") or "").strip()
            except Exception:
                return ""

        runtime = get("PROTONTRICKS_STEAM_RUNTIME").lower()
        inside = get("PROTONTRICKS_INSIDE_STEAM_RUNTIME")
        if runtime:
            if runtime == "bwrap":
                return "protontricks-bwrap"
            if runtime in ("legacy", "off"):
                return "protontricks-host"
            return "protontricks-bwrap" if inside else "protontricks-host"
        if inside:
            return "protontricks-bwrap"
        if get("STEAM_COMPAT_DATA_PATH"):
            return "proton"
        if (get("UMU_ID") or get("GAMEID")) and get("PROTONPATH"):
            return "umu"
        if get("WINEPREFIX"):
            return "wine"
    except Exception:
        return "none"
    return "none"


# --- path + window translation ---------------------------------------------

def _deref_bytes(value):
    """Bytes behind a wine_get_unix_file_name result (fake- and ctypes-safe)."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, int) and value:
        try:
            return ctypes.string_at(value)
        except Exception:
            return None
    return None


def unix_path(win_path: str, kernel32=None) -> Optional[str]:
    """Translate a Windows path to its Unix spelling, or None off Wine.

    The result buffer is HeapAlloc'd by Wine and is freed here exactly once.
    """
    if not win_path:
        return None
    k32 = kernel32 if kernel32 is not None else _load("kernel32")
    fn = _export(k32, "wine_get_unix_file_name")
    if fn is None:
        return None
    raw = None
    try:
        fn.argtypes = [ctypes.c_wchar_p]
        fn.restype = ctypes.c_void_p
        raw = fn(win_path)
        data = _deref_bytes(raw)
    except Exception:
        return None
    finally:
        if raw:
            _heap_free(k32, raw)
    if not data:
        return None
    return data.decode("utf-8", "replace") or None


def _heap_free(k32, ptr) -> None:
    """Release a Wine-allocated buffer; failures are swallowed by design."""
    try:
        get_heap = _export(k32, "GetProcessHeap")
        free = _export(k32, "HeapFree")
        if get_heap is None or free is None:
            return
        get_heap.argtypes = []
        get_heap.restype = ctypes.c_void_p
        free.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        free.restype = ctypes.c_int
        free(get_heap(), 0, ptr)
    except Exception:
        return


def x_window_of(hwnd: int, user32=None) -> Optional[int]:
    """The X11 window id behind a Wine HWND, or None when there is none.

    None means either "not under Wine" or "Wine's Wayland driver is active"
    - both cases the caller must report rather than retry.
    """
    if not hwnd:
        return None
    fn = _export(user32 if user32 is not None else _load("user32"), "GetPropW")
    if fn is None:
        return None
    try:
        fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        fn.restype = ctypes.c_void_p
        value = fn(hwnd, X_WINDOW_PROP)
    except Exception:
        return None
    if not value:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _win_spellings(unix):
    """Both Windows spellings of a Unix path (`\\\\?\\unix...` and `Z:...`)."""
    return ("\\\\?\\unix" + unix, "Z:" + unix.replace("/", "\\"))


def find_unix_python(exists=os.path.exists) -> list:
    """Ordered Unix python3 interpreters that exist, in the Unix spelling.

    Each candidate is probed through BOTH Windows spellings; a candidate that
    answers True for either is returned once.  Off Wine nothing exists, so the
    list is empty.  Never raises.
    """
    found = []
    seen = set()
    for candidate in PYTHON_CANDIDATES:
        if candidate in seen:
            continue
        for spelling in _win_spellings(candidate):
            try:
                present = bool(exists(spelling))
            except Exception:
                present = False
            if present:
                seen.add(candidate)
                found.append(candidate)
                break
    return found


# --- snapshot ---------------------------------------------------------------

@dataclass(frozen=True)
class WineFacts:
    """Immutable snapshot of the host, safe to log and to ship in a report."""

    is_wine: bool = False
    version: Optional[str] = None
    host: Optional[Tuple[str, str]] = None
    launch_kind: str = "none"
    display: Optional[str] = None
    xauthority: Optional[str] = None
    pythons: Tuple[str, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        host = "-" if not self.host else "%s/%s" % (_ascii(self.host[0]),
                                                    _ascii(self.host[1]))
        pythons = ",".join(_ascii(p) for p in self.pythons) or "-"
        # XAUTHORITY is reported as present/absent only - the path names the
        # user's per-session cookie file and never belongs in a pasted report.
        return ("wine=%s host=%s launch=%s display=%s xauthority=%s pythons=%s"
                % (_ascii(self.version) if self.is_wine else "no",
                   host,
                   _ascii(self.launch_kind),
                   _ascii(self.display),
                   "yes" if self.xauthority else "no",
                   pythons))


def collect(environ=None, *, ntdll=None, exists=None) -> WineFacts:
    """Gather every host fact in one shot.  Never raises.

    The interpreter probe runs only under Wine: on Windows a ``Z:\\`` probe
    could touch a real mapped drive for nothing.
    """
    try:
        env = os.environ if environ is None else environ
    except Exception:
        env = {}

    def get(key):
        try:
            value = env.get(key)
        except Exception:
            return None
        return value or None

    try:
        wine = bool(is_wine(ntdll))
    except Exception:
        wine = False
    version = host = None
    pythons = ()
    if wine:
        try:
            version = wine_version(ntdll)
        except Exception:
            version = None
        try:
            host = wine_host(ntdll)
        except Exception:
            host = None
        try:
            found = (find_unix_python() if exists is None
                     else find_unix_python(exists=exists))
            pythons = tuple(found or ())
        except Exception:
            pythons = ()
    try:
        kind = launch_kind(env)
    except Exception:
        kind = "none"
    return WineFacts(is_wine=wine, version=version, host=host,
                     launch_kind=kind, display=get("DISPLAY"),
                     xauthority=get("XAUTHORITY"), pythons=pythons)
