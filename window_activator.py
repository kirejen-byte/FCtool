"""The ONLY module allowed to change another window's state (spec §4 invariant #1).
Focus-change ladder verified vs EVE-O (SetForegroundWindow+restore is its entire
mechanism) + documented SetForegroundWindow rules (caller received last input =
allowed, which is our click/hotkey situation). ALT-nudge injects an ALT press into
OUR OWN input queue only — never targeted at any client window. AttachThreadInput
deliberately omitted (deadlock-prone). One call = one focus change; no exceptions.

``activate`` is HONEST about the outcome (it used to ``return True`` on every
path, which made a failed switch invisible, unretried and unlogged while the
caller's cycle ring advanced past a client the user never saw). Every rung of the
escalation ladder now records whether it was attempted and what it returned, and
the ladder closes with a SINGLE READ-ONLY ``GetForegroundWindow`` probe: the
return value is True only when that probe confirms the target (or its GA_ROOT
top-level) really is the foreground window.

There is deliberately NO wait / poll / sleep / retry loop around that probe. The
denied-SetForegroundWindow regime (foreground lock -> alt-nudge ->
SwitchToThisWindow) completes asynchronously, so a verify-WAIT would stall the Tk
thread for exactly the duration of the case it was meant to diagnose. A switch we
cannot confirm is a fact to LOG; the caller's next press is the retry.

Compliance envelope (spec §4) is unchanged: focus APIs only, verification is
strictly read-only (GetForegroundWindow / GetAncestor), and no input is injected
beyond the pre-existing ALT-nudge into our own queue.
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6
SW_RESTORE = 9
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x2

# GetAncestor flag: the root top-level window of an hwnd's parent chain. EVE
# client hwnds reach us from EnumWindows and so are already top-level; resolving
# the target's root is belt-and-braces for the "(or its root)" half of the
# activate() contract, never a substitute for the direct comparison.
GA_ROOT = 2

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
        # Read-only probes for activate()'s closing confirmation. Neither changes
        # any window state — they are the whole reason the check is compliant.
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        u.GetAncestor.restype = wintypes.HWND
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

    def get_foreground(self):
        """READ-ONLY: hwnd of the current foreground window (0 if none)."""
        return int(self._u.GetForegroundWindow() or 0)

    def get_ancestor(self, hwnd, flag):
        """READ-ONLY: GetAncestor; used only to resolve a target's GA_ROOT."""
        return int(self._u.GetAncestor(hwnd, flag) or 0)

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


# Escalation ladder, in order. EVERY rung is reported on every call so the
# failure log has one fixed, greppable shape; a rung the ladder never reached
# reads "skip", which is itself the diagnostic ("did we even get that far?").
_RUNGS = ("is_iconic", "restore", "set_foreground", "alt_nudge",
          "set_foreground_retry", "switch_to_window")

# A client that is permanently unfocusable must not flood the log while the user
# keeps hammering the cycle key: one WARNING per hwnd per interval.
_FAIL_LOG_MIN_INTERVAL_S = 10.0
_FAIL_LOG_MAX_TRACKED = 64
_last_fail_log: dict = {}


def _run_rung(outcomes, name, fn):
    """Run one ladder rung and record what it did.

    Records ``True``/``False`` for rungs that report status, ``ok`` for the ones
    that return nothing (alt_nudge, SwitchToThisWindow), and ``ERR(<Type>)`` when
    the backend raises. A raising rung is reported as a non-success and the
    ladder escalates past it — a broken backend call must never read as a win.
    """
    try:
        res = fn()
    except Exception as exc:
        outcomes[name] = "ERR(%s)" % type(exc).__name__
        return None
    outcomes[name] = "ok" if res is None else str(bool(res))
    return res


def _root_of(u, hwnd):
    """GA_ROOT top-level of `hwnd` (READ-ONLY), or `hwnd` itself when the backend
    exposes no GetAncestor. Optional-by-design: the comparison must still work
    against any injected backend that only implements the focus surface."""
    get_ancestor = getattr(u, "get_ancestor", None)
    if get_ancestor is None:
        return hwnd
    try:
        return int(get_ancestor(hwnd, GA_ROOT) or 0) or hwnd
    except Exception:
        return hwnd


def _confirm_foreground(u, hwnd):
    """The closing confirmation: ONE read of GetForegroundWindow, compared with
    the target (and, only if that misses, with the target's GA_ROOT top-level).

    Read-only and instantaneous by contract — no waiting, no polling, no sleep.
    Returns ``(confirmed, detail)``; `detail` is an ASCII fragment for the log.
    An unavailable or raising probe is "not confirmed", never an optimistic True.
    """
    try:
        fg = int(u.get_foreground() or 0)
    except Exception as exc:
        return False, "foreground=ERR(%s) target=%s" % (type(exc).__name__, hwnd)
    if fg and fg == hwnd:
        return True, "foreground=%s target=%s" % (fg, hwnd)
    root = _root_of(u, hwnd)
    ok = bool(fg) and fg == root
    return ok, "foreground=%s target=%s root=%s" % (fg, hwnd, root)


def _log_failed_activate(hwnd, outcomes, detail):
    """One throttled, ASCII-only WARNING per unconfirmed activate.

    ASCII matters: this box's console is cp1252, and a non-cp1252 glyph anywhere
    in a log string makes StreamHandler.emit spew a "Logging error" traceback.
    """
    now = time.monotonic()
    last = _last_fail_log.get(hwnd)
    if last is not None and (now - last) < _FAIL_LOG_MIN_INTERVAL_S:
        return
    if len(_last_fail_log) >= _FAIL_LOG_MAX_TRACKED:
        cutoff = now - _FAIL_LOG_MIN_INTERVAL_S
        for stale in [k for k, t in _last_fail_log.items() if t < cutoff]:
            _last_fail_log.pop(stale, None)
    _last_fail_log[hwnd] = now
    log.warning("activate could not confirm foreground: %s; rungs: %s", detail,
                " ".join("%s=%s" % (n, outcomes[n]) for n in _RUNGS))


def activate(hwnd: int, win32=None) -> bool:
    """Bring an EVE client to the foreground. Focus APIs only — nothing else, ever.

    Returns True ONLY when the closing read-only foreground probe confirms that
    `hwnd` (or its GA_ROOT top-level) actually IS the foreground window. False is
    a real, reportable failure: the switch did not happen, a caller's cycle ring
    must not advance as though it had, and one throttled WARNING carrying the
    per-rung outcomes lands in the log (the field diagnostic for the
    "sometimes ignores a cycle" report).

    The ladder itself is unchanged — this reports it, it does not extend it:
      0. restore, if the window is minimized
      1. SetForegroundWindow
      2. ALT-nudge (unlocks the foreground system) + SetForegroundWindow again
      3. SwitchToThisWindow — alt-tab equivalent, last resort

    Rungs 2-3 are asynchronous/maybe-no-op when Windows holds the foreground
    lock, which is precisely why the confirmation is a single read rather than a
    wait: blocking here would stall the Tk thread in the one regime that already
    feels slow. An unconfirmed switch is retried by the next press, never here.
    """
    u = win32 or _real_user32()
    outcomes = {name: "skip" for name in _RUNGS}

    if _run_rung(outcomes, "is_iconic", lambda: u.is_iconic(hwnd)):
        _run_rung(outcomes, "restore", lambda: u.show_window(hwnd, SW_RESTORE))
    if not _run_rung(outcomes, "set_foreground", lambda: u.set_foreground(hwnd)):
        _run_rung(outcomes, "alt_nudge", lambda: u.alt_nudge())
        if not _run_rung(outcomes, "set_foreground_retry",
                         lambda: u.set_foreground(hwnd)):
            _run_rung(outcomes, "switch_to_window",
                      lambda: u.switch_to_window(hwnd))

    confirmed, detail = _confirm_foreground(u, hwnd)
    if not confirmed:
        _log_failed_activate(hwnd, outcomes, detail)
    return confirmed


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
