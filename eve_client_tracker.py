"""Enumerate EVE client windows (the inverse filter of eveo_tracker: we WANT
owner exe == exefile.exe). Pure snapshot + diff; cadence lives in the fc_gui tick.
Title prefixes handled: "EVE" (login), "EVE - Name", "EVE Frontier - Name";
em-dash (U+2014) normalized to "-" (EVE-O precedent, localized clients)."""
from __future__ import annotations

import sys
from dataclasses import dataclass

EVE_EXE = "exefile.exe"
LOGIN_TITLE = "EVE"
PREFIXES = ("EVE - ", "EVE Frontier - ")


@dataclass(frozen=True)
class ClientWindow:
    hwnd: int
    char_name: str          # "" for login screens
    title: str
    rect: tuple
    is_iconic: bool
    pid: int

    @property
    def is_login(self) -> bool:
        return self.char_name == ""

    @property
    def key(self) -> str:
        """Layout/ESI join key: lowercased char name."""
        return self.char_name.lower()


def _normalize(title: str) -> str:
    return title.replace("—", "-")


def _char_from_title(title: str) -> str | None:
    """Char name, "" for a login screen, None for a non-EVE window."""
    if title == LOGIN_TITLE:
        return ""
    for prefix in PREFIXES:
        if title.startswith(prefix) and len(title) > len(prefix):
            return title[len(prefix):]
    return None


def find_clients(win32=None) -> list[ClientWindow]:
    w = win32 or _real_win32()
    out = []
    for hwnd in w.enum_windows():
        try:
            if not w.is_visible(hwnd):
                continue
            title = _normalize(w.get_title(hwnd))
            char = _char_from_title(title)
            if char is None:
                continue
            if w.get_owner_exe(hwnd).lower() != EVE_EXE:
                continue
            out.append(ClientWindow(hwnd=hwnd, char_name=char, title=title,
                                    rect=tuple(w.get_rect(hwnd)),
                                    is_iconic=bool(w.is_iconic(hwnd)),
                                    pid=w.get_pid(hwnd)))
        except Exception:
            continue  # window died mid-enumeration — skip
    return out


def diff_clients(prev: dict, cur: dict):
    """(added, retitled [(old,new)], removed) keyed by hwnd dicts."""
    added = [c for h, c in cur.items() if h not in prev]
    removed = [c for h, c in prev.items() if h not in cur]
    retitled = [(prev[h], c) for h, c in cur.items()
                if h in prev and prev[h].title != c.title]
    return added, retitled, removed


def still_same_client(client: ClientWindow, win32=None) -> bool:
    """HWND-reuse guard: IsWindow + title still an EVE title + same PID."""
    w = win32 or _real_win32()
    try:
        if not w.is_window(client.hwnd):
            return False
        title = _normalize(w.get_title(client.hwnd))
        if _char_from_title(title) is None:
            return False
        return w.get_pid(client.hwnd) == client.pid
    except Exception:
        return False


class _RealWin32:
    """Thin real ctypes Win32 wrapper. Instantiated lazily; never used in tests
    (they inject a fake with the same method surface). Mirrors
    eveo_tracker._RealWin32, extended with is_iconic/is_window/get_pid."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32

        self._WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.IsIconic.argtypes = [wintypes.HWND]
        self._user32.IsIconic.restype = wintypes.BOOL
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.GetWindowRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def enum_windows(self) -> list[int]:
        out: list[int] = []

        def _cb(hwnd, _lparam):
            out.append(int(hwnd))
            return True

        proc = self._WNDENUMPROC(_cb)
        self._user32.EnumWindows(proc, 0)
        return out

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(hwnd))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._user32.IsIconic(hwnd))

    def is_window(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(hwnd))

    def get_title(self, hwnd: int) -> str:
        ctypes = self._ctypes
        n = self._user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        self._user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def get_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        wintypes = self._wintypes
        r = wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, self._ctypes.byref(r)):
            return (0, 0, 0, 0)
        return (r.left, r.top, r.right, r.bottom)

    def get_pid(self, hwnd: int) -> int:
        ctypes = self._ctypes
        wintypes = self._wintypes
        pid = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def get_owner_exe(self, hwnd: int) -> str:
        """Owning process basename via GetWindowThreadProcessId +
        QueryFullProcessImageNameW (stdlib ctypes; no psutil)."""
        ctypes = self._ctypes
        wintypes = self._wintypes
        pid = self.get_pid(hwnd)
        if not pid:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = self._kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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
            return full.replace("/", "\\").rsplit("\\", 1)[-1]
        finally:
            self._kernel32.CloseHandle(h)


# Lazily-created singleton so importing the module on non-Windows / headless
# never touches ctypes.windll. Tests always pass their own fake.
_REAL_WIN32_SINGLETON: "_RealWin32 | None" = None


def _real_win32():
    global _REAL_WIN32_SINGLETON
    if _REAL_WIN32_SINGLETON is None:
        if sys.platform != "win32":
            raise RuntimeError("eve_client_tracker real backend requires Windows")
        _REAL_WIN32_SINGLETON = _RealWin32()
    return _REAL_WIN32_SINGLETON
