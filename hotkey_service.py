"""Global hotkeys via RegisterHotKey on a dedicated daemon thread.

Verified facts this design rests on (do not "simplify" them away):
- RegisterHotKey(hWnd=NULL) posts WM_HOTKEY to the REGISTERING THREAD's queue;
  no window needed. Tk's pump cannot see WM_HOTKEY — hence this thread.
- Registration is thread-affine: register/unregister must happen ON the worker.
  Config changes therefore restart the thread (simplest correct lifecycle).
- Matched keystrokes are swallowed system-wide (EVE never sees them) — that is
  the desired focus-key behavior AND the reason defaults ship EMPTY.
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

    def start(self, bindings: dict[int, tuple[int, int]]):
        self.stop()
        self.failures = {}
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, args=(dict(bindings),), daemon=True,
            name="preview-hotkeys")
        self._thread.start()
        self._ready.wait(timeout=2)

    def restart(self, bindings):
        self.start(bindings)

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
