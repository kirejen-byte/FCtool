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

UNDER WINE the ladder is DIFFERENT, because four of its rungs are provably
useless or harmful there (all source-verified against Wine, 2026-09-13):

  * ``ShowWindow`` on a FOREIGN window is a synchronous cross-process send
    (win32u/window.c -> WM_WINE_SHOWWINDOW) that blocks this thread for one of
    the target's frames; ``ShowWindowAsync`` posts instead.
  * the wineserver DENIES SetForegroundWindow from a process that is not
    foreground whose input ``user_time`` is older than the foreground's, and
    ``WM_HOTKEY`` never bumps ``user_time`` -- so after the one-shot per-window
    freebie EVERY hotkey-driven swap is denied, forever (tile clicks still
    work, which is exactly the field report).
  * ``SwitchToThisWindow`` is a stub, and the ALT nudge is delivered to the EVE
    client (a spurious ALT that perturbs the next keystroke) while ALSO bumping
    the target's ``user_time``, making the retry LESS likely to pass.
  * ``GetForegroundWindow`` right after a successful activate still returns the
    OLD window (the target's thread writes ``active`` later), so the closing
    probe is always stale and False would be a lie.

So under Wine the real mechanism is an X11 ``_NET_ACTIVE_WINDOW`` (source=2,
"pager") + StackMode Above sent by the preview helper on its own X connection
-- Wine's FocusIn handler then sets the foreground internally, bypassing the
deny rule (this is EVE Preview Manager's mechanism). That callable is INJECTED
via ``set_wine_activator`` (this module never imports the helper), the three
dead rungs are skipped, and an unconfirmable switch returns the truthy sentinel
``ISSUED`` rather than False. When that callable succeeds, SetForegroundWindow
is skipped too (``skip(x11)``): a second activation racing the EWMH one only
buys a wineserver denial round trip -- ~100 ms of click-to-front lag in the
field. Off Wine NOTHING here changes.
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
        # POSTs the show-state change instead of sending it. Used only under
        # Wine, where ShowWindow on a foreign window blocks this thread for a
        # frame of the TARGET's message loop.
        u.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindowAsync.restype = wintypes.BOOL
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

    def show_window_async(self, hwnd, cmd):
        return bool(self._u.ShowWindowAsync(hwnd, cmd))

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

# The Wine ladder reports the SAME rungs (so the log shape never moves) plus
# the X11 one; the three rungs Wine makes useless read "skip(wine)".
_WINE_RUNG = "x11_activate"
_WINE_RUNGS = ("is_iconic", "restore", _WINE_RUNG, "set_foreground",
               "alt_nudge", "set_foreground_retry", "switch_to_window")
_WINE_SKIPPED = ("alt_nudge", "set_foreground_retry", "switch_to_window")
_SKIP_WINE = "skip(wine)"
# SetForegroundWindow is ALSO skipped when the X11 activator already took the
# request (round 5): the wineserver's denial of the second, racing activation
# is a cross-process round trip, and the field measured ~100 ms from click to
# the client coming forward with both in flight.
_SKIP_X11 = "skip(x11)"

# The last WINE activate()'s per-rung milliseconds, plus "total_ms" and
# "verdict" ("confirmed"/"issued"). Instrumentation for the Linux field
# reports (rendered by x11_thumbs.build_report); the WINDOWS ladder never
# writes it, so real Windows pays nothing for it. REPLACED wholesale, never
# mutated in place, so a reader on another thread can never see half a ladder.
last_activate_timing: dict = {}


def get_last_activate_timing() -> dict:
    """A COPY of the last Wine ``activate``'s ``{rung: ms, ...}`` timing.

    Empty until one activate has run under Wine. Read-only by contract: the
    diagnostic report reads it from the UI thread while a hotkey writes it.
    """
    return dict(last_activate_timing)


def _ms_since(t0) -> float:
    """Milliseconds since a ``time.perf_counter()`` stamp, 1 decimal."""
    return round((time.perf_counter() - t0) * 1000.0, 1)


class _Issued(int):
    """``activate``'s "sent, cannot be confirmed" verdict under Wine.

    An int subclass so it is unambiguously TRUTHY (``bool(ISSUED) is True``)
    for every existing caller that only branches on the return, while still
    being distinguishable from a confirmed True by identity - Wine's
    GetForegroundWindow is stale right after a switch, so there is nothing
    honest to confirm with and False would wrongly read as "did not happen".
    """

    __slots__ = ()

    def __repr__(self):          # pragma: no cover - cosmetic
        return "ISSUED"


ISSUED = _Issued(1)

# The injected X11 focus callable (``hwnd -> bool``), or None. Registered by
# the preview layer; this module never imports the helper.
_wine_activator = None


def set_wine_activator(fn) -> None:
    """Register (``fn``) or clear (``None``) the Wine X11 focus callable.

    Anything not callable clears it: a half-wired backend must degrade to the
    plain ladder, never raise inside a hotkey.
    """
    global _wine_activator
    _wine_activator = fn if callable(fn) else None


def get_wine_activator():
    """The registered Wine X11 focus callable, or None."""
    return _wine_activator


def _is_wine() -> bool:
    """True under Wine/Proton. The import is LAZY and the result total.

    ``wine_detect`` touches ctypes, and this module is imported at boot on
    real Windows, so the probe must not run at import time; ``is_wine()``
    caches its own answer, so calling this per activate is free.
    """
    try:
        import wine_detect
        return bool(wine_detect.is_wine())
    except Exception:
        return False


def _show(u, hwnd, cmd):
    """Change a window's show-state: POSTed under Wine, SENT on Windows.

    Falls back to ``show_window`` when the backend predates
    ``show_window_async`` (injected test doubles, older duck types), so the
    Wine path can never crash on a missing method.
    """
    if _is_wine():
        fn = getattr(u, "show_window_async", None)
        if fn is not None:
            return fn(hwnd, cmd)
    return u.show_window(hwnd, cmd)

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
                _rung_text(outcomes, _RUNGS))


def _rung_text(outcomes, rungs):
    """The fixed, greppable ``name=outcome`` line for one ladder run."""
    return " ".join("%s=%s" % (name, outcomes.get(name, "skip"))
                    for name in rungs)


# Timing keys in log/report order: the rungs that were measured, then the
# closing probe, the wall total and the verdict.
_TIMING_ORDER = _WINE_RUNGS + ("confirm", "total_ms", "verdict")


def _timing_text(timing):
    """The ``name=ms`` half of the Wine DEBUG line (unmeasured rungs omitted)."""
    return " ".join("%s=%s" % (name, timing[name])
                    for name in _TIMING_ORDER if name in timing)


def _activate_wine(hwnd, u):
    """The Wine ladder: restore (posted) -> X11 activate -> SetForeground.

    The SetForegroundWindow rung runs ONLY when the X11 activator was absent
    or returned falsy. When the activator took the request, the EWMH
    ``_NET_ACTIVE_WINDOW`` is already in flight and a SetForegroundWindow
    behind it is a SECOND, racing activation -- one the wineserver denies over
    a cross-process round trip, which is what the field measured as ~100 ms of
    click-to-front lag (round 5). That rung then reads ``skip(x11)``.

    Returns True when the (stale-prone) probe happens to confirm the target
    anyway, else ``ISSUED``: the request WAS made and Wine simply cannot be
    asked yet. Never False and never a WARNING - under Wine "unconfirmed" is
    the normal case, so the old warning would fire on every single switch.

    Every rung is also timed into ``last_activate_timing`` (Wine only): the
    Linux tester has no profiler, so the report IS the measurement.
    """
    outcomes = {name: "skip" for name in _WINE_RUNGS}
    for name in _WINE_SKIPPED:
        outcomes[name] = _SKIP_WINE
    timing = {}
    started = time.perf_counter()

    def timed(name, fn):
        t0 = time.perf_counter()
        try:
            return _run_rung(outcomes, name, fn)
        finally:
            timing[name] = _ms_since(t0)

    if timed("is_iconic", lambda: u.is_iconic(hwnd)):
        timed("restore", lambda: _show(u, hwnd, SW_RESTORE))
    activator = _wine_activator
    x11_ok = False
    if activator is not None:
        x11_ok = bool(timed(_WINE_RUNG, lambda: activator(hwnd)))
    if x11_ok:
        outcomes["set_foreground"] = _SKIP_X11
    else:
        # No activator (or it failed): this is the only rung left, and it is
        # free when this process IS foreground (a tile click).
        timed("set_foreground", lambda: u.set_foreground(hwnd))

    probe_t0 = time.perf_counter()
    confirmed, detail = _confirm_foreground(u, hwnd)
    timing["confirm"] = _ms_since(probe_t0)
    timing["total_ms"] = _ms_since(started)
    timing["verdict"] = "confirmed" if confirmed else "issued"
    global last_activate_timing
    last_activate_timing = timing
    if confirmed:
        return True
    log.debug("activate issued (wine, foreground unconfirmable): %s; "
              "rungs: %s; ms: %s", detail, _rung_text(outcomes, _WINE_RUNGS),
              _timing_text(timing))
    return ISSUED


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

    UNDER WINE this delegates to ``_activate_wine`` (see the module docstring):
    a posted restore, the injected X11 activation, ONE SetForegroundWindow, and
    a truthy ``ISSUED`` instead of an un-confirmable False. Off Wine, every line
    below runs exactly as it did before that path existed.
    """
    u = win32 or _real_user32()
    if _is_wine():
        return _activate_wine(hwnd, u)
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
    _show(win32 or _real_user32(), hwnd, SW_MINIMIZE)


def restore_no_focus(hwnd: int, win32=None) -> None:
    _show(win32 or _real_user32(), hwnd, SW_SHOWNOACTIVATE)


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
