"""The ONLY module allowed to change another window's state (spec §4 invariant #1).
Focus-change ladder verified vs EVE-O (SetForegroundWindow+restore is its entire
mechanism) + documented SetForegroundWindow rules (caller received last input =
allowed, which is our click/hotkey situation). ALT-nudge injects an ALT press into
OUR OWN input queue only — never targeted at any client window. AttachThreadInput
deliberately omitted (deadlock-prone). One call = one focus change; no exceptions."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6
SW_RESTORE = 9
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x2


class _RealUser32:  # pragma: no cover — exercised by spike/live
    def __init__(self):
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        u.SwitchToThisWindow.restype = None
        u.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte,
                                  wintypes.DWORD, ctypes.POINTER(wintypes.ULONG)]
        self._u = u

    def is_iconic(self, hwnd):
        return bool(self._u.IsIconic(hwnd))

    def show_window(self, hwnd, cmd):
        return bool(self._u.ShowWindow(hwnd, cmd))

    def set_foreground(self, hwnd):
        return bool(self._u.SetForegroundWindow(hwnd))

    def alt_nudge(self):
        self._u.keybd_event(_VK_MENU, 0, 0, None)
        self._u.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, None)

    def switch_to_window(self, hwnd):
        self._u.SwitchToThisWindow(hwnd, True)


_real = None


def _real_user32():  # pragma: no cover
    global _real
    if _real is None:
        _real = _RealUser32()
    return _real


def activate(hwnd: int, win32=None) -> bool:
    """Bring an EVE client to the foreground. Focus APIs only — nothing else, ever."""
    u = win32 or _real_user32()
    if u.is_iconic(hwnd):
        u.show_window(hwnd, SW_RESTORE)
    if u.set_foreground(hwnd):
        return True
    u.alt_nudge()               # unlocks the foreground system for a retry
    if u.set_foreground(hwnd):
        return True
    u.switch_to_window(hwnd)    # alt-tab-equivalent, last resort
    return True


def minimize(hwnd: int, win32=None) -> None:
    (win32 or _real_user32()).show_window(hwnd, SW_MINIMIZE)


def restore_no_focus(hwnd: int, win32=None) -> None:
    (win32 or _real_user32()).show_window(hwnd, SW_SHOWNOACTIVATE)
