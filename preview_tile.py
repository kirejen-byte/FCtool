"""One native-preview tile: an opaque, override-redirect Tk Toplevel hosting a
DWM live thumbnail under a ~20px caption strip.

Design (spec §5):
- Opaque Toplevel, overrideredirect(True); ex-styles WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE
  applied to the GA_ROOT top-level hwnd ONCE, before the first map (no LAYERED,
  no TRANSPARENT, no -transparentcolor — those are overlay-only).
- A caption strip (tk.Frame height STRIP_H) with name/dot/chip/tag labels, over a
  black body frame. The DWM thumbnail is composited by the compositor OVER the
  body area; it is letterboxed so it never covers the strip.
- Placement is ONLY via win32.set_window_pos(GA_ROOT, x, y, w, h) in physical px —
  never Tk geometry (Tk geometry is logical px under PMv2 and would misplace).
- Mouse model (EVE-O parity): LEFT click/release = activate (Ctrl = minimize);
  RIGHT drag = move; RIGHT drag + Ctrl (or + left held) = resize. All callbacks
  receive the tile's char_key; the controller owns snapping/persistence.

All Win32 is behind an injectable backend (default _real_tile_win32); tests inject
a fake. Tk-thread only touches this object (house rule #1).
"""
from __future__ import annotations

import sys

import tkinter as tk

import preview_layout
from dwm_thumbs import Thumbnail, aspect_fit

STRIP_H = 20
_MOVE_JITTER = 4          # left-release within this many px still counts as a click
_CORNER_ZONE = 12         # px hot-zone at each tile corner that arms a resize
_MIN_W = 120              # min tile width (physical px)
_MIN_BODY_H = 68          # min body height (physical px); strip height is constant

# Tk diagonal-resize cursor names (Windows): NW/SE share one, NE/SW the other.
_CORNER_CURSOR = {"nw": "size_nw_se", "se": "size_nw_se",
                  "ne": "size_ne_sw", "sw": "size_ne_sw"}


class _RealTileWin32:  # pragma: no cover — mirror _RealOverlayWin32 minus TRANSPARENT
    """Real ctypes wrapper for the tile window's tool/no-activate styling and
    physical-pixel placement. Mirrors eveo_overlay._RealOverlayWin32 but WITHOUT
    WS_EX_LAYERED / WS_EX_TRANSPARENT (tiles are opaque and clickable)."""

    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    GA_ROOT = 2

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
        self._user32.GetParent.restype = wintypes.HWND
        self._user32.GetParent.argtypes = [wintypes.HWND]
        self._user32.GetAncestor.restype = wintypes.HWND
        self._user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]

    def get_root_hwnd(self, tk_id: int) -> int:
        """Resolve the REAL top-level HWND from the TkChild winfo_id().
        GetAncestor(child, GA_ROOT) first, GetParent fallback, child last resort."""
        child = int(tk_id)
        try:
            root = int(self._user32.GetAncestor(child, self.GA_ROOT) or 0)
            if root:
                return root
        except Exception:
            pass
        try:
            parent = int(self._user32.GetParent(child) or 0)
            if parent:
                return parent
        except Exception:
            pass
        return child

    def make_tool_noactivate(self, hwnd: int) -> None:
        ex = int(self._get(hwnd, self.GWL_EXSTYLE))
        ex |= (self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE)
        self._set(hwnd, self.GWL_EXSTYLE, ex)

    def set_window_pos(self, hwnd: int, x: int, y: int, w: int, h: int) -> None:
        self._user32.SetWindowPos(hwnd, self.HWND_TOPMOST, x, y, w, h,
                                  self.SWP_NOACTIVATE)

    def retop(self, hwnd: int) -> None:
        self._user32.SetWindowPos(
            hwnd, self.HWND_TOPMOST, 0, 0, 0, 0,
            self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE)


_REAL_TILE_WIN32 = None


def _real_tile_win32():  # pragma: no cover
    global _REAL_TILE_WIN32
    if _REAL_TILE_WIN32 is None:
        _REAL_TILE_WIN32 = _RealTileWin32()
    return _REAL_TILE_WIN32


class TileWindow:
    """One live-preview tile for one EVE client. Tk-thread only."""

    def __init__(self, root, char_key, palette, win32=None, dwm=None,
                 on_activate=None, on_minimize=None, on_move_end=None,
                 on_resize_end=None, on_exclude=None, on_switch_external=None,
                 lock_layout=False):
        self._win32 = win32 or _real_tile_win32()
        self._lock_layout = bool(lock_layout)   # when True, all drag-moves are no-ops
        self._dwm_backend = dwm
        self._key = char_key
        self._palette = palette
        self._on_activate = on_activate or (lambda k: None)
        self._on_minimize = on_minimize or (lambda k: None)
        self._on_move_end = on_move_end or (lambda k, x, y: None)
        self._on_resize_end = on_resize_end or (lambda k, w, h: None)
        self._on_exclude = on_exclude or (lambda k: None)          # Shift+Left
        self._on_switch_external = on_switch_external or (lambda: None)  # Ctrl+Shift+Left
        self._excluded = False        # session-only cycle-exclusion flag (C4)

        self._thumb = None
        self._src_size = (0, 0)
        self._w = 0
        self._body_h = 0
        self._pos = (0, 0)            # last-placed top-left (physical px)
        self._badge = None
        self._hidden = False          # withdrawn by a C2 hide rule (layout kept)

        # hover / opacity / zoom state (Task C1)
        self._alpha = 1.0             # last requested window alpha (mirror for tests)
        self._opacity_inactive = 1.0
        self._opacity_hover = 1.0
        self._active = False          # last-activated / foreground EVE client tile
        self._hovering = False
        self._zoom_enabled = False
        self._zoom_factor = 2.0
        self._zoom_anchor = "nw"
        self._zoomed = False          # currently displaying a zoomed rect
        self._prezoom_rect = None     # (x, y, w, body_h) to restore on <Leave>

        # drag state
        self._press_root = None       # (x_root, y_root) at button-3 press
        self._press_pos = None        # (x, y) tile position at press
        self._press_size = None       # (w, body_h) at press
        self._mode = None             # None | "move" | "resize"

        # LEFT-drag-on-strip (title-bar) move state (BUG B). Separate from the
        # body left-click (activate) path and from the right-drag move path.
        self._strip_press_root = None  # (x_root, y_root) at strip button-1 press
        self._strip_press_pos = None   # (x, y) tile position at strip press
        self._strip_moving = False     # True once a strip left-drag passed jitter

        # corner-hover resize state (this task). `_corner` is the currently-armed
        # corner under the cursor ('nw'/'ne'/'sw'/'se'/None); once a left-press
        # lands on an armed corner, `_corner_resizing` gates the strip-move and
        # body-activate paths so a corner drag never doubles as a move/click.
        self._corner = None            # armed corner under the pointer, or None
        self._corner_resizing = False  # True while a corner drag is in progress
        self._corner_anchor = None     # (ax, ay) opposite corner, fixed (physical)
        self._corner_press_root = None # (x_root, y_root) at the corner press
        self._corner_press_size = None # (w, body_h) at the corner press

        bg_panel = palette.get("BG_PANEL", "#16213e")
        bg_dark = palette.get("BG_DARK", "#1a1a2e")
        fg_text = palette.get("FG_TEXT", "#e0e0e0")
        fg_accent = palette.get("FG_ACCENT", "#00d4ff")
        fg_dim = palette.get("FG_DIM", "#808090")
        self._fg_text = fg_text
        self._fg_dim = fg_dim
        self._fg_accent = fg_accent

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.configure(bg="#000000")
        self.top.withdraw()

        # ── caption strip ───────────────────────────────────────────────────
        self._strip = tk.Frame(self.top, bg=bg_panel, height=STRIP_H)
        self._strip.pack(fill="x", side="top")
        self._strip.pack_propagate(False)

        self._dot = tk.Canvas(self._strip, width=10, height=10, bg=bg_panel,
                              highlightthickness=0, bd=0)
        self._dot.pack(side="left", padx=(4, 3))
        self._dot_item = self._dot.create_oval(1, 1, 9, 9, fill=bg_panel,
                                               outline="")

        self._name_lbl = tk.Label(self._strip, text="", bg=bg_panel, fg=fg_text,
                                   font=("Consolas", 9, "bold"))
        self._name_lbl.pack(side="left")

        # cycle-exclusion marker (C4): shown only while the tile is excluded from
        # hotkey cycling (Shift+Left toggles it). A dim dot glyph beside the name.
        self._excl_lbl = tk.Label(self._strip, text="", bg=bg_panel, fg=fg_dim,
                                  font=("Consolas", 9, "bold"))
        self._excl_lbl.pack(side="left", padx=(2, 0))

        self._tag_lbl = tk.Label(self._strip, text="", bg=bg_panel, fg=fg_dim,
                                 font=("Consolas", 8))
        self._tag_lbl.pack(side="right", padx=(0, 4))

        self._chip_lbl = tk.Label(self._strip, text="", bg=bg_panel, fg=fg_accent,
                                  font=("Consolas", 8, "bold"))
        self._chip_lbl.pack(side="right", padx=(0, 4))

        # ── body (DWM composites the live thumbnail over this) ──────────────
        self._body = tk.Frame(self.top, bg="#000000")
        self._body.pack(fill="both", expand=True, side="top")

        # caption text mirror for tests / logging (badge overrides name display)
        self._name = ""
        self._chip = ""
        self._tag = ""

        self.top.update_idletasks()
        self._hwnd = self._win32.get_root_hwnd(self.top.winfo_id())
        self._win32.make_tool_noactivate(self._hwnd)   # BEFORE first map

        self._bind_mouse()
        # Enter/Leave on the toplevel — child widgets fire their own Enter/Leave,
        # so bind on self.top and rely on Tk's toplevel-level crossing events.
        self.top.bind("<Enter>", self._on_enter)
        self.top.bind("<Leave>", self._on_leave)

    # ── mouse model (EVE-O parity) ──────────────────────────────────────────
    def _bind_mouse(self):
        # RIGHT-drag move/resize + LEFT click-activate are bound on every widget.
        for w in (self.top, self._strip, self._body, self._name_lbl,
                  self._chip_lbl, self._tag_lbl, self._dot):
            w.bind("<Button-3>", self._on_b3_press)
            w.bind("<B3-Motion>", self._on_b3_motion)
            w.bind("<ButtonRelease-3>", self._on_b3_release)
        # LEFT on the BODY (and toplevel) = activate/minimize/etc (click semantics).
        for w in (self.top, self._body):
            w.bind("<Button-1>", self._on_b1_press)
            w.bind("<ButtonRelease-1>", self._on_b1_release)
        # LEFT on the CAPTION STRIP = title-bar drag-to-move; a plain click there
        # still activates (BUG B). The strip cluster (strip + its child labels)
        # gets the strip handlers so a left-drag anywhere on the caption moves.
        for w in (self._strip, self._name_lbl, self._excl_lbl, self._chip_lbl,
                  self._tag_lbl, self._dot):
            w.bind("<ButtonPress-1>", self._on_strip_b1_press)
            w.bind("<B1-Motion>", self._on_strip_b1_motion)
            w.bind("<ButtonRelease-1>", self._on_strip_b1_release)
        # Corner-hover resize: a plain <Motion> over any strip/body widget arms
        # the nearest corner (12 px zone) and swaps the cursor to the matching
        # diagonal-resize glyph. The armed corner + a left press start a resize.
        for w in (self.top, self._strip, self._body, self._name_lbl,
                  self._excl_lbl, self._chip_lbl, self._tag_lbl, self._dot):
            w.bind("<Motion>", self._on_corner_motion, add="+")
        # A corner drag that begins on the BODY (or toplevel) needs its own
        # B1-Motion/Release routing — the body has no strip-move handlers, and
        # the strip cluster's B1 handlers already defer to _corner_resizing.
        for w in (self.top, self._body):
            w.bind("<B1-Motion>", self._corner_motion, add="+")
            w.bind("<ButtonRelease-1>", self._corner_release, add="+")

    def _on_b1_press(self, event):
        # A press that lands on an armed corner starts a resize instead of the
        # click-activate path; consume it so no activate fires on release.
        if self._corner is not None and not self._lock_layout:
            self._corner_press(event)
            self._b1_press_root = None
            return
        self._b1_press_root = (event.x_root, event.y_root)

    def _on_b1_release(self, event):
        press = getattr(self, "_b1_press_root", None)
        self._b1_press_root = None
        if press is None:
            return
        dx = abs(event.x_root - press[0])
        dy = abs(event.y_root - press[1])
        if dx > _MOVE_JITTER or dy > _MOVE_JITTER:
            return  # was a drag, not a click
        ctrl = bool(event.state & 0x0004)
        shift = bool(event.state & 0x0001)
        # Modifier ladder (EVE-O parity + FCTool C4 extras). Check the two-modifier
        # combo first so Ctrl+Shift never falls through to a single-modifier branch.
        if ctrl and shift:                  # Ctrl+Shift+Left → switch to last non-EVE window
            self._on_switch_external()
        elif shift:                         # Shift+Left → toggle cycle-exclusion
            self._on_exclude(self._key)
        elif ctrl:                          # Ctrl+Left → minimize
            self._on_minimize(self._key)
        else:                               # plain Left → activate
            self._on_activate(self._key)

    # ── LEFT-drag-to-move on the caption strip (title-bar semantics, BUG B) ──
    def set_lock_layout(self, flag):
        """When True, BOTH left-drag (strip) and right-drag moves are no-ops. The
        controller pushes this from the `lock_layout` config so a locked layout
        can't be nudged by an instinctive drag."""
        self._lock_layout = bool(flag)

    def _on_strip_b1_press(self, event):
        # A press on an armed corner starts a resize, not a strip-move. Consume it
        # so neither the move nor the click-activate path runs for this gesture.
        if self._corner is not None and not self._lock_layout:
            self._corner_press(event)
            self._strip_press_root = None
            return
        # Record the press so a left-drag on the caption can move the tile. A plain
        # click (release within jitter) falls through to the activate ladder. Anchor
        # on self._pos (the authoritative last-placed PHYSICAL top-left) rather than
        # Tk winfo_root* — Tk geometry is logical px under PMv2 and would misplace.
        self._strip_press_root = (event.x_root, event.y_root)
        self._strip_press_pos = self._pos
        self._strip_moving = False

    def _on_strip_b1_motion(self, event):
        if self._corner_resizing:
            self._corner_motion(event)
            return
        press = self._strip_press_root
        if press is None or self._lock_layout:
            return  # not tracking, or layout locked → no move
        dx = event.x_root - press[0]
        dy = event.y_root - press[1]
        if not self._strip_moving:
            if abs(dx) <= _MOVE_JITTER and abs(dy) <= _MOVE_JITTER:
                return  # still within jitter → treat as a click, not a drag yet
            self._strip_moving = True
        x = self._strip_press_pos[0] + dx
        y = self._strip_press_pos[1] + dy
        self._pos = (x, y)
        # SAME Win32 physical-px placement _on_b3_motion uses (GA_ROOT hwnd).
        self._win32.set_window_pos(self._hwnd, x, y, self._w,
                                   self._body_h + STRIP_H)

    def _on_strip_b1_release(self, event):
        if self._corner_resizing:
            self._corner_release(event)
            return
        press = self._strip_press_root
        self._strip_press_root = None
        if press is None:
            return
        if self._strip_moving:
            self._strip_moving = False
            if self._lock_layout:
                return
            dx = event.x_root - press[0]
            dy = event.y_root - press[1]
            x = self._strip_press_pos[0] + dx
            y = self._strip_press_pos[1] + dy
            self._pos = (x, y)
            self._on_move_end(self._key, x, y)
            return
        # No drag → a plain caption click: run the same activate/modifier ladder
        # as a body left-click (reuse the _on_b1_release semantics). Seed the
        # press anchor from the strip press so the jitter check inside sees a
        # click, then dispatch the release (which reads modifiers off `event`).
        self._b1_press_root = press
        self._on_b1_release(event)

    # ── corner-hover resize (any of the 4 corners; opposite corner anchored) ──
    def _detect_corner(self, event):
        """Which corner (if any) the pointer is over, as an inset zone. Uses the
        pointer position relative to the tile's PHYSICAL top-left (self._pos) vs
        the tile's known w / (body_h + STRIP_H). This matches the move/resize
        gestures which also delta event.x_root against the physical self._pos —
        under PMv2 the tile's monitor makes Tk root coords 1:1 with physical px,
        so no scale factor is needed (FLAG: re-verify on mixed-DPI monitors)."""
        w = self._w
        h = self._body_h + STRIP_H
        if w <= 0 or h <= 0:
            return None
        lx = event.x_root - self._pos[0]
        ly = event.y_root - self._pos[1]
        if lx < 0 or ly < 0 or lx > w or ly > h:
            return None
        left = lx <= _CORNER_ZONE
        right = lx >= w - _CORNER_ZONE
        top = ly <= _CORNER_ZONE
        bottom = ly >= h - _CORNER_ZONE
        if top and left:
            return "nw"
        if top and right:
            return "ne"
        if bottom and left:
            return "sw"
        if bottom and right:
            return "se"
        return None

    def _set_cursor(self, cursor):
        try:
            self.top.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _on_corner_motion(self, event):
        """Plain hover: arm the corner under the pointer (unless locked or already
        mid-resize) and swap the cursor to the matching diagonal-resize glyph.
        Moving off every corner disarms and restores the normal cursor."""
        if self._corner_resizing:
            return
        if self._lock_layout:
            if self._corner is not None:
                self._corner = None
                self._set_cursor("")
            return
        corner = self._detect_corner(event)
        if corner == self._corner:
            return
        self._corner = corner
        self._set_cursor(_CORNER_CURSOR.get(corner, "") if corner else "")

    def _corner_press(self, event):
        """Begin a corner resize: capture the OPPOSITE corner as a fixed anchor
        (physical px, from self._pos + current w/h) and enter resize mode. The
        strip-move and activate paths are already gated on _corner_resizing."""
        if self._lock_layout or self._corner is None:
            return
        x, y = self._pos
        w, h = self._w, self._body_h + STRIP_H
        # anchor = the corner OPPOSITE the grabbed one; it stays put through drag.
        ax = x if self._corner in ("ne", "se") else x + w
        ay = y if self._corner in ("sw", "se") else y + h
        self._corner_anchor = (ax, ay)
        self._corner_press_root = (event.x_root, event.y_root)
        self._corner_press_size = (self._w, self._body_h)
        self._corner_resizing = True

    def _corner_motion(self, event):
        if not self._corner_resizing or self._lock_layout:
            return
        dx = event.x_root - self._corner_press_root[0]
        dy = event.y_root - self._corner_press_root[1]
        w0, body0 = self._corner_press_size
        # Grabbed edge moves with the pointer; the OTHER edge is the fixed anchor.
        # West/North corners invert the delta (drag right/down shrinks them).
        if self._corner in ("nw", "sw"):
            w = w0 - dx
        else:
            w = w0 + dx
        if self._corner in ("nw", "ne"):
            body_h = body0 - dy
        else:
            body_h = body0 + dy
        w = max(_MIN_W, int(w))
        body_h = max(_MIN_BODY_H, int(body_h))
        ax, ay = self._corner_anchor
        # Left edge = anchor_x for E-anchored (ne/se) grabs; else anchor_x - w.
        x = ax if self._corner in ("ne", "se") else ax - w
        y = ay if self._corner in ("sw", "se") else ay - (body_h + STRIP_H)
        self._w, self._body_h = w, body_h
        self._pos = (x, y)
        self._win32.set_window_pos(self._hwnd, x, y, w, body_h + STRIP_H)
        self._push_thumb_rect()

    def _corner_release(self, event):
        if not self._corner_resizing:
            return
        self._corner_resizing = False
        self._corner_anchor = None
        self._corner_press_root = None
        self._corner_press_size = None
        if self._lock_layout:
            return
        # Persist through the SAME resize-end path the legacy Ctrl/L+R resize uses;
        # the controller branches on uniform_size to route the new size.
        self._on_resize_end(self._key, self._w, self._body_h)

    def _on_b3_press(self, event):
        self._press_root = (event.x_root, event.y_root)
        # Anchor on self._pos (authoritative last-placed PHYSICAL top-left), not
        # Tk winfo_root* (logical px under PMv2 → misplaces). Matches the strip
        # left-drag path so both move gestures share one coordinate basis.
        self._press_pos = self._pos
        self._press_size = (self._w, self._body_h)
        # resize if Ctrl (0x0004) or left button (0x0100) is also held
        self._mode = "resize" if (event.state & 0x0004 or
                                  event.state & 0x0100) else "move"

    def _on_b3_motion(self, event):
        if self._press_root is None:
            return
        dx = event.x_root - self._press_root[0]
        dy = event.y_root - self._press_root[1]
        if self._mode == "resize":
            w = max(_MIN_W, self._press_size[0] + dx)
            body_h = max(_MIN_BODY_H, self._press_size[1] + dy)
            self._w, self._body_h = w, body_h
            self._win32.set_window_pos(self._hwnd, self._press_pos[0],
                                       self._press_pos[1], w, body_h + STRIP_H)
            self._push_thumb_rect()
        else:  # move
            if self._lock_layout:
                return  # locked layout → right-drag move is a no-op (BUG B)
            x = self._press_pos[0] + dx
            y = self._press_pos[1] + dy
            self._pos = (x, y)
            self._win32.set_window_pos(self._hwnd, x, y, self._w,
                                       self._body_h + STRIP_H)

    def _on_b3_release(self, event):
        if self._press_root is None:
            return
        dx = event.x_root - self._press_root[0]
        dy = event.y_root - self._press_root[1]
        mode = self._mode
        self._press_root = None
        self._mode = None
        if mode == "resize":
            self._on_resize_end(self._key, self._w, self._body_h)
        elif not self._lock_layout:
            x = self._press_pos[0] + dx
            y = self._press_pos[1] + dy
            self._on_move_end(self._key, x, y)

    def _cur_pos(self):
        """Current tile top-left in physical px (best-effort from Tk)."""
        try:
            return (self.top.winfo_rootx(), self.top.winfo_rooty())
        except tk.TclError:
            return (0, 0)

    # ── placement / DWM ─────────────────────────────────────────────────────
    def place(self, x, y, w, body_h):
        self._w, self._body_h = w, body_h
        self._pos = (x, y)
        self.top.deiconify()
        self._win32.set_window_pos(self._hwnd, x, y, w, body_h + STRIP_H)
        self._push_thumb_rect()

    def hide(self):
        """Withdraw the tile without destroying it (Task C2 hide rules). The DWM
        thumbnail registration and the saved layout are untouched; show() re-maps
        it instantly. Idempotent."""
        if getattr(self, "_hidden", False):
            return
        self._hidden = True
        try:
            self.top.withdraw()
        except tk.TclError:
            pass

    def show(self):
        """Re-map a tile hidden by hide(). Idempotent; re-pushes the thumbnail rect
        so the live image resumes exactly where it left off."""
        if not getattr(self, "_hidden", False):
            return
        self._hidden = False
        try:
            self.top.deiconify()
            self.top.update_idletasks()
            self._win32.set_window_pos(self._hwnd, self._pos[0], self._pos[1],
                                       self._w, self._body_h + STRIP_H)
            self._push_thumb_rect()
        except tk.TclError:
            pass

    def attach_source(self, src_hwnd):
        self.detach()
        self._thumb = Thumbnail(self._hwnd, src_hwnd, dwm=self._dwm_backend)
        self._src_size = self._thumb.source_size()
        self._push_thumb_rect()

    def _push_thumb_rect(self):
        if not self._thumb:
            return
        fx, fy, fw, fh = aspect_fit(self._w, self._body_h, *self._src_size)
        self._thumb.show((fx, STRIP_H + fy, fx + fw, STRIP_H + fy + fh))

    def refresh_source_size(self):
        """Called by the tick every ~8th cycle: re-letterbox if the client resized."""
        if self._thumb:
            size = self._thumb.source_size()
            if size != self._src_size:
                self._src_size = size
                self._push_thumb_rect()

    def set_key(self, char_key):
        """Re-key an existing tile (login screen -> character, and back). The
        mouse callbacks read self._key at call time, so updating it here is
        enough for move/resize persistence to target the new layout key."""
        self._key = char_key

    def retop(self):
        self._win32.retop(self._hwnd)

    def set_alpha(self, a):
        self._alpha = a
        try:
            self.top.attributes("-alpha", a)
        except tk.TclError:
            pass

    def current_alpha(self):
        """Last requested window alpha (mirror for tests / logging)."""
        return self._alpha

    # ── opacity / hover / zoom (Task C1) ────────────────────────────────────
    def configure_hover(self, inactive, hover):
        """Set the inactive/hover opacities and apply the resting opacity now."""
        self._opacity_inactive = inactive
        self._opacity_hover = hover
        self._apply_resting_alpha()

    def configure_zoom(self, enabled, factor, anchor):
        """Set hover-zoom parameters. Does not zoom until the next <Enter>."""
        self._zoom_enabled = bool(enabled)
        self._zoom_factor = factor
        self._zoom_anchor = anchor

    def set_active(self, active):
        """Mark this tile as the active client's (last-activated / foreground).
        An active tile rests at hover opacity even when not hovered."""
        self._active = bool(active)
        self._apply_resting_alpha()

    def _apply_resting_alpha(self):
        """Alpha when not mid-hover: hover if hovering-or-active, else inactive."""
        if self._hovering or self._active:
            self.set_alpha(self._opacity_hover)
        else:
            self.set_alpha(self._opacity_inactive)

    def _dragging(self):
        return self._press_root is not None or self._mode is not None

    def _on_enter(self, _event):
        self._hovering = True
        self.set_alpha(self._opacity_hover)
        self._apply_zoom()

    def _on_leave(self, _event):
        self._hovering = False
        self._restore_zoom()
        self._apply_resting_alpha()

    def _apply_zoom(self):
        if not self._zoom_enabled or self._zoomed or self._dragging():
            return
        rect = (self._pos[0], self._pos[1], self._w, self._body_h)
        self._prezoom_rect = rect
        zx, zy, zw, zbody = preview_layout.zoom_rect(
            rect, self._zoom_factor, self._zoom_anchor)
        self._zoomed = True
        self._w, self._body_h = zw, zbody
        self._win32.set_window_pos(self._hwnd, zx, zy, zw, zbody + STRIP_H)
        self._push_thumb_rect()
        self._win32.retop(self._hwnd)

    def _restore_zoom(self):
        if not self._zoomed or self._prezoom_rect is None:
            return
        x, y, w, body_h = self._prezoom_rect
        self._zoomed = False
        self._prezoom_rect = None
        self._w, self._body_h = w, body_h
        self._win32.set_window_pos(self._hwnd, x, y, w, body_h + STRIP_H)
        self._push_thumb_rect()
        self._win32.retop(self._hwnd)

    def detach(self):
        if self._thumb:
            self._thumb.close()
            self._thumb = None

    # ── caption / badge / border ────────────────────────────────────────────
    def set_caption(self, name, dot=None, chip="", tag=""):
        self._name = name or ""
        self._chip = chip or ""
        self._tag = tag or ""
        self._render_caption()
        if dot:
            try:
                self._dot.itemconfigure(self._dot_item, fill=dot)
            except tk.TclError:
                pass

    def _render_caption(self):
        shown = self._badge or self._name
        try:
            self._name_lbl.configure(text=shown)
            self._chip_lbl.configure(text=self._chip)
            self._tag_lbl.configure(text=self._tag)
        except tk.TclError:
            pass

    def set_badge(self, text):
        """Overlay a status word in place of the name (MINIMIZED / login screen /
        None to clear)."""
        self._badge = text or None
        self._render_caption()

    def caption_text(self) -> str:
        """The composed caption string (name-or-badge + chip + tag) — for tests
        and logging."""
        parts = [p for p in (self._badge or self._name, self._chip, self._tag) if p]
        return " ".join(parts)

    def set_excluded(self, flag):
        """Mark this tile as excluded from hotkey cycling (session-only, C4). Shows
        a dim dot on the strip while excluded; clears it when re-included."""
        self._excluded = bool(flag)
        try:
            self._excl_lbl.configure(text="●" if self._excluded else "")
        except tk.TclError:
            pass

    def is_excluded(self) -> bool:
        return self._excluded

    def set_border(self, color):
        """Highlight/flash border via strip+body highlightbackground (None clears)."""
        try:
            if color:
                self._strip.configure(highlightthickness=2,
                                      highlightbackground=color,
                                      highlightcolor=color)
                self._body.configure(highlightthickness=2,
                                     highlightbackground=color,
                                     highlightcolor=color)
            else:
                self._strip.configure(highlightthickness=0)
                self._body.configure(highlightthickness=0)
        except tk.TclError:
            pass

    def destroy(self):
        self.detach()
        try:
            self.top.destroy()
        except tk.TclError:
            pass
