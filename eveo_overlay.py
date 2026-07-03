"""The single desktop-spanning transparent overlay Toplevel that draws activity
labels on Eve-O Preview thumbnails.

overrideredirect + -topmost + -transparentcolor key, then
WS_EX_LAYERED|TRANSPARENT|NOACTIVATE|TOOLWINDOW via the injectable win32 wrapper
=> click-through, no focus steal, no taskbar entry. retop() re-asserts
HWND_TOPMOST every controller tick (the topmost band is insertion-ordered, so
Eve-O's own thumbnails/label forms would otherwise cover us).

IMPORTANT — hwnd resolution: `Toplevel.winfo_id()` is Tk's *child* window
(class TkChild, WS_CHILD). Tk applies -transparentcolor / WS_EX_LAYERED to the
*parent* (TkTopLevel), and SetWindowPos(HWND_TOPMOST) on a WS_CHILD is a silent
z-order no-op. So `_hwnd()` resolves the true top-level via
GetAncestor(child, GA_ROOT) (GetParent fallback) before every style / topmost
call.

All Win32 is behind win32 (default _real_overlay_win32); tests inject a fake.
"""
from __future__ import annotations

import sys
import tkinter as tk

# Transparent color key: an unlikely near-black the app never draws with. The
# Toplevel bg is set to this and registered as -transparentcolor, so every
# pixel except the label text is fully click-through/invisible.
TRANSPARENT_KEY = "#010203"
SHADOW_COLOR = "#000000"
SHADOW_OFFSET = 1


class _RealOverlayWin32:
    """Real ctypes wrapper for the overlay window's layered/topmost styling."""

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    GA_ROOT = 2                     # GetAncestor: root top-level of the chain

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._user32 = ctypes.windll.user32
        self._get = getattr(self._user32, "GetWindowLongPtrW",
                            self._user32.GetWindowLongW)
        self._set = getattr(self._user32, "SetWindowLongPtrW",
                            self._user32.SetWindowLongW)
        self._get.restype = ctypes.c_ssize_t
        self._get.argtypes = [wintypes.HWND, ctypes.c_int]
        self._set.restype = ctypes.c_ssize_t
        self._set.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._user32.SetWindowPos.restype = wintypes.BOOL
        self._user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        # Top-level resolution: Tk hands us the TkChild (WS_CHILD) window; the
        # styled/z-ordered window is its top-level ancestor.
        self._user32.GetParent.restype = wintypes.HWND
        self._user32.GetParent.argtypes = [wintypes.HWND]
        self._user32.GetAncestor.restype = wintypes.HWND
        self._user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]

    def get_parent(self, hwnd: int) -> int:
        return int(self._user32.GetParent(hwnd) or 0)

    def get_ancestor(self, hwnd: int, flag: int) -> int:
        return int(self._user32.GetAncestor(hwnd, flag) or 0)

    def make_click_through(self, hwnd: int) -> None:
        ex = int(self._get(hwnd, self.GWL_EXSTYLE))
        ex |= (self.WS_EX_LAYERED | self.WS_EX_TRANSPARENT
               | self.WS_EX_NOACTIVATE | self.WS_EX_TOOLWINDOW)
        self._set(hwnd, self.GWL_EXSTYLE, ex)

    def set_topmost(self, hwnd: int) -> None:
        self._user32.SetWindowPos(
            hwnd, self.HWND_TOPMOST, 0, 0, 0, 0,
            self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE)


_REAL_OVERLAY_WIN32 = None


def _real_overlay_win32():
    global _REAL_OVERLAY_WIN32
    if _REAL_OVERLAY_WIN32 is None:
        _REAL_OVERLAY_WIN32 = _RealOverlayWin32()
    return _REAL_OVERLAY_WIN32


class OverlayWindow:
    """One transparent, click-through, topmost Toplevel spanning the virtual
    desktop, drawing one text label per thumbnail rect."""

    def __init__(self, root, palette: dict, win32=None):
        self._root = root
        self._font_size = int(palette.get("font_size", 11))
        self._color = palette.get("color", "#00d4ff")
        self._anchor = palette.get("anchor", "top-left")
        self._transparent_key = palette.get("transparent_key", TRANSPARENT_KEY)
        self._win32 = win32
        self._styled = False
        self.visible = False

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.configure(bg=self._transparent_key)
        try:
            self.top.attributes("-topmost", True)
            self.top.attributes("-transparentcolor", self._transparent_key)
        except tk.TclError:
            pass
        # Span the whole virtual desktop so any thumbnail on any monitor is
        # covered. winfo_screenwidth/height is the primary screen; good enough
        # as a default and correct on single-monitor. Multi-monitor spanning is
        # handled by _place_fullspan() using Win32 virtual metrics when real.
        self._place_fullspan()

        self._canvas = tk.Canvas(
            self.top, bg=self._transparent_key, highlightthickness=0, bd=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self.top.withdraw()

    # ── geometry ────────────────────────────────────────────────────────────
    def _place_fullspan(self):
        x0, y0, w, h = self._virtual_bounds()
        self.top.geometry(f"{w}x{h}+{x0}+{y0}")
        self._origin = (x0, y0)

    def _virtual_bounds(self):
        """(x0, y0, width, height) of the full virtual desktop. Uses Win32
        SM_*VIRTUALSCREEN on Windows; falls back to the primary Tk screen."""
        if sys.platform == "win32":
            try:
                import ctypes
                gsm = ctypes.windll.user32.GetSystemMetrics
                x0, y0 = gsm(76), gsm(77)
                w, h = gsm(78), gsm(79)
                if w > 0 and h > 0:
                    return x0, y0, w, h
            except Exception:
                pass
        return (0, 0, self._root.winfo_screenwidth(),
                self._root.winfo_screenheight())

    # ── styling / z-order ───────────────────────────────────────────────────
    def _resolve_win32(self):
        """The active win32 wrapper (injected fake, or the real singleton on
        Windows), or None headless."""
        if self._win32 is not None:
            return self._win32
        return _real_overlay_win32() if sys.platform == "win32" else None

    def _ensure_styled(self):
        if self._styled:
            return
        w32 = self._resolve_win32()
        if w32 is None:
            self._styled = True
            return
        try:
            hwnd = self._hwnd(w32)
            w32.make_click_through(hwnd)
            self._styled = True
        except Exception:
            self._styled = True     # never retry-storm; give up for the session

    def _hwnd(self, w32=None) -> int:
        """Resolve the REAL top-level HWND to style / z-order.

        `self.top.winfo_id()` is Tk's TkChild window (class TkChild, WS_CHILD).
        Tk applies -transparentcolor / WS_EX_LAYERED to the PARENT (TkTopLevel),
        and SetWindowPos(HWND_TOPMOST) on a WS_CHILD is a silent no-op for
        z-order. So we walk up to the true top-level: GetAncestor(child, GA_ROOT)
        first (correct even for deeper chains), GetParent as a fallback, and the
        child id itself as a last resort so the call is never made with 0."""
        child = int(self.top.winfo_id())
        if w32 is None:
            w32 = self._resolve_win32()
        if w32 is None:
            return child
        try:
            root = int(w32.get_ancestor(child, 2) or 0)   # GA_ROOT = 2
            if root:
                return root
        except Exception:
            pass
        try:
            parent = int(w32.get_parent(child) or 0)
            if parent:
                return parent
        except Exception:
            pass
        return child

    def retop(self):
        """Re-assert HWND_TOPMOST. Called every controller tick."""
        if not self.visible:
            return
        w32 = self._resolve_win32()
        if w32 is None:
            return
        try:
            w32.set_topmost(self._hwnd(w32))
        except Exception:
            pass

    # ── style setters (live-applied from settings) ──────────────────────────
    def set_font_size(self, size: int):
        self._font_size = int(size)

    def set_color(self, color: str):
        self._color = color

    def set_anchor(self, anchor: str):
        self._anchor = anchor

    # ── drawing ─────────────────────────────────────────────────────────────
    def _anchor_xy(self, rect):
        l, t, r, b = rect
        ox, oy = self._origin
        pad = 4
        # translate physical desktop coords into the overlay's local space
        l -= ox; t -= oy; r -= ox; b -= oy
        if self._anchor == "top-left":
            return l + pad, t + pad, "nw"
        if self._anchor == "top-right":
            return r - pad, t + pad, "ne"
        if self._anchor == "bottom-left":
            return l + pad, b - pad, "sw"
        if self._anchor == "bottom-right":
            return r - pad, b - pad, "se"
        return l + pad, t + pad, "nw"

    def set_labels(self, items):
        """Redraw all labels. `items` = list of (rect, text). Empty text is
        skipped. When nothing is drawn, the window is withdrawn entirely."""
        self._canvas.delete("all")
        drawn = 0
        font = ("Consolas", self._font_size, "bold")
        for rect, text in items or ():
            if not text:
                continue
            x, y, anchor = self._anchor_xy(rect)
            # dark offset shadow, then foreground (no boxy background)
            self._canvas.create_text(
                x + SHADOW_OFFSET, y + SHADOW_OFFSET, text=text, anchor=anchor,
                fill=SHADOW_COLOR, font=font)
            self._canvas.create_text(
                x, y, text=text, anchor=anchor, fill=self._color, font=font)
            drawn += 1
        if drawn:
            self._place_fullspan()
            self.top.deiconify()
            self.visible = True
            self._ensure_styled()
            self.retop()
        else:
            self.top.withdraw()
            self.visible = False

    def destroy(self):
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        self.visible = False
