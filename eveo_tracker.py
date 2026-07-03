"""Enumerate EVE-O Preview thumbnail windows via ctypes Win32 (no pywin32,
no psutil). All Win32 access is behind the injectable _real_win32 wrapper so
tests run headless with a fake.

A thumbnail qualifies when ALL hold:
  * window is visible
  * title starts with 'EVE - ' (bare 'EVE' pre-login clients are excluded)
  * ex-style has WS_EX_TOOLWINDOW
  * owning process basename is NOT 'ExeFile.exe' (the real game client, whose
    titles match too but is not a tool window and belongs to ExeFile.exe)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

TITLE_PREFIX = "EVE - "
GAME_CLIENT_EXE = "exefile.exe"   # compared case-insensitively
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


def preview_running(win32=None) -> bool:
    """True iff Eve-O Preview appears to be running: at least one thumbnail is
    present OR its main window (a visible top-level titled exactly
    'EVE-O Preview') is open even with no clients previewed yet. Uses the same
    EnumWindows pass — no psutil. Never raises; returns False on any failure."""
    if win32 is None:
        if sys.platform != "win32":
            return False
        win32 = _real_win32()
    if find_thumbs(win32=win32):
        return True
    # Fallback: the Eve-O Preview main window itself (no thumbnails yet).
    try:
        hwnds = win32.enum_windows()
    except Exception:
        return False
    for hwnd in hwnds:
        try:
            if not win32.is_visible(hwnd):
                continue
            if (win32.get_title(hwnd) or "").strip() == PREVIEW_MAIN_TITLE:
                return True
        except Exception:
            continue
    return False
