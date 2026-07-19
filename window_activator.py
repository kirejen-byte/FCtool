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

# SetWindowPos flags for move_window: never activate, never change z-order. This
# is the whole point of the monitor-pin move — reposition without stealing focus
# or popping the client above the tiles/overlay.
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


class _WINDOWPLACEMENT(ctypes.Structure):  # pragma: no cover — live only
    _fields_ = [("length", wintypes.UINT),
                ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


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
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL
        u.GetWindowPlacement.argtypes = [wintypes.HWND,
                                         ctypes.POINTER(_WINDOWPLACEMENT)]
        u.GetWindowPlacement.restype = wintypes.BOOL
        u.SetWindowPlacement.argtypes = [wintypes.HWND,
                                         ctypes.POINTER(_WINDOWPLACEMENT)]
        u.SetWindowPlacement.restype = wintypes.BOOL
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

    def set_window_pos(self, hwnd, x, y, w, h, flags):
        # hWndInsertAfter (0) is ignored because SWP_NOZORDER is set.
        self._u.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), int(flags))

    def get_window_placement(self, hwnd):
        wp = _WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        self._u.GetWindowPlacement(hwnd, ctypes.byref(wp))
        r = wp.rcNormalPosition
        return int(wp.showCmd), (r.left, r.top, r.right, r.bottom)

    def set_window_placement(self, hwnd, show_cmd, normal_rect):
        wp = _WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        # Fetch current so flags / min / max positions are preserved; override
        # only showCmd + the restore ("normal") rectangle.
        self._u.GetWindowPlacement(hwnd, ctypes.byref(wp))
        wp.showCmd = int(show_cmd)
        l, t, r, b = normal_rect
        wp.rcNormalPosition = wintypes.RECT(int(l), int(t), int(r), int(b))
        self._u.SetWindowPlacement(hwnd, ctypes.byref(wp))


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


def move_window(hwnd: int, x: int, y: int, w: int, h: int, win32=None) -> None:
    """Move + resize a window WITHOUT activating it or changing its z-order.

    Dumb primitive: it takes a final PHYSICAL-pixel ``(x, y, w, h)`` rect and no
    policy (the monitor-fit math lives in ``monitor_pin.plan_move``).

    Two paths, because SetWindowPos on a minimized window is wrong (it would move
    the -32000 iconic slot, not where the window restores to):
      * visible → SetWindowPos with SWP_NOACTIVATE | SWP_NOZORDER.
      * minimized → set the RESTORE rectangle via SetWindowPlacement
        (rcNormalPosition), keeping the current showCmd, so the window restores
        onto the target monitor.
    """
    u = win32 or _real_user32()
    x, y, w, h = int(x), int(y), int(w), int(h)
    if u.is_iconic(hwnd):
        show_cmd, _cur = u.get_window_placement(hwnd)
        u.set_window_placement(hwnd, show_cmd, (x, y, x + w, y + h))
    else:
        u.set_window_pos(hwnd, x, y, w, h, SWP_NOACTIVATE | SWP_NOZORDER)
