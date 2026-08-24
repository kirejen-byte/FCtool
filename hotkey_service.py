"""Global hotkeys via RegisterHotKey on a dedicated daemon thread.

Verified facts this design rests on (do not "simplify" them away):
- RegisterHotKey(hWnd=NULL) posts WM_HOTKEY to the REGISTERING THREAD's queue;
  no window needed. Tk's pump cannot see WM_HOTKEY — hence this thread.
- Registration is thread-affine: register/unregister must happen ON the worker.
  Config changes therefore restart the thread (simplest correct lifecycle).
- Matched keystrokes are swallowed system-wide (EVE never sees them) — that is
  the desired focus-key behavior AND the reason defaults ship EMPTY.
  It is ALSO why suspend()/resume() exist: a bound Tab is dead in Discord too,
  and declining to act on the WM_HOTKEY would not give the keystroke back. Only
  UNREGISTERING releases the key, so an "only while EVE is focused" gate has to
  be dynamic registration — see suspend() below.
- ERROR_HOTKEY_ALREADY_REGISTERED == 1409 → surfaced per-binding via .failures.
"""
from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

_VK = {**{f"F{i}": 0x6F + i for i in range(1, 25)}}          # F1=0x70 … F24=0x87
_VK.update({c: ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"})
_VK.update({"SPACE": 0x20, "TAB": 0x09, "HOME": 0x24, "END": 0x23,
            "PGUP": 0x21, "PGDN": 0x22, "INSERT": 0x2D, "DELETE": 0x2E})
_MODS = {"CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL,
         "ALT": MOD_ALT, "SHIFT": MOD_SHIFT}


def parse_hotkey(text: str) -> tuple[int, int]:
    """'Control+Shift+F4' -> (mods|MOD_NOREPEAT, vk). Raises ValueError."""
    parts = [p.strip().upper() for p in text.split("+")]
    if not parts or not parts[-1]:
        raise ValueError(f"empty key in hotkey: {text!r}")
    mods = MOD_NOREPEAT
    for part in parts[:-1]:
        if part in ("WIN", "WINDOWS", "SUPER"):
            raise ValueError("Win modifier is not supported")
        if part not in _MODS:
            raise ValueError(f"unknown modifier {part!r} in {text!r}")
        mods |= _MODS[part]
    key = parts[-1]
    if key not in _VK:
        raise ValueError(f"unknown key {key!r} in {text!r}")
    return (mods, _VK[key])


# Tk `event.state` modifier bit masks ON WINDOWS. Only these three are real
# hotkey modifiers here: Shift 0x0001, Control 0x0004, Alt 0x20000. 0x0008
# (Mod1) is NumLock on Windows Tk — it must NOT be read as Alt, or every keypress
# with NumLock on would spuriously gain an Alt modifier. Composed in the fixed
# Control+Alt+Shift order so the result matches parse_hotkey's tolerant parser.
_EVENT_MODS = ((0x0004, "Control"), (0x20000, "Alt"), (0x0001, "Shift"))

# Named keysyms → the token parse_hotkey understands (see _VK above).
_EVENT_KEYSYMS = {
    "space": "SPACE", "Tab": "TAB", "Prior": "PGUP", "Next": "PGDN",
    "Home": "HOME", "End": "END", "Insert": "INSERT", "Delete": "DELETE",
}

# Bare modifier / lock keysyms — a press of one of these alone is not a hotkey.
_EVENT_MODIFIER_KEYSYMS = frozenset({
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Win_L", "Win_R", "Meta_L", "Meta_R",
    "Caps_Lock", "Num_Lock", "Scroll_Lock",
})


def event_to_hotkey(keysym: str, state: int) -> "str | None":
    """Build a parse_hotkey-valid string from a Tk key event, or None.

    `keysym` is `event.keysym`, `state` is `event.state` (Windows Tk masks).
    Returns e.g. "Control+Shift+F4" for the capture widget. Bare modifier
    presses, unmapped keys, and anything parse_hotkey would reject all return
    None (the caller keeps waiting / shows an inline note). Every non-None
    result is validated by round-tripping through parse_hotkey."""
    if not keysym or keysym in _EVENT_MODIFIER_KEYSYMS:
        return None
    key = None
    if len(keysym) == 1 and keysym.isalpha():
        key = keysym.upper()
    elif len(keysym) == 1 and keysym.isdigit():
        key = keysym
    elif keysym[0] in "Ff" and keysym[1:].isdigit():   # F1..F24 (range checked below)
        key = "F" + keysym[1:]
    elif keysym in _EVENT_KEYSYMS:
        key = _EVENT_KEYSYMS[keysym]
    if key is None:
        return None
    parts = [name for mask, name in _EVENT_MODS if state & mask]
    parts.append(key)
    result = "+".join(parts)
    try:
        parse_hotkey(result)          # authoritative validation (VK range, modifiers)
    except ValueError:
        return None
    return result


def format_error(code: int) -> str:
    return ("in use by another application" if code == 1409
            else f"registration failed (error {code})")


class _RealBackend:  # pragma: no cover — exercised by spike S2 + live
    def __init__(self):
        self._u = ctypes.WinDLL("user32", use_last_error=True)
        self._k = ctypes.WinDLL("kernel32")
        self._u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                           wintypes.UINT, wintypes.UINT]
        self._u.RegisterHotKey.restype = wintypes.BOOL
        self._u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                               wintypes.WPARAM, wintypes.LPARAM]

    def current_thread_id(self):
        return self._k.GetCurrentThreadId()

    def register(self, hk_id, mods, vk):
        return bool(self._u.RegisterHotKey(None, hk_id, mods, vk))

    def last_error(self):
        return ctypes.get_last_error()

    def unregister(self, hk_id):
        self._u.UnregisterHotKey(None, hk_id)

    def get_message(self):
        msg = wintypes.MSG()
        r = self._u.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r <= 0:
            return (WM_QUIT, 0)
        return (msg.message, msg.wParam)

    def post_quit(self, thread_id):
        self._u.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)


class HotkeyService:
    """Owns the worker thread. Emits hotkey ids into .events (queue.Queue).
    NEVER touches shared app state — the Tk tick drains .events (house rule)."""

    def __init__(self, backend=None):
        self._backend = backend or _RealBackend()
        self.events: "queue.Queue[int]" = queue.Queue()
        self.failures: dict[int, int] = {}
        self._thread = None
        self._tid = None
        self._ready = threading.Event()
        # Last set handed to start()/restart(), kept verbatim so suspend() can
        # give the keys back without the caller re-deriving them, and so a
        # config change made WHILE suspended is what resume() registers.
        self._bindings: dict[int, tuple[int, int]] = {}
        self._suspended = False

    @property
    def suspended(self) -> bool:
        """True while the gate holds the bindings off the keyboard. Read-only:
        suspend()/resume() are the only writers, which is what keeps a redundant
        call cheap instead of a second registration."""
        return self._suspended

    def start(self, bindings: dict[int, tuple[int, int]]):
        """(Re)register `bindings` on a fresh worker thread.

        The set is REMEMBERED either way. While suspended nothing is registered
        — the new set is only recorded, and resume() is what puts it on the
        keyboard. That is what makes "suspend → user edits a hotkey → resume"
        land the NEW set rather than the stale one."""
        self.stop()
        self._bindings = dict(bindings)
        self.failures = {}
        if self._suspended:
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, args=(dict(bindings),), daemon=True,
            name="preview-hotkeys")
        self._thread.start()
        self._ready.wait(timeout=2)

    def restart(self, bindings):
        self.start(bindings)

    def suspend(self):
        """Release every registered key WITHOUT forgetting the bindings.

        Unregistering happens where registering did — on the worker thread, in
        its own `finally`, reached by the same WM_QUIT `stop()` posts. That is
        the module's threading model, not a shortcut: RegisterHotKey is
        thread-affine, so there is no way to unregister from the Tk thread.

        Idempotent by design: the caller flips only on a real state CHANGE, but
        a redundant suspend must still cost nothing and must never lose the
        bindings. `stop()` is deliberately left alone — it is the shutdown path
        and does not touch this flag, so a teardown of a suspended service is a
        plain no-op with no registrations and no bindings left behind."""
        if self._suspended:
            return
        self._suspended = True
        self.stop()

    def resume(self):
        """Put the remembered bindings back on the keyboard.

        Idempotent: a service that is not suspended returns immediately, so a
        redundant resume can never double-register. Registration failures land
        in `.failures` exactly as they do for the first registration — same
        code path, so callers surface them the same way."""
        if not self._suspended:
            return
        self._suspended = False
        self.start(self._bindings)

    def stop(self):
        if self._thread and self._thread.is_alive() and self._tid is not None:
            self._backend.post_quit(self._tid)
            self._thread.join(timeout=2)
        self._thread = None
        self._tid = None

    def _run(self, bindings):
        be = self._backend
        self._tid = be.current_thread_id()
        registered = []
        for hk_id, (mods, vk) in bindings.items():
            if be.register(hk_id, mods, vk):
                registered.append(hk_id)
            else:
                self.failures[hk_id] = be.last_error()
        self._ready.set()
        try:
            while True:
                msg, wparam = be.get_message()
                if msg == WM_QUIT:
                    break
                if msg == WM_HOTKEY:
                    self.events.put(int(wparam))
        finally:
            for hk_id in registered:
                be.unregister(hk_id)
