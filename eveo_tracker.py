"""Enumerate EVE-O Preview thumbnail windows via ctypes Win32 (no pywin32,
no psutil). All Win32 access is behind the injectable _real_win32 wrapper so
tests run headless with a fake.

A thumbnail qualifies when ALL hold:
  * window is visible
  * title starts with 'EVE - ' (bare 'EVE' pre-login clients are excluded)
  * ex-style has WS_EX_TOOLWINDOW
  * owning process basename is NOT 'ExeFile.exe' (the real game client, whose
    titles match too but is not a tool window and belongs to ExeFile.exe)

Detection (preview_running) is process-based so it survives Eve-O configs that
hide the thumbnail windows unless an EVE client is focused AND minimise the
Eve-O main window to the tray — in that state neither a thumbnail nor the main
window is visible, yet the process is still running. list_process_names()
enumerates running-process basenames via CreateToolhelp32Snapshot (stdlib
ctypes, no psutil) so we can detect "EVE-O Preview.exe" regardless of window
visibility. That snapshot is ~12 ms on a normal desktop and preview_running()
is the FIRST thing the 250 ms native-preview tick does on the Tk thread, so the
snapshot is time-cached (see _PROCESS_PROBE_INTERVAL_S) while the cheap window
signals stay on every call.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass

TITLE_PREFIX = "EVE - "
GAME_CLIENT_EXE = "exefile.exe"   # compared case-insensitively
PREVIEW_EXE = "eve-o preview.exe"   # compared case-insensitively
WS_EX_TOOLWINDOW = 0x00000080
GWL_EXSTYLE = -20


@dataclass(frozen=True)
class Thumb:
    hwnd: int
    char_name: str                       # parsed from "EVE - <name>"
    rect: tuple[int, int, int, int]      # physical px (L, T, R, B)


class _RealWin32:
    """Thin real ctypes Win32 wrapper. Instantiated lazily; never used in tests
    (they inject a fake with the same method surface)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32

        # LONG_PTR-correct Get/SetWindowLongPtrW on 64-bit; fall back to the
        # 32-bit *W entry points on 32-bit Python.
        self._GetWindowLongPtr = getattr(
            self._user32, "GetWindowLongPtrW", self._user32.GetWindowLongW)
        self._GetWindowLongPtr.restype = ctypes.c_ssize_t
        self._GetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int]

        self._WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.GetWindowRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        # Toolhelp process-snapshot entry points (kernel32). Used by
        # list_process_names() for window-visibility-independent detection.
        self._kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._kernel32.CreateToolhelp32Snapshot.argtypes = [
            wintypes.DWORD, wintypes.DWORD]
        self._kernel32.Process32FirstW.restype = wintypes.BOOL
        self._kernel32.Process32NextW.restype = wintypes.BOOL

    def enum_windows(self) -> list[int]:
        ctypes = self._ctypes
        wintypes = self._wintypes
        out: list[int] = []

        def _cb(hwnd, _lparam):
            out.append(int(hwnd))
            return True

        proc = self._WNDENUMPROC(_cb)
        self._user32.EnumWindows(proc, 0)
        return out

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(hwnd))

    def get_title(self, hwnd: int) -> str:
        ctypes = self._ctypes
        n = self._user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        self._user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def get_ex_style(self, hwnd: int) -> int:
        return int(self._GetWindowLongPtr(hwnd, GWL_EXSTYLE))

    def get_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        wintypes = self._wintypes
        r = wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, self._ctypes.byref(r)):
            return (0, 0, 0, 0)
        return (r.left, r.top, r.right, r.bottom)

    def get_owner_exe(self, hwnd: int) -> str:
        """Owning process basename via GetWindowThreadProcessId +
        QueryFullProcessImageNameW (stdlib ctypes; no psutil)."""
        ctypes = self._ctypes
        wintypes = self._wintypes
        pid = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = self._kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            size = wintypes.DWORD(260)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = self._kernel32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(size))
            if not ok:
                return ""
            full = buf.value
            # basename without importing os for a hot path
            return full.replace("/", "\\").rsplit("\\", 1)[-1]
        finally:
            self._kernel32.CloseHandle(h)

    def list_process_names(self) -> list[str]:
        """Return the basenames of all running processes (e.g. 'EVE-O
        Preview.exe') via CreateToolhelp32Snapshot / Process32FirstW / NextW.
        No psutil. Never raises; returns [] on any Win32 failure."""
        ctypes = self._ctypes
        wintypes = self._wintypes
        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        MAX_PATH = 260

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * MAX_PATH),
            ]

        snap = self._kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return []
        out: list[str] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = self._kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile
                if name:
                    out.append(name)
                ok = self._kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snap)
        return out


# Lazily-created singleton so importing the module on non-Windows / headless
# never touches ctypes.windll. Tests always pass their own fake.
_REAL_WIN32_SINGLETON: "_RealWin32 | None" = None


def _real_win32():
    global _REAL_WIN32_SINGLETON
    if _REAL_WIN32_SINGLETON is None:
        _REAL_WIN32_SINGLETON = _RealWin32()
    return _REAL_WIN32_SINGLETON


def _is_thumb(win32, hwnd: int) -> "Thumb | None":
    try:
        if not win32.is_visible(hwnd):
            return None
        title = win32.get_title(hwnd) or ""
        if not title.startswith(TITLE_PREFIX):
            return None
        name = title[len(TITLE_PREFIX):].strip()
        if not name:                       # bare "EVE - " with no name
            return None
        if not (win32.get_ex_style(hwnd) & WS_EX_TOOLWINDOW):
            return None
        exe = (win32.get_owner_exe(hwnd) or "").lower()
        if exe == GAME_CLIENT_EXE:         # the real game client
            return None
        rect = tuple(win32.get_rect(hwnd))
        return Thumb(hwnd=hwnd, char_name=name, rect=rect)  # type: ignore[arg-type]
    except Exception:
        return None


def find_thumbs(win32=None) -> list[Thumb]:
    """Return the current EVE-O Preview thumbnail windows. Never raises;
    returns [] on any Win32 failure."""
    if win32 is None:
        if sys.platform != "win32":
            return []
        win32 = _real_win32()
    out: list[Thumb] = []
    try:
        hwnds = win32.enum_windows()
    except Exception:
        return []
    for hwnd in hwnds:
        thumb = _is_thumb(win32, hwnd)
        if thumb is not None:
            out.append(thumb)
    return out


PREVIEW_MAIN_TITLE = "EVE-O Preview"   # the app's own main window title

# ── Process-probe cache ──────────────────────────────────────────────────────
# _preview_process_running walks the FULL process table
# (CreateToolhelp32Snapshot over ~440 processes): measured 11.89 ms of a
# 14.55 ms preview_running call, 9.7 ms of 11.9 ms on a re-measure the same
# day. preview_running is called 4x/s (4 Hz) from the native preview tick
# while tiles are on screen -- that's the EVE-O-absent hot path this cache
# cures -- synchronously on the Tk thread, so that walk was ~5.8% of the UI
# loop delivered as hard hitches. While blocked (EVE-O detected, every tile
# retired) the tick itself slows to the 2 s idle cadence, i.e. 0.5 Hz, not
# 4x/s. "Is Eve-O Preview running?" is a session-scale question, so the
# answer is cached and re-probed at most once per interval.
# The cheap window signals are NOT cached — Eve-O appearing WITH visible
# windows is still caught on the very next tick; only the tray/all-hidden case
# waits for a re-probe. Because that wait rides the slow 2 s idle tick,
# noticing that Eve-O has exited can lag up to ~7 s worst case (5 s of cache
# staleness plus one more idle-cadence tick), not just the 5 s interval alone.
_PROCESS_PROBE_INTERVAL_S = 5.0

_probe_cache_win32 = None      # the win32 wrapper the cached answer came from
_probe_cache_ts = None         # monotonic stamp of that answer (None = cold)
_probe_cache_val = False


def _now() -> float:
    """Monotonic clock indirection so tests can drive the probe interval
    without sleeping."""
    return time.monotonic()


def reset_process_cache() -> None:
    """Drop the cached process-probe answer so the next call takes a fresh
    snapshot. Used by tests; also the honest way for any caller that needs an
    immediate re-probe."""
    global _probe_cache_win32, _probe_cache_ts, _probe_cache_val
    _probe_cache_win32 = None
    _probe_cache_ts = None
    _probe_cache_val = False


def _preview_process_running(win32) -> bool:
    """True iff a process named 'EVE-O Preview.exe' (case-insensitive) is
    running, OR a renamed Eve-O fork that owns a thumbnail-style window is
    running. Window-visibility-independent: works even when Eve-O has hidden
    every thumbnail AND minimised its main window to the tray.

    The renamed-fork case is derived from live windows: any visible tool window
    titled 'EVE - <name>' whose owner exe is neither the game client nor
    already matched contributes its basename as an "Eve-O fork" candidate, and
    if that basename is in the process list we count it. This mirrors
    find_thumbs' renamed-exe tolerance without hard-coding fork names.
    Never raises; returns False on any failure (e.g. no list_process_names on
    an older fake)."""
    lister = getattr(win32, "list_process_names", None)
    if lister is None:
        return False
    try:
        names = lister() or []
    except Exception:
        return False
    lowered = {(n or "").rsplit("\\", 1)[-1].lower() for n in names}
    if PREVIEW_EXE in lowered:
        return True
    # Renamed-fork tolerance: match the owner exe of any live thumbnail-style
    # tool window against the running-process list.
    try:
        for hwnd in win32.enum_windows():
            try:
                if not win32.is_visible(hwnd):
                    continue
                if not (win32.get_title(hwnd) or "").startswith(TITLE_PREFIX):
                    continue
                if not (win32.get_ex_style(hwnd) & WS_EX_TOOLWINDOW):
                    continue
                exe = (win32.get_owner_exe(hwnd) or "").lower()
                if exe and exe != GAME_CLIENT_EXE and exe in lowered:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _preview_process_running_cached(win32) -> bool:
    """`_preview_process_running` behind a monotonic time gate.

    Identity-keyed on the win32 wrapper: production has exactly ONE (the lazy
    _RealWin32 singleton), so there this is a plain time cache; a test handing
    in a different fake always gets a fresh probe, so no case can inherit
    another's cached answer. A COLD cache always probes — Eve-O already running
    when FCTool starts is detected on the first call, never as a stale
    'absent'. Semantics are untouched: this only changes how often the answer
    is recomputed."""
    global _probe_cache_win32, _probe_cache_ts, _probe_cache_val
    now = _now()
    ts = _probe_cache_ts
    if (ts is not None and _probe_cache_win32 is win32
            and 0.0 <= (now - ts) < _PROCESS_PROBE_INTERVAL_S):
        return _probe_cache_val
    val = _preview_process_running(win32)
    _probe_cache_win32 = win32
    _probe_cache_ts = now
    _probe_cache_val = val
    return val


def _window_signals(win32) -> bool:
    """Signals 1 and 2 (any Eve-O thumbnail / the Eve-O main window) in ONE
    EnumWindows pass instead of two, stopping at the first hit.

    Predicates are unchanged — thumb-ness is still decided by `_is_thumb`, the
    single owner of that rule. Visibility and title are read once per window
    and used to pre-filter, which cannot hide anything `_is_thumb` would have
    accepted (it rejects invisible windows and titles without TITLE_PREFIX
    itself). Never raises."""
    try:
        hwnds = win32.enum_windows()
    except Exception:
        return False
    for hwnd in hwnds:
        try:
            if not win32.is_visible(hwnd):
                continue
            title = win32.get_title(hwnd) or ""
        except Exception:
            continue
        # Signal 2: the Eve-O Preview main window (no clients previewed yet).
        if title.strip() == PREVIEW_MAIN_TITLE:
            return True
        # Signal 1: a thumbnail window. Only "EVE - ..." titles can qualify, so
        # the full (rect-reading) check runs for a handful of windows at most.
        if (title.startswith(TITLE_PREFIX)
                and _is_thumb(win32, hwnd) is not None):
            return True
    return False


def preview_running(win32=None) -> bool:
    """True iff Eve-O Preview appears to be running via ANY of three signals:
      1. at least one thumbnail window is present, OR
      2. its main window (a visible top-level titled exactly 'EVE-O Preview')
         is open (no clients previewed yet), OR
      3. an 'EVE-O Preview.exe' process is running even with every thumbnail
         hidden and the main window minimised to the tray.

    Signals 1-2 share ONE EnumWindows pass and run on EVERY call, so Eve-O
    showing any window is detected immediately. Signal 3 needs a full process
    snapshot (~12 ms, no psutil) and is therefore time-cached — at most one
    snapshot per _PROCESS_PROBE_INTERVAL_S — because this is called 4x/s
    (while tiles are on screen; 0.5x/s while blocked/idle) from the native
    preview tick on the Tk thread while the answer changes at most once or
    twice a session. Never raises; returns False on any failure."""
    if win32 is None:
        if sys.platform != "win32":
            return False
        win32 = _real_win32()
    if _window_signals(win32):
        return True
    # Fallback: the process is running even though every window is hidden
    # (thumbnails suppressed AND the main window minimised to tray). Cached.
    return _preview_process_running_cached(win32)
