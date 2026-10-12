"""The ONLY module allowed to change another window's state (spec §4 invariant #1).
Focus-change ladder verified vs EVE-O (SetForegroundWindow+restore is its entire
mechanism) + documented SetForegroundWindow rules (caller received last input =
allowed, which is our click/hotkey situation). The ladder injects NO input of any
kind: no keystroke and no mouse event is synthesized, into any window or into the
system input stream. AttachThreadInput deliberately omitted (deadlock-prone). One
``activate`` = one focus change, always.

``activate`` is HONEST about the outcome (it used to ``return True`` on every
path, which made a failed switch invisible, unretried and unlogged while the
caller's cycle ring advanced past a client the user never saw). Every rung of the
escalation ladder now records whether it was attempted and what it returned, and
the ladder closes with a SINGLE READ-ONLY ``GetForegroundWindow`` probe: the
return value is True only when that probe confirms the target (or its GA_ROOT
top-level) really is the foreground window.

There is deliberately NO wait / poll / sleep / retry loop around that probe. The
denied-SetForegroundWindow regime (foreground lock -> SwitchToThisWindow)
completes asynchronously, so a verify-WAIT would stall the Tk thread for exactly
the duration of the case it was meant to diagnose. A switch we cannot confirm is
a fact to LOG; the caller's next press is the retry.

Compliance envelope (spec §4): focus APIs only, verification is strictly
read-only (GetForegroundWindow / GetAncestor), and no input is injected. When
Windows refuses SetForegroundWindow the switch rests on SwitchToThisWindow
alone, and may not land until the user's next press.

ONE deliberate exception to "focus APIs only" (owner-approved; close-all-clients
spec §3): ``close_window`` posts ``WM_CLOSE`` to a client's top-level window.
That is a window-management close REQUEST — what a window's own close button
ends in — and it is not input: no key, no click, nothing addressed to anything
INSIDE the client. The client decides what to do with it (exit, or put up its
own "quit?" prompt, which is never answered from here). The binding behind it,
``_RealUser32.post_close``, takes the hwnd and NOTHING else: the message and
both of its parameters are hard-coded, so it cannot be repurposed to deliver
anything but that one request. It is reachable only from ``close_window``,
which is reachable only from the slide-confirmed Close Clients dialog — never
from a hotkey, a timer, a chat command or any other automatic path, never
re-posted, never followed by a force-kill. ``close_window`` also refuses the
handle values Win32 would fan out to every top-level window on the desktop
(``_BROADCAST_HWNDS``) before the backend is ever reached. Source tests pin
the binding, the token count, and that nothing in this module calls either.

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
  * ``SwitchToThisWindow`` is a stub.
  * ``GetForegroundWindow`` right after a successful activate still returns the
    OLD window (the target's thread writes ``active`` later), so the closing
    probe is always stale and False would be a lie.

So under Wine the real mechanism is an X11 ``_NET_ACTIVE_WINDOW`` (source=2,
"pager") + StackMode Above sent by the preview helper on its own X connection
-- Wine's FocusIn handler then sets the foreground internally, bypassing the
deny rule (this is EVE Preview Manager's mechanism). That callable is INJECTED
via ``set_wine_activator`` (this module never imports the helper), the dead
``SwitchToThisWindow`` rung is skipped, and an unconfirmable switch returns the
truthy sentinel ``ISSUED`` rather than False. When that callable succeeds, SetForegroundWindow
is skipped too (``skip(x11)``): a second activation racing the EWMH one only
buys a wineserver denial round trip -- ~100 ms of click-to-front lag in the
field. Off Wine NOTHING here changes.

The Wine ladder is also the only path that measures itself, and round 6 adds
the segment nobody could see. The field numbers were: the app-side ladder
0.5 ms, the helper's X request -> the client's FocusIn 14.6 ms, the PERCEIVED
swap ~100 ms. Everything between FocusIn and the user seeing the client is
Wine flipping the foreground from inside the EVE client's own message pump,
so the only place it is observable is ``GetForegroundWindow`` some time AFTER
activate returned. A short-lived daemon thread (``preview-fg-probe``, one at a
time, Wine only) polls that read-only probe and folds ``fg_flip_ms`` into the
timing dict of the activate it was started for. It never blocks the caller,
never touches Tk, and cannot raise into anything.
"""
from __future__ import annotations

import ctypes
import logging
import operator
import threading
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6
SW_RESTORE = 9

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

# The ONE message this module ever posts (module docstring, "ONE deliberate
# exception"): a close REQUEST to a top-level window.
WM_CLOSE = 0x0010

# Handle values the message functions do NOT treat as one window: 0xFFFF is
# HWND_BROADCAST (every top-level window on the desktop), and the all-ones
# handle is taken as a broadcast too by at least Wine's implementation. A close
# request must never be able to reach either, so close_window refuses them (and
# 0 / negatives, which are not single windows) before any backend is touched.
# Compared on the low 32 bits — the significant part of a handle on Win64, and
# what is left of a wider number once it is narrowed to one. For the same
# reason close_window also refuses anything whose low 32 bits are ZERO (a wrap
# onto NULL: posted there, the message is a thread message to FCTool itself
# and the post reports success) and anything wider than 64 bits.
_BROADCAST_HWNDS = frozenset((0xFFFF, 0xFFFFFFFF))


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
        # The close REQUEST behind close_window; post_close is its only caller.
        # argtypes are load-bearing (an undeclared HWND is truncated to 32 bits
        # on Win64, and LPARAM/WPARAM are pointer-sized).
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        self._u = u

    def is_iconic(self, hwnd):
        return bool(self._u.IsIconic(hwnd))

    def show_window(self, hwnd, cmd):
        return bool(self._u.ShowWindow(hwnd, cmd))

    def show_window_async(self, hwnd, cmd):
        return bool(self._u.ShowWindowAsync(hwnd, cmd))

    def set_foreground(self, hwnd):
        return bool(self._u.SetForegroundWindow(hwnd))

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

    # Takes the hwnd and NOTHING else: the message and both of its parameters
    # are literals here, so this binding can only ever ask a window to close.
    # 0 = posted; otherwise the Win32 error (5 = access denied, e.g. an
    # elevated client; 1400 = no such window), or -1 when Windows named none.
    # Source-pinned by tests/test_window_activator.py — do not add parameters.
    def post_close(self, hwnd):
        ctypes.set_last_error(0)
        if self._u.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            return 0
        return ctypes.get_last_error() or -1


_real = None


def _real_user32():  # pragma: no cover
    global _real
    if _real is None:
        _real = _RealUser32()
    return _real


# Escalation ladder, in order. EVERY rung is reported on every call so the
# failure log has one fixed, greppable shape; a rung the ladder never reached
# reads "skip", which is itself the diagnostic ("did we even get that far?").
_RUNGS = ("is_iconic", "restore", "set_foreground", "switch_to_window")

# The Wine ladder reports the SAME rungs (so the log shape never moves) plus
# the X11 one; the rung Wine makes useless (SwitchToThisWindow is a stub
# there) reads "skip(wine)".
_WINE_RUNG = "x11_activate"
_WINE_RUNGS = ("is_iconic", "restore", _WINE_RUNG, "set_foreground",
               "switch_to_window")
_WINE_SKIPPED = ("switch_to_window",)
_SKIP_WINE = "skip(wine)"
# SetForegroundWindow is ALSO skipped when the X11 activator already took the
# request (round 5): the wineserver's denial of the second, racing activation
# is a cross-process round trip, and the field measured ~100 ms from click to
# the client coming forward with both in flight.
_SKIP_X11 = "skip(x11)"

# The last WINE activate()'s per-rung milliseconds, plus "total_ms" and
# "verdict" ("confirmed"/"issued"), and later "fg_flip_ms" from the probe
# below. Instrumentation for the Linux field reports (rendered by
# x11_thumbs.build_report); the WINDOWS ladder never writes it, so real
# Windows pays nothing for it. REPLACED wholesale, never mutated in place, so
# a reader on another thread can never see half a ladder.
last_activate_timing: dict = {}

# Guards that wholesale replacement. TWO writers exist under Wine: the hotkey
# thread running the ladder, and the foreground probe finishing a previous
# switch.
_timing_lock = threading.Lock()


def get_last_activate_timing() -> dict:
    """A COPY of the last Wine ``activate``'s ``{rung: ms, ...}`` timing.

    Empty until one activate has run under Wine. Read-only by contract: the
    diagnostic report reads it from the UI thread while a hotkey writes it.
    """
    with _timing_lock:
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
# keeps hammering the cycle key: one line per hwnd per log level per interval.
_FAIL_LOG_MIN_INTERVAL_S = 10.0
_FAIL_LOG_MAX_TRACKED = 64
_last_fail_log: dict = {}


def _run_rung(outcomes, name, fn):
    """Run one ladder rung and record what it did.

    Records ``True``/``False`` for rungs that report status, ``ok`` for the one
    that returns nothing (SwitchToThisWindow), and ``ERR(<Type>)`` when the
    backend raises. A raising rung is reported as a non-success and the
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
    Returns ``(confirmed, detail, fg)``; `detail` is an ASCII fragment for the
    log, and `fg` is the hwnd that was read: ``0`` when NO window is foreground,
    ``None`` when the probe itself failed. An unavailable or raising probe is
    "not confirmed", never an optimistic True.
    """
    try:
        fg = int(u.get_foreground() or 0)
    except Exception as exc:
        return (False,
                "foreground=ERR(%s) target=%s" % (type(exc).__name__, hwnd),
                None)
    if fg and fg == hwnd:
        return True, "foreground=%s target=%s" % (fg, hwnd), fg
    root = _root_of(u, hwnd)
    ok = bool(fg) and fg == root
    return ok, "foreground=%s target=%s root=%s" % (fg, hwnd, root), fg


# --------------------------------------------------------- foreground probe
# The ONE segment the round-5 instrumentation could not see: Wine flips the
# foreground from inside the EVE client's own message pump, long after the
# ladder returned and after the helper has already seen the X FocusIn. So the
# measurement has to outlive activate(). Constraints, all deliberate:
#   * WINE ONLY -- real Windows starts no thread and pays nothing;
#   * ONE probe at a time -- a second would race the first for the dict while
#     measuring a switch the user is no longer looking at;
#   * it NEVER blocks the caller (the hotkey thread must return immediately)
#     and it touches ONLY the injected user32 read-only probes: no Tk, no
#     window state, one DEBUG line at the end.
_FG_PROBE_NAME = "preview-fg-probe"
_FG_PROBE_INTERVAL_S = 0.005
_FG_PROBE_TIMEOUT_S = 0.6

_fg_probe_running = False


def _record_fg_flip(owned, flip_ms):
    """Fold one probe's answer into the timing dict it was started for.

    Identity-guarded: a newer activate may already have replaced the module
    dict, and writing this (older) switch's ms into it would misattribute the
    number in the report. Replaces wholesale under the lock; ``owned`` itself
    is never mutated. Returns True when the answer was recorded.
    """
    global last_activate_timing
    with _timing_lock:
        if last_activate_timing is not owned:
            return False
        merged = dict(owned)
        merged["fg_flip_ms"] = flip_ms
        if flip_ms is None:
            merged["fg_flip_timeout"] = True
        last_activate_timing = merged
    return True


def _fg_probe_body(u, hwnd, owned, t0, sleep=time.sleep,
                   clock=time.perf_counter):
    """Poll the read-only foreground until it is the target, or give up.

    ``fg_flip_ms`` is measured from ``t0`` (the ladder's own start stamp), so
    it is directly comparable with ``total_ms``. A timeout records
    ``fg_flip_ms=None`` plus ``fg_flip_timeout=True`` -- "we watched and it
    never came forward" is a different fact from "we never looked". Nothing in
    here may raise: it runs on a thread nobody joins.
    """
    global _fg_probe_running
    try:
        flip_ms = None
        while True:
            confirmed, _detail, _fg = _confirm_foreground(u, hwnd)
            elapsed = clock() - t0
            if confirmed:
                flip_ms = round(elapsed * 1000.0, 1)
                break
            if elapsed >= _FG_PROBE_TIMEOUT_S:
                break
            sleep(_FG_PROBE_INTERVAL_S)
        _record_fg_flip(owned, flip_ms)
        log.debug("activate foreground flip (wine): target=%s fg_flip_ms=%s",
                  hwnd, flip_ms)
    except Exception:
        pass
    finally:
        with _timing_lock:
            _fg_probe_running = False


def _start_fg_probe(u, hwnd, owned, t0, sleep=time.sleep,
                    clock=time.perf_counter):
    """Start the foreground-flip probe. Never blocks, never raises.

    Returns the thread, or None when one is already in flight (that probe is
    left alone to finish) or the thread could not be started at all.
    """
    global _fg_probe_running
    try:
        with _timing_lock:
            if _fg_probe_running:
                return None
            _fg_probe_running = True
        try:
            thread = threading.Thread(target=_fg_probe_body,
                                      args=(u, hwnd, owned, t0, sleep, clock),
                                      name=_FG_PROBE_NAME, daemon=True)
            thread.start()
        except Exception:
            with _timing_lock:
                _fg_probe_running = False
            return None
        return thread
    except Exception:
        return None


def _log_failed_activate(hwnd, outcomes, detail, level=logging.WARNING):
    """One throttled, ASCII-only line per unconfirmed activate, at ``level``.

    ASCII matters: this box's console is cp1252, and a non-cp1252 glyph anywhere
    in a log string makes StreamHandler.emit spew a "Logging error" traceback.

    The throttle is per hwnd AND per level: the DEBUG line of a switch that is
    merely still in flight must never use up the slot of a real failure on the
    same window.
    """
    now = time.monotonic()
    key = (hwnd, level)
    last = _last_fail_log.get(key)
    if last is not None and (now - last) < _FAIL_LOG_MIN_INTERVAL_S:
        return
    if len(_last_fail_log) >= _FAIL_LOG_MAX_TRACKED:
        cutoff = now - _FAIL_LOG_MIN_INTERVAL_S
        for stale in [k for k, t in _last_fail_log.items() if t < cutoff]:
            _last_fail_log.pop(stale, None)
    _last_fail_log[key] = now
    log.log(level, "activate could not confirm foreground: %s; rungs: %s",
            detail, _rung_text(outcomes, _RUNGS))


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
    Linux tester has no profiler, so the report IS the measurement. The ladder
    closes by arming the foreground probe, which appends ``fg_flip_ms`` to
    that same dict once (and if) the client actually comes forward.
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
    confirmed, detail, _fg = _confirm_foreground(u, hwnd)
    timing["confirm"] = _ms_since(probe_t0)
    timing["total_ms"] = _ms_since(started)
    timing["verdict"] = "confirmed" if confirmed else "issued"
    global last_activate_timing
    with _timing_lock:
        last_activate_timing = timing
    # The switch is now the client's problem; the probe watches for it from
    # its own thread so this one returns to the hotkey immediately.
    _start_fg_probe(u, hwnd, timing, started)
    if confirmed:
        return True
    log.debug("activate issued (wine, foreground unconfirmable): %s; "
              "rungs: %s; ms: %s", detail, _rung_text(outcomes, _WINE_RUNGS),
              _timing_text(timing))
    return ISSUED


def activate(hwnd: int, win32=None) -> bool:
    """Bring an EVE client to the foreground. Focus APIs only — nothing else, ever.

    Returns True ONLY when the closing read-only foreground probe confirms that
    `hwnd` (or its GA_ROOT top-level) actually IS the foreground window. False
    means NOT CONFIRMED: a caller's cycle ring must not advance as though the
    switch had landed, and one throttled line carrying the per-rung outcomes
    lands in the log (the field diagnostic for the "sometimes ignores a cycle"
    report). That line is a WARNING, with one exception: SetForegroundWindow
    was accepted and the probe read NO foreground window at all - Windows
    between two foreground windows, the switch in flight - is logged at DEBUG.

    The ladder injects no input of any kind:
      0. restore, if the window is minimized
      1. SetForegroundWindow
      2. SwitchToThisWindow, only when rung 1 did not report success

    Rung 2 is asynchronous/maybe-no-op when Windows holds the foreground lock,
    which is precisely why the confirmation is a single read rather than a
    wait: blocking here would stall the Tk thread in the one regime that already
    feels slow. An unconfirmed switch is retried by the next press, never here.

    UNDER WINE this delegates to ``_activate_wine`` (see the module docstring):
    a posted restore, the injected X11 activation, ONE SetForegroundWindow, and
    a truthy ``ISSUED`` instead of an un-confirmable False. Off Wine the lines
    below are the whole ladder.
    """
    u = win32 or _real_user32()
    if _is_wine():
        return _activate_wine(hwnd, u)
    outcomes = {name: "skip" for name in _RUNGS}

    if _run_rung(outcomes, "is_iconic", lambda: u.is_iconic(hwnd)):
        _run_rung(outcomes, "restore", lambda: u.show_window(hwnd, SW_RESTORE))
    accepted = _run_rung(outcomes, "set_foreground",
                         lambda: u.set_foreground(hwnd))
    if not accepted:
        _run_rung(outcomes, "switch_to_window", lambda: u.switch_to_window(hwnd))

    confirmed, detail, fg = _confirm_foreground(u, hwnd)
    if not confirmed:
        # Windows ACCEPTED the request and the probe read no foreground window
        # at all: it ran in the instant between the old foreground and the new
        # one, with the switch in flight. Worth a DEBUG line, not a WARNING. A
        # refused or raising request, a DIFFERENT window in front, or a probe
        # that failed (fg is None) is a real "did not happen".
        in_flight = bool(accepted) and fg == 0
        _log_failed_activate(hwnd, outcomes, detail,
                             logging.DEBUG if in_flight else logging.WARNING)
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


def close_window(hwnd: int, win32=None) -> int:
    """Ask ONE top-level window to close (posts ``WM_CLOSE``); never waits.

    A close REQUEST and nothing more — see the module docstring's "ONE
    deliberate exception". Returns ``0`` when the request was posted (which
    says nothing about whether the window then closed: the caller watches for
    that), otherwise the Win32 error code, or ``-1`` when there is none to
    report. ``-1`` also covers the two cases in which nothing is sent at all:

      * ``hwnd`` is not ONE real window handle. Only a true integer is
        accepted — nothing is coerced, so a float, a string and a bool are
        refused, and so is an object whose ``__index__`` raises (whatever it
        raises: nothing propagates) — and it must be positive, fit in 64
        bits, and have low 32 bits (the significant part of a handle) that
        are neither zero nor one of ``_BROADCAST_HWNDS``. A number that would
        WRAP onto NULL or onto a broadcast handle when narrowed is refused
        like the value it wraps to;
      * the backend has no ``post_close`` (an injected double, an older duck
        type) — the ``_show`` getattr-fallback, so a missing method can never
        raise out of the dialog.

    Every refusal happens before a backend is even looked up, and the same
    way on Windows and under Wine (there is no Wine branch in here). An
    injected ``win32`` is used whenever one was given, falsy or not: only
    ``None`` means the real backend.

    One call posts at most one request to at most one window. There is no
    retry, no escalation and no force-kill here or anywhere behind it.
    """
    if isinstance(hwnd, bool):                 # an int subclass, not a handle
        return -1
    try:
        hwnd = operator.index(hwnd)            # true integers only: no coercion
    except Exception:                          # also an __index__ that raises
        return -1
    if hwnd <= 0 or hwnd >> 64:
        return -1
    low = hwnd & 0xFFFFFFFF
    if low == 0 or low in _BROADCAST_HWNDS:
        return -1
    u = win32 if win32 is not None else _real_user32()
    fn = getattr(u, "post_close", None)
    if fn is None:
        return -1
    return int(fn(hwnd))
