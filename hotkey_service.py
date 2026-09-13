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
# Numpad virtual-key codes (VK_NUMPAD0..VK_NUMPAD9 = 0x60..0x69, then the
# operator keys). IMPORTANT Windows fact, MEASURED 2026-09-13 by injecting
# real win32 key events into a live Tk 8.6.15 root (py3.12 + py3.13): Windows
# Tk never emits `KP_*` keysyms at all — a numpad digit with NumLock ON
# arrives as the PLAIN digit keysym (e.g. "1") with `event.keycode` set to
# the VK_NUMPAD* code, and the operator keys arrive as ordinary symbol
# keysyms ("plus"/"minus"/"asterisk"/"slash"/"period") with their own
# keycodes. `event.keycode` is therefore the ONLY way to tell a numpad press
# from the main-row digit/symbol it shares a keysym with — see
# event_to_hotkey, which decides by keycode first. RegisterHotKey fires on
# these VK codes ONLY while NumLock is ON — with NumLock off, the SAME
# physical keys send the nav-cluster VK codes (Home/End/arrows/Insert/
# Delete/Clear) instead, indistinguishable from the dedicated main-block key
# except by the "extended" state bit (see _EXTENDED_KEY below), which is why
# event_to_hotkey rejects a numpad-with-NumLock-off press outright rather
# than aliasing it to HOME/END/etc. No NumpadEnter: VK_RETURN is shared with
# the main Enter key and RegisterHotKey can't tell them apart either. No
# +/-/*// symbol tokens in the parser — parse_hotkey splits on "+", so only
# the word spellings below are accepted.
_VK.update({f"NUMPAD{i}": 0x60 + i for i in range(10)})       # NUMPAD0=0x60 … NUMPAD9=0x69
_VK.update({"NUMPADMULTIPLY": 0x6A, "NUMPADADD": 0x6B,
            "NUMPADSUBTRACT": 0x6D, "NUMPADDECIMAL": 0x6E, "NUMPADDIVIDE": 0x6F})
# .NET `Keys` enum spellings EVE-O Preview writes into EVE-O-Preview.json
# ("NumPad1" upper-cases to NUMPAD1 already via parse_hotkey; these four are
# the ones that don't).
_VK.update({"MULTIPLY": 0x6A, "ADD": 0x6B, "SUBTRACT": 0x6D,
            "DECIMAL": 0x6E, "DIVIDE": 0x6F})
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
    vk = _VK[key]
    # MEASURED 2026-09-13 (scancode injection): with NumLock on, holding
    # Shift over a numpad DIGIT or Numpad. makes Windows send the
    # NAV-cluster VK code instead of VK_NUMPAD*/VK_DECIMAL (the historical
    # "Shift temporarily cancels NumLock" behavior) — with the Shift bit
    # itself STRIPPED from the event — so RegisterHotKey(MOD_SHIFT,
    # VK_NUMPAD*/VK_DECIMAL) can never fire. The operator keys (+ - * /) are
    # NOT NumLock-multiplexed and are unaffected: Shift+NumpadAdd delivers
    # VK_ADD with the Shift bit SET, same as any other key, so only digits
    # and Decimal are rejected here. Control/Alt are unaffected for all of
    # them and stay allowed.
    if (vk in range(0x60, 0x6A) or vk == 0x6E) and mods & MOD_SHIFT:
        raise ValueError(
            "Shift cannot be combined with a numpad digit or Numpad. "
            "(Windows sends the navigation keys instead)")
    return (mods, vk)


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

# Windows Tk `event.state` "extended key" bit. MEASURED 2026-09-13 (real
# win32 key events injected into a live Tk 8.6.15 root, py3.12 + py3.13): SET
# on the dedicated main-block nav cluster (Home/End/PageUp/PageDown/arrows/
# Insert/Delete) — and also SET on numpad `/` (VK_DIVIDE, 0x6F) and numpad
# Enter (VK_RETURN, 0x0D) even though those originate from the numpad, so
# this bit alone does not mean "not the numpad" in general, only for the
# nav-cluster VK codes below. (Right-hand Ctrl/Alt/Enter are documented
# Windows E0-scancode keys and would be expected to behave the same way —
# not independently measured here.) CLEAR when a nav-cluster keysym+keycode
# pair instead comes from the numpad with NumLock off (numpad-7-as-Home
# sends keysym "Home", keycode 0x24 — same as the real Home key — with this
# bit clear; numpad-5-as-Clear sends keysym "Clear", keycode 0x0C, with no
# main-block equivalent at all). Within `_NUMPAD_NAV_VKS` it is the ONLY
# discriminator between the dedicated main-block key and the numpad's
# NumLock-off alias; keysym and keycode alone are ambiguous there.
_EXTENDED_KEY = 0x40000

# Numpad digit VK codes reachable via `event.keycode` (NumLock on; see the
# _VK numpad block above for the measured facts this rests on).
_NUMPAD_VK_DIGITS = range(0x60, 0x6A)          # VK_NUMPAD0..VK_NUMPAD9
# Numpad operator VK codes → the canonical "Numpad<Op>" token.
_NUMPAD_VK_OPS = {
    0x6A: "NumpadMultiply", 0x6B: "NumpadAdd", 0x6D: "NumpadSubtract",
    0x6E: "NumpadDecimal", 0x6F: "NumpadDivide",
}
# Nav-cluster VK codes the numpad ALSO sends when NumLock is off (PgUp/PgDn/
# End/Home/Left/Up/Right/Down/Insert/Delete/Clear-for-5) — ambiguous with the
# main-block key at the same VK code except for _EXTENDED_KEY (see above). A
# press of one of these with the extended bit clear is a numpad key with
# NumLock off: unbindable (RegisterHotKey's VK_NUMPAD*/VK_DECIMAL codes only
# fire with NumLock on), so event_to_hotkey rejects it outright instead of
# silently aliasing it to HOME/END/etc — which would also fire for the
# dedicated main-block keys.
_NUMPAD_NAV_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                             0x2D, 0x2E, 0x0C})

# Bare modifier / lock keysyms — a press of one of these alone is not a hotkey.
_EVENT_MODIFIER_KEYSYMS = frozenset({
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Win_L", "Win_R", "Meta_L", "Meta_R",
    "Caps_Lock", "Num_Lock", "Scroll_Lock",
})


def event_to_hotkey(keysym: str, state: int, keycode: "int | None" = None) -> "str | None":
    """Build a parse_hotkey-valid string from a Tk key event, or None.

    `keysym` is `event.keysym`, `state` is `event.state` (Windows Tk masks).
    `keycode` is `event.keycode` (the raw VK code) — optional and defaulting
    to None for backward compatibility; every existing 2-arg call site keeps
    working exactly as before (numpad detection below is skipped entirely
    when keycode is None). Returns e.g. "Control+Shift+F4" for the capture
    widget. Bare modifier presses, unmapped keys, and anything parse_hotkey
    would reject all return None (the caller keeps waiting / shows an inline
    note). Every non-None result is validated by round-tripping through
    parse_hotkey.

    Numpad detection is keycode-FIRST, decided before the ordinary
    alphanumeric/keysym branches: Windows Tk never emits `KP_*` keysyms
    (measured fact, see _VK's numpad comment) — a numpad digit with NumLock
    on arrives as the PLAIN digit/symbol keysym, identical to the main-row
    key, so `keycode` (the VK code) is the only reliable discriminator. A
    numpad key pressed with NumLock OFF sends the same VK code as a
    main-block nav key (Home/End/etc); `_EXTENDED_KEY` in `state` is what
    tells them apart, and the NumLock-off numpad case returns None outright
    (RegisterHotKey's VK_NUMPAD*/VK_DECIMAL codes only fire with NumLock on)."""
    if not keysym or keysym in _EVENT_MODIFIER_KEYSYMS:
        return None
    key = None
    if keycode is not None:
        if keycode in _NUMPAD_VK_DIGITS:
            key = "Numpad" + str(keycode - 0x60)
        elif keycode in _NUMPAD_VK_OPS:
            key = _NUMPAD_VK_OPS[keycode]
        elif keycode in _NUMPAD_NAV_VKS and not (state & _EXTENDED_KEY):
            return None                    # numpad nav key, NumLock off: unbindable
    if key is None:
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


def capture_hint(keysym: str, state: int = 0, keycode: "int | None" = None) -> str:
    """Message for the hotkey capture box after
    event_to_hotkey(keysym, state, keycode) returned None.

    `state`/`keycode` default so existing (keysym-only) callers still get the
    generic message. When `keycode` names one of the numpad-shared nav VK
    codes (_NUMPAD_NAV_VKS) AND the extended bit is clear — i.e. event_to_hotkey
    rejected this as a numpad DIGIT or Numpad. pressed with NumLock off (the
    operator keys +-*/ never land in _NUMPAD_NAV_VKS, since they aren't
    NumLock-multiplexed) — give a targeted nudge instead of the generic
    "not a usable hotkey" line. Shift is called out too: Windows substitutes
    this same nav-cluster VK the moment Shift is held over a numpad digit or
    Numpad. even with NumLock on — see parse_hotkey's narrower Shift
    rejection — so a capture landing here from that path gets the same
    actionable wording; that Shift inference was not independently
    re-measured the way the plain NumLock-off case was."""
    if (keycode is not None and keycode in _NUMPAD_NAV_VKS
            and not (state & _EXTENDED_KEY)):
        return ("numpad digits and Numpad. work as hotkeys only with NumLock "
                "on and without Shift; turn NumLock on and press again.")
    return f"'{keysym}' is not a usable hotkey — try again."


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
